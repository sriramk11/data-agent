"""Token -> dollar accounting.

This is the "live cost counter" stretch goal: every model turn's usage is
converted to a dollar figure and accumulated on the session, and the
orchestrator enforces MAX_COST_USD as a loop-breaking guardrail alongside
the step budget.
"""
from __future__ import annotations

from dataclasses import dataclass

# $ per million tokens (input, output). Sonnet 5 intro pricing runs through
# 2026-08-31; swap to the standard rate after that date.
PRICING_PER_MTOK = {
    "claude-sonnet-5": (2.00, 10.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-haiku-4-5": (1.00, 5.00),
}
_DEFAULT_PRICING = (3.00, 15.00)  # fallback if MODEL isn't in the table above


@dataclass
class Usage:
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


def turn_cost_usd(model: str, usage: Usage) -> float:
    in_price, out_price = PRICING_PER_MTOK.get(model, _DEFAULT_PRICING)
    # Cache reads are ~10% of input price; cache writes ~1.25x. Approximate
    # rather than exact-to-the-cent -- good enough for a live progress counter.
    input_cost = (usage.input_tokens / 1_000_000) * in_price
    cache_read_cost = (usage.cache_read_input_tokens / 1_000_000) * in_price * 0.1
    cache_write_cost = (usage.cache_creation_input_tokens / 1_000_000) * in_price * 1.25
    output_cost = (usage.output_tokens / 1_000_000) * out_price
    return input_cost + cache_read_cost + cache_write_cost + output_cost


class CostTracker:
    def __init__(self, model: str):
        self.model = model
        self.total_usd = 0.0
        self.total_input_tokens = 0
        self.total_output_tokens = 0

    def add(self, usage: Usage) -> float:
        """Record a turn's usage; returns that turn's incremental cost."""
        cost = turn_cost_usd(self.model, usage)
        self.total_usd += cost
        self.total_input_tokens += usage.input_tokens
        self.total_output_tokens += usage.output_tokens
        return cost
