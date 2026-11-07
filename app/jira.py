"""Jira Server / Data Center client (REST API v2) and the Markdown -> Jira wiki-markup converter.

Auth is Basic (username + password); with an empty username the password field is used as a Personal Access Token
(Bearer). Nothing here logs credentials or ticket text.
"""
import logging
import re

import httpx

import db

log = logging.getLogger("app.jira")

TIMEOUT = 30
KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")  # a project key; it is placed inside JQL, so it must be plain
ISSUE_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*-\d+$")
transport: httpx.AsyncBaseTransport | None = None  # tests inject httpx.MockTransport here


class JiraError(Exception):
    """Raised for any Jira failure; the message is shown to the user."""


# ---------- Markdown -> wiki markup ----------

_INLINE = re.compile(r"\*\*(.+?)\*\*|`(.+?)`|\[([^\]]+)\]\(([^)]+)\)|(?<![*\w])\*([^*\n]+)\*(?![*\w])")


def _inline(text: str) -> str:
    def convert(m: re.Match) -> str:
        if m[1] is not None:
            return f"*{m[1]}*"
        if m[2] is not None:
            return "{{" + m[2] + "}}"
        if m[3] is not None:
            return f"[{m[3]}|{m[4]}]"
        return f"_{m[5]}_"

    return _INLINE.sub(convert, text)


def markdown_to_wiki(text: str) -> str:
    # ponytail: covers what the model writes (headings, lists, checklists, bold/italic/code/links); literal { and [ are left as is.
    out = []
    for line in text.splitlines():
        if heading := re.match(r"^(#{1,6})\s+(.*)", line):
            out.append(f"h{len(heading[1])}. {_inline(heading[2])}")
        elif box := re.match(r"^(\s*)[-*]\s+\[([ xX])\]\s+(.*)", line):
            out.append(f"{'*' * (1 + len(box[1]) // 2)} {'☑' if box[2].strip() else '☐'} {_inline(box[3])}")
        elif bullet := re.match(r"^(\s*)[-*]\s+(.*)", line):
            out.append(f"{'*' * (1 + len(bullet[1]) // 2)} {_inline(bullet[2])}")
        elif number := re.match(r"^(\s*)\d+[.)]\s+(.*)", line):
            out.append(f"{'#' * (1 + len(number[1]) // 2)} {_inline(number[2])}")
        else:
            out.append(_inline(line))
    return "\n".join(out)


# ---------- HTTP ----------

def _config() -> dict:
    cfg = db.settings()
    if not cfg["jira_url"]:
        raise JiraError("آدرس Jira در تنظیمات وارد نشده است.")
    return cfg


def _explain(response: httpx.Response) -> str:
    headers = response.headers
    if headers.get("X-Authentication-Denied-Reason", "").startswith("CAPTCHA"):
        return "Jira به‌دلیل چند ورود ناموفق CAPTCHA می‌خواهد؛ یک بار در مرورگر وارد Jira شوید، CAPTCHA را حل کنید و دوباره تست کنید."
    if "text/html" in headers.get("content-type", ""):  # Jira's API answers in JSON; an HTML error page comes from a CDN, WAF or proxy in front of it
        front = headers.get("server") or "پراکسی/CDN"
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", response.text)).strip()[:160]
        return (f"پاسخ ({response.status_code}) از لایه‌ی جلوی Jira ({front}) آمد، نه از خود Jira؛ پس رمز هنوز بررسی نشده است: {text}. "
                "معمولاً یعنی از شبکه‌ی فعلی (مثلاً VPN یا IP خارج از ایران) به Jira دسترسی ندارید.")
    if "Basic Authentication has been disabled" in response.text:
        return ("این Jira ورود با نام کاربری و رمز را برای API غیرفعال کرده است. یک توکن شخصی بسازید: در Jira از منوی پروفایل ← "
                "Personal Access Tokens ← Create token. بعد در تنظیمات، «نام کاربری» را خالی بگذارید و توکن را در کادر «رمز عبور» بگذارید.")
    try:
        body = response.json()
        problems = [*body.get("errorMessages", []), *(f"{field}: {text}" for field, text in body.get("errors", {}).items())]
    except (ValueError, AttributeError):
        problems = []
    detail = "؛ ".join(problems) or response.text[:200]
    if response.status_code == 401:
        reason = response.headers.get("X-Seraph-LoginReason", "")
        return f"Jira ورود را رد کرد (۴۰۱): نام کاربری یا رمز عبور درست نیست{f' [{reason}]' if reason else ''}."
    if response.status_code == 403:
        return f"دسترسی لازم را در Jira ندارید (۴۰۳): {detail}"
    return f"Jira خطا داد ({response.status_code}): {detail}"


