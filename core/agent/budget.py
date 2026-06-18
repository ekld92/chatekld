"""Per-turn LLM usage accumulator for the agent loop.

Each LLM round-trip the loop makes already records its own entry to
:data:`core.llm.usage.usage_tracker` (so the existing ``llm_usage.jsonl``
persistence and ``/api/usage`` summary keep working unchanged). This
class is a per-turn aggregate the loop can surface to the UI without
re-querying the tracker.
"""
from __future__ import annotations

from dataclasses import dataclass

from core.llm.types import LLMUsage


@dataclass
class UsageBudget:
    """Sum of every LLM call's usage within a single agent turn."""

    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    estimated_cost_usd: float = 0.0
    iteration_count: int = 0

    def record(self, usage: LLMUsage) -> None:
        self.input_tokens += int(usage.input_tokens or 0)
        self.output_tokens += int(usage.output_tokens or 0)
        self.cached_input_tokens += int(usage.cached_input_tokens or 0)
        self.estimated_cost_usd += float(usage.estimated_cost_usd or 0.0)
        self.iteration_count += 1

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def as_dict(self) -> dict:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "total_tokens": self.total_tokens,
            "estimated_cost_usd": self.estimated_cost_usd,
            "iteration_count": self.iteration_count,
        }
