"""Hacker News discussion lookup, for gauging how practitioners are reacting to a story."""

from __future__ import annotations

import html
import re

import httpx


def _clean(text: str) -> str:
    text = re.sub(r"<p>", "\n", text or "")
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def hn_discussion(story_id: str, max_comments: int = 12) -> dict:
    """Return the story title, points, and the top-level comments (in HN's ranked order)."""
    resp = httpx.get(f"https://hn.algolia.com/api/v1/items/{story_id}", timeout=15.0)
    resp.raise_for_status()
    item = resp.json()
    comments = []
    for child in item.get("children", []):
        if not child.get("text") or child.get("author") is None:
            continue
        comments.append({"author": child["author"], "text": _clean(child["text"])[:1200]})
        if len(comments) >= max_comments:
            break
    return {
        "title": item.get("title"),
        "url": item.get("url"),
        "points": item.get("points"),
        "discussion_url": f"https://news.ycombinator.com/item?id={story_id}",
        "top_comments": comments,
    }
