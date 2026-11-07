"""Smoke tests with the network faked. Run: docker compose run --rm app python test_app.py"""
import json
import os
import tempfile
from pathlib import Path

os.environ["DATA_DIR"] = tempfile.mkdtemp()
os.environ["LOG_LEVEL"] = "ERROR"  # keep test output quiet; meeting timelines still record INFO

from fastapi.testclient import TestClient  # noqa: E402

import httpx  # noqa: E402
import jira  # noqa: E402
import llm  # noqa: E402
import logs  # noqa: E402
import people  # noqa: E402
import server  # noqa: E402
import transcribe  # noqa: E402

logs.setup()

original_chat = llm.chat

TASKS_REPLY = (
    'Sure!\n```json\n{"tasks": [{"owner": "Sara", "title": "Send the budget", "description": "## Context\nBudget review.",\n'
    ' "priority": "HIGH", "deadline": "next week"}, {"title": ""}, "junk"]}\n```'  # raw newline inside a string, like real models emit
)


def fake_services(llm_fails=False):
    async def fake_transcribe(path, cfg, on_progress):
        on_progress("preparing")
        return "Speaker 1: Sara will send the budget."

    async def fake_chat(cfg, system, user, **kwargs):
        if llm_fails:
            raise llm.LLMError("model offline")
        return "# Budget sync\n## Summary\nOk." if system is llm.MINUTES_PROMPT else TASKS_REPLY

    transcribe.transcribe, llm.chat = fake_transcribe, fake_chat


def upload(client):
    response = client.post("/api/meetings", files={"file": ("sync.mp3", b"audio")}, data={"participants": "Sara"})
    assert response.status_code == 202
    return response.json()["id"]


def test_parse_tasks_tolerates_fences_and_bad_items():
    tasks = llm.parse_tasks(TASKS_REPLY)
    assert tasks == [{
        "owner": "Sara", "title": "Send the budget", "deadline": None, "priority": "high",
        "description": "## Context\nBudget review.\n\n**موعد (طبق گفتار):** next week",  # unresolvable deadline is kept as text
    }]
    iso = llm.parse_tasks('{"tasks": [{"title": "A", "deadline": "2026-11-05"}, {"title": "B", "deadline": "2026-13-45"}]}')
    assert [t["deadline"] for t in iso] == ["2026-11-05", None], "only real ISO dates may reach the calendar"


def test_pipeline_failure_then_retry():
    with TestClient(server.app) as client:
        fake_services(llm_fails=True)
        mid = upload(client)
        meeting = client.get(f"/api/meetings/{mid}").json()
        assert meeting["status"] == "failed" and "model offline" in meeting["error"]
        assert meeting["transcript"], "transcript must survive a summarization failure"

        fake_services()
        assert client.post(f"/api/meetings/{mid}/retry").status_code == 202
        meeting = client.get(f"/api/meetings/{mid}").json()
        assert meeting["status"] == "done" and meeting["title"] == "Budget sync"
        assert [t["title"] for t in meeting["tasks"]] == ["Send the budget"]
        timeline = [(e["level"], e["message"]) for e in meeting["events"]]
        assert any(level == "ERROR" and "job failed" in text and "model offline" in text for level, text in timeline)
        assert any("job done" in text for _, text in timeline), "the timeline must record how the meeting ended"
        assert not list(server.AUDIO_DIR.glob(f"{mid}*")), "audio is deleted once processed"

        task_id = meeting["tasks"][0]["id"]
        client.patch(f"/api/tasks/{task_id}", json={"done": True})
        assert client.get("/api/tasks").json() == []
        assert len(client.get("/api/tasks?include_done=true").json()) == 1


def test_partial_failure_keeps_the_finished_half_and_retries_only_the_rest():
    calls, tasks_reply = [], ["not json at all"]

    async def fake_chat(cfg, system, user, **kwargs):
        calls.append(system)
        return "# Budget sync\n## Summary\nOk." if system is llm.MINUTES_PROMPT else tasks_reply[0]

    fake_services()
    llm.chat = fake_chat
    with TestClient(server.app) as client:
        mid = upload(client)
        meeting = client.get(f"/api/meetings/{mid}").json()
        assert meeting["status"] == "failed" and "وظایف" in meeting["error"]
        assert meeting["minutes"] and meeting["title"] == "Budget sync", "the finished half must be kept"
        assert not meeting["tasks_extracted"]

        calls.clear()
        tasks_reply[0] = TASKS_REPLY
        client.post(f"/api/meetings/{mid}/retry")
        meeting = client.get(f"/api/meetings/{mid}").json()
        assert calls == [llm.TASKS_PROMPT], "only the missing half is regenerated"
        assert meeting["status"] == "done" and meeting["tasks_extracted"] and len(meeting["tasks"]) == 1


