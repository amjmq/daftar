"""SQLite helpers. ponytail: one short-lived connection per call; fine for a single-user app."""
import os
import sqlite3
from contextlib import closing

DATA_DIR = os.environ.get("DATA_DIR", "data")
DB_PATH = os.path.join(DATA_DIR, "app.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS meetings (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    filename TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    error TEXT,
    participants TEXT NOT NULL DEFAULT '',
    transcript TEXT,
    minutes TEXT,
    tasks_extracted INTEGER NOT NULL DEFAULT 0,
    model TEXT NOT NULL DEFAULT '',  -- the language model chosen for this meeting
    held_on TEXT,  -- the day the meeting took place (ISO date); calendar position
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    owner TEXT,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',  -- Markdown ticket body
    deadline TEXT,  -- ISO date or NULL
    priority TEXT NOT NULL DEFAULT 'medium',
    done INTEGER NOT NULL DEFAULT 0,
    jira_key TEXT,  -- set once the task has been created in Jira
    deleted_at TEXT  -- soft delete: set when the user removes the task, cleared by undo
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS people (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,  -- full name; what appears in minutes and on tasks
    role TEXT NOT NULL DEFAULT '',
    aliases TEXT NOT NULL DEFAULT '[]',  -- JSON list of other names the person is called by
    jira_username TEXT NOT NULL DEFAULT ''  -- used as the assignee when the person's tasks are sent to Jira
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    level TEXT NOT NULL,
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_by_meeting ON events (meeting_id, id);
"""

SETTING_KEYS = ("base_url", "api_key", "stt_base_url", "stt_api_key", "stt_model", "language", "jira_url", "jira_user", "jira_password", "jira_verify_tls")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring a database created by an earlier version up to the current schema."""
    meetings, tasks = _columns(conn, "meetings"), _columns(conn, "tasks")
    if "tasks_extracted" not in meetings:
        conn.execute("ALTER TABLE meetings ADD COLUMN tasks_extracted INTEGER NOT NULL DEFAULT 0")
        conn.execute("UPDATE meetings SET tasks_extracted = 1 WHERE status = 'done'")
    if "model" not in meetings:
        conn.execute("ALTER TABLE meetings ADD COLUMN model TEXT NOT NULL DEFAULT ''")
    if "held_on" not in meetings:
        conn.execute("ALTER TABLE meetings ADD COLUMN held_on TEXT")
        conn.execute("UPDATE meetings SET held_on = date(created_at)")
    if "task" in tasks:
        conn.execute("ALTER TABLE tasks RENAME COLUMN task TO title")
    if "description" not in tasks:
        conn.execute("ALTER TABLE tasks ADD COLUMN description TEXT NOT NULL DEFAULT ''")
    if "jira_key" not in tasks:
        conn.execute("ALTER TABLE tasks ADD COLUMN jira_key TEXT")
    if "deleted_at" not in tasks:
        conn.execute("ALTER TABLE tasks ADD COLUMN deleted_at TEXT")
    if "jira_username" not in _columns(conn, "people"):
        conn.execute("ALTER TABLE people ADD COLUMN jira_username TEXT NOT NULL DEFAULT ''")


def init() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with closing(_connect()) as conn, conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


def query(sql: str, args: tuple = ()) -> list[dict]:
    with closing(_connect()) as conn:
        return [dict(row) for row in conn.execute(sql, args)]


def execute(sql: str, args: tuple = ()) -> int:
    with closing(_connect()) as conn, conn:
        return conn.execute(sql, args).lastrowid


def add_event(meeting_id: str, level: str, message: str) -> None:
    """Append to a meeting's timeline; silently a no-op once the meeting is gone."""
    execute(
        "INSERT INTO events (meeting_id, level, message) SELECT ?, ?, ? WHERE EXISTS (SELECT 1 FROM meetings WHERE id = ?)",
        (meeting_id, level, message, meeting_id),
    )


def settings() -> dict:
    """Stored settings layered over environment defaults."""
    defaults = {
        "base_url": os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1"),
        "api_key": os.environ.get("LLM_API_KEY", ""),
        "stt_base_url": os.environ.get("STT_BASE_URL", ""),  # empty: the same provider as the language model
        "stt_api_key": os.environ.get("STT_API_KEY", ""),
        "stt_model": os.environ.get("STT_MODEL", "whisper-1"),
        "language": "Persian",
        "jira_url": "",
        "jira_user": "",
        "jira_password": "",
        "jira_verify_tls": "true",
    }
    return defaults | {r["key"]: r["value"] for r in query("SELECT key, value FROM settings")}


def save_settings(values: dict) -> None:
    for key, value in values.items():
        if key in SETTING_KEYS:
            execute(
                "INSERT INTO settings VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
