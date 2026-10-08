"""Model prices, for turning token counts into an estimated cost."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Mapping

from seawall.api.usage import UsageSnapshot

if TYPE_CHECKING:  # pragma: no cover
    from seawall.config.settings import ModelPriceConfig


@dataclass(frozen=True)
class ModelPrice:
    """Prices in US dollars per million tokens.

    Cache prices default to the usual multipliers on the input price (writes 1.25x,
    reads 0.1x); give them explicitly when the provider differs.
    """

    input: float
    output: float
    cache_write: float | None = None
    cache_read: float | None = None

    def cost(self, usage: UsageSnapshot) -> float:
        cache_write = self.cache_write if self.cache_write is not None else self.input * 1.25
        cache_read = self.cache_read if self.cache_read is not None else self.input * 0.1
        return (
            usage.input_tokens * self.input
            + usage.output_tokens * self.output
            + usage.cache_creation_input_tokens * cache_write
            + usage.cache_read_input_tokens * cache_read
        ) / 1_000_000


# Approximate list prices, matched by longest model-name prefix. They are a starting
# point, not an authority: providers change prices, resellers charge differently, and
# models newer than this table are simply absent. Override or extend them with
# ``pricing`` in settings.json. A model with no price here is reported as unpriced; the
# harness never guesses.
DEFAULT_PRICES: dict[str, ModelPrice] = {
    "claude-opus-4-5": ModelPrice(5, 25),
    "claude-opus-4-1": ModelPrice(15, 75),
    "claude-opus-4": ModelPrice(15, 75),
    "claude-3-opus": ModelPrice(15, 75),
    "claude-sonnet-4": ModelPrice(3, 15),
    "claude-3-7-sonnet": ModelPrice(3, 15),
    "claude-3-5-sonnet": ModelPrice(3, 15),
    "claude-haiku-4-5": ModelPrice(1, 5),
    "claude-3-5-haiku": ModelPrice(0.8, 4),
    "claude-3-haiku": ModelPrice(0.25, 1.25),
    "gpt-4o-mini": ModelPrice(0.15, 0.6, cache_read=0.075),
    "gpt-4o": ModelPrice(2.5, 10, cache_read=1.25),
    "gpt-4.1-nano": ModelPrice(0.1, 0.4, cache_read=0.025),
    "gpt-4.1-mini": ModelPrice(0.4, 1.6, cache_read=0.1),
    "gpt-4.1": ModelPrice(2, 8, cache_read=0.5),
    "gpt-5-nano": ModelPrice(0.05, 0.4, cache_read=0.005),
    "gpt-5-mini": ModelPrice(0.25, 2, cache_read=0.025),
    "gpt-5": ModelPrice(1.25, 10, cache_read=0.125),
    "o3-mini": ModelPrice(1.1, 4.4, cache_read=0.55),
    "o4-mini": ModelPrice(1.1, 4.4, cache_read=0.275),
    "o3": ModelPrice(2, 8, cache_read=0.5),
}


def normalize_model_name(model: str) -> str:
    """Lower-case a model id and drop a provider prefix and a ``[1m]``-style suffix."""
    name = model.strip().lower()
    name = re.sub(r"\[[^\]]*\]$", "", name)
    return name.split("/")[-1]


class PriceTable:
    """Look up prices by longest model-name prefix; configured prices win over built-in ones."""

    def __init__(self, overrides: Mapping[str, ModelPrice] | None = None) -> None:
        self._overrides = {normalize_model_name(key): price for key, price in (overrides or {}).items()}

    @classmethod
    def from_settings(cls, pricing: Mapping[str, ModelPriceConfig]) -> PriceTable:
        """Build a table from the ``pricing`` section of settings."""
        return cls(
            {
                name: ModelPrice(
                    input=price.input,
                    output=price.output,
                    cache_write=price.cache_write,
                    cache_read=price.cache_read,
                )
                for name, price in pricing.items()
            }
        )

    def lookup(self, model: str) -> ModelPrice | None:
        name = normalize_model_name(model)
        for table in (self._overrides, DEFAULT_PRICES):
            matches = [key for key in table if name.startswith(key)]
            if matches:
                return table[max(matches, key=len)]
        return None

    def cost(self, model: str, usage: UsageSnapshot) -> float | None:
        """Estimated cost of ``usage`` on ``model``, or None when the model has no price."""
        price = self.lookup(model)
        return price.cost(usage) if price is not None else None