def test_migration_keeps_existing_meetings_and_marks_finished_ones():
    import sqlite3
    from pathlib import Path

    import db

    old = Path(os.environ["DATA_DIR"]) / "old.db"
    with sqlite3.connect(old) as conn:  # the schema from before partial results existed
        conn.executescript("""CREATE TABLE meetings (id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '', filename TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL, error TEXT, participants TEXT NOT NULL DEFAULT '', transcript TEXT, minutes TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, meeting_id TEXT NOT NULL, owner TEXT, task TEXT NOT NULL,
            deadline TEXT, priority TEXT NOT NULL DEFAULT 'medium', done INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE people (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, role TEXT NOT NULL DEFAULT '', aliases TEXT NOT NULL DEFAULT '[]');
            INSERT INTO people (name) VALUES ('Old Person');
            INSERT INTO meetings (id, status, minutes, created_at) VALUES ('finished', 'done', 'm', '2026-11-02 10:00:00'), ('broken', 'failed', NULL, '2026-11-01 09:00:00');
            INSERT INTO tasks (meeting_id, task) VALUES ('finished', 'old task');""")
    original, db.DB_PATH = db.DB_PATH, str(old)
    try:
        db.init()
        rows = {r["id"]: (r["tasks_extracted"], r["held_on"]) for r in db.query("SELECT id, tasks_extracted, held_on FROM meetings")}
        assert db.query("SELECT model FROM meetings")[0]["model"] == "", "old meetings have no model until one is chosen"
        assert db.query("SELECT jira_key, deleted_at FROM tasks")[0] == {"jira_key": None, "deleted_at": None}, "old tasks gain the Jira key and soft-delete columns"
        assert db.query("SELECT jira_username FROM people")[0]["jira_username"] == "", "people can now carry a Jira username"
        task = db.query("SELECT title, description FROM tasks")[0]
    finally:
        db.DB_PATH = original
    assert rows == {"finished": (1, "2026-11-02"), "broken": (0, "2026-11-01")}
    assert task == {"title": "old task", "description": ""}, "the old one-line task becomes the ticket title"


def test_live_recording_streams_in_and_survives_retries():
    fake_services()
    with TestClient(server.app) as client:
        mid = client.post("/api/recordings", json={"mime": "audio/webm;codecs=opus"}).json()["id"]
        assert client.get(f"/api/meetings/{mid}").json()["status"] == "recording"

        part = b"a" * 3000
        put = lambda offset, data: client.put(f"/api/recordings/{mid}/chunks", params={"offset": offset}, content=data)
        assert put(0, part).json() == {"size": 3000}
        assert put(0, part).json() == {"size": 3000}, "a retried chunk that already landed is accepted"
        assert put(3000, part).json() == {"size": 6000}
        assert put(9000, part).status_code == 409, "a gap means lost audio and must be refused"
        assert put(6000, b"").json() == {"size": 6000}, "an empty body is a heartbeat"

        assert client.post(f"/api/recordings/{mid}/finish", json={"title": "Standup", "participants": "Sara"}).status_code == 202
        meeting = client.get(f"/api/meetings/{mid}").json()
        assert meeting["status"] == "done" and meeting["title"] == "Standup" and meeting["participants"] == "Sara"
        assert client.post(f"/api/recordings/{mid}/finish", json={}).status_code == 202, "finishing twice is harmless"
        assert put(6000, part).status_code == 409, "a finished recording takes no more audio"


def test_empty_recording_is_discarded_instead_of_processed():
    with TestClient(server.app) as client:
        mid = client.post("/api/recordings", json={}).json()["id"]
        client.put(f"/api/recordings/{mid}/chunks", params={"offset": 0}, content=b"x" * 100)
        assert client.post(f"/api/recordings/{mid}/finish", json={}).status_code == 422
        assert client.get(f"/api/meetings/{mid}").status_code == 404


def test_abandoned_recordings_are_processed_by_the_watchdog_and_at_startup():
    import time

    fake_services()
    server.live["ghost"] = time.monotonic() - server.RECORDING_TIMEOUT - 1
    server.live["active"] = time.monotonic()
    assert server._stale_recordings(time.monotonic()) == ["ghost"]
    server.live.clear()

    # a recording cut off by a server restart: row in "recording" state with audio on disk but nothing live
    import db

    db.init()
    server.AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    (server.AUDIO_DIR / "cutoff.webm").write_bytes(b"a" * 8000)
    db.execute("INSERT INTO meetings (id, filename, status) VALUES ('cutoff', 'ضبط جلسه', 'recording')")
    db.execute("INSERT INTO meetings (id, filename, status) VALUES ('empty', 'ضبط جلسه', 'recording')")
    with TestClient(server.app) as client:
        for _ in range(50):
            meeting = client.get("/api/meetings/cutoff").json()
            if meeting["status"] == "done":
                break
            time.sleep(0.1)
        assert meeting["status"] == "done", "what was captured is processed, not lost"
        assert client.get("/api/meetings/empty").status_code == 404, "a recording with no audio is cleaned up"


def test_interrupted_job_resumes_on_startup():
    import db
    import time

    db.init()
    db.execute("INSERT INTO meetings (id, filename, status, transcript) VALUES ('stuck', 'x', 'summarizing', 'Speaker 1: hi')")
    fake_services()
    with TestClient(server.app) as client:
        for _ in range(50):  # the resumed job runs on the app's event loop
            meeting = client.get("/api/meetings/stuck").json()
            if meeting["status"] == "done":
                break
            time.sleep(0.1)
        assert meeting["status"] == "done" and meeting["progress"] is None


def test_part_progress_goes_from_queued_to_sending_to_done():
    part = transcribe.Part(Path(os.environ["DATA_DIR"]) / "part.mp3")
    assert part.fraction == 0
    part.phase = "sending"
    assert 0 < part.fraction < 1
    part.phase = "done"
    assert part.fraction == 1


