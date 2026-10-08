"""Usage tracking models."""

from __future__ import annotations

from pydantic import BaseModel


class UsageSnapshot(BaseModel):
    """Token usage returned by the model provider.

    ``input_tokens`` counts input at the full rate. Tokens served from a prompt
    cache are reported separately, so providers that report cache hits as part of
    the prompt (OpenAI style) are normalized to the Anthropic convention: cached
    tokens are *not* included in ``input_tokens``.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        """Return the total number of input and output tokens billed at the full rate."""
        return self.input_tokens + self.output_tokens

    @property
    def all_tokens(self) -> int:
        """Every token the model processed or produced, including prompt-cache traffic."""
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_creation_input_tokens
            + self.cache_read_input_tokens
        )

    def plus(self, other: UsageSnapshot) -> UsageSnapshot:
        """Return the sum of two snapshots."""
        return UsageSnapshot(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_creation_input_tokens=self.cache_creation_input_tokens
            + other.cache_creation_input_tokens,
            cache_read_input_tokens=self.cache_read_input_tokens + other.cache_read_input_tokens,
        )
