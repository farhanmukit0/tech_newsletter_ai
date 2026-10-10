"""Web app: landing page, signup flow, subscriber preferences, issue archive, admin."""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager

from email_validator import EmailNotValidError, validate_email
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, func, select

from .categories import CATEGORIES, CATEGORY_KEYS
from .config import BASE_DIR, settings
from .db import Edition, Subscriber, init_db, new_token, session_scope, utcnow
from .pipeline import build_edition, deliver_edition, run_issue
from .render import pretty_date, render_confirmation, render_issue
from .tools.email import OutgoingEmail, send_email

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    scheduler = None
    if settings.scheduler_enabled:
        from .scheduler import start_scheduler

        scheduler = start_scheduler()
    yield
    if scheduler:
        scheduler.shutdown(wait=False)


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=BASE_DIR / "app" / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "app" / "templates")
templates.env.globals.update(name=settings.newsletter_name, tagline=settings.tagline, categories=CATEGORIES)

# Very small per-IP rate limit for the signup form.
_hits: dict[str, deque] = defaultdict(deque)


def _rate_limited(ip: str, limit: int = 5, window: int = 600) -> bool:
    now = time.time()
    q = _hits[ip]
    while q and now - q[0] > window:
        q.popleft()
    q.append(now)
    return len(q) > limit


