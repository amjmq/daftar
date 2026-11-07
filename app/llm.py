"""Client for any OpenAI-compatible chat API and the meeting prompts."""
import asyncio
import json
import logging
import re
import time
from datetime import date

import httpx

log = logging.getLogger("app.llm")

MINUTES_PROMPT = """You are a senior executive assistant who writes the official minutes of meetings.
The readers were not in the meeting and will not read the transcript, so the minutes must tell them exactly what happened.
Completeness matters more than brevity.

You receive a transcript produced by speech recognition, so it may contain recognition errors. It may carry speaker labels ("Speaker N: text") or none at all.

Write the minutes in the language of the transcript, as clean Markdown, with exactly this structure
(translate the headings into that language):

# <Specific meeting title>
## Summary
5-8 sentences: the purpose of the meeting, who took part, the main topics and the outcome.
## Participants
One line per person: the name (or "Speaker N") and the role or responsibility if it was stated.
## Discussion
The full account, in the order the meeting went. One "###" subsection per topic. In every subsection cover:
the background (why it came up); what each person said, argued, asked or answered (attributed); every concrete detail
that was mentioned (names, systems, tools, tables, fields, numbers, quantities, costs, dates, deadlines, versions);
the alternatives that were considered and why they were accepted or rejected; and how the topic ended.
## Decisions
Every decision as "decision, who decided or agreed, reason". Write "None" if there were none.
## Action items
One bullet per commitment: who, what, by when. (They are also turned into tickets separately; list all of them here too.)
## Open questions and risks
Each with who raised it and what is needed to resolve it. Write "None" if there were none.

Rules:
- Lose nothing of substance. Every topic, figure, name, date, commitment, objection, preference and reason that was discussed must appear. When in doubt, include it.
- Remove only noise: greetings and small talk, filler words, false starts, repetition. Silently fix obvious recognition errors.
- Never compress a topic into one vague sentence. A 30-minute meeting should give several pages of minutes.
- Long recordings are split into parts marked "[بخش N از M ...]". Speaker labels restart in every part and may not be the same person, so reconcile them using names and context.
- People are often called by nicknames or other names. The people directory lists each person's full name and the other names they go by: treat all of them as the same person and always write the full name, never an alias.
- Attribute statements to people by their full names when you can map speakers confidently, using the directory, the participant list or the conversation itself; otherwise keep "Speaker N".
- Stay faithful. Never invent facts, names, numbers or decisions. If something is unclear, say so.
- Dates: when you mention the meeting date, copy it from "Meeting date" exactly. Never convert between the Persian and Gregorian calendars yourself.
- Output only the Markdown."""

TASKS_PROMPT = """You turn meeting transcripts into tickets, written the way a good engineer writes Jira issues.
You receive a transcript produced by speech recognition, so it may contain recognition errors. It may carry speaker labels ("Speaker N: text") or none at all.

Return ONLY a JSON object, with no prose and no code fences:
{"tasks": [{"title": string, "description": string, "owner": string|null, "deadline": string|null, "priority": "high"|"medium"|"low"}]}

Rules:
- Create one ticket for every action that someone committed to or was clearly assigned, including small ones. Do not merge unrelated actions and never invent tasks. If there are none, return {"tasks": []}.
- title (the Jira "Summary"): one specific line of at most 90 characters naming the action, the object and the system. Write it as a natural title in the language of the transcript: in English start with a verb ("Add the missing country field to the customer database"); in Persian use the verbal-noun form ("افزودن فیلد کشور به دیتابیس مشتریان"), never a verb-first sentence. "Follow up on database" is too vague.
- description: the ticket body as Markdown, written in the language of the transcript, with exactly these sections
  (use these headings; for Persian: ## زمینه, ## توضیحات, ## معیار پذیرش, ## وابستگی‌ها و نکات):
  ## Context
  Why this ticket exists: the background from the discussion, who raised it and the problem it solves.
  ## Description
  What has to be done, in enough detail that someone who did not hear the meeting can start: scope, steps, constraints, systems, tables, names, numbers, versions and anything that was said about how to do it.
  ## Acceptance criteria
  A checklist, one "- [ ] ..." line per verifiable outcome, derived from what was said.
  ## Dependencies and notes
  People to coordinate with, prerequisites, risks, related decisions and open questions. Omit this section when nothing applies.
- Lose no detail: every figure, name, date and constraint that was mentioned about a task must appear in its description.
- Long recordings are split into parts marked "[بخش N از M ...]". Speaker labels restart in every part and may not be the same person, so reconcile them using names and context.
- owner: the person's full name from the people directory (people are often called by nicknames or other names; map those to the full name), otherwise the name as spoken, otherwise "Speaker N"; null if nobody is responsible.
- deadline: an ISO date (YYYY-MM-DD) resolved from "Meeting date" (it states the weekday) when a deadline was stated or clearly implied (e.g. "Thursday", "next week"); otherwise null. If a deadline was spoken but cannot be resolved, mention it in the description. Never convert between the Persian and Gregorian calendars yourself.
- priority: from the urgency expressed in the meeting; default "medium"."""

