"""Meeting assistant API: audio -> transcript -> minutes + tasks."""
import asyncio
import contextvars
import json
import logging
import shutil
import time
import uuid
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import db
import jira
import llm
import logs
import people
import transcribe

log = logging.getLogger("app.job")
http_log = logging.getLogger("app.http")
KNOWN_FAILURES = (transcribe.TranscribeError, llm.LLMError, RuntimeError)  # expected: no stack trace needed

AUDIO_DIR = Path(db.DATA_DIR) / "audio"
MAX_BYTES = 256 * 1024 * 1024  # upload limit; the audio is re-encoded and split before it goes to the transcription API
SUMMARY_LABELS = {"minutes": "صورت‌جلسه", "tasks": "وظایف"}
IN_PROGRESS = "('queued', 'preparing', 'transcribing', 'summarizing')"
RECORDING_TIMEOUT = 120  # seconds without a chunk or heartbeat before an unattended recording is processed as it is
MIN_RECORDING_BYTES = 4096  # anything smaller holds no usable audio
MAX_CHUNK_BYTES = 8 * 1024 * 1024
RECORDING_EXTENSIONS = {"audio/webm": ".webm", "audio/mp4": ".m4a", "audio/ogg": ".ogg"}

progress: dict[str, dict] = {}  # live state of running jobs; ephemeral by design
live: dict[str, float] = {}  # recording id -> monotonic time of its last chunk or heartbeat
jobs: set[asyncio.Task] = set()  # strong references so jobs started outside a request are not garbage collected


def _spawn(coroutine) -> None:
    job = asyncio.create_task(coroutine)
    jobs.add(job)
    job.add_done_callback(jobs.discard)


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init()
    interrupted = db.query(f"SELECT id FROM meetings WHERE status IN {IN_PROGRESS}")
    log.info("app started", extra={"db": db.DB_PATH, "interrupted_jobs": len(interrupted)})
    # Jobs interrupted by a restart pick up where they left off (a saved transcript is never redone).
    for row in interrupted:
        _set(row["id"], status="queued")
        _spawn(process(row["id"]))
    for row in db.query("SELECT id FROM meetings WHERE status = 'recording'"):  # recordings cut off by the restart
        contextvars.copy_context().run(_finish_unattended, row["id"])
    watcher = asyncio.create_task(_watch_recordings())
    yield
    watcher.cancel()


app = FastAPI(lifespan=lifespan)


@app.middleware("http")
async def trace_requests(request: Request, call_next):
    """Give every request an id (echoed as X-Request-ID) and log it; polling GETs stay at DEBUG."""
    rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
    logs.request_id.set(rid)
    began = time.monotonic()
    try:
        response = await call_next(request)
    except Exception:
        http_log.exception("%s %s crashed", request.method, request.url.path)
        raise
    response.headers["X-Request-ID"] = rid
    quiet = request.method == "GET" or not request.url.path.startswith("/api/") or request.url.path.endswith("/chunks")
    http_log.log(
        logging.DEBUG if quiet else logging.INFO, "%s %s -> %d", request.method, request.url.path, response.status_code,
        extra={"status": response.status_code, "ms": round((time.monotonic() - began) * 1000)},
    )
    return response


@app.exception_handler(llm.LLMError)
async def llm_error(_, e: llm.LLMError):
    return JSONResponse({"detail": str(e)}, status_code=502)


@app.exception_handler(jira.JiraError)
async def jira_error(_, e: jira.JiraError):
    return JSONResponse({"detail": str(e)}, status_code=502)


@app.exception_handler(transcribe.TranscribeError)
async def transcribe_error(_, e: transcribe.TranscribeError):
    return JSONResponse({"detail": str(e)}, status_code=502)


def _iso_date(value: str) -> str | None:
    """Validate an ISO date ("" means none); anything else is a client error."""
    if not value:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise HTTPException(422, "تاریخ باید به شکل YYYY-MM-DD باشد.") from None


