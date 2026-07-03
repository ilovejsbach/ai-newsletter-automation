"""Lightweight OpenAI token accounting.

A single process-wide accumulator that every OpenAI call reports into, so a build
can record how many tokens each run cost. Reset at the start of a build.
"""

from __future__ import annotations

import threading


class UsageTracker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0

    def reset(self) -> None:
        with self._lock:
            self.calls = 0
            self.input_tokens = 0
            self.output_tokens = 0

    def record(self, response: object) -> None:
        """Accumulate usage from an OpenAI Responses API result (best-effort)."""
        u = getattr(response, "usage", None)
        if u is None:
            return
        # Responses API: input_tokens / output_tokens. Fall back to chat-style names.
        inp = getattr(u, "input_tokens", None)
        out = getattr(u, "output_tokens", None)
        if inp is None:
            inp = getattr(u, "prompt_tokens", 0)
        if out is None:
            out = getattr(u, "completion_tokens", 0)
        with self._lock:
            self.calls += 1
            self.input_tokens += int(inp or 0)
            self.output_tokens += int(out or 0)

    def summary(self) -> dict[str, int]:
        with self._lock:
            return {
                "openai_calls": self.calls,
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "total_tokens": self.input_tokens + self.output_tokens,
            }


usage = UsageTracker()