PRIORITIES = {"high", "medium", "low"}
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


PERSIAN_MONTHS = ["فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور", "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند"]
_DAYS_BEFORE_MONTH = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]


def to_jalali(day: date) -> tuple[int, int, int]:
    """Gregorian -> Persian (year, month, day). Models convert calendars unreliably, so the server does it."""
    y2 = day.year + 1 if day.month > 2 else day.year
    days = 355666 + 365 * day.year + (y2 + 3) // 4 - (y2 + 99) // 100 + (y2 + 399) // 400 + day.day + _DAYS_BEFORE_MONTH[day.month - 1]
    year = -1595 + 33 * (days // 12053)
    days %= 12053
    year += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        year += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        return year, 1 + days // 31, 1 + days % 31
    return year, 7 + (days - 186) // 30, 1 + (days - 186) % 30


def describe_date(iso: str) -> str:
    """"2026-11-02" -> "2026-11-02, Monday (11 آبان 1405 in the Persian calendar)", so relative deadlines resolve correctly."""
    day = date.fromisoformat(iso)
    year, month, dom = to_jalali(day)
    return f"{iso}, {day.strftime('%A')} ({dom} {PERSIAN_MONTHS[month - 1]} {year} in the Persian calendar)"


class LLMError(Exception):
    """Raised for any language-model API failure; the message is shown to the user."""


async def _request(cfg: dict, method: str, path: str, timeout: float, **kwargs) -> dict:
    if not cfg["base_url"]:
        raise LLMError("آدرس API مدل زبانی در تنظیمات وارد نشده است.")
    headers = {"Authorization": f"Bearer {cfg['api_key']}"} if cfg["api_key"] else {}
    try:
        async with httpx.AsyncClient(base_url=cfg["base_url"].rstrip("/") + "/", headers=headers, timeout=timeout) as client:
            response = await client.request(method, path.lstrip("/"), **kwargs)
    except httpx.HTTPError as e:
        log.warning("LLM API unreachable: %s %s: %s", method, path, e or type(e).__name__)
        raise LLMError(f"اتصال به API مدل زبانی ممکن نشد: {e or type(e).__name__}") from e
    if response.status_code >= 400:
        log.warning("LLM API answered %d for %s %s", response.status_code, method, path, extra={"status": response.status_code})
        raise LLMError(f"API مدل زبانی خطا داد ({response.status_code}): {response.text[:300]}")
    try:
        return response.json()
    except ValueError as e:
        raise LLMError(f"پاسخ API مدل زبانی JSON نیست: {response.text[:200]}") from e


async def chat(cfg: dict, system: str, user: str, model: str | None = None, max_tokens: int | None = None, timeout: float = 600) -> str:
    model = model or cfg.get("model")
    if not model:
        raise LLMError("برای این جلسه مدلی انتخاب نشده است؛ در صفحه‌ی جلسه یک مدل انتخاب کنید و «تلاش دوباره» را بزنید.")
    payload = {
        "model": model,
        "temperature": 0.2,
        "stream": False,  # some providers stream unless told otherwise
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
    }
    if max_tokens:
        payload["max_tokens"] = max_tokens
    began = time.monotonic()
    data = await _request(cfg, "POST", "chat/completions", timeout, json=payload)
    try:
        content = data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError, AttributeError) as e:
        raise LLMError(f"پاسخ نامعتبر از مدل: {str(data)[:200]}") from e
    usage = data.get("usage") or {}
    finish = data["choices"][0].get("finish_reason")
    log.info(
        "llm %s answered in %.1fs (%d chars in, %d chars out)", model, time.monotonic() - began, len(user), len(content),
        extra={"model": model, "seconds": round(time.monotonic() - began, 1), "prompt_tokens": usage.get("prompt_tokens"),
               "completion_tokens": usage.get("completion_tokens"), "finish_reason": finish},
    )
    if finish == "length" and content:  # a silently cut-off document would look complete
        log.warning("llm %s hit its output limit", model, extra={"model": model})
        raise LLMError("خروجی مدل به سقف طول رسید و ناقص ماند؛ مدلی با خروجی بلندتر انتخاب کنید.")
    if not content:  # reasoning models can spend the whole token budget thinking
        log.warning("llm %s returned an empty reply", model, extra={"model": model})
        raise LLMError("مدل پاسخ خالی داد؛ مدل دیگری انتخاب کنید یا دوباره تلاش کنید.")
    return content