def test_transcription_goes_to_the_configured_openai_compatible_endpoint():
    path = Path(os.environ["DATA_DIR"]) / "part.mp3"
    path.write_bytes(b"audio")
    seen = []

    def handler(request):
        seen.append(request)
        if request.headers["authorization"] == "Bearer bad":
            return httpx.Response(401, json={"error": "invalid key"})
        return httpx.Response(200, json={"text": " سلام "})

    cfg = {"base_url": "https://llm.test/v1", "api_key": "llm-key", "stt_base_url": "", "stt_api_key": "", "stt_model": "whisper-1", "language": "Persian"}
    transcribe.transport = httpx.MockTransport(handler)
    try:
        assert transcribe._send(transcribe.Part(path), cfg) == "سلام"
        request = seen[-1]
        assert str(request.url) == "https://llm.test/v1/audio/transcriptions", "an empty transcription URL means the language model's provider"
        assert request.headers["authorization"] == "Bearer llm-key"
        assert b'name="model"\r\n\r\nwhisper-1' in request.content and b'name="language"\r\n\r\nfa' in request.content, "Persian is sent as the code fa"

        own = {**cfg, "stt_base_url": "https://stt.test/v1/", "stt_api_key": "stt-key", "language": "Klingon"}
        transcribe._send(transcribe.Part(path), own)
        request = seen[-1]
        assert str(request.url) == "https://stt.test/v1/audio/transcriptions" and request.headers["authorization"] == "Bearer stt-key", "the LLM key never goes to another host"
        assert b'name="language"' not in request.content, "an unknown language is left to auto-detection"

        for broken, message in (({**cfg, "api_key": "bad"}, "401"), ({**cfg, "base_url": "", "stt_base_url": ""}, "وارد نشده")):
            try:
                transcribe._send(transcribe.Part(path), broken)
            except transcribe.TranscribeError as e:
                assert message in str(e)
            else:
                raise AssertionError("a failing endpoint must raise TranscribeError")
    finally:
        transcribe.transport = None


def test_transcription_api_key_is_masked_and_kept_when_blank():
    with TestClient(server.app) as client:
        client.put("/api/settings", json={"stt_api_key": "stt-secret", "stt_base_url": "https://stt.test/v1", "stt_model": "whisper-large-v3"})
        client.put("/api/settings", json={"stt_api_key": ""})
        saved = client.get("/api/settings").json()
        assert saved["stt_api_key"] == "" and saved["stt_api_key_set"]
        assert (saved["stt_base_url"], saved["stt_model"]) == ("https://stt.test/v1", "whisper-large-v3")


def test_silent_audio_is_detected_before_sending():
    import subprocess
    from pathlib import Path

    import imageio_ffmpeg

    def make(name, source):
        path = Path(os.environ["DATA_DIR"]) / name
        subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", "-f", "lavfi", "-i", source, "-t", "1", str(path)], check=True)
        return path

    assert transcribe._is_silent(make("silence.wav", "anullsrc=r=16000:cl=mono"))
    assert not transcribe._is_silent(make("tone.wav", "sine=frequency=440:sample_rate=16000"))


def test_server_side_persian_dates_match_the_calendar():
    from datetime import date

    assert llm.to_jalali(date(2026, 3, 21)) == (1405, 1, 1)  # Nowruz
    assert llm.to_jalali(date(2026, 11, 2)) == (1405, 8, 11)
    assert llm.to_jalali(date(2027, 3, 20)) == (1405, 12, 29)
    assert llm.to_jalali(date(2024, 3, 20)) == (1403, 1, 1)
    assert llm.describe_date("2026-11-02") == "2026-11-02, Monday (11 آبان 1405 in the Persian calendar)"


def test_people_matching_ignores_letter_variants_and_half_spaces():
    directory = [{"id": 1, "name": "نیما رضایی", "role": "مهندس بک‌اند", "aliases": ["نیما ر", "نیما"]}]
    assert people.canonical("نیما", directory) == "نیما رضایی"
    assert people.canonical("نيما  ر", directory) == "نیما رضایی", "Arabic yeh and extra spaces still match"
    assert people.canonical("سارا", directory) == "سارا", "unknown names pass through"
    assert people.canonical(None, directory) is None
    assert people.clean_participants("نیما, علی، نیما ر", directory) == "نیما رضایی, علی"
    context = people.prompt_context("علی", directory)
    assert "Participants of this meeting: علی" in context and "also called: نیما ر, نیما" in context and "role: مهندس بک‌اند" in context


def test_people_api_validates_and_prevents_ambiguous_names():
    with TestClient(server.app) as client:
        created = client.post("/api/people", json={"name": " نیما  رضایی ", "role": "مهندس", "aliases": ["نیما", "نیما", " ", "نیما ر"]})
        assert created.status_code == 201
        person = next(p for p in client.get("/api/people").json() if p["name"] == "نیما رضایی")
        assert person["aliases"] == ["نیما", "نیما ر"], "names are tidied and deduplicated"
        assert client.post("/api/people", json={"name": "نيما"}).status_code == 409, "an alias cannot belong to two people"
        assert client.post("/api/people", json={"name": "   "}).status_code == 422
        assert client.put(f"/api/people/{person['id']}", json={"name": "نیما رضایی", "aliases": ["نیما"], "role": "مدیر"}).status_code == 200
        assert client.delete(f"/api/people/{person['id']}").status_code == 204
        assert all(p["id"] != person["id"] for p in client.get("/api/people").json())


