"""Token accounting and cost estimates across every Claude call in a run.

Prices are USD per million tokens, from Anthropic's published rates. Update them here if they
change; they only feed the estimate shown on the admin page and stored with each issue.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

# model: (input, output, cache read, 5-minute cache write)
_PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20, 2.50),
    "claude-opus-5-5": (4.00, 20.00, 0.20, 5.00),
}
# Haiku 5.5 bills a request whose prompt exceeds 100K tokens on a higher rate card.
_LONG_PROMPT_THRESHOLD = {"claude-haiku-5-5": 100_000}
_LONG_PROMPT_PRICES = {"claude-haiku-5-5": (0.50, 2.50, 0.05, 0.625)}

FIELDS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def prompt_tokens(usage) -> int:
    """Total prompt size of one request (cached and uncached)."""
    return sum(getattr(usage, f, 0) or 0 for f in FIELDS if f != "output_tokens")


def request_cost(model: str, usage) -> float | None:
    prices = _PRICES.get(model)
    if prices is None:
        return None
    if prompt_tokens(usage) > _LONG_PROMPT_THRESHOLD.get(model, float("inf")):
        prices = _LONG_PROMPT_PRICES[model]
    inp, out, read, write = prices
    return (
        (getattr(usage, "input_tokens", 0) or 0) * inp
        + (getattr(usage, "output_tokens", 0) or 0) * out
        + (getattr(usage, "cache_read_input_tokens", 0) or 0) * read
        + (getattr(usage, "cache_creation_input_tokens", 0) or 0) * write
    ) / 1_000_000


@dataclass
class _Bucket:
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cost_usd: float = 0.0
    cost_known: bool = True
    max_prompt_tokens: int = 0


class UsageTracker:
    """Collects usage per role ('editor', 'condenser'). Safe to call from worker threads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._buckets: dict[str, _Bucket] = {}

    def add(self, role: str, model: str, usage) -> None:
        cost = request_cost(model, usage)
        with self._lock:
            b = self._buckets.setdefault(role, _Bucket())
            b.requests += 1
            for f in FIELDS:
                setattr(b, f, getattr(b, f) + (getattr(usage, f, 0) or 0))
            b.max_prompt_tokens = max(b.max_prompt_tokens, prompt_tokens(usage))
            if cost is None:
                b.cost_known = False
            else:
                b.cost_usd += cost

    def summary(self) -> dict:
        with self._lock:
            out: dict = {role: {**vars(b), "cost_usd": round(b.cost_usd, 4)} for role, b in self._buckets.items()}
            if all(b.cost_known for b in self._buckets.values()):
                out["total_cost_usd"] = round(sum(b.cost_usd for b in self._buckets.values()), 4)
            return out
