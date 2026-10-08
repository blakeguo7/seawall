"""Token and cost accounting for a session."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from seawall.api.usage import UsageSnapshot
from seawall.engine.pricing import PriceTable


@dataclass
class ModelUsage:
    """Usage and estimated cost attributed to one model."""

    usage: UsageSnapshot
    calls: int = 0
    cost_usd: float | None = 0.0  # None: the model has no price


class CostTracker:
    """Accumulate usage and estimated cost over the lifetime of a session.

    Every model call the engine makes goes through here (see
    :class:`~seawall.engine.metering.MeteredApiClient`), so compaction
    summaries and memory extraction count, not only the main turns.
    """

    def __init__(self, prices: PriceTable | None = None) -> None:
        self.prices = prices or PriceTable()
        self._usage = UsageSnapshot()
        self._by_model: dict[str, ModelUsage] = {}
        self._cost_usd = 0.0

    def add(self, usage: UsageSnapshot, model: str | None = None) -> None:
        """Add one model call to the running totals."""
        self._usage = self._usage.plus(usage)
        key = model or "(unknown)"
        entry = self._by_model.setdefault(key, ModelUsage(UsageSnapshot()))
        entry.usage = entry.usage.plus(usage)
        entry.calls += 1
        cost = self.prices.cost(model, usage) if model else None
        if cost is None:
            entry.cost_usd = None
        else:
            self._cost_usd += cost
            if entry.cost_usd is not None:
                entry.cost_usd += cost

    def reset(self) -> None:
        self._usage = UsageSnapshot()
        self._by_model.clear()
        self._cost_usd = 0.0

    def restore(
        self, usage: UsageSnapshot, cost_usd: float, unpriced_models: Iterable[str] = ()
    ) -> None:
        """Start from totals saved earlier, for a session that is loaded back from storage.

        The per-model breakdown is not kept, so the restored part appears as one ``(restored)``
        entry; models that were missing a price stay reported as unpriced.
        """
        self.reset()
        self._usage = usage
        self._cost_usd = cost_usd
        self._by_model["(restored)"] = ModelUsage(usage, calls=0, cost_usd=cost_usd)
        for name in unpriced_models:
            self._by_model[name] = ModelUsage(UsageSnapshot(), calls=0, cost_usd=None)

    @property
    def total(self) -> UsageSnapshot:
        """The aggregated usage."""
        return self._usage

    @property
    def cost_usd(self) -> float:
        """Estimated cost of the calls whose model has a price."""
        return self._cost_usd

    @property
    def calls(self) -> int:
        return sum(entry.calls for entry in self._by_model.values())

    @property
    def unpriced_models(self) -> tuple[str, ...]:
        """Models that were used but have no price, so their cost is missing from ``cost_usd``."""
        return tuple(sorted(name for name, entry in self._by_model.items() if entry.cost_usd is None))

    def by_model(self) -> dict[str, ModelUsage]:
        return dict(self._by_model)
