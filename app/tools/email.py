"""Email delivery through Resend, with a console/outbox backend for local development."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime

import httpx

from ..config import settings

log = logging.getLogger(__name__)

RESEND_URL = "https://api.resend.com"


@dataclass
class OutgoingEmail:
    to: str
    subject: str
    html: str
    text: str
    headers: dict[str, str] = field(default_factory=dict)
    tags: list[dict[str, str]] = field(default_factory=list)

    def to_resend(self) -> dict:
        payload = {
            "from": settings.email_from,
            "to": [self.to],
            "subject": self.subject,
            "html": self.html,
            "text": self.text,
        }
        if settings.email_reply_to:
            payload["reply_to"] = settings.email_reply_to
        if self.headers:
            payload["headers"] = self.headers
        if self.tags:
            payload["tags"] = self.tags
        return payload


class EmailError(RuntimeError):
    pass


def _post(path: str, payload) -> dict:
    if not settings.resend_api_key:
        raise EmailError("EMAIL_BACKEND is 'resend' but RESEND_API_KEY is unset")
    for attempt in range(4):
        resp = httpx.post(
            f"{RESEND_URL}{path}",
            json=payload,
            headers={"Authorization": f"Bearer {settings.resend_api_key}"},
            timeout=30.0,
        )
        if resp.status_code == 429 or resp.status_code >= 500:
            time.sleep(1.5 * (attempt + 1))
            continue
        if resp.status_code >= 400:
            raise EmailError(f"Resend rejected the request ({resp.status_code}): {resp.text[:300]}")
        return resp.json()
    raise EmailError(f"Resend kept failing after retries ({resp.status_code})")


def _write_outbox(email: OutgoingEmail) -> str:
    settings.outbox_dir.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    slug = re.sub(r"[^a-z0-9]+", "-", email.to.lower()).strip("-")[:40]
    path = settings.outbox_dir / f"{stamp}-{slug}.html"
    path.write_text(email.html, encoding="utf-8")
    log.info("[console email] to=%s subject=%r -> %s", email.to, email.subject, path)
    return f"outbox:{path.name}"


def send_email(email: OutgoingEmail) -> str:
    """Send one email; returns the provider message id."""
    if settings.email_backend != "resend":
        return _write_outbox(email)
    return _post("/emails", email.to_resend()).get("id", "")


def send_batch(emails: list[OutgoingEmail]) -> list[str]:
    """Send many emails (Resend batch API, 100 per request). Returns ids in input order."""
    if settings.email_backend != "resend":
        return [_write_outbox(e) for e in emails]
    ids: list[str] = []
    for i in range(0, len(emails), 100):
        chunk = emails[i : i + 100]
        # The batch endpoint does not accept tags; headers (List-Unsubscribe) are supported.
        payload = [{k: v for k, v in e.to_resend().items() if k != "tags"} for e in chunk]
        result = _post("/emails/batch", payload)
        ids.extend(item.get("id", "") for item in result.get("data", []))
        time.sleep(0.6)  # stay under Resend's default 2 requests/second
    return ids
