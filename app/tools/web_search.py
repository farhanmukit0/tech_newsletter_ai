"""Web and news search via the Brave Search API."""

from __future__ import annotations

import httpx

from ..config import settings

BRAVE_BASE = "https://api.search.brave.com/res/v1"
FRESHNESS = {"day": "pd", "week": "pw", "month": "pm", "any": None}


class SearchUnavailable(RuntimeError):
    pass


def web_search(query: str, kind: str = "news", freshness: str = "week", count: int = 8) -> list[dict]:
    """Return a list of {title, url, source, age, description} results."""
    if not settings.brave_api_key:
        raise SearchUnavailable("Web search is not configured (BRAVE_API_KEY is unset).")

    endpoint = "news/search" if kind == "news" else "web/search"
    params: dict[str, str | int] = {"q": query, "count": max(1, min(count, 20))}
    if FRESHNESS.get(freshness):
        params["freshness"] = FRESHNESS[freshness]

    resp = httpx.get(
        f"{BRAVE_BASE}/{endpoint}",
        params=params,
        headers={"Accept": "application/json", "X-Subscription-Token": settings.brave_api_key},
        timeout=15.0,
    )
    resp.raise_for_status()
    data = resp.json()
    raw = data.get("results", []) if kind == "news" else data.get("web", {}).get("results", [])

    results = []
    for r in raw:
        results.append(
            {
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "source": (r.get("meta_url") or {}).get("hostname") or r.get("profile", {}).get("name", ""),
                "age": r.get("age") or r.get("page_age") or "",
                "description": _clean(r.get("description", "")),
            }
        )
    return results


def _clean(text: str) -> str:
    return text.replace("<strong>", "").replace("</strong>", "").strip()
