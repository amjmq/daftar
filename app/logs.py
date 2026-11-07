"""Structured logging with request and meeting correlation.

Every record carries `request_id` (the HTTP request that started the work) and `meeting` (the meeting being
processed), so one grep over the container logs reconstructs a meeting's whole life. Transcripts, audio and
credentials are never logged, only sizes and timings. A meeting's INFO+ lines are also kept in the database so the
UI can show them without shell access.

Env: LOG_LEVEL (default INFO), LOG_FORMAT=text for human-readable lines instead of JSON.
"""
import json
import logging
import os
import sys
from contextvars import ContextVar
from datetime import datetime, timezone

import db

request_id: ContextVar[str] = ContextVar("request_id", default="-")
meeting_id: ContextVar[str] = ContextVar("meeting_id", default="-")

# Attributes every LogRecord has; anything else was passed through `extra=` and is a structured field.
_STANDARD = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime", "request_id", "meeting"}


class ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id, record.meeting = request_id.get(), meeting_id.get()
        return True


def _fields(record: logging.LogRecord) -> dict:
    return {k: v for k, v in record.__dict__.items() if k not in _STANDARD}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "meeting": record.meeting,
            "request_id": record.request_id,
            **_fields(record),
        }
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        fields = " ".join(f"{k}={v}" for k, v in _fields(record).items())
        line = f"{self.formatTime(record, '%H:%M:%S')} {record.levelname:<7} {record.name:<15} [{record.meeting} {record.request_id}] {record.getMessage()} {fields}".rstrip()
        return line + (f"\n{self.formatException(record.exc_info)}" if record.exc_info else "")


class EventHandler(logging.Handler):
    """Mirrors a meeting's log lines into its persisted timeline."""

    def emit(self, record: logging.LogRecord) -> None:
        if record.meeting == "-":
            return
        try:
            db.add_event(record.meeting, record.levelname, record.getMessage())
        except Exception:  # logging must never break the work it observes
            self.handleError(record)


def setup() -> None:
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(TextFormatter() if os.environ.get("LOG_FORMAT") == "text" else JsonFormatter())
    stream.addFilter(ContextFilter())
    stream.setLevel(level)
    root = logging.getLogger()
    root.handlers[:] = [stream]
    root.setLevel(min(logging.getLevelName(level), logging.INFO))  # a quiet LOG_LEVEL must not thin out meeting timelines

    events = EventHandler(logging.INFO)
    events.addFilter(ContextFilter())
    logging.getLogger("app").addHandler(events)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # httpx logs every request line at INFO