def _meeting(mid: str) -> dict:
    rows = db.query("SELECT * FROM meetings WHERE id = ?", (mid,))
    if not rows:
        raise HTTPException(404, "جلسه پیدا نشد.")
    return rows[0]


def _set(mid: str, **columns) -> None:
    assignments = ", ".join(f"{name} = ?" for name in columns)
    db.execute(f"UPDATE meetings SET {assignments} WHERE id = ?", (*columns.values(), mid))


def _audio(mid: str) -> Path | None:
    return next(AUDIO_DIR.glob(f"{mid}*"), None)


def _live(mid: str) -> dict | None:
    job = progress.get(mid)
    if not job:
        return None
    now = time.monotonic()
    public = {k: v for k, v in job.items() if k not in ("started", "stage_started")}
    return public | {"elapsed": round(now - job["started"]), "stage_elapsed": round(now - job["stage_started"])}


async def process(mid: str) -> None:
    """Background job: transcribe (skipped when a transcript exists), then summarize with the meeting's own model.

    Only outputs that are still missing are generated, so a retry never repeats work that succeeded.
    """
    logs.meeting_id.set(mid)
    meeting = _meeting(mid)
    cfg = {**db.settings(), "model": meeting["model"]}  # the model is chosen per meeting, there is no global default
    now = time.monotonic()
    progress[mid] = {"started": now, "stage_started": now}
    log.info("job started", extra={"has_transcript": bool(meeting["transcript"]), "model": cfg["model"]})

    def report(stage: str, **fields) -> None:
        job = progress[mid]
        if job.get("stage") != stage:
            if "stage" in job:
                log.info("stage %s finished in %.1fs", job["stage"], time.monotonic() - job["stage_started"])
            log.info("stage %s started", stage)
            job["stage_started"] = time.monotonic()
            _set(mid, status=stage)
        job.update(stage=stage, **fields)

    results: dict = {}
    errors: dict[str, str] = {}
    directory = people.all_people()
    try:
        transcript = meeting["transcript"]
        if not transcript:
            audio = _audio(mid)
            if audio is None:
                raise RuntimeError("فایل صوتی پیدا نشد؛ جلسه را دوباره آپلود کنید.")
            _set(mid, error=None)
            transcript = await transcribe.transcribe(str(audio), cfg, report)
            _set(mid, transcript=transcript)
            audio.unlink(missing_ok=True)  # the transcript is saved, so the recording is no longer needed
        wanted = [name for name, have in (("minutes", meeting["minutes"]), ("tasks", meeting["tasks_extracted"])) if not have]
        finished: list[str] = []

        def on_step(label: str) -> None:
            finished.append(label)
            report("summarizing", fraction=len(finished) / len(wanted), finished=list(finished))

        report("summarizing", fraction=0, finished=[])
        context = people.prompt_context(meeting["participants"], directory)
        results, errors = await llm.summarize(cfg, transcript, context, meeting["held_on"] or meeting["created_at"][:10], wanted, on_step)
    except Exception as e:  # job boundary: any failure must reach the UI instead of dying silently
        log.log(logging.ERROR, "job failed after %.0fs: %s", time.monotonic() - now, e,
                exc_info=not isinstance(e, KNOWN_FAILURES), extra={"error_type": type(e).__name__})
        _set(mid, status="failed", error=str(e) or type(e).__name__)
        return
    finally:
        progress.pop(mid, None)

    # Save whatever succeeded, even when the other half failed.
    if "minutes" in results:
        _set(mid, minutes=results["minutes"], title=meeting["title"] or llm.title_of(results["minutes"]))
    if "tasks" in results:
        db.execute("DELETE FROM tasks WHERE meeting_id = ?", (mid,))
        for t in results["tasks"]:
            db.execute(
                "INSERT INTO tasks (meeting_id, owner, title, description, deadline, priority) VALUES (?, ?, ?, ?, ?, ?)",
                (mid, people.canonical(t["owner"], directory), t["title"], t["description"], t["deadline"], t["priority"]),
            )
        _set(mid, tasks_extracted=1)
    elapsed = time.monotonic() - now
    if errors:
        detail = "؛ ".join(f"{SUMMARY_LABELS[name]}: {message}" for name, message in errors.items())
        log.error("job failed after %.0fs, kept %s: %s", elapsed, sorted(results) or "nothing", detail,
                  extra={"kept": sorted(results), "failed": sorted(errors)})
        _set(mid, status="failed", error=detail)
        return
    _set(mid, status="done", error=None)
    log.info("job done in %.0fs: %d tasks, %d chars of minutes", elapsed, len(results.get("tasks", [])), len(results.get("minutes", "")),
             extra={"seconds": round(elapsed)})


