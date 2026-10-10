"""Runtime configuration, loaded from environment variables (and .env in development)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # Publication
    newsletter_name: str = os.getenv("NEWSLETTER_NAME", "The Kernel")
    tagline: str = os.getenv("NEWSLETTER_TAGLINE", "The tech news worth your time.")
    base_url: str = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")
    timezone: str = os.getenv("TIMEZONE", "America/New_York")

    # Claude
    anthropic_api_key: str | None = os.getenv("ANTHROPIC_API_KEY")
    # Needed only for keys that aren't scoped to a single workspace.
    anthropic_workspace_id: str | None = os.getenv("ANTHROPIC_WORKSPACE_ID") or None
    model: str = os.getenv("EDITOR_MODEL", "claude-opus-5-5")
    effort: str = os.getenv("EDITOR_EFFORT", "high")
    max_agent_turns: int = int(os.getenv("MAX_AGENT_TURNS", "60"))

    # Search
    brave_api_key: str | None = os.getenv("BRAVE_API_KEY")

    # Email
    email_backend: str = os.getenv("EMAIL_BACKEND", "console")  # "resend" or "console"
    resend_api_key: str | None = os.getenv("RESEND_API_KEY")
    email_from: str = os.getenv("EMAIL_FROM", "The Kernel <onboarding@resend.dev>")
    email_reply_to: str | None = os.getenv("EMAIL_REPLY_TO") or None
    outbox_dir: Path = BASE_DIR / "outbox"

    # Storage
    database_url: str = os.getenv("DATABASE_URL") or f"sqlite:///{BASE_DIR / 'data.db'}"

    # Scheduling
    scheduler_enabled: bool = _bool("SCHEDULER_ENABLED", True)
    daily_send_hour: int = int(os.getenv("DAILY_SEND_HOUR", "7"))
    daily_send_minute: int = int(os.getenv("DAILY_SEND_MINUTE", "0"))
    weekly_send_day: str = os.getenv("WEEKLY_SEND_DAY", "sun")
    weekly_send_hour: int = int(os.getenv("WEEKLY_SEND_HOUR", "8"))

    # Admin
    admin_token: str | None = os.getenv("ADMIN_TOKEN") or None

    # Video transcription fallback (local Whisper via faster-whisper + yt-dlp)
    whisper_fallback: bool = _bool("WHISPER_FALLBACK", False)
    whisper_model: str = os.getenv("WHISPER_MODEL", "base")

    user_agent: str = field(
        default=os.getenv(
            "HTTP_USER_AGENT",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/129.0 Safari/537.36",
        )
    )


settings = Settings()
