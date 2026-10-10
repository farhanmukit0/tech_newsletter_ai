"""Collect candidate stories from feeds, Hacker News, Reddit and YouTube.

This step is deliberately deterministic and cheap: it gathers a wide net of recent items,
merges duplicates of the same story across outlets, and ranks them by signal. The editor
agent then decides what is actually worth running.
"""

from __future__ import annotations

import calendar
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import feedparser
import httpx

from ..config import settings
from .registry import HN_MIN_POINTS, RSS_FEEDS, SUBREDDITS, YOUTUBE_CHANNELS

log = logging.getLogger(__name__)

TRACKING_PARAMS = {"ref", "ref_src", "source", "fbclid", "gclid", "mc_cid", "mc_eid", "cmpid", "smid"}

# Titles that are almost never news: deals posts, listicles, horoscope-grade filler.
JUNK_PATTERNS = re.compile(
    r"\b(deal|deals|% off|discount|coupon|best .* (to buy|of 20\d\d)|gift guide|"
    r"how to watch|wordle|connections hint|horoscope|sponsored|giveaway|"
    r"black friday|cyber monday|prime day|save \$\d+)\b",
    re.IGNORECASE,
)


@dataclass
class Candidate:
    title: str
    url: str
    source: str
    published: datetime | None
    kind: str = "article"  # article | video | discussion
    summary: str = ""
    score: float = 0.0  # engagement signal (HN points, Reddit upvotes)
    comments: int = 0
    weight: float = 1.0
    hn_id: str | None = None
    also_covered_by: list[str] = field(default_factory=list)
    id: str = ""  # assigned after ranking: c1, c2, ...

    @property
    def rank_score(self) -> float:
        coverage = 1 + 0.6 * len(set(self.also_covered_by))
        engagement = 1 + min(self.score, 2000) / 400
        return self.weight * coverage * engagement


def normalize_url(url: str) -> str:
    parsed = urlparse(url.strip())
    query = [(k, v) for k, v in parse_qsl(parsed.query) if not k.lower().startswith("utm_") and k.lower() not in TRACKING_PARAMS]
    host = parsed.netloc.lower().removeprefix("www.")
    path = parsed.path.rstrip("/") or "/"
    return urlunparse(("https", host, path, "", urlencode(query), ""))