@app.get("/api/meetings")
def list_meetings():
    rows = db.query(
        """SELECT m.id, COALESCE(NULLIF(m.title, ''), m.filename) AS title, m.status, m.created_at, m.held_on,
                  (SELECT COUNT(*) FROM tasks t WHERE t.meeting_id = m.id AND NOT t.done AND t.deleted_at IS NULL) AS open_tasks
           FROM meetings m ORDER BY m.created_at DESC, m.rowid DESC"""
    )
    return [row | {"progress": _live(row["id"])} for row in rows]


@app.post("/api/meetings", status_code=202)
def create_meeting(
    background: BackgroundTasks,
    file: UploadFile = File(...),
    title: str = Form(""),
    participants: str = Form(""),
    held_on: str = Form(""),
    model: str = Form(""),
):
    mid = uuid.uuid4().hex[:12]
    logs.meeting_id.set(mid)
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    path = AUDIO_DIR / f"{mid}{Path(file.filename or '').suffix}"
    with path.open("wb") as out:
        shutil.copyfileobj(file.file, out)
    size = path.stat().st_size
    if size > MAX_BYTES:
        path.unlink()
        log.warning("upload rejected: %d MB exceeds the limit", size // 2**20)
        raise HTTPException(413, "حجم فایل بیش از ۲۵۶ مگابایت است.")
    db.execute(
        "INSERT INTO meetings (id, title, filename, participants, held_on, model, status) VALUES (?, ?, ?, ?, ?, ?, 'queued')",
        (mid, title.strip(), Path(file.filename or "جلسه").stem, people.clean_participants(participants, people.all_people()), _iso_date(held_on) or date.today().isoformat(), model.strip()),
    )
    log.info("meeting created from %r (%.1f MB)", file.filename, size / 2**20,
             extra={"bytes": size, "participants": bool(participants.strip())})
    background.add_task(process, mid)
    return {"id": mid}


class RecordingStart(BaseModel):
    mime: str = "audio/webm"
    held_on: str = ""


@app.post("/api/recordings", status_code=201)
def start_recording(body: RecordingStart):
    """Open a live recording. The browser streams chunks in, so a crash or closed tab never loses the meeting."""
    mid = uuid.uuid4().hex[:12]
    logs.meeting_id.set(mid)
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    (AUDIO_DIR / f"{mid}{RECORDING_EXTENSIONS.get(body.mime.split(';')[0], '.bin')}").touch()
    db.execute(
        "INSERT INTO meetings (id, filename, held_on, status) VALUES (?, 'ضبط جلسه', ?, 'recording')",
        (mid, _iso_date(body.held_on) or date.today().isoformat()),
    )
    live[mid] = time.monotonic()
    log.info("recording started", extra={"mime": body.mime})
    return {"id": mid}


@app.put("/api/recordings/{mid}/chunks")
async def append_chunk(mid: str, offset: int, request: Request):
    """Append bytes at `offset`. A retried chunk that already landed is accepted again, so retries are safe.

    An empty body is a heartbeat: it keeps a paused recording from being treated as abandoned.
    """
    if _meeting(mid)["status"] != "recording":
        raise HTTPException(409, "این ضبط دیگر باز نیست.")
    data = await request.body()
    if len(data) > MAX_CHUNK_BYTES:
        raise HTTPException(413, "بخش ضبط بیش از حد بزرگ است.")
    path = _audio(mid)
    size = path.stat().st_size
    if offset == size:
        with path.open("ab") as out:
            out.write(data)
        size += len(data)
    elif offset + len(data) != size:
        raise HTTPException(409, "ترتیب بخش‌های ضبط درست نیست.")
    live[mid] = time.monotonic()
    return {"size": size}


class RecordingFinish(BaseModel):
    title: str = ""
    participants: str = ""
    model: str = ""


@app.post("/api/recordings/{mid}/finish", status_code=202)
def finish_recording(mid: str, body: RecordingFinish, background: BackgroundTasks):
    meeting = _meeting(mid)
    if meeting["status"] != "recording":
        return {"id": mid}  # already finished, e.g. by the watchdog: finishing twice is harmless
    logs.meeting_id.set(mid)
    path = _audio(mid)
    if path.stat().st_size < MIN_RECORDING_BYTES:
        log.warning("recording discarded: nothing was captured")
        db.execute("DELETE FROM meetings WHERE id = ?", (mid,))
        path.unlink(missing_ok=True)
        live.pop(mid, None)
        raise HTTPException(422, "چیزی ضبط نشد؛ میکروفن را بررسی کنید.")
    live.pop(mid, None)
    db.execute(
        "UPDATE meetings SET title = ?, participants = ?, model = ?, status = 'queued' WHERE id = ?",
        (body.title.strip(), people.clean_participants(body.participants, people.all_people()), body.model.strip(), mid),
    )
    log.info("recording finished (%.1f MB)", path.stat().st_size / 2**20, extra={"bytes": path.stat().st_size})
    background.add_task(process, mid)
    return {"id": mid}


def _stale_recordings(now: float) -> list[str]:
    return [mid for mid, seen in live.items() if now - seen > RECORDING_TIMEOUT]


def _finish_unattended(mid: str) -> None:
    """Process what an abandoned recording captured (tab closed, laptop asleep, server restarted)."""
    logs.meeting_id.set(mid)
    live.pop(mid, None)
    path = _audio(mid)
    if path is None or path.stat().st_size < MIN_RECORDING_BYTES:
        log.warning("abandoned recording had no usable audio; removed")
        db.execute("DELETE FROM meetings WHERE id = ?", (mid,))
        if path:
            path.unlink(missing_ok=True)
        return
    log.warning("recording went silent; processing the %.1f MB captured so far", path.stat().st_size / 2**20)
    _set(mid, status="queued")
    _spawn(process(mid))


async def _watch_recordings() -> None:
    while True:
        await asyncio.sleep(30)
        for mid in _stale_recordings(time.monotonic()):
            contextvars.copy_context().run(_finish_unattended, mid)  # own context: the watcher must not keep a meeting id


@app.get("/api/meetings/{mid}")
def get_meeting(mid: str):
    meeting = _meeting(mid)
    meeting["title"] = meeting["title"] or meeting["filename"]
    meeting["progress"] = _live(mid)
    meeting["tasks"] = [t | {"jira_link": _jira_link(t["jira_key"])} for t in db.query("SELECT * FROM tasks WHERE meeting_id = ? AND deleted_at IS NULL ORDER BY id", (mid,))]
    meeting["events"] = db.query("SELECT at, level, message FROM events WHERE meeting_id = ? ORDER BY id", (mid,))
    return meeting


class MeetingPatch(BaseModel):
    held_on: str | None = None
    model: str | None = None


@app.patch("/api/meetings/{mid}")
def patch_meeting(mid: str, body: MeetingPatch):
    _meeting(mid)
    changes = {}
    if body.held_on is not None:
        changes["held_on"] = _iso_date(body.held_on) or date.today().isoformat()
    if body.model is not None:
        changes["model"] = body.model.strip()
    if changes:
        _set(mid, **changes)


@app.post("/api/meetings/{mid}/retry", status_code=202)
def retry_meeting(mid: str, background: BackgroundTasks):
    _meeting(mid)
    logs.meeting_id.set(mid)
    log.info("retry requested")
    _set(mid, status="queued", error=None)
    background.add_task(process, mid)
    return {"id": mid}


@app.delete("/api/meetings/{mid}", status_code=204)
def delete_meeting(mid: str):
    _meeting(mid)
    live.pop(mid, None)
    db.execute("DELETE FROM meetings WHERE id = ?", (mid,))
    if audio := _audio(mid):
        audio.unlink(missing_ok=True)
    log.info("meeting %s deleted", mid)


class TaskPatch(BaseModel):
    done: bool | None = None
    deadline: str | None = None  # ISO date; "" clears it
    owner: str | None = None  # a person's name or alias; "" clears it
    title: str | None = None
    description: str | None = None  # Markdown
    priority: str | None = None  # high | medium | low


def _jira_link(key: str | None) -> str | None:
    base = db.settings()["jira_url"]
    return f"{base}/browse/{key}" if key and base else None


@app.get("/api/tasks")
def list_tasks(include_done: bool = False):
    """Lightweight rows for lists and the calendar (no descriptions)."""
    rows = db.query(
        f"""SELECT t.id, t.meeting_id, t.owner, t.title, t.deadline, t.priority, t.done, t.jira_key,
                   COALESCE(NULLIF(m.title, ''), m.filename) AS meeting_title
            FROM tasks t JOIN meetings m ON m.id = t.meeting_id
            WHERE t.deleted_at IS NULL {'' if include_done else 'AND NOT t.done'} ORDER BY m.created_at DESC, t.id"""
    )
    return [row | {"jira_link": _jira_link(row["jira_key"])} for row in rows]


@app.get("/api/tasks/{task_id}")
def get_task(task_id: int):
    rows = db.query(
        """SELECT t.*, COALESCE(NULLIF(m.title, ''), m.filename) AS meeting_title, m.held_on AS meeting_held_on
           FROM tasks t JOIN meetings m ON m.id = t.meeting_id WHERE t.id = ? AND t.deleted_at IS NULL""",
        (task_id,),
    )
    if not rows:
        raise HTTPException(404, "وظیفه پیدا نشد.")
    return rows[0] | {"jira_link": _jira_link(rows[0]["jira_key"])}


@app.delete("/api/tasks/{task_id}")
def delete_task(task_id: int):
    """Soft delete, so an accidental click can be undone. A task already sent to Jira keeps its ticket there."""
    task = get_task(task_id)  # 404 when it does not exist or is already deleted
    db.execute("UPDATE tasks SET deleted_at = CURRENT_TIMESTAMP WHERE id = ?", (task_id,))
    logs.meeting_id.set(task["meeting_id"])
    log.info("task %d deleted", task_id, extra={"jira_key": task["jira_key"]})
    return {"jira_key": task["jira_key"]}


@app.post("/api/tasks/{task_id}/restore", status_code=204)
def restore_task(task_id: int):
    if not db.query("SELECT 1 FROM tasks WHERE id = ? AND deleted_at IS NOT NULL", (task_id,)):
        raise HTTPException(404, "وظیفه‌ی حذف‌شده پیدا نشد.")
    db.execute("UPDATE tasks SET deleted_at = NULL WHERE id = ?", (task_id,))


@app.patch("/api/tasks/{task_id}")
def patch_task(task_id: int, body: TaskPatch):
    changes = {}
    if body.done is not None:
        changes["done"] = int(body.done)
    if "deadline" in body.model_fields_set:
        changes["deadline"] = _iso_date(body.deadline or "")
    if "owner" in body.model_fields_set:
        changes["owner"] = people.canonical((body.owner or "").strip(), people.all_people()) or None
    if body.title is not None:
        if not (title := " ".join(body.title.split())):
            raise HTTPException(422, "عنوان وظیفه نمی‌تواند خالی باشد.")
        changes["title"] = title
    if body.description is not None:
        changes["description"] = body.description.strip()
    if body.priority is not None:
        if body.priority not in llm.PRIORITIES:
            raise HTTPException(422, "اولویت باید high، medium یا low باشد.")
        changes["priority"] = body.priority
    if changes:
        assignments = ", ".join(f"{name} = ?" for name in changes)
        db.execute(f"UPDATE tasks SET {assignments} WHERE id = ?", (*changes.values(), task_id))
        log.info("task %d updated: %s", task_id, ", ".join(changes))


class PersonIn(BaseModel):
    name: str
    role: str = ""
    aliases: list[str] = []
    jira_username: str = ""


def _person_fields(body: PersonIn, ignore_id: int | None = None) -> tuple[str, str, str, str]:
    name = " ".join(body.name.split())
    if not name:
        raise HTTPException(422, "نام را وارد کنید.")
    aliases = [a for a in dict.fromkeys(" ".join(a.split()) for a in body.aliases) if a and people.normalize(a) != people.normalize(name)]
    if taken := people.conflict([name, *aliases], people.all_people(), ignore_id):
        raise HTTPException(409, f"این نام قبلاً ثبت شده است: {taken}")
    return name, body.role.strip(), json.dumps(aliases, ensure_ascii=False), body.jira_username.strip()


@app.get("/api/people")
def list_people():
    return people.all_people()


@app.post("/api/people", status_code=201)
def create_person(body: PersonIn):
    name, role, aliases, jira_username = _person_fields(body)
    return {"id": db.execute("INSERT INTO people (name, role, aliases, jira_username) VALUES (?, ?, ?, ?)", (name, role, aliases, jira_username))}


@app.put("/api/people/{person_id}")
def update_person(person_id: int, body: PersonIn):
    name, role, aliases, jira_username = _person_fields(body, ignore_id=person_id)
    db.execute("UPDATE people SET name = ?, role = ?, aliases = ?, jira_username = ? WHERE id = ?", (name, role, aliases, jira_username, person_id))


@app.delete("/api/people/{person_id}", status_code=204)
def delete_person(person_id: int):
    db.execute("DELETE FROM people WHERE id = ?", (person_id,))  # past meetings and tasks keep the name as text


class SettingsIn(BaseModel):
    base_url: str | None = None
    api_key: str | None = None
    stt_base_url: str | None = None
    stt_api_key: str | None = None
    stt_model: str | None = None
    language: str | None = None
    jira_url: str | None = None
    jira_user: str | None = None
    jira_password: str | None = None
    jira_verify_tls: bool | None = None


@app.get("/api/settings")
def get_settings():
    cfg = db.settings()
    return {
        **cfg, "api_key": "", "api_key_set": bool(cfg["api_key"]), "stt_api_key": "", "stt_api_key_set": bool(cfg["stt_api_key"]),
        "jira_password": "", "jira_password_set": bool(cfg["jira_password"]), "jira_verify_tls": cfg["jira_verify_tls"] != "false",
    }


@app.put("/api/settings")
def put_settings(body: SettingsIn):
    values = {k: ("true" if v else "false") if isinstance(v, bool) else v.strip() for k, v in body.model_dump().items() if v is not None}
    for secret in ("api_key", "stt_api_key", "jira_password"):
        if not values.get(secret):
            values.pop(secret, None)  # blank keeps the stored secret
    if "jira_url" in values:
        values["jira_url"] = values["jira_url"].rstrip("/")
        if values["jira_url"] and not values["jira_url"].startswith(("http://", "https://")):
            raise HTTPException(422, "آدرس Jira باید با http:// یا https:// شروع شود.")
    db.save_settings(values)
    log.info("settings updated", extra={"keys": sorted(values)})  # never the values of secrets


@app.get("/api/jira/status")
async def jira_status():
    try:
        return {"ok": True, **await jira.status()}
    except jira.JiraError as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/jira/projects")
async def jira_projects():
    return await jira.projects()


@app.get("/api/jira/projects/{key}/meta")
async def jira_project_meta(key: str):
    return await jira.project_meta(key)


@app.get("/api/jira/issues")
async def jira_issues(project: str, q: str = ""):
    return await jira.find_parents(project, q)


@app.get("/api/jira/users")
async def jira_users(project: str, q: str = ""):
    return await jira.assignable_users(project, q)


@app.get("/api/jira/priorities")
async def jira_priorities():
    return await jira.priorities()


class JiraItem(BaseModel):
    task_id: int
    issue_type_id: str
    epic_key: str = ""  # the epic a regular issue joins
    parent_key: str = ""  # the issue a sub-task belongs to
    assignee: str = ""
    priority_id: str = ""
    due: str = ""


class JiraSend(BaseModel):
    project: str
    items: list[JiraItem]


def _source_line(task: dict) -> str:
    label = task["meeting_title"] or task["filename"]
    when = ""
    if task["held_on"]:
        year, month, day = llm.to_jalali(date.fromisoformat(task["held_on"]))
        when = f" ({day} {llm.PERSIAN_MONTHS[month - 1]} {year})"
    return f"منبع: جلسه «{label}»{when}"


@app.post("/api/jira/send")
async def send_to_jira(body: JiraSend):
    """Create one Jira issue per task. One failing task never stops the rest, and each key is saved the moment it exists."""
    dues = [_iso_date(item.due) or "" for item in body.items]  # reject bad dates before anything is created
    results = []
    for item, due in zip(body.items, dues):
        rows = db.query(
            """SELECT t.*, m.title AS meeting_title, m.filename, m.held_on FROM tasks t JOIN meetings m ON m.id = t.meeting_id WHERE t.id = ? AND t.deleted_at IS NULL""",
            (item.task_id,),
        )
        if not rows:
            results.append({"task_id": item.task_id, "error": "وظیفه پیدا نشد."})
            continue
        task = rows[0]
        logs.meeting_id.set(task["meeting_id"])
        if task["jira_key"]:
            results.append({"task_id": item.task_id, "error": f"قبلاً به Jira ارسال شده است ({task['jira_key']})."})
            continue
        try:
            key = await jira.create_issue(
                project=body.project, issue_type_id=item.issue_type_id, title=task["title"], description=task["description"],
                source=_source_line(task), parent_key=item.parent_key, assignee=item.assignee.strip(), priority_id=item.priority_id, due=due,
            )
        except jira.JiraError as e:
            log.warning("task %d was not sent to Jira: %s", item.task_id, e)
            results.append({"task_id": item.task_id, "error": str(e)})
            continue
        db.execute("UPDATE tasks SET jira_key = ? WHERE id = ?", (key, item.task_id))
        result = {"task_id": item.task_id, "key": key, "link": _jira_link(key)}
        if item.epic_key:
            try:
                await jira.add_to_epic(item.epic_key, key)
            except jira.JiraError as e:
                result["warning"] = f"ایجاد شد ولی به اپیک وصل نشد: {e}"
        log.info("task %d sent to Jira as %s", item.task_id, key, extra={"issue": key})
        results.append(result)
    return {"results": results}


@app.get("/api/models")
async def get_models():
    return await llm.list_models(db.settings())


class ModelTest(BaseModel):
    model: str


@app.post("/api/settings/test")
async def test_model(body: ModelTest):
    result = await llm.test_model(db.settings(), body.model)
    log.info("model test %s: %s", body.model, "ok" if result["ok"] else result["error"], extra={"model": body.model, "ok": result["ok"]})
    return result


class RevalidatedStatic(StaticFiles):
    """Always revalidate (ETag), so UI changes show up without a hard refresh."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/", RevalidatedStatic(directory=Path(__file__).parent / "static", html=True), name="static")
