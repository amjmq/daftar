"""Speech to text through any OpenAI-compatible /audio/transcriptions endpoint (OpenAI, Groq, a local Whisper server...).

Uploads are limited in size and duration by most providers, and silent audio makes speech models invent text, so the
recording is re-encoded to speech-grade mono MP3, split at pauses in the speech (never mid-sentence), silent parts are
skipped, and the parts are transcribed two at a time.
"""
import asyncio
import logging
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
import imageio_ffmpeg

log = logging.getLogger("app.transcribe")

transport: httpx.BaseTransport | None = None  # tests inject httpx.MockTransport here
LANGUAGE_CODES = {"persian": "fa", "farsi": "fa", "english": "en", "arabic": "ar", "turkish": "tr", "german": "de", "french": "fr",
                  "spanish": "es", "russian": "ru", "italian": "it", "hindi": "hi", "urdu": "ur"}
PART_SECONDS = 600  # target part length
MAX_STRETCH = 1.2  # a final part up to this many times PART_SECONDS is kept whole instead of leaving a stub
CUT_WINDOW = 60  # seconds either side of a target boundary searched for a pause
PAUSE_FILTER = "silencedetect=noise=-35dB:d=0.5"
BYTES_PER_SECOND = 4000  # 32 kbps constant bitrate, so a duration follows from a size
CONCURRENCY = 2
ATTEMPTS = 2
SILENCE_DB = -40  # a part whose loudest sample is quieter than this is treated as silence


class TranscribeError(Exception):
    """Raised for any transcription failure; the message is shown to the user."""


@dataclass
class Part:
    path: Path
    index: int = 1
    start: float = 0.0  # position in the original recording, seconds
    end: float = 0.0
    phase: str = "queued"  # queued | sending | done
    text: str = ""

    @property
    def fraction(self) -> float:
        return {"done": 1.0, "sending": 0.5}.get(self.phase, 0.0)