def test_model_sees_the_directory_and_owners_become_full_names():
    prompts = []

    async def spying_chat(cfg, system, user, **kwargs):
        prompts.append(user)
        return "# Budget sync\n## Summary\nOk." if system is llm.MINUTES_PROMPT else TASKS_REPLY  # the model says "Sara"

    fake_services()
    llm.chat = spying_chat
    with TestClient(server.app) as client:
        person = client.post("/api/people", json={"name": "سارا احمدی", "role": "مدیر محصول", "aliases": ["Sara"]}).json()["id"]
        mid = client.post("/api/meetings", files={"file": ("a.mp3", b"audio")}, data={"participants": "Sara, علی"}).json()["id"]
        meeting = client.get(f"/api/meetings/{mid}").json()
        assert meeting["participants"] == "سارا احمدی, علی", "an alias typed as a participant is stored as the full name"
        assert all("also called: Sara" in p and "role: مدیر محصول" in p for p in prompts)
        assert meeting["tasks"][0]["owner"] == "سارا احمدی", "the model's alias is mapped back to the full name"

        task_id = meeting["tasks"][0]["id"]
        client.patch(f"/api/tasks/{task_id}", json={"owner": "sara"})
        assert client.get(f"/api/tasks/{task_id}").json()["owner"] == "سارا احمدی"
        client.patch(f"/api/tasks/{task_id}", json={"owner": ""})
        assert client.get(f"/api/tasks/{task_id}").json()["owner"] is None, "an empty owner clears the assignment"
        client.delete(f"/api/people/{person}")


def test_cut_points_prefer_the_longest_pause_near_the_target():
    pauses = [(580.0, 580.6), (598.0, 600.0), (605.0, 605.5), (900.0, 910.0)]
    assert transcribe._cut_points(1500, pauses) == [599.0, 1199.0], "longest pause first; hard cut where none is close"
    assert transcribe._cut_points(700, pauses) == [], "a short remainder stays in one part"
    assert transcribe._cut_points(1500, [])[0] == 600, "no pause nearby falls back to a hard cut"


def test_audio_is_split_at_pauses_not_mid_speech():
    import math
    import wave
    from pathlib import Path

    rate, workdir = 16000, Path(tempfile.mkdtemp())
    tone = lambda seconds: [int(12000 * math.sin(2 * math.pi * 300 * i / rate)) for i in range(int(seconds * rate))]
    quiet = lambda seconds: [0] * int(seconds * rate)
    samples = tone(8) + quiet(1.5) + tone(8) + quiet(1.5) + tone(6)  # pauses centred at 8.75 s and 18.25 s
    source = workdir / "talk.wav"
    with wave.open(str(source), "wb") as out:
        out.setnchannels(1), out.setsampwidth(2), out.setframerate(rate)
        out.writeframes(b"".join(s.to_bytes(2, "little", signed=True) for s in samples))

    transcribe.PART_SECONDS, transcribe.CUT_WINDOW = 10, 3
    try:
        parts = transcribe._split(source, workdir)
    finally:
        transcribe.PART_SECONDS, transcribe.CUT_WINDOW = 600, 60
    assert len(parts) == 3
    assert abs(parts[1].start - 8.75) < 0.3 and abs(parts[2].start - 18.25) < 0.3
    assert not any(p.phase == "done" for p in parts), "speech parts are not mistaken for silence"


def test_video_audio_is_extracted_and_a_video_without_sound_is_rejected():
    import subprocess
    from pathlib import Path

    import imageio_ffmpeg

    def make(name, *inputs):
        path = Path(tempfile.mkdtemp()) / name
        subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", *inputs, "-t", "2", "-pix_fmt", "yuv420p", str(path)], check=True)
        return path

    picture = ("-f", "lavfi", "-i", "testsrc=size=320x240:rate=25")
    with_sound = make("talk.mp4", *picture, "-f", "lavfi", "-i", "sine=frequency=300:sample_rate=44100")
    parts = transcribe._split(with_sound, Path(tempfile.mkdtemp()))
    assert [p.path.suffix for p in parts] == [".mp3"] and parts[0].path.stat().st_size < with_sound.stat().st_size

    try:
        transcribe._split(make("mute.mp4", *picture), Path(tempfile.mkdtemp()))
    except transcribe.TranscribeError as e:
        assert "هیچ صدایی" in str(e)
    else:
        raise AssertionError("a video without an audio track must be rejected")


def test_json_log_lines_carry_request_and_meeting_ids():
    import io
    import json
    import logging

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logs.JsonFormatter())
    handler.addFilter(logs.ContextFilter())
    logger = logging.getLogger("app.test")
    logger.addHandler(handler)
    logs.request_id.set("req123")
    logs.meeting_id.set("meet456")
    logger.error("part %d failed", 2, extra={"part": 2})
    entry = json.loads(stream.getvalue())
    assert (entry["request_id"], entry["meeting"], entry["part"], entry["msg"]) == ("req123", "meet456", 2, "part 2 failed")
    logger.removeHandler(handler)