def _page(request: Request, template: str, status: int = 200, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(request, template, ctx, status_code=status)


def _message(request: Request, title: str, body: str, status: int = 200) -> HTMLResponse:
    return _page(request, "message.html", status=status, title=title, body=body)


def _clean_categories(values: list[str]) -> list[str]:
    picked = [v for v in values if v in CATEGORIES]
    return picked or list(CATEGORY_KEYS)


def _latest_digest() -> tuple[dict | None, Edition | None]:
    with session_scope() as s:
        edition = s.scalar(
            select(Edition).where(Edition.digest_json.is_not(None)).order_by(desc(Edition.created_at)).limit(1)
        )
        return (edition.digest, edition) if edition else (None, None)


# --- Public pages -------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def landing(request: Request):
    digest, edition = _latest_digest()
    with session_scope() as s:
        readers = s.scalar(select(func.count()).select_from(Subscriber).where(Subscriber.confirmed.is_(True))) or 0
    return _page(
        request, "landing.html",
        digest=digest,
        edition=edition,
        edition_date=pretty_date(edition.issue_date) if edition else None,
        readers=readers,
    )


@app.post("/subscribe", response_class=HTMLResponse)
def subscribe(
    request: Request,
    email: str = Form(...),
    cadence: str = Form("daily"),
    categories: list[str] = Form(default=[]),
    website: str = Form(""),  # honeypot: humans never see or fill this field
):
    if website:
        return _message(request, "Check your inbox", "We sent you a link to confirm your subscription.")
    if _rate_limited(request.client.host if request.client else "unknown"):
        return _message(request, "Slow down a little", "Too many signups from your network. Try again in a few minutes.", 429)
    try:
        address = validate_email(email.strip(), check_deliverability=False).normalized.lower()
    except EmailNotValidError:
        return _message(request, "That address doesn't look right", "Go back and check the email address for typos.", 400)
    cadence = cadence if cadence in ("daily", "weekly") else "daily"
    picked = _clean_categories(categories)

    with session_scope() as s:
        sub = s.scalar(select(Subscriber).where(Subscriber.email == address))
        if sub is None:
            sub = Subscriber(email=address, cadence=cadence, categories=picked)
            s.add(sub)
        elif not sub.confirmed or not sub.active:
            # New or returning reader: (re)confirm before anything is sent.
            sub.cadence, sub.categories, sub.active, sub.confirmed = cadence, picked, True, False
        else:
            # Already subscribed and confirmed: don't let a stranger change their settings.
            return _message(
                request, "You're already on the list",
                "That address is already subscribed. Use the link at the bottom of any issue to change your settings.",
            )
        sub.token = sub.token or new_token()
        s.flush()
        token = sub.token

    subject, html, text = render_confirmation(f"{settings.base_url}/confirm/{token}", cadence)
    try:
        send_email(OutgoingEmail(to=address, subject=subject, html=html, text=text))
    except Exception:
        log.exception("confirmation email failed for %s", address)
        return _message(request, "Something went wrong", "We couldn't send the confirmation email. Please try again shortly.", 502)
    return _message(
        request, "Check your inbox",
        f"We sent a confirmation link to {address}. Click it and your first issue arrives with the next {cadence} edition.",
    )


@app.get("/confirm/{token}", response_class=HTMLResponse)
def confirm(request: Request, token: str):
    with session_scope() as s:
        sub = s.scalar(select(Subscriber).where(Subscriber.token == token))
        if sub is None:
            return _message(request, "Link not recognised", "This confirmation link is invalid or has expired.", 404)
        if not sub.confirmed:
            sub.confirmed, sub.confirmed_at = True, utcnow()
        sub.active = True
        cadence = sub.cadence
    return _message(
        request, "You're subscribed",
        f"Your first {cadence} issue is on its way with the next edition. You can change sections or frequency from the link at the bottom of every email.",
    )


@app.get("/preferences/{token}", response_class=HTMLResponse)
def preferences(request: Request, token: str, saved: bool = False):
    with session_scope() as s:
        sub = s.scalar(select(Subscriber).where(Subscriber.token == token))
        if sub is None:
            return _message(request, "Link not recognised", "This preferences link is invalid.", 404)
        return _page(request, "preferences.html", sub=sub, saved=saved)


@app.post("/preferences/{token}")
def save_preferences(token: str, cadence: str = Form("daily"), categories: list[str] = Form(default=[])):
    with session_scope() as s:
        sub = s.scalar(select(Subscriber).where(Subscriber.token == token))
        if sub is None:
            raise HTTPException(404)
        sub.cadence = cadence if cadence in ("daily", "weekly") else sub.cadence
        sub.categories = _clean_categories(categories)
        sub.active = True
    return RedirectResponse(f"/preferences/{token}?saved=1", status_code=303)


@app.get("/unsubscribe/{token}", response_class=HTMLResponse)
def unsubscribe_page(request: Request, token: str):
    with session_scope() as s:
        sub = s.scalar(select(Subscriber).where(Subscriber.token == token))
        if sub is None:
            return _message(request, "Link not recognised", "This unsubscribe link is invalid.", 404)
        return _page(request, "unsubscribe.html", sub=sub)


@app.post("/unsubscribe/{token}", response_class=HTMLResponse)
def unsubscribe(request: Request, token: str):
    # Also serves RFC 8058 one-click unsubscribe POSTs from mail clients.
    with session_scope() as s:
        sub = s.scalar(select(Subscriber).where(Subscriber.token == token))
        if sub is not None:
            sub.active = False
    return _message(
        request, "You're unsubscribed",
        "You won't get any more emails from us. Changed your mind? You can sign up again any time from the home page.",
    )


@app.get("/issues", response_class=HTMLResponse)
def archive(request: Request):
    with session_scope() as s:
        editions = list(s.scalars(
            select(Edition).where(Edition.status.in_(["ready", "sent"])).order_by(desc(Edition.issue_date), Edition.cadence).limit(120)
        ))
    items = [
        {"cadence": e.cadence, "date": e.issue_date, "pretty": pretty_date(e.issue_date), "number": e.issue_number,
         "subject": (e.digest or {}).get("subject", "")}
        for e in editions
    ]
    return _page(request, "archive.html", items=items)


@app.get("/issues/{cadence}/{issue_date}", response_class=HTMLResponse)
def issue_web(cadence: str, issue_date: str):
    with session_scope() as s:
        edition = s.scalar(select(Edition).where(Edition.cadence == cadence, Edition.issue_date == issue_date))
        if edition is None or edition.digest_json is None:
            raise HTTPException(404, "Issue not found")
        _, html, _ = render_issue(edition.digest, cadence=cadence, issue_date=issue_date, issue_number=edition.issue_number)
    return HTMLResponse(html)


@app.get("/healthz")
def healthz():
    return {"ok": True}


# --- Admin --------------------------------------------------------------------------------


def _require_admin(token: str | None) -> None:
    if not settings.admin_token or token != settings.admin_token:
        raise HTTPException(403, "Admin token required")


_running: set[str] = set()


def _background(label: str, fn, *args, **kwargs) -> bool:
    if label in _running:
        return False

    def target():
        _running.add(label)
        try:
            fn(*args, **kwargs)
        except Exception:
            log.exception("admin job %s failed", label)
        finally:
            _running.discard(label)

    threading.Thread(target=target, daemon=True).start()
    return True


@app.get("/admin", response_class=HTMLResponse)
def admin(request: Request, token: str | None = None):
    _require_admin(token)
    with session_scope() as s:
        editions = list(s.scalars(select(Edition).order_by(desc(Edition.created_at)).limit(30)))
        counts = {
            "confirmed": s.scalar(select(func.count()).select_from(Subscriber).where(Subscriber.confirmed.is_(True), Subscriber.active.is_(True))) or 0,
            "daily": s.scalar(select(func.count()).select_from(Subscriber).where(Subscriber.confirmed.is_(True), Subscriber.active.is_(True), Subscriber.cadence == "daily")) or 0,
            "weekly": s.scalar(select(func.count()).select_from(Subscriber).where(Subscriber.confirmed.is_(True), Subscriber.active.is_(True), Subscriber.cadence == "weekly")) or 0,
            "pending": s.scalar(select(func.count()).select_from(Subscriber).where(Subscriber.confirmed.is_(False))) or 0,
        }
    return _page(request, "admin.html", editions=editions, counts=counts, token=token, running=sorted(_running))


@app.post("/admin/build")
def admin_build(token: str = Form(...), cadence: str = Form("daily"), send: str = Form("no")):
    _require_admin(token)
    if send == "yes":
        _background(f"run:{cadence}", run_issue, cadence, force=True)
    else:
        _background(f"build:{cadence}", build_edition, cadence, force=True)
    return RedirectResponse(f"/admin?token={token}", status_code=303)


@app.post("/admin/send/{edition_id}")
def admin_send(edition_id: int, token: str = Form(...), test_email: str = Form("")):
    _require_admin(token)
    if test_email.strip():
        _background(f"test:{edition_id}", _send_test, edition_id, test_email.strip().lower())
    else:
        _background(f"send:{edition_id}", deliver_edition, edition_id)
    return RedirectResponse(f"/admin?token={token}", status_code=303)


def _send_test(edition_id: int, address: str) -> None:
    with session_scope() as s:
        if s.scalar(select(Subscriber).where(Subscriber.email == address)) is None:
            s.add(Subscriber(email=address, confirmed=False, active=False))
    deliver_edition(edition_id, only_email=address)


@app.get("/admin/preview/{edition_id}", response_class=HTMLResponse)
def admin_preview(edition_id: int, token: str | None = None, fmt: str = "html"):
    _require_admin(token)
    with session_scope() as s:
        edition = s.get(Edition, edition_id)
        if edition is None or edition.digest_json is None:
            raise HTTPException(404)
        _, html, text = render_issue(edition.digest, cadence=edition.cadence, issue_date=edition.issue_date, issue_number=edition.issue_number)
    if fmt == "text":
        return Response(text, media_type="text/plain; charset=utf-8")
    return HTMLResponse(html)