async def _request(method: str, path: str, **kwargs):
    cfg = _config()
    user, secret = cfg["jira_user"], cfg["jira_password"]
    headers = {"Accept": "application/json"}
    if not user and secret:
        headers["Authorization"] = f"Bearer {secret}"  # Personal Access Token
    try:
        async with httpx.AsyncClient(
            base_url=cfg["jira_url"].rstrip("/") + "/", headers=headers, auth=httpx.BasicAuth(user, secret) if user else None,
            timeout=TIMEOUT, verify=cfg["jira_verify_tls"] != "false", transport=transport,
        ) as client:
            response = await client.request(method, path.lstrip("/"), **kwargs)
    except httpx.HTTPError as e:
        log.warning("Jira unreachable: %s %s: %s", method, path, e or type(e).__name__)
        raise JiraError(f"اتصال به Jira ممکن نشد: {e or type(e).__name__}") from e
    if response.status_code >= 400:
        log.warning("Jira answered %d for %s %s", response.status_code, method, path, extra={"status": response.status_code})
        raise JiraError(_explain(response))
    return response.json() if response.content else None


def _quote(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


# ---------- reading ----------

async def status() -> dict:
    me, info = await _request("GET", "rest/api/2/myself"), await _request("GET", "rest/api/2/serverInfo")
    return {"user": me.get("displayName") or me.get("name"), "version": info.get("version"), "deployment": info.get("deploymentType")}


async def projects() -> list[dict]:
    rows = await _request("GET", "rest/api/2/project")
    return sorted(({"key": p["key"], "name": p["name"]} for p in rows), key=lambda p: p["name"].casefold())


async def priorities() -> list[dict]:
    return [{"id": p["id"], "name": p["name"]} for p in await _request("GET", "rest/api/2/priority")]


async def search(jql: str, limit: int = 50) -> list[dict]:
    data = await _request("POST", "rest/api/2/search", json={"jql": jql, "maxResults": limit, "fields": ["summary", "issuetype", "status"]})
    return [{"key": i["key"], "summary": i["fields"]["summary"], "type": (i["fields"].get("issuetype") or {}).get("name", ""),
             "status": (i["fields"].get("status") or {}).get("name", "")} for i in data["issues"]]


def _project_key(key: str) -> str:
    if not KEY_RE.match(key):
        raise JiraError("کلید پروژه معتبر نیست.")
    return key


def _issue_key(key: str) -> str:
    if not ISSUE_KEY_RE.match(key):  # it ends up in a URL path
        raise JiraError(f"کلید کار «{key}» معتبر نیست.")
    return key.upper()


async def project_meta(key: str) -> dict:
    """Issue types (with the sub-task flag) and the open epics of a project."""
    key = _project_key(key)
    project = await _request("GET", f"rest/api/2/project/{key}")
    types = [{"id": t["id"], "name": t["name"], "subtask": bool(t.get("subtask"))} for t in project.get("issueTypes", [])]
    try:
        epics = await search(f'project = "{key}" AND issuetype = Epic AND statusCategory != Done ORDER BY updated DESC', 100)
    except JiraError:
        epics = []  # a project without the Epic type (e.g. a service desk) simply has none
    return {"issue_types": types, "epics": epics}


async def find_parents(project: str, text: str = "") -> list[dict]:
    """Open issues that can be the parent of a sub-task: the latest ones, or those matching a key or words in the summary."""
    project, text = _project_key(project), text.strip()
    scope = f'project = "{project}" AND issuetype not in subtaskIssueTypes() AND statusCategory != Done'
    if ISSUE_KEY_RE.match(text):
        scope += f' AND key = "{text.upper()}"'
    elif text:
        scope += f' AND summary ~ "{_quote(text)}"'
    found = await search(f"{scope} ORDER BY updated DESC", 40)
    return [i for i in found if i["type"].casefold() != "epic"][:30]  # an epic cannot have sub-tasks


async def assignable_users(project: str, text: str = "") -> list[dict]:
    """People the project allows as assignee, narrowed by what the user typed."""
    # 'username' is the parameter Server/DC 8 and 9 read; 'query' is its newer name
    rows = await _request("GET", "rest/api/2/user/assignable/search", params={"project": _project_key(project), "username": text.strip(), "query": text.strip(), "maxResults": 20})
    return [{"name": u["name"], "display": u.get("displayName") or u["name"]} for u in rows if u.get("name") and u.get("active", True)]


# ---------- writing ----------

async def create_issue(*, project: str, issue_type_id: str, title: str, description: str, source: str = "",
                       parent_key: str = "", assignee: str = "", priority_id: str = "", due: str = "") -> str:
    """Create one issue and return its key. Never retried automatically: a retry could make a duplicate."""
    fields = {
        "project": {"key": _project_key(project)},
        "issuetype": {"id": str(issue_type_id)},
        "summary": " ".join(title.split())[:255],
        "description": markdown_to_wiki(description) + (f"\n\n----\n_{source}_" if source else ""),
    }
    if parent_key:
        fields["parent"] = {"key": _issue_key(parent_key)}
    if assignee:
        fields["assignee"] = {"name": assignee}
    if priority_id:
        fields["priority"] = {"id": str(priority_id)}
    if due:
        fields["duedate"] = due
    created = await _request("POST", "rest/api/2/issue", json={"fields": fields})
    log.info("jira issue %s created", created["key"], extra={"issue": created["key"], "project": project})
    return created["key"]


async def add_to_epic(epic_key: str, issue_key: str) -> None:
    """Link through the Agile API, which needs no instance-specific 'Epic Link' custom-field id."""
    await _request("POST", f"rest/agile/1.0/epic/{_issue_key(epic_key)}/issue", json={"issues": [issue_key]})