async def list_models(cfg: dict) -> list[str]:
    data = await _request(cfg, "GET", "models", 30)
    return sorted(m["id"] for m in data.get("data", []))


async def test_model(cfg: dict, model: str) -> dict:
    start = time.monotonic()
    try:
        reply = await chat(cfg, "Reply with exactly: OK", "ping", model=model, max_tokens=512, timeout=60)
    except LLMError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "latency_ms": round((time.monotonic() - start) * 1000), "reply": reply[:80]}


def extract_json(text: str) -> dict:
    """Parse the outermost JSON object in a model reply, tolerating code fences and chatter."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise LLMError("خروجی وظایف قابل خواندن نیست.")
    try:
        return json.loads(text[start : end + 1], strict=False)  # strict=False: models put raw newlines in Markdown strings
    except json.JSONDecodeError as e:
        raise LLMError("خروجی وظایف قابل خواندن نیست.") from e


def _is_iso_date(value: str) -> bool:
    try:
        return bool(ISO_DATE.match(value)) and bool(date.fromisoformat(value))
    except ValueError:
        return False


def parse_tasks(text: str) -> list[dict]:
    tasks = []
    for item in extract_json(text).get("tasks") or []:
        if not isinstance(item, dict) or not str(item.get("title") or "").strip():
            continue
        priority = str(item.get("priority") or "").lower()
        deadline = str(item.get("deadline") or "").strip()
        description = str(item.get("description") or "").strip()
        if deadline and not _is_iso_date(deadline):  # keep what was said, but only real dates may reach the calendar
            description += f"\n\n**موعد (طبق گفتار):** {deadline}"
            deadline = ""
        tasks.append({
            "owner": str(item["owner"]).strip() if item.get("owner") else None,
            "title": str(item["title"]).strip(),
            "description": description,
            "deadline": deadline or None,
            "priority": priority if priority in PRIORITIES else "medium",
        })
    return tasks


def title_of(minutes: str) -> str:
    match = re.search(r"^#\s+(.+)$", minutes, re.MULTILINE)
    return match.group(1).strip() if match else ""


async def summarize(cfg: dict, transcript: str, context: str, meeting_date: str, wanted=("minutes", "tasks"), on_step=lambda name: None) -> tuple[dict, dict]:
    """Generate the wanted outputs concurrently -> (results, errors), each keyed by "minutes" / "tasks".

    One output failing never discards the other; on_step(name) fires as each finishes.
    """
    # ponytail: whole transcript in one request per output; chunk + merge if a model's context window is exceeded.
    user = f"Meeting date: {describe_date(meeting_date)}\n{context}\n\nTranscript:\n{transcript}"
    prompts = {"minutes": MINUTES_PROMPT, "tasks": TASKS_PROMPT}

    async def run(name: str):
        reply = await chat(cfg, prompts[name], user)
        result = parse_tasks(reply) if name == "tasks" else reply  # unreadable JSON counts as the tasks output failing
        on_step(name)
        return result

    outcomes = await asyncio.gather(*(run(name) for name in wanted), return_exceptions=True)
    results, errors = {}, {}
    for name, outcome in zip(wanted, outcomes):
        if isinstance(outcome, LLMError):
            errors[name] = str(outcome)
        elif isinstance(outcome, BaseException):
            raise outcome  # a bug, not a model failure
        else:
            results[name] = outcome
    return results, errors