def _ffmpeg(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-hide_banner", "-nostats", *args], capture_output=True, text=True)


def _duration(path: Path) -> float:
    found = re.search(r"Duration: (\d+):(\d+):([\d.]+)", _ffmpeg("-i", str(path)).stderr)
    if not found:
        return path.stat().st_size / BYTES_PER_SECOND
    hours, minutes, seconds = found.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _clock(seconds: float) -> str:
    hours, rest = divmod(int(seconds), 3600)
    return f"{hours}:{rest // 60:02d}:{rest % 60:02d}" if hours else f"{rest // 60}:{rest % 60:02d}"


def _is_silent(path: Path) -> bool:
    """Speech models invent plausible text for silent audio, so silent parts must never be sent."""
    report = _ffmpeg("-i", str(path), "-af", "volumedetect", "-f", "null", "-").stderr
    loudest = re.search(r"max_volume: (-?[\d.]+) dB", report)
    return bool(loudest) and float(loudest.group(1)) < SILENCE_DB


def _cut_points(duration: float, pauses: list[tuple[float, float]]) -> list[float]:
    """Cut near every PART_SECONDS, at the longest pause close to the target; hard cut if there is none."""
    cuts, start = [], 0.0
    while duration - start > PART_SECONDS * MAX_STRETCH:
        target = start + PART_SECONDS
        near = [(end - begin, (begin + end) / 2) for begin, end in pauses if abs((begin + end) / 2 - target) <= CUT_WINDOW]
        if not near:
            log.warning("no pause near %s; cutting mid-speech", _clock(target), extra={"target_s": round(target)})
        start = max(near)[1] if near else target
        cuts.append(start)
    return cuts


def _split(source: Path, workdir: Path) -> list[Part]:
    began = time.monotonic()
    full = workdir / "full.mp3"
    encoded = _ffmpeg("-i", str(source), "-vn", "-ac", "1", "-ar", "16000", "-af", PAUSE_FILTER,
                      "-c:a", "libmp3lame", "-b:a", "32k", str(full))
    if encoded.returncode or not full.exists():
        if "does not contain any stream" in encoded.stderr:  # e.g. a screen recording without a microphone track
            raise TranscribeError("این فایل هیچ صدایی ندارد.")
        raise TranscribeError(f"فایل صوتی قابل خواندن نیست: {encoded.stderr.strip()[-200:]}")
    pauses = [(float(end) - float(length), float(end)) for end, length in
              re.findall(r"silence_end: ([\d.]+) \| silence_duration: ([\d.]+)", encoded.stderr)]
    duration = _duration(full)
    cuts = _cut_points(duration, pauses)

    parts = []
    bounds = [0.0, *cuts, duration]
    for index, (start, end) in enumerate(zip(bounds, bounds[1:]), 1):
        path = workdir / f"part{index:03d}.mp3"
        cut = _ffmpeg("-ss", str(start), "-t", str(end - start), "-i", str(full), "-c", "copy", str(path))
        if cut.returncode or not path.exists():
            raise TranscribeError(f"بخش‌بندی صدا ناموفق بود: {cut.stderr.strip()[-200:]}")
        parts.append(Part(path, index, start, end, phase="done") if _is_silent(path) else Part(path, index, start, end))
    log.info(
        "audio prepared: %s in %d part(s)", _clock(duration), len(parts),
        extra={"duration_s": round(duration), "parts": len(parts), "cuts": [_clock(c) for c in cuts], "pauses_found": len(pauses),
               "silent_parts": [p.index for p in parts if p.phase == "done"], "seconds": round(time.monotonic() - began, 1)},
    )
    return parts


def _language_code(language: str) -> str:
    """Providers want an ISO 639-1 code ("fa"); the setting is a name ("Persian") that the prompts also use. Unknown -> auto-detect."""
    name = language.strip().lower()
    return LANGUAGE_CODES.get(name, name if re.fullmatch(r"[a-z]{2}", name) else "")


def _endpoint(cfg: dict) -> tuple[str, dict]:
    """URL and headers; an empty transcription URL means "the same provider as the language model", key included."""
    own = cfg["stt_base_url"].strip()
    base, key = (own, cfg["stt_api_key"]) if own else (cfg["base_url"], cfg["stt_api_key"] or cfg["api_key"])
    if not base:
        raise TranscribeError("آدرس سرویس رونویسی در تنظیمات وارد نشده است.")
    return base.rstrip("/") + "/audio/transcriptions", ({"Authorization": f"Bearer {key}"} if key else {})


def _send(part: Part, cfg: dict) -> str:
    url, headers = _endpoint(cfg)
    part.phase = "sending"
    log.info("part %d sending (%d KB)", part.index, part.path.stat().st_size // 1024, extra={"part": part.index})
    data = {"model": cfg["stt_model"] or "whisper-1", "response_format": "json"}
    if code := _language_code(cfg["language"]):
        data["language"] = code
    try:
        with part.path.open("rb") as f, httpx.Client(timeout=httpx.Timeout(900, connect=30), transport=transport) as client:
            response = client.post(url, headers=headers, data=data, files={"file": (part.path.name, f, "audio/mpeg")})
    except httpx.HTTPError as e:
        raise TranscribeError(f"اتصال به سرویس رونویسی ممکن نشد: {e or type(e).__name__}") from e
    if response.status_code >= 400:
        raise TranscribeError(f"سرویس رونویسی خطا داد ({response.status_code}): {response.text[:200]}")
    try:
        return str(response.json()["text"]).strip()  # empty when the part has no speech
    except (ValueError, KeyError, TypeError) as e:
        raise TranscribeError("پاسخ سرویس رونویسی قابل خواندن نیست.") from e


def _transcribe_part(part: Part, cfg: dict) -> str:
    for attempt in range(1, ATTEMPTS + 1):
        began = time.monotonic()
        try:
            text = _send(part, cfg)
            log.info("part %d transcribed in %.0fs: %d chars", part.index, time.monotonic() - began, len(text),
                     extra={"part": part.index, "seconds": round(time.monotonic() - began, 1), "chars": len(text)})
            return text
        except TranscribeError as e:
            retrying = attempt < ATTEMPTS
            log.log(logging.WARNING if retrying else logging.ERROR, "part %d failed (attempt %d/%d)%s: %s", part.index, attempt,
                    ATTEMPTS, ", retrying" if retrying else "", e, extra={"part": part.index, "attempt": attempt})
            if not retrying:
                raise


async def _report_progress(parts: list[Part], on_progress) -> None:
    while True:
        on_progress("transcribing", fraction=sum(p.fraction for p in parts) / len(parts), parts=len(parts),
                    done=sum(p.phase == "done" for p in parts))
        await asyncio.sleep(2)


async def transcribe(path: str, cfg: dict, on_progress) -> str:
    """Transcribe an audio/video file; on_progress(stage, **fields) is called while it runs."""
    began = time.monotonic()
    on_progress("preparing")
    with tempfile.TemporaryDirectory() as workdir:
        parts = await asyncio.to_thread(_split, Path(path), Path(workdir))
        gate = asyncio.Semaphore(CONCURRENCY)

        async def run(part: Part) -> None:
            if part.phase == "done":  # silent part, nothing to send
                return
            async with gate:
                part.text = await asyncio.to_thread(_transcribe_part, part, cfg)
                part.phase = "done"

        reporter = asyncio.create_task(_report_progress(parts, on_progress))
        try:
            await asyncio.gather(*(run(p) for p in parts))
        finally:
            reporter.cancel()
    transcript = "\n\n".join(
        f"[بخش {p.index} از {len(parts)} · {_clock(p.start)}–{_clock(p.end)}]\n{p.text}" if len(parts) > 1 else p.text
        for p in parts if p.text.strip()
    )
    if not transcript:
        raise TranscribeError("گفتاری در فایل تشخیص داده نشد.")
    log.info("transcription finished: %d chars", len(transcript), extra={"chars": len(transcript), "seconds": round(time.monotonic() - began, 1)})
    return transcript
