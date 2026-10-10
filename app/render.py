"""Turn a Digest into per-subscriber HTML and plain-text emails."""

from __future__ import annotations

import re
import textwrap
from datetime import date

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .categories import CATEGORIES, CATEGORY_KEYS
from .config import BASE_DIR, settings

env = Environment(
    loader=FileSystemLoader(BASE_DIR / "app" / "templates"),
    autoescape=select_autoescape(["html"]),
    trim_blocks=True,
    lstrip_blocks=True,
)

_DASHES = re.compile(r"\s*[—–]\s*")


def tidy(text: str) -> str:
    """Last-line defence for house style: no em/en dashes, no stray markdown emphasis."""
    text = _DASHES.sub(", ", text or "")
    text = re.sub(r"(\*\*|__)(.+?)\1", r"\2", text)
    return re.sub(r"\s+", " ", text).strip()


env.filters["tidy"] = tidy


def pretty_date(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d.strftime('%A')}, {d.strftime('%B')} {d.day}, {d.year}"


def _sections_for(digest: dict, categories: list[str] | None) -> list[dict]:
    wanted = set(categories or CATEGORY_KEYS)
    by_key = {s["category"]: s for s in digest.get("sections", []) if s.get("stories")}
    return [
        {"key": key, "label": CATEGORIES[key]["label"], "stories": by_key[key]["stories"]}
        for key in CATEGORY_KEYS
        if key in by_key and key in wanted
    ]


def render_issue(
    digest: dict,
    *,
    cadence: str,
    issue_date: str,
    issue_number: int,
    categories: list[str] | None = None,
    subscriber_token: str | None = None,
) -> tuple[str, str, str]:
    """Return (subject, html, text) for one recipient."""
    sections = _sections_for(digest, categories)
    links = {
        "preferences": f"{settings.base_url}/preferences/{subscriber_token}" if subscriber_token else settings.base_url,
        "unsubscribe": f"{settings.base_url}/unsubscribe/{subscriber_token}" if subscriber_token else settings.base_url,
        "web": f"{settings.base_url}/issues/{cadence}/{issue_date}",
        "home": settings.base_url,
    }
    ctx = {
        "d": digest,
        "sections": sections,
        "cadence": cadence,
        "issue_label": "Daily" if cadence == "daily" else "Weekly",
        "issue_number": issue_number,
        "pretty_date": pretty_date(issue_date),
        "name": settings.newsletter_name,
        "tagline": settings.tagline,
        "links": links,
        "category_label": lambda k: CATEGORIES.get(k, {}).get("label", k),
    }
    html = env.get_template("email/issue.html").render(**ctx)
    text = env.get_template("email/issue.txt").render(**ctx, wrap=lambda s: textwrap.fill(tidy(s), 72))
    text = re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"
    return tidy(digest["subject"]), html, text


def render_confirmation(confirm_url: str, cadence: str) -> tuple[str, str, str]:
    ctx = {"name": settings.newsletter_name, "confirm_url": confirm_url, "cadence": cadence, "home": settings.base_url}
    html = env.get_template("email/confirm.html").render(**ctx)
    text = (
        f"Confirm your subscription to {settings.newsletter_name}\n\n"
        f"Someone (hopefully you) signed up for the {cadence} edition with this address.\n"
        f"Confirm here: {confirm_url}\n\n"
        "If this wasn't you, ignore this email and nothing will happen.\n"
    )
    return f"Confirm your {settings.newsletter_name} subscription", html, text