def test_tickets_calendar_dates_and_validation():
    fake_services()
    with TestClient(server.app) as client:
        response = client.post("/api/meetings", files={"file": ("sync.mp3", b"audio")}, data={"held_on": "2026-10-28"})
        mid = response.json()["id"]
        meeting = client.get(f"/api/meetings/{mid}").json()
        assert meeting["held_on"] == "2026-10-28", "the calendar uses the meeting day, not the upload day"
        assert client.post("/api/meetings", files={"file": ("a.mp3", b"x")}, data={"held_on": "31 Oct"}).status_code == 422

        assert client.patch(f"/api/meetings/{mid}", json={"held_on": "2026-10-30"}).status_code == 200
        assert client.get(f"/api/meetings/{mid}").json()["held_on"] == "2026-10-30"

        task = next(t for t in client.get("/api/tasks").json() if t["meeting_id"] == mid)  # the DB is shared with other tests
        assert "description" not in task, "list rows stay light"
        ticket = client.get(f"/api/tasks/{task['id']}").json()
        assert ticket["description"].startswith("## Context") and ticket["meeting_held_on"] == "2026-10-30"

        client.patch(f"/api/tasks/{task['id']}", json={"deadline": "2026-11-05"})
        assert client.get(f"/api/tasks/{task['id']}").json()["deadline"] == "2026-11-05"
        assert client.patch(f"/api/tasks/{task['id']}", json={"deadline": "soon"}).status_code == 422
        client.patch(f"/api/tasks/{task['id']}", json={"deadline": ""})
        assert client.get(f"/api/tasks/{task['id']}").json()["deadline"] is None, "an empty deadline clears it"
        assert client.get("/api/tasks/99999").status_code == 404


def test_deleted_tasks_disappear_everywhere_and_can_be_restored():
    called = []
    mock_jira(lambda request: called.append(request.url.path) or httpx.Response(201, json={"key": "OPS-1"}))
    fake_services()
    try:
        with TestClient(server.app) as client:
            configure_jira(client)
            mid = upload(client)
            task_id = next(t["id"] for t in client.get("/api/tasks").json() if t["meeting_id"] == mid)
            open_tasks = lambda: next(m for m in client.get("/api/meetings").json() if m["id"] == mid)["open_tasks"]
            assert open_tasks() == 1

            response = client.delete(f"/api/tasks/{task_id}")
            assert response.status_code == 200 and response.json() == {"jira_key": None}
            assert all(t["id"] != task_id for t in client.get("/api/tasks?include_done=true").json())
            assert client.get(f"/api/tasks/{task_id}").status_code == 404
            assert client.get(f"/api/meetings/{mid}").json()["tasks"] == []
            assert open_tasks() == 0, "the open-task count ignores deleted tasks"
            assert client.delete(f"/api/tasks/{task_id}").status_code == 404, "deleting twice is a clean 404"

            sent = client.post("/api/jira/send", json={"project": "OPS", "items": [{"task_id": task_id, "issue_type_id": "1"}]}).json()["results"][0]
            assert "error" in sent and not called, "a deleted task is never sent to Jira"

            assert client.post(f"/api/tasks/{task_id}/restore").status_code == 204
            assert client.get(f"/api/tasks/{task_id}").json()["title"] == "Send the budget" and open_tasks() == 1
            assert client.post(f"/api/tasks/{task_id}/restore").status_code == 404, "only deleted tasks can be restored"
    finally:
        jira.transport = None


def test_settings_keep_key_when_blank():
    with TestClient(server.app) as client:
        client.put("/api/settings", json={"api_key": "secret", "language": "English"})
        client.put("/api/settings", json={"api_key": "", "language": "Persian"})
        saved = client.get("/api/settings").json()
        assert saved["api_key"] == "" and saved["api_key_set"] and saved["language"] == "Persian"
        assert "model" not in saved, "the model is chosen per meeting, not globally"


def test_each_meeting_is_processed_with_its_own_model():
    seen = []

    async def fake_chat(cfg, system, user, **kwargs):
        seen.append(cfg["model"])
        return "# Budget sync\n## Summary\nOk." if system is llm.MINUTES_PROMPT else TASKS_REPLY

    fake_services()
    llm.chat = fake_chat
    with TestClient(server.app) as client:
        heavy = client.post("/api/meetings", files={"file": ("a.mp3", b"audio")}, data={"model": "gpt-4o"}).json()["id"]
        assert client.get(f"/api/meetings/{heavy}").json()["model"] == "gpt-4o"
        assert set(seen) == {"gpt-4o"}

        seen.clear()
        mid = client.post("/api/recordings", json={}).json()["id"]
        client.put(f"/api/recordings/{mid}/chunks", params={"offset": 0}, content=b"a" * 5000)
        client.post(f"/api/recordings/{mid}/finish", json={"model": "gpt-4o-mini"})
        assert set(seen) == {"gpt-4o-mini"}, "a recording takes the model chosen when it ended"

        # no model chosen (e.g. an abandoned recording): fails clearly, and the model can be set afterwards
        seen.clear()
        llm.chat = original_chat
        bare = client.post("/api/meetings", files={"file": ("b.mp3", b"audio")}).json()["id"]
        meeting = client.get(f"/api/meetings/{bare}").json()
        assert meeting["status"] == "failed" and "مدلی انتخاب نشده" in meeting["error"] and meeting["transcript"]
        llm.chat = fake_chat
        assert client.patch(f"/api/meetings/{bare}", json={"model": "oc/free"}).status_code == 200
        client.post(f"/api/meetings/{bare}/retry")
        assert client.get(f"/api/meetings/{bare}").json()["status"] == "done" and set(seen) == {"oc/free"}


