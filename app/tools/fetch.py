"""Fetch a web page and extract its readable article text."""

from __future__ import annotations

import json

import httpx
import trafilatura

from ..config import settings

MAX_CHARS = 14_000


class FetchError(RuntimeError):
    pass


def read_article(url: str, max_chars: int = MAX_CHARS) -> dict:
    """Return {url, title, author, date, site, text, truncated} for an article URL."""
    try:
        resp = httpx.get(
            url,
            headers={"User-Agent": settings.user_agent, "Accept-Language": "en-US,en;q=0.9"},
            follow_redirects=True,
            timeout=httpx.Timeout(20.0, connect=8.0),
        )
    except httpx.HTTPError as exc:
        raise FetchError(f"Could not reach {url}: {type(exc).__name__}") from exc

    if resp.status_code >= 400:
        raise FetchError(f"{url} returned HTTP {resp.status_code} (possibly paywalled or blocking bots)")
    content_type = resp.headers.get("content-type", "")
    if "html" not in content_type and "xml" not in content_type and "text" not in content_type:
        raise FetchError(f"{url} is not a web page ({content_type or 'unknown type'})")

    extracted = trafilatura.extract(
        resp.text,
        url=str(resp.url),
        output_format="json",
        with_metadata=True,
        include_comments=False,
        include_tables=True,
        favor_precision=True,
    )
    if not extracted:
        raise FetchError(f"No readable article text found at {url} (page may need JavaScript or be paywalled)")

    data = json.loads(extracted)
    text = (data.get("text") or "").strip()
    if len(text) < 200:
        raise FetchError(f"Only a fragment of text was available at {url}; likely paywalled")

    truncated = len(text) > max_chars
    return {
        "url": str(resp.url),
        "title": data.get("title"),
        "author": data.get("author"),
        "date": data.get("date"),
        "site": data.get("sitename") or data.get("hostname"),
        "text": text[:max_chars],
        "truncated": truncated,
    }
