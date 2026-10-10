"""One issue, end to end: collect -> edit -> store -> deliver."""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from .agent.editor import run_editor
from .config import settings
from .db import CoveredStory, Delivery, Edition, Subscriber, session_scope, utcnow
from .render import render_issue
from .sources.collect import collect_candidates
from .tools.email import OutgoingEmail, send_batch

log = logging.getLogger(__name__)

WINDOW_HOURS = {"daily": 30, "weekly": 7 * 24 + 6}
LOOKBACK_DAYS = {"daily": 4, "weekly": 21}


def today_local() -> str:
    return datetime.now(ZoneInfo(settings.timezone)).date().isoformat()


def _claim_edition(cadence: str, issue_date: str, force: bool) -> int | None:
    """Create the edition row (a unique key per cadence+date stops double runs). Returns its id."""
    with session_scope() as s:
        existing = s.scalar(select(Edition).where(Edition.cadence == cadence, Edition.issue_date == issue_date))
        if existing:
            created = existing.created_at if existing.created_at.tzinfo else existing.created_at.replace(tzinfo=timezone.utc)
            stale_draft = existing.status == "drafting" and utcnow() - created > timedelta(hours=2)
            if existing.status in ("sent", "ready", "drafting") and not (force or stale_draft):
                log.info("%s edition for %s already %s; skipping", cadence, issue_date, existing.status)
                return None
            existing.status, existing.error = "drafting", None
            return existing.id
        number = (s.scalar(select(func.count()).select_from(Edition).where(Edition.cadence == cadence, Edition.status == "sent")) or 0) + 1
        edition = Edition(cadence=cadence, issue_date=issue_date, issue_number=number, status="drafting")
        s.add(edition)
        try:
            s.flush()
        except IntegrityError:
            return None
        return edition.id


def _recent_coverage(cadence: str) -> tuple[set[str], list[str]]:
    since = utcnow() - timedelta(days=LOOKBACK_DAYS[cadence])
    with session_scope() as s:
        rows = s.execute(select(CoveredStory.url, CoveredStory.headline).where(CoveredStory.covered_at >= since)).all()
    urls = {r.url for r in rows}
    headlines = sorted({r.headline for r in rows})
    return urls, headlines


def build_edition(cadence: str, issue_date: str | None = None, force: bool = False) -> int | None:
    """Collect and edit an issue. Returns the edition id, or None if one already exists."""
    issue_date = issue_date or today_local()
    edition_id = _claim_edition(cadence, issue_date, force)
    if edition_id is None:
        return None

    started = time.monotonic()
    with session_scope() as s:
        # A rebuild replaces this edition, so its own stories must not count as already covered.
        s.execute(delete(CoveredStory).where(CoveredStory.edition_id == edition_id))
    try:
        covered_urls, recent_headlines = _recent_coverage(cadence)
        candidates, collect_stats = collect_candidates(WINDOW_HOURS[cadence], exclude_urls=covered_urls)
        log.info("collected %d candidates (%s)", len(candidates), collect_stats)
        result = run_editor(cadence, issue_date, candidates, recent_headlines)
    except Exception as exc:
        log.exception("edition %s failed", edition_id)
        with session_scope() as s:
            edition = s.get(Edition, edition_id)
            edition.status, edition.error = "failed", f"{type(exc).__name__}: {exc}"[:2000]
        raise

    digest = result.digest
    with session_scope() as s:
        edition = s.get(Edition, edition_id)
        edition.digest_json = digest.model_dump_json()
        edition.status = "ready"
        edition.stats = {
            "collect": collect_stats,
            "editor_turns": result.turns,
            "tool_calls": result.tool_calls,
            "usage": result.usage,
            "dropped_unverified_urls": result.dropped_urls,
            "seconds": round(time.monotonic() - started),
        }
        for url, headline in digest.headlines():
            s.add(CoveredStory(url=url, headline=headline[:500], edition_id=edition_id))
    log.info("edition %s ready in %ss", edition_id, round(time.monotonic() - started))
    return edition_id


def deliver_edition(edition_id: int, only_email: str | None = None) -> int:
    """Send a ready edition to every confirmed subscriber on its cadence. Returns emails sent."""
    with session_scope() as s:
        edition = s.get(Edition, edition_id)
        if edition is None or edition.digest_json is None:
            raise ValueError(f"edition {edition_id} has no digest")
        digest = json.loads(edition.digest_json)
        query = select(Subscriber).where(Subscriber.confirmed.is_(True), Subscriber.active.is_(True), Subscriber.cadence == edition.cadence)
        if only_email:
            query = select(Subscriber).where(Subscriber.email == only_email)
        already = set(s.scalars(select(Delivery.subscriber_id).where(Delivery.edition_id == edition_id)))
        recipients = [sub for sub in s.scalars(query) if only_email or sub.id not in already]
        cadence, issue_date, number = edition.cadence, edition.issue_date, edition.issue_number

    emails, recipient_ids = [], []
    for sub in recipients:
        subject, html, text = render_issue(
            digest, cadence=cadence, issue_date=issue_date, issue_number=number,
            categories=sub.categories, subscriber_token=sub.token,
        )
        unsubscribe = f"{settings.base_url}/unsubscribe/{sub.token}"
        emails.append(OutgoingEmail(
            to=sub.email, subject=subject, html=html, text=text,
            headers={"List-Unsubscribe": f"<{unsubscribe}>", "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"},
        ))
        recipient_ids.append(sub.id)

    sent = 0
    for i in range(0, len(emails), 100):
        ids = send_batch(emails[i : i + 100])
        if only_email:
            sent += len(ids)
            continue
        with session_scope() as s:
            for sub_id, provider_id in zip(recipient_ids[i : i + 100], ids):
                s.add(Delivery(edition_id=edition_id, subscriber_id=sub_id, provider_id=provider_id))
        sent += len(ids)

    if not only_email:
        with session_scope() as s:
            edition = s.get(Edition, edition_id)
            edition.status, edition.sent_at = "sent", datetime.now(timezone.utc)
    log.info("edition %s delivered to %d recipients", edition_id, sent)
    return sent


def run_issue(cadence: str, *, force: bool = False, send: bool = True) -> int | None:
    issue_date = today_local()
    edition_id = build_edition(cadence, issue_date, force=force)
    if edition_id is None:
        # Already built today. If a previous run built it but died before sending, send it now.
        with session_scope() as s:
            existing = s.scalar(select(Edition).where(Edition.cadence == cadence, Edition.issue_date == issue_date))
            edition_id = existing.id if existing and existing.status == "ready" else None
    if edition_id is not None and send:
        deliver_edition(edition_id)
    return edition_id