def mock_jira(handler):
    """Replace the network under the Jira client with a function (request -> httpx.Response)."""
    jira.transport = httpx.MockTransport(handler)


def configure_jira(client, user="jira-user", password="pw-secret"):
    body = {"jira_url": "https://jira.test/", "jira_user": user, "jira_password": password}
    assert client.put("/api/settings", json=body).status_code == 200


def test_markdown_becomes_jira_wiki_markup():
    markdown = "## Context\nText with **bold**, *it*, `code` and [site](http://x).\n- item\n  - nested\n- [ ] todo\n- [x] done\n1. first"
    assert jira.markdown_to_wiki(markdown).splitlines() == [
        "h2. Context", "Text with *bold*, _it_, {{code}} and [site|http://x].", "* item", "** nested", "* ☐ todo", "* ☑ done", "# first",
    ]


def test_jira_client_authentication_payloads_and_errors():
    import asyncio
    import base64

    seen = []

    def handler(request):
        body = json.loads(request.content) if request.content else None
        seen.append((request.method, request.url.path, request.headers.get("authorization"), body))
        if request.url.path == "/rest/api/2/issue":
            return httpx.Response(201, json={"key": "OPS-7"})
        if request.url.path.startswith("/rest/agile/1.0/epic/"):
            return httpx.Response(204)
        if request.url.path == "/rest/api/2/search":
            epic = {"key": "OPS-1", "fields": {"summary": "Billing", "issuetype": {"name": "Epic"}, "status": {"name": "Open"}}}
            story = {"key": "OPS-3", "fields": {"summary": "Pay run", "issuetype": {"name": "Story"}, "status": {"name": "In Progress"}}}
            return httpx.Response(200, json={"issues": [epic] if "issuetype = Epic" in body["jql"] else [epic, story]})
        if request.url.path == "/rest/api/2/user/assignable/search":
            return httpx.Response(200, json=[{"name": "sara.j", "displayName": "Sara", "active": True}, {"name": "old.u", "active": False}])
        if request.url.path == "/rest/api/2/project/OPS":
            return httpx.Response(200, json={"issueTypes": [{"id": "1", "name": "Task", "subtask": False}, {"id": "5", "name": "Sub-task", "subtask": True}]})
        return httpx.Response(404, json={"errorMessages": ["nope"], "errors": {}})

    mock_jira(handler)
    try:
        with TestClient(server.app) as client:
            configure_jira(client)
            key = asyncio.run(jira.create_issue(project="OPS", issue_type_id="10002", title="  Fix   billing  ", description="## Context\nWhy.",
                                                source="منبع: جلسه «X»", assignee="sara.j", priority_id="3", due="2026-11-05", parent_key="OPS-2"))
            assert key == "OPS-7"
            method, path, auth, body = seen[-1]
            assert (method, path) == ("POST", "/rest/api/2/issue")
            assert auth == "Basic " + base64.b64encode(b"jira-user:pw-secret").decode()
            assert body["fields"] == {
                "project": {"key": "OPS"}, "issuetype": {"id": "10002"}, "summary": "Fix billing", "parent": {"key": "OPS-2"},
                "assignee": {"name": "sara.j"}, "priority": {"id": "3"}, "duedate": "2026-11-05",
                "description": "h2. Context\nWhy.\n\n----\n_منبع: جلسه «X»_",
            }

            meta = asyncio.run(jira.project_meta("OPS"))
            assert meta["issue_types"][1] == {"id": "5", "name": "Sub-task", "subtask": True} and meta["epics"] == [{"key": "OPS-1", "summary": "Billing", "type": "Epic", "status": "Open"}]
            asyncio.run(jira.add_to_epic("OPS-1", "OPS-7"))
            assert seen[-1][1] == "/rest/agile/1.0/epic/OPS-1/issue" and seen[-1][3] == {"issues": ["OPS-7"]}

            parents = asyncio.run(jira.find_parents("OPS"))
            assert [p["key"] for p in parents] == ["OPS-3"] and parents[0]["status"] == "In Progress", "epics cannot be parents of sub-tasks"
            assert "statusCategory != Done" in seen[-1][3]["jql"]
            assert asyncio.run(jira.assignable_users("OPS", "sa")) == [{"name": "sara.j", "display": "Sara"}], "inactive users are not offered"
            assert seen[-1][1] == "/rest/api/2/user/assignable/search"
            for bad_key in ("OPS-1/../../x", "ops", ""):
                for call in (lambda: jira.add_to_epic(bad_key, "OPS-7"), lambda: jira.create_issue(project="OPS", issue_type_id="5", title="t", description="", parent_key=bad_key)):
                    if not bad_key:
                        continue
                    try:
                        asyncio.run(call())
                    except jira.JiraError:
                        pass
                    else:
                        raise AssertionError(f"issue key {bad_key!r} must be refused")
            asyncio.run(jira.find_parents("OPS", "ops-5"))
            assert 'key = "OPS-5"' in seen[-1][3]["jql"] and "subtaskIssueTypes()" in seen[-1][3]["jql"]
            asyncio.run(jira.find_parents("OPS", 'say "hi"'))
            assert 'summary ~ "say \\"hi\\""' in seen[-1][3]["jql"], "quotes in the search text cannot break out of the JQL string"
            for bad in ('OPS" OR 1=1', "1OPS", ""):
                try:
                    asyncio.run(jira.project_meta(bad))
                except jira.JiraError:
                    pass
                else:
                    raise AssertionError(f"project key {bad!r} must be refused")

            configure_jira(client, user="", password="token123")  # empty username: the password is a personal access token
            asyncio.run(jira.find_parents("OPS"))
            assert seen[-1][2] == "Bearer token123"

        mock_jira(lambda request: httpx.Response(400, json={"errorMessages": [], "errors": {"customfield_10010": "Team is required"}}))
        try:
            asyncio.run(jira.create_issue(project="OPS", issue_type_id="1", title="t", description=""))
        except jira.JiraError as e:
            assert "customfield_10010: Team is required" in str(e)
        gateway = "<html><body><h1>Error 504 | Gateway Timeout</h1><p>Website server temporarily Unavailable!</p></body></html>"
        mock_jira(lambda request: httpx.Response(504, headers={"content-type": "text/html", "server": "ArvanCloud"}, text=gateway))
        try:
            asyncio.run(jira.projects())
        except jira.JiraError as e:
            assert "ArvanCloud" in str(e) and "نه از خود Jira" in str(e) and "VPN" in str(e) and "<h1>" not in str(e), "a CDN error page must not be mistaken for a Jira answer"
        mock_jira(lambda request: httpx.Response(403, json={"message": "Basic Authentication has been disabled on this instance."}))
        try:
            asyncio.run(jira.projects())
        except jira.JiraError as e:
            assert "Personal Access Tokens" in str(e) and "خالی" in str(e), "tell the user to use a token instead of a password"
        mock_jira(lambda request: httpx.Response(403, headers={"X-Authentication-Denied-Reason": "CAPTCHA_CHALLENGE; login-url=https://j/login"}, text=""))
        try:
            asyncio.run(jira.projects())
        except jira.JiraError as e:
            assert "CAPTCHA" in str(e)
        mock_jira(lambda request: httpx.Response(401, headers={"X-Seraph-LoginReason": "AUTHENTICATION_DENIED"}, text=""))
        try:
            asyncio.run(jira.projects())
        except jira.JiraError as e:
            assert "AUTHENTICATION_DENIED" in str(e) and "۴۰۱" in str(e)
    finally:
        jira.transport = None


