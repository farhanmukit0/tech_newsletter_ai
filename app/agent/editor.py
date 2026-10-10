"""The editor agent: Claude reads the candidates, researches with tools, and submits a Digest.

A manual tool loop (rather than the SDK's beta tool runner) so we can enforce a turn and
context budget, condense sources before they enter the conversation, verify every citation,
and bounce invalid submissions back to the model with specific feedback.

Context engineering, in short:
- Items are referred to by short ids (c12, s3) instead of URLs, in the briefing, in tool
  results and in citations. Code maps ids back to URLs, so the model never writes a link.
- Articles, transcripts and comment threads are condensed by a separate small call before
  they reach the editor, so each source adds ~200 tokens to the conversation instead of
  1,000-12,000. The editor can still ask for the full text when it needs it.
- A context budget stops research before any single request crosses the size where it
  gets expensive (100K tokens on Claude Haiku 5.5).
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from urllib.parse import urlparse

import anthropic
from pydantic import ValidationError

from ..config import settings
from ..sources.collect import Candidate, normalize_url
from ..tools.fetch import FetchError, read_article
from ..tools.hn import hn_discussion
from ..tools.transcripts import TranscriptError, get_transcript
from ..tools.web_search import SearchUnavailable, web_search
from .condense import condense
from .prompts import SYSTEM_PROMPT, build_briefing
from .schema import Digest, strict_schema
from .usage import UsageTracker, prompt_tokens

log = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Models that accept the server-side refusal fallback (Claude Haiku 5.5 does not).
FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
# Models that accept mid-conversation {"role": "system"} messages.
SYSTEM_MESSAGE_MODELS = {
    "claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5", "claude-opus-5",
    "claude-opus-4-8", "claude-fable-5", "claude-fable-5-1",
}

FULL_TEXT_CHARS = 8_000     # cap when the editor explicitly asks for full text
CONDENSE_INPUT_CHARS = 24_000  # how much of a source the condenser gets to read
UNCONDENSED_CHARS = 5_000   # cap when condensing is switched off
HN_FOCUS = (
    "This is a Hacker News comment thread. Summarize the main reactions: points of agreement, "
    "the strongest criticism, and any factual corrections or first-hand accounts. No usernames."
)


class EditorError(RuntimeError):
    pass


TOOLS: list[dict] = [
    {
        "name": "read_article",
        "description": (
            "Read a candidate (c12) or search result (s3) and get condensed notes: key facts, numbers, names, "
            "dates, what is confirmed versus claimed, and at most one quote. Use it on every story you plan to run. "
            "Pass `focus` to say what you need. Set `full` to true only when the notes aren't enough "
            "(usually just the lead story); full text is much more expensive. Fails on paywalled or "
            "JavaScript-only pages; when it does, read another outlet's coverage."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string", "description": "Candidate or search result id, e.g. 'c12' or 's3'."},
                "focus": {"type": "string", "description": "Optional. What you need from this source."},
                "full": {"type": "boolean", "description": "Optional. Return the article text instead of notes."},
            },
            "required": ["ref"],
            "additionalProperties": False,
        },
    },
    {
        "name": "web_search",
        "description": (
            "Search the web or recent news (Brave Search). Use it to verify surprising claims, find the primary "
            "source behind a story, get missing specifics, or check for newer developments. Returns results with "
            "ids (s1, s2, ...), outlets, ages and snippets. Snippets are not enough to write from; read the best one."
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
            "Get condensed notes from a video candidate's transcript (what it covers, claims made, notable "
            "segments with [mm:ss] times). Use it on videos whose titles suggest real substance."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string", "description": "Candidate id of the video, e.g. 'c140'."},
                "focus": {"type": "string", "description": "Optional. What you need from the video."},
            },
            "required": ["ref"],
            "additionalProperties": False,
        },
    },
    {
        "name": "hn_discussion",
        "description": (
            "Summarize how Hacker News commenters are reacting to a candidate that has an HN thread "
            "(shown with 'pts' in the candidate list): agreement, criticism, corrections."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"ref": {"type": "string", "description": "Candidate id, e.g. 'c3'."}},
            "required": ["ref"],
            "additionalProperties": False,
        },
    },
    {
        "name": "submit_digest",
        "description": (
            "Submit the finished issue. Call this exactly once when the issue is complete; the run ends when a "
            "submission is accepted. Cite sources by id. If it returns problems, fix them and call it again."
        ),
        # Strict tool use constrains generation to the schema, so the issue always arrives as valid
        # JSON. (Smaller models occasionally emit malformed JSON in a payload this size otherwise.)
        "strict": True,
        "input_schema": strict_schema(Digest),
    },
]

RESEARCH_TOOLS = {"read_article", "web_search", "get_video_transcript", "hn_discussion"}


@dataclass
class EditorResult:
    digest: Digest
    turns: int
    tool_calls: dict[str, int] = field(default_factory=dict)
    usage: dict = field(default_factory=dict)
    dropped_refs: list[str] = field(default_factory=list)


@dataclass
class _Ref:
    url: str
    title: str = ""
    source: str = ""
    hn_id: str | None = None
    kind: str = "article"


class _Session:
    """Per-run state: the id registry, tool counts, and whether research is still allowed."""

    def __init__(self, candidates: list[Candidate], client: anthropic.Anthropic, tracker: UsageTracker):
        self.client = client
        self.tracker = tracker
        self.refs: dict[str, _Ref] = {}
        self._by_url: dict[str, str] = {}
        self._search_count = 0
        self._lock = threading.Lock()  # parallel searches register ids concurrently
        self.tool_calls: dict[str, int] = {}
        self.research_closed = False
        for c in candidates:
            self._register(c.id, _Ref(c.url, c.title, c.source, c.hn_id, c.kind))

    def _register(self, ref_id: str, ref: _Ref) -> str:
        self.refs[ref_id] = ref
        self._by_url.setdefault(normalize_url(ref.url), ref_id)
        return ref_id

    def _new_search_ref(self, ref: _Ref) -> str:
        with self._lock:
            existing = self._by_url.get(normalize_url(ref.url))
            if existing:
                return existing  # a search hit already seen keeps its id
            self._search_count += 1
            return self._register(f"s{self._search_count}", ref)

    def resolve(self, ref_id) -> _Ref:
        ref = self.refs.get(str(ref_id).strip()) if ref_id else None
        if ref is None:
            raise ValueError(f"Unknown id {ref_id!r}. Use an id from the candidate list or a search result.")
        return ref

    def _condense(self, kind: str, title, site, text: str, focus=None) -> str:
        return condense(self.client, self.tracker, kind=kind, title=title, site=site, text=text, focus=focus)

    # Each tool returns (content string, is_error)
    def run(self, name: str, args: dict) -> tuple[str, bool]:
        self.tool_calls[name] = self.tool_calls.get(name, 0) + 1
        if self.research_closed and name in RESEARCH_TOOLS:
            return "Research is closed: the context budget is used up. Write the issue from what you have and call submit_digest.", True
        try:
            if name == "read_article":
                return self._read_article(args), False
            if name == "web_search":
                return self._web_search(args), False
            if name == "get_video_transcript":
                return self._transcript(args), False
            if name == "hn_discussion":
                return self._hn(args), False
            return f"Unknown tool {name!r}.", True
        except (FetchError, TranscriptError, SearchUnavailable, ValueError) as exc:
            return str(exc), True
        except Exception as exc:  # network flakiness etc. should not end the run
            log.warning("tool %s failed: %s", name, exc)
            return f"{name} failed: {type(exc).__name__}: {str(exc)[:200]}", True

    def _read_article(self, args: dict) -> str:
        ref_id = args.get("ref")
        ref = self.resolve(ref_id)
        want_full = bool(args.get("full")) or not settings.condense_sources
        limit = FULL_TEXT_CHARS if args.get("full") else (UNCONDENSED_CHARS if want_full else CONDENSE_INPUT_CHARS)
        data = read_article(ref.url, max_chars=limit)
        header = " | ".join(x for x in [ref_id, data.get("site"), data.get("date"), data.get("title")] if x)
        if want_full:
            note = " (truncated)" if data["truncated"] else ""
            return f"{header}\n<article_text untrusted=\"true\"{note}>\n{data['text']}\n</article_text>"
        notes = self._condense("article", data.get("title"), data.get("site"), data["text"], args.get("focus"))
        return f"{header}\n<notes>\n{notes}\n</notes>"

    def _web_search(self, args: dict) -> str:
        query = args.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("Missing required argument 'query'.")
        results = web_search(query.strip(), args.get("kind", "news"), args.get("freshness", "week"), count=6)
        if not results:
            return "No results. Try a different or broader query."
        lines = []
        for r in results:
            rid = self._new_search_ref(_Ref(r["url"], r["title"], r["source"]))
            domain = urlparse(r["url"]).netloc.removeprefix("www.")
            lines.append(f"{rid} | {domain} | {r['age'] or '?'} | {r['title']} | {r['description'][:160]}")
        return "\n".join(lines)

    def _transcript(self, args: dict) -> str:
        ref_id = args.get("ref")
        ref = self.resolve(ref_id)
        data = get_transcript(ref.url)
        if not settings.condense_sources:
            return f"{ref_id} | {data['source']}\n<transcript untrusted=\"true\">\n{data['text'][:UNCONDENSED_CHARS]}\n</transcript>"
        notes = self._condense("video transcript", ref.title, ref.source, data["text"], args.get("focus"))
        return f"{ref_id} | {ref.source} | {ref.title} ({data['source']})\n<notes>\n{notes}\n</notes>"

    def _hn(self, args: dict) -> str:
        ref_id = args.get("ref")
        ref = self.resolve(ref_id)
        if not ref.hn_id:
            raise ValueError(f"{ref_id} has no Hacker News thread.")
        data = hn_discussion(ref.hn_id)
        if not data["top_comments"]:
            return f"{ref_id}: {data.get('points') or 0} points, no comments yet."
        thread = "\n\n".join(c["text"] for c in data["top_comments"])
        if not settings.condense_sources:
            return f"{ref_id}: {data.get('points')} points\n<comments untrusted=\"true\">\n{thread[:UNCONDENSED_CHARS]}\n</comments>"
        notes = self._condense("comment thread", data.get("title"), "Hacker News", thread, HN_FOCUS)
        return f"{ref_id}: {data.get('points')} points, {len(data['top_comments'])} top comments\n<notes>\n{notes}\n</notes>"


def _check_digest(raw: dict, session: _Session, attempt: int) -> tuple[Digest | None, str | None, list[str]]:
    """Validate a submission and resolve its ids to URLs. Returns (digest, problems, dropped ids)."""
    try:
        digest = Digest.model_validate(raw)
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:15])
        return None, f"The submission did not match the schema: {problems}", []

    unknown = sorted({item.ref for item in digest.cited() if item.ref not in session.refs})
    if unknown and attempt < 2:
        return None, (
            f"These ids don't exist: {', '.join(unknown)}. Cite only candidate ids (c12) or search result ids (s3) "
            "that you were given, or remove that source or item. Then call submit_digest again with the full issue."
        ), []
    if unknown:  # out of retries: drop what can't be resolved rather than publish it
        digest = _drop_unknown(digest, session)
    for item in digest.cited():
        item.url = session.refs[item.ref].url
    return digest, None, unknown


def _drop_unknown(digest: Digest, session: _Session) -> Digest:
    ok = lambda item: item.ref in session.refs  # noqa: E731
    digest.lead.sources = [s for s in digest.lead.sources if ok(s)]
    for section in digest.sections:
        for story in section.stories:
            story.sources = [s for s in story.sources if ok(s)]
        section.stories = [s for s in section.stories if s.sources]
    digest.sections = [s for s in digest.sections if s.stories]
    digest.quick_hits = [q for q in digest.quick_hits if ok(q)]
    digest.watch = [w for w in digest.watch if ok(w)]
    if not digest.lead.sources:
        raise EditorError("Lead story had no valid sources")
    return digest


def anthropic_client() -> anthropic.Anthropic:
    headers = {"anthropic-workspace-id": settings.anthropic_workspace_id} if settings.anthropic_workspace_id else None
    return anthropic.Anthropic(api_key=settings.anthropic_api_key, max_retries=4, default_headers=headers)


def _reminder(text: str) -> dict:
    """A harness notice, as a system message where the model supports it."""
    if settings.model in SYSTEM_MESSAGE_MODELS:
        return {"role": "system", "content": text}
    return {"role": "user", "content": text}


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
    tracker = UsageTracker()
    session = _Session(candidates, client, tracker)
    max_turns = settings.max_agent_turns
    briefing = build_briefing(
        cadence, issue_date, candidates, recent_headlines, max_turns, search_enabled=bool(settings.brave_api_key)
    )
    messages: list[dict] = [{"role": "user", "content": briefing}]
    extra: dict = {}
    if settings.model in FALLBACK_MODELS:
        extra = {"betas": [FALLBACK_BETA], "fallbacks": "default"}
    submit_attempts = 0
    nudges = 0
    wrap_up_sent = False
    bad_json_retries = 0

    for turn in range(1, max_turns + 1):
        try:
            with client.beta.messages.stream(
                model=settings.model,
                max_tokens=32000,
                system=[{"type": "text", "text": SYSTEM_PROMPT}],
                tools=TOOLS,
                messages=messages,
                thinking={"type": "adaptive"},
                output_config={"effort": settings.effort},
                cache_control={"type": "ephemeral"},
                **extra,
            ) as stream:
                response = stream.get_final_message()
        except ValueError as exc:
            # The SDK raises ValueError when a tool call's JSON can't be parsed at all. There is no
            # tool_use block to answer, so re-issue the same request (anthropic.APIError is not caught).
            bad_json_retries += 1
            log.warning("unparseable tool input on turn %d (retry %d): %s", turn, bad_json_retries, str(exc)[:200])
            if bad_json_retries > 2:
                raise EditorError("Model kept producing unparseable tool input") from exc
            continue

        tracker.add("editor", settings.model, response.usage)
        context_size = prompt_tokens(response.usage) + (response.usage.output_tokens or 0)
        log.info("editor turn %d: stop=%s context=%d out=%d", turn, response.stop_reason, context_size, response.usage.output_tokens)

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

        # Once the conversation is near the budget, close research before running more tools.
        if context_size >= settings.context_budget_tokens:
            session.research_closed = True

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
            return EditorResult(accepted, turn, session.tool_calls, tracker.summary(), dropped)

        messages.append({"role": "user", "content": results})
        remaining = max_turns - turn
        if not wrap_up_sent and (session.research_closed or remaining <= 5):
            wrap_up_sent = True
            why = "The context budget is used up" if session.research_closed else f"Only {remaining} turns are left"
            messages.append(_reminder(f"{why}. Stop researching and call submit_digest with the best issue you can make from what you have."))

    raise EditorError(f"Turn budget of {max_turns} exhausted without an accepted digest")
