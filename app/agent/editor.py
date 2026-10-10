"""The editor agent: Claude reads the candidates, researches with tools, and submits a Digest.

A manual tool loop (rather than the SDK's beta tool runner) so we can enforce a turn budget,
verify every cited URL against what the agent actually saw, and bounce invalid submissions
back to the model with specific feedback.
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import anthropic
from pydantic import ValidationError

from ..config import settings
from ..sources.collect import Candidate, normalize_url
from ..tools.fetch import FetchError, read_article
from ..tools.hn import hn_discussion
from ..tools.transcripts import TranscriptError, get_transcript
from ..tools.web_search import SearchUnavailable, web_search
from .prompts import SYSTEM_PROMPT, build_briefing
from .schema import Digest, inline_schema

log = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class EditorError(RuntimeError):
    pass


TOOLS: list[dict] = [
    {
        "name": "read_article",
        "description": (
            "Fetch a web page and return its main article text with title, author, date and site. "
            "Use it on every story you plan to run, preferring primary sources and original reporting. "
            "Fails on paywalled or JavaScript-only pages; when it does, try another outlet's coverage."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string", "description": "Full URL from the candidates or a tool result."}},
            "required": ["url"],
            "additionalProperties": False,
        },
    },
    {
        "name": "web_search",
        "description": (
            "Search the web or recent news (Brave Search). Use it to verify surprising claims, find the primary "
            "source behind a story, get missing specifics, or check for newer developments. Returns titles, URLs, "
            "outlets, ages and snippets. Snippets are not enough to write from; read the best result."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "A specific query, e.g. 'Cloudflare Deno acquisition price'."},
                "kind": {"type": "string", "enum": ["news", "web"], "description": "'news' for recent coverage, 'web' for docs, papers, announcements."},
                "freshness": {"type": "string", "enum": ["day", "week", "month", "any"]},
            },
            "required": ["query", "kind", "freshness"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_video_transcript",
        "description": (
            "Get the transcript of a YouTube video (or other media URL when local transcription is enabled), "
            "with [mm:ss] markers every two minutes. Use it on videos whose titles suggest real substance."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
            "additionalProperties": False,
        },
    },
    {
        "name": "hn_discussion",
        "description": (
            "Get a Hacker News story's points and its top-ranked comments, to see how practitioners are reacting. "
            "Use the hn_id shown on a candidate."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"hn_id": {"type": "string"}},
            "required": ["hn_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "submit_digest",
        "description": (
            "Submit the finished issue. Call this exactly once when the issue is complete; the run ends when a "
            "submission is accepted. If it returns problems, fix them and call it again."
        ),
        "input_schema": inline_schema(Digest),
    },
]
for _tool in TOOLS:
    # Large tool inputs (the whole issue) stream as they are generated; we validate them ourselves.
    _tool["eager_input_streaming"] = True


@dataclass
class EditorResult:
    digest: Digest
    turns: int
    tool_calls: dict[str, int] = field(default_factory=dict)
    usage: dict[str, int] = field(default_factory=dict)
    dropped_urls: list[str] = field(default_factory=list)


class _Session:
    """Holds per-run state: which URLs the agent has seen, tool call counts."""

    def __init__(self, candidates: list[Candidate]):
        self.seen: set[str] = set()
        self.tool_calls: dict[str, int] = {}
        for c in candidates:
            self._see(c.url)
            if c.hn_id:
                self._see(f"https://news.ycombinator.com/item?id={c.hn_id}")

    def _see(self, url: str | None) -> None:
        if url:
            self.seen.add(normalize_url(url))

    def known(self, url: str) -> bool:
        return normalize_url(url) in self.seen

    # Each tool returns (content string, is_error)
    def run(self, name: str, args: dict) -> tuple[str, bool]:
        self.tool_calls[name] = self.tool_calls.get(name, 0) + 1
        try:
            if name == "read_article":
                data = read_article(_require(args, "url"))
                self._see(args["url"])
                self._see(data["url"])
                body = data.pop("text")
                return (
                    json.dumps(data, ensure_ascii=False)
                    + f"\n<article_content untrusted=\"true\">\n{body}\n</article_content>",
                    False,
                )
            if name == "web_search":
                results = web_search(_require(args, "query"), args.get("kind", "news"), args.get("freshness", "week"))
                for r in results:
                    self._see(r["url"])
                if not results:
                    return "No results. Try a different or broader query.", False
                return json.dumps(results, ensure_ascii=False), False
            if name == "get_video_transcript":
                data = get_transcript(_require(args, "url"))
                self._see(args["url"])
                body = data.pop("text")
                return (
                    json.dumps(data, ensure_ascii=False)
                    + f"\n<transcript untrusted=\"true\">\n{body}\n</transcript>",
                    False,
                )
            if name == "hn_discussion":
                data = hn_discussion(_require(args, "hn_id"))
                self._see(data.get("url"))
                self._see(data["discussion_url"])
                return json.dumps(data, ensure_ascii=False), False
            return f"Unknown tool {name!r}.", True
        except (FetchError, TranscriptError, SearchUnavailable, ValueError) as exc:
            return str(exc), True
        except Exception as exc:  # network flakiness etc. should not end the run
            log.warning("tool %s failed: %s", name, exc)
            return f"{name} failed: {type(exc).__name__}: {str(exc)[:200]}", True


def _require(args: dict, key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Missing required argument '{key}'.")
    return value.strip()


def _check_digest(raw: dict, session: _Session, attempt: int) -> tuple[Digest | None, str | None, list[str]]:
    """Validate a submission. Returns (digest, problems_for_model, dropped_urls)."""
    try:
        digest = Digest.model_validate(raw)
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:15])
        return None, f"The submission did not match the schema: {problems}", []

    unknown = sorted({u for u in digest.all_urls() if not session.known(u)})
    if unknown and attempt < 2:
        listing = "\n".join(f"- {u}" for u in unknown)
        return None, (
            "These URLs were not in the candidate list or any tool result, so they can't be verified:\n"
            f"{listing}\nReplace each with a URL you actually saw (read or search for one), or remove that source "
            "or item. Then call submit_digest again with the full issue."
        ), []

    if unknown:  # out of retries: strip unverifiable links rather than publish them
        digest = _strip_unknown(digest, session)
    return digest, None, unknown


def _strip_unknown(digest: Digest, session: _Session) -> Digest:
    digest.lead.sources = [s for s in digest.lead.sources if session.known(s.url)]
    for section in digest.sections:
        for story in section.stories:
            story.sources = [s for s in story.sources if session.known(s.url)]
        section.stories = [s for s in section.stories if s.sources]
    digest.sections = [s for s in digest.sections if s.stories]
    digest.quick_hits = [q for q in digest.quick_hits if session.known(q.url)]
    digest.watch = [w for w in digest.watch if session.known(w.url)]
    if not digest.lead.sources:
        raise EditorError("Lead story had no verifiable sources")
    return digest


def anthropic_client() -> anthropic.Anthropic:
    headers = {"anthropic-workspace-id": settings.anthropic_workspace_id} if settings.anthropic_workspace_id else None
    return anthropic.Anthropic(api_key=settings.anthropic_api_key, max_retries=4, default_headers=headers)


def run_editor(
    cadence: str,
    issue_date: str,
    candidates: list[Candidate],
    recent_headlines: list[str],
) -> EditorResult:
    if not settings.anthropic_api_key:
        raise EditorError("ANTHROPIC_API_KEY is not set")
    if not candidates:
        raise EditorError("No candidates were collected; nothing to edit")

    client = anthropic_client()
    session = _Session(candidates)
    max_turns = settings.max_agent_turns
    briefing = build_briefing(
        cadence, issue_date, candidates, recent_headlines, max_turns, search_enabled=bool(settings.brave_api_key)
    )
    messages: list[dict] = [{"role": "user", "content": briefing}]
    usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    submit_attempts = 0
    nudges = 0

    for turn in range(1, max_turns + 1):
        with client.beta.messages.stream(
            model=settings.model,
            max_tokens=64000,
            system=[{"type": "text", "text": SYSTEM_PROMPT}],
            tools=TOOLS,
            messages=messages,
            thinking={"type": "adaptive"},
            output_config={"effort": settings.effort},
            cache_control={"type": "ephemeral"},
            betas=[FALLBACK_BETA],
            fallbacks="default",
        ) as stream:
            response = stream.get_final_message()

        for key in usage:
            usage[key] += getattr(response.usage, key, 0) or 0
        log.info("editor turn %d: stop=%s out=%d", turn, response.stop_reason, response.usage.output_tokens)

        if response.stop_reason == "refusal":
            details = response.stop_details
            raise EditorError(f"Model declined the request ({getattr(details, 'category', None)}): {getattr(details, 'explanation', '')}")
        if response.stop_reason == "max_tokens":
            raise EditorError("Model hit max_tokens mid-response; raise max_tokens or lower effort")

        messages.append({"role": "assistant", "content": response.content})
        tool_uses = [b for b in response.content if b.type == "tool_use"]

        if not tool_uses:
            nudges += 1
            if nudges > 2:
                raise EditorError("Model stopped without submitting a digest")
            messages.append({"role": "user", "content": "You stopped without calling submit_digest. Finish the issue and submit it now."})
            continue

        results: list[dict] = []
        accepted: Digest | None = None
        dropped: list[str] = []
        other_calls = [b for b in tool_uses if b.name != "submit_digest"]

        with ThreadPoolExecutor(max_workers=8) as pool:
            outputs = list(pool.map(lambda b: session.run(b.name, b.input if isinstance(b.input, dict) else {}), other_calls))
        by_id = {b.id: out for b, out in zip(other_calls, outputs)}

        for block in tool_uses:
            if block.name == "submit_digest":
                submit_attempts += 1
                session.tool_calls["submit_digest"] = session.tool_calls.get("submit_digest", 0) + 1
                raw = block.input if isinstance(block.input, dict) else {}
                digest, problems, dropped = _check_digest(raw, session, submit_attempts - 1)
                if digest is not None:
                    accepted = digest
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": "Accepted."})
                else:
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": problems, "is_error": True})
            else:
                content, is_error = by_id[block.id]
                result = {"type": "tool_result", "tool_use_id": block.id, "content": content}
                if is_error:
                    result["is_error"] = True
                results.append(result)

        if accepted is not None:
            return EditorResult(accepted, turn, session.tool_calls, usage, dropped)

        remaining = max_turns - turn
        if remaining <= 6:
            results.append({
                "type": "text",
                "text": f"{remaining} turns left. Stop researching and call submit_digest with the best issue you can make from what you have.",
            })
        messages.append({"role": "user", "content": results})

    raise EditorError(f"Turn budget of {max_turns} exhausted without an accepted digest")