def test_sending_tasks_to_jira_keeps_partial_progress_and_never_leaks_the_password():
    created = []

    def handler(request):
        if request.url.path == "/rest/api/2/issue":
            payload = json.loads(request.content)
            if len(created) == 1:  # the second ticket is refused by Jira
                created.append(None)
                return httpx.Response(400, json={"errors": {"customfield_10010": "Team is required"}})
            created.append(payload["fields"])
            return httpx.Response(201, json={"key": f"OPS-{len(created)}"})
        if request.url.path == "/rest/agile/1.0/epic/OPS-9/issue":
            return httpx.Response(400, json={"errorMessages": ["epic is closed"]})
        return httpx.Response(404, json={})

    fake_services()
    mock_jira(handler)
    try:
        with TestClient(server.app) as client:
            configure_jira(client)
            saved = client.get("/api/settings").json()
            assert saved["jira_password"] == "" and saved["jira_password_set"] and "pw-secret" not in json.dumps(saved)
            assert client.get("/api/settings").json()["jira_url"] == "https://jira.test", "the trailing slash is dropped"
            assert client.put("/api/settings", json={"jira_url": "jira.test"}).status_code == 422
            client.put("/api/settings", json={"jira_password": ""})  # blank keeps the stored secret
            assert client.get("/api/settings").json()["jira_password_set"]

            ids = []
            for _ in range(2):
                mid = upload(client)
                ids.append(next(t["id"] for t in client.get("/api/tasks").json() if t["meeting_id"] == mid))
            body = {"project": "OPS", "items": [{"task_id": i, "issue_type_id": "10002", "epic_key": "OPS-9", "assignee": "sara.j", "priority_id": "3", "due": "2026-11-05"} for i in ids]}

            assert client.post("/api/jira/send", json={**body, "items": [{**body["items"][0], "due": "soon"}]}).status_code == 422
            assert not created, "a bad date is rejected before anything is created"

            results = client.post("/api/jira/send", json=body).json()["results"]
            assert results[0]["key"] == "OPS-1" and results[0]["link"] == "https://jira.test/browse/OPS-1"
            assert "epic is closed" in results[0]["warning"], "the issue exists even though linking it to the epic failed"
            assert "customfield_10010: Team is required" in results[1]["error"]
            fields = created[0]
            assert fields["assignee"] == {"name": "sara.j"} and fields["priority"] == {"id": "3"} and fields["duedate"] == "2026-11-05"
            assert fields["description"].startswith("h2. Context\nBudget review.") and "منبع: جلسه «Budget sync»" in fields["description"]

            assert client.get(f"/api/tasks/{ids[0]}").json()["jira_key"] == "OPS-1"
            assert client.get(f"/api/tasks/{ids[1]}").json()["jira_key"] is None, "a failed task can be sent again"
            assert next(t for t in client.get("/api/tasks").json() if t["id"] == ids[0])["jira_link"] == "https://jira.test/browse/OPS-1"
            again = client.post("/api/jira/send", json={**body, "items": [body["items"][0]]}).json()["results"][0]
            assert "قبلاً" in again["error"], "a task is never created twice"
    finally:
        jira.transport = None


