"""Video (and podcast) transcription.

Primary path: YouTube's own captions through youtube-transcript-api (fast, free, no download).
Fallback: download the audio with yt-dlp and transcribe locally with faster-whisper. The
fallback is off by default because it is slow on CPU; enable with WHISPER_FALLBACK=true and
install requirements-whisper.txt.
"""

from __future__ import annotations

import logging
import re
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api import YouTubeTranscriptApiException

from ..config import settings

log = logging.getLogger(__name__)

MAX_CHARS = 40_000
MARKER_EVERY_SECONDS = 120


class TranscriptError(RuntimeError):
    pass


def youtube_video_id(url: str) -> str | None:
    parsed = urlparse(url)
    host = parsed.netloc.lower().removeprefix("www.").removeprefix("m.")
    if host == "youtu.be":
        return parsed.path.lstrip("/").split("/")[0] or None
    if host.endswith("youtube.com"):
        if parsed.path == "/watch":
            return parse_qs(parsed.query).get("v", [None])[0]
        match = re.match(r"^/(?:shorts|embed|live|v)/([\w-]{11})", parsed.path)
        if match:
            return match.group(1)
    return None


def _format(segments: list[tuple[float, str]]) -> str:
    """Join caption segments into paragraphs with a [mm:ss] marker every couple of minutes."""
    parts: list[str] = []
    next_marker = 0.0
    for start, text in segments:
        text = text.replace("\n", " ").strip()
        if not text or text.startswith("[") and text.endswith("]"):
            continue
        if start >= next_marker:
            minutes, seconds = divmod(int(start), 60)
            parts.append(f"\n[{minutes:02d}:{seconds:02d}] ")
            next_marker = start + MARKER_EVERY_SECONDS
        parts.append(text + " ")
    return "".join(parts).strip()


def _youtube_captions(video_id: str) -> tuple[str, str]:
    api = YouTubeTranscriptApi()
    try:
        fetched = api.fetch(video_id, languages=["en", "en-US", "en-GB"])
    except YouTubeTranscriptApiException:
        # No English track: take whatever exists and translate it to English if YouTube allows.
        transcript_list = api.list(video_id)
        transcript = next(iter(transcript_list))
        if transcript.is_translatable and not transcript.language_code.startswith("en"):
            transcript = transcript.translate("en")
        fetched = transcript.fetch()
    segments = [(s.start, s.text) for s in fetched.snippets]
    source = "auto-generated captions" if fetched.is_generated else "published captions"
    return _format(segments), source


def _whisper(url: str) -> tuple[str, str]:
    try:
        import yt_dlp  # type: ignore
        from faster_whisper import WhisperModel  # type: ignore
    except ImportError as exc:
        raise TranscriptError("Whisper fallback is enabled but faster-whisper/yt-dlp are not installed") from exc

    with tempfile.TemporaryDirectory() as tmp:
        opts = {
            "format": "bestaudio/best",
            "outtmpl": str(Path(tmp) / "audio.%(ext)s"),
            "quiet": True,
            "noplaylist": True,
            "max_filesize": 300 * 1024 * 1024,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
        audio = next(Path(tmp).glob("audio.*"), None)
        if audio is None:
            raise TranscriptError("Audio download produced no file")
        model = WhisperModel(settings.whisper_model, device="auto", compute_type="int8")
        segments, _info = model.transcribe(str(audio), vad_filter=True)
        return _format([(seg.start, seg.text) for seg in segments]), f"whisper-{settings.whisper_model}"


def get_transcript(url: str, max_chars: int = MAX_CHARS) -> dict:
    """Return {url, source, text, truncated} for a YouTube video or other media URL."""
    video_id = youtube_video_id(url)
    errors: list[str] = []

    if video_id:
        try:
            text, source = _youtube_captions(video_id)
            if text:
                return _result(url, source, text, max_chars)
            errors.append("captions were empty")
        except (YouTubeTranscriptApiException, StopIteration) as exc:
            errors.append(f"captions unavailable ({type(exc).__name__})")

    if settings.whisper_fallback:
        try:
            text, source = _whisper(url)
            return _result(url, source, text, max_chars)
        except Exception as exc:  # yt-dlp and whisper raise a wide range of errors
            log.warning("whisper fallback failed for %s: %s", url, exc)
            errors.append(f"whisper fallback failed ({type(exc).__name__})")
    elif not video_id:
        errors.append("not a YouTube URL and the Whisper fallback is disabled")

    raise TranscriptError("; ".join(errors) or "no transcript available")


def _result(url: str, source: str, text: str, max_chars: int) -> dict:
    return {"url": url, "source": source, "text": text[:max_chars], "truncated": len(text) > max_chars}