def _title_tokens(title: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", title.lower())
    stop = {"the", "a", "an", "to", "of", "and", "in", "on", "for", "with", "is", "its", "it", "at", "as", "by", "from", "new", "now", "after", "says"}
    return {w for w in words if w not in stop and len(w) > 1}


def _similar(a: str, b: str) -> bool:
    ta, tb = _title_tokens(a), _title_tokens(b)
    if not ta or not tb:
        return False
    overlap = len(ta & tb) / min(len(ta), len(tb))
    return overlap >= 0.7 and len(ta & tb) >= 4


def _struct_time_to_dt(st) -> datetime | None:
    if not st:
        return None
    return datetime.fromtimestamp(calendar.timegm(st), tz=timezone.utc)


def _strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", text).strip()


def _client() -> httpx.Client:
    return httpx.Client(
        timeout=httpx.Timeout(15.0, connect=8.0),
        follow_redirects=True,
        headers={"User-Agent": settings.user_agent},
    )


# --- Individual collectors ---------------------------------------------------------------


def fetch_rss(name: str, url: str, weight: float, since: datetime) -> list[Candidate]:
    with _client() as client:
        resp = client.get(url)
        resp.raise_for_status()
    feed = feedparser.parse(resp.content)
    items: list[Candidate] = []
    for entry in feed.entries:
        published = _struct_time_to_dt(entry.get("published_parsed") or entry.get("updated_parsed"))
        if published and published < since:
            continue
        link = entry.get("link")
        title = _strip_html(entry.get("title", ""))
        if not link or not title or JUNK_PATTERNS.search(title):
            continue
        items.append(
            Candidate(
                title=title,
                url=link,
                source=name,
                published=published,
                summary=_strip_html(entry.get("summary", ""))[:400],
                weight=weight,
            )
        )
    return items


def fetch_hacker_news(since: datetime) -> list[Candidate]:
    params = {
        "tags": "story",
        "numericFilters": f"created_at_i>{int(since.timestamp())},points>{HN_MIN_POINTS}",
        "hitsPerPage": 100,
    }
    with _client() as client:
        resp = client.get("https://hn.algolia.com/api/v1/search", params=params)
        resp.raise_for_status()
    items = []
    for hit in resp.json().get("hits", []):
        title = hit.get("title") or ""
        hn_url = f"https://news.ycombinator.com/item?id={hit['objectID']}"
        url = hit.get("url") or hn_url
        if not title or title.lower().startswith(("ask hn", "tell hn", "who is hiring")):
            continue
        items.append(
            Candidate(
                title=title,
                url=url,
                source="Hacker News",
                published=datetime.fromtimestamp(hit["created_at_i"], tz=timezone.utc),
                kind="article" if hit.get("url") else "discussion",
                score=float(hit.get("points") or 0),
                comments=int(hit.get("num_comments") or 0),
                weight=1.0,
                hn_id=str(hit["objectID"]),
            )
        )
    return items


def fetch_reddit(subreddit: str, since: datetime, window: str) -> list[Candidate]:
    # The JSON API rejects anonymous clients; the RSS view of "top" still works.
    url = f"https://www.reddit.com/r/{subreddit}/top/.rss"
    with _client() as client:
        resp = client.get(url, params={"t": window, "limit": 25})
        resp.raise_for_status()
    feed = feedparser.parse(resp.content)
    items = []
    for position, entry in enumerate(feed.entries):
        published = _struct_time_to_dt(entry.get("published_parsed") or entry.get("updated_parsed"))
        if published and published < since:
            continue
        permalink = entry.get("link", "")
        content = entry.get("content", [{}])[0].get("value", "") if entry.get("content") else ""
        external = re.search(r'<a href="([^"]+)">\[link\]</a>', content)
        link = external.group(1) if external else permalink
        is_self = "reddit.com" in link or "redd.it" in link
        if is_self and ("i.redd.it" in link or "v.redd.it" in link):
            continue  # image and video posts rarely carry news
        items.append(
            Candidate(
                title=_strip_html(entry.get("title", "")),
                url=permalink if is_self else link,
                source=f"r/{subreddit}",
                published=published,
                kind="discussion" if is_self else "article",
                summary=_strip_html(content)[:300] if is_self else "",
                # RSS carries no vote counts; position in the "top" listing is the signal.
                score=max(0.0, 250 - position * 10),
                weight=0.7,
            )
        )
    return items


def fetch_all_reddit(since: datetime, window: str) -> list[Candidate]:
    """Reddit rate-limits anonymous clients hard, so go one subreddit at a time."""
    items: list[Candidate] = []
    for sub in SUBREDDITS:
        for attempt in range(2):
            try:
                items.extend(fetch_reddit(sub, since, window))
                break
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 429 or attempt:
                    log.warning("reddit r/%s failed: %s", sub, exc.response.status_code)
                    break
                time.sleep(6)
        time.sleep(2)
    return items


def fetch_youtube(name: str, channel_id: str, since: datetime) -> list[Candidate]:
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    with _client() as client:
        resp = client.get(url)
        resp.raise_for_status()
    feed = feedparser.parse(resp.content)
    items = []
    for entry in feed.entries:
        published = _struct_time_to_dt(entry.get("published_parsed"))
        if published and published < since:
            continue
        title = entry.get("title", "")
        if "#shorts" in title.lower() or "/shorts/" in entry.get("link", ""):
            continue
        items.append(
            Candidate(
                title=title,
                url=entry.get("link", ""),
                source=f"YouTube: {name}",
                published=published,
                kind="video",
                summary=_strip_html(entry.get("summary", ""))[:300],
                weight=0.6,
            )
        )
    return items


# --- Orchestration -----------------------------------------------------------------------


def merge_duplicates(items: list[Candidate]) -> list[Candidate]:
    """Fold the same story from several outlets into one candidate, keeping the best version."""
    by_url: dict[str, Candidate] = {}
    for item in items:
        key = normalize_url(item.url)
        existing = by_url.get(key)
        if existing is None:
            by_url[key] = item
            continue
        # Same link seen twice (e.g. an outlet's RSS plus its HN submission): keep outlet, add signal.
        keep, other = (existing, item) if existing.source != "Hacker News" else (item, existing)
        keep.score = max(keep.score, other.score)
        keep.comments = max(keep.comments, other.comments)
        keep.hn_id = keep.hn_id or other.hn_id
        keep.also_covered_by.append(other.source)
        by_url[key] = keep

    merged: list[Candidate] = []
    for item in sorted(by_url.values(), key=lambda c: c.weight, reverse=True):
        twin = next((m for m in merged if m.kind == item.kind and _similar(m.title, item.title)), None)
        if twin:
            twin.also_covered_by.append(item.source)
            twin.also_covered_by.extend(item.also_covered_by)
            twin.score = max(twin.score, item.score)
            twin.hn_id = twin.hn_id or item.hn_id
        else:
            merged.append(item)
    return merged


def collect_candidates(
    window_hours: int,
    exclude_urls: set[str] | None = None,
    limit: int = 180,
) -> tuple[list[Candidate], dict]:
    since = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    reddit_window = "day" if window_hours <= 36 else "week"

    jobs = []
    for name, url, weight in RSS_FEEDS:
        jobs.append((f"rss:{name}", fetch_rss, (name, url, weight, since)))
    jobs.append(("hn", fetch_hacker_news, (since,)))
    jobs.append(("reddit", fetch_all_reddit, (since, reddit_window)))
    for name, channel_id in YOUTUBE_CHANNELS:
        jobs.append((f"youtube:{name}", fetch_youtube, (name, channel_id, since)))

    raw: list[Candidate] = []
    failures: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = {pool.submit(fn, *args): label for label, fn, args in jobs}
        for future, label in futures.items():
            try:
                raw.extend(future.result())
            except Exception as exc:  # one dead feed should never sink an issue
                failures[label] = f"{type(exc).__name__}: {exc}"[:200]
                log.warning("source %s failed: %s", label, exc)

    exclude = {normalize_url(u) for u in (exclude_urls or set())}
    fresh = [c for c in raw if normalize_url(c.url) not in exclude]
    merged = merge_duplicates(fresh)

    # Keep a healthy share of videos even though they rank lower on raw signal.
    articles = sorted((c for c in merged if c.kind != "video"), key=lambda c: c.rank_score, reverse=True)
    videos = sorted((c for c in merged if c.kind == "video"), key=lambda c: c.published or since, reverse=True)
    ranked = articles[: limit - min(len(videos), 15)] + videos[:15]
    for i, cand in enumerate(ranked, start=1):
        cand.id = f"c{i}"

    stats = {
        "sources_attempted": len(jobs),
        "sources_failed": failures,
        "raw_items": len(raw),
        "after_exclusions": len(fresh),
        "after_merge": len(merged),
        "sent_to_editor": len(ranked),
    }
    return ranked, stats