def test_each_task_goes_to_its_own_epic_or_parent_and_pickers_load_from_jira():
    created, linked = [], []

    def handler(request):
        if request.url.path == "/rest/api/2/issue":
            created.append(json.loads(request.content)["fields"])
            return httpx.Response(201, json={"key": f"OPS-{20 + len(created)}"})
        if request.url.path.startswith("/rest/agile/1.0/epic/"):
            linked.append(request.url.path)
            return httpx.Response(204)
        if request.url.path == "/rest/api/2/search":
            return httpx.Response(200, json={"issues": [{"key": "OPS-3", "fields": {"summary": "Pay run", "issuetype": {"name": "Story"}, "status": {"name": "Open"}}}]})
        if request.url.path == "/rest/api/2/user/assignable/search":
            return httpx.Response(200, json=[{"name": "sara.j", "displayName": "Sara"}])
        return httpx.Response(404, json={})

    fake_services()
    mock_jira(handler)
    try:
        with TestClient(server.app) as client:
            configure_jira(client)
            assert client.get("/api/jira/issues", params={"project": "OPS"}).json() == [{"key": "OPS-3", "summary": "Pay run", "type": "Story", "status": "Open"}]
            assert client.get("/api/jira/users", params={"project": "OPS", "q": "sa"}).json() == [{"name": "sara.j", "display": "Sara"}]
            ids = []
            for _ in range(2):
                mid = upload(client)
                ids.append(next(t["id"] for t in client.get("/api/tasks").json() if t["meeting_id"] == mid))
            items = [{"task_id": ids[0], "issue_type_id": "1", "epic_key": "OPS-1"}, {"task_id": ids[1], "issue_type_id": "5", "parent_key": "OPS-3"}]
            results = client.post("/api/jira/send", json={"project": "OPS", "items": items}).json()["results"]
            assert [r["key"] for r in results] == ["OPS-21", "OPS-22"]
            assert linked == ["/rest/agile/1.0/epic/OPS-1/issue"], "only the regular issue joins the epic"
            assert "parent" not in created[0] and created[1]["parent"] == {"key": "OPS-3"} and created[1]["issuetype"] == {"id": "5"}
            bad = client.post("/api/jira/send", json={"project": "OPS", "items": [{"task_id": ids[0], "issue_type_id": "1", "parent_key": "x/../y"}]}).json()["results"][0]
            assert "error" in bad, "a task already sent is refused before its parent key is even looked at"
    finally:
        jira.transport = None


def test_connection_check_and_task_editing_and_jira_usernames():
    def handler(request):
        if request.url.path == "/rest/api/2/myself":
            return httpx.Response(200, json={"displayName": "Demo User"})
        return httpx.Response(200, json={"version": "9.12.1", "deploymentType": "Server"})

    mock_jira(handler)
    try:
        with TestClient(server.app) as client:
            configure_jira(client)
            assert client.get("/api/jira/status").json() == {"ok": True, "user": "Demo User", "version": "9.12.1", "deployment": "Server"}
            mock_jira(lambda request: httpx.Response(401, text=""))
            status = client.get("/api/jira/status").json()
            assert status["ok"] is False and "۴۰۱" in status["error"], "a failed check is reported, not raised"

        fake_services()
        with TestClient(server.app) as client:
            task_id = next(t["id"] for t in client.get("/api/tasks").json() if t["title"] == "Send the budget")
            edited = {"title": "  Send   the Q4 budget ", "description": "## Context\nEdited.  ", "priority": "high"}
            assert client.patch(f"/api/tasks/{task_id}", json=edited).status_code == 200
            task = client.get(f"/api/tasks/{task_id}").json()
            assert (task["title"], task["description"], task["priority"]) == ("Send the Q4 budget", "## Context\nEdited.", "high")
            assert client.patch(f"/api/tasks/{task_id}", json={"title": "   "}).status_code == 422
            assert client.patch(f"/api/tasks/{task_id}", json={"priority": "urgent"}).status_code == 422

            person = client.post("/api/people", json={"name": "سارا احمدی", "jira_username": " sara.j "}).json()["id"]
            assert next(p for p in client.get("/api/people").json() if p["id"] == person)["jira_username"] == "sara.j"
            client.put(f"/api/people/{person}", json={"name": "سارا احمدی", "jira_username": "sara.ahmadi"})
            assert next(p for p in client.get("/api/people").json() if p["id"] == person)["jira_username"] == "sara.ahmadi"
            client.delete(f"/api/people/{person}")
    finally:
        jira.transport = None


if __name__ == "__main__":
    for name, test in list(globals().items()):
        if name.startswith("test_"):
            test()
            print("ok", name)
