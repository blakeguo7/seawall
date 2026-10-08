"""Human-readable summary of a session's usage, cost and limits."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from seawall.engine.query_engine import QueryEngine


def format_cost_report(engine: QueryEngine) -> str:
    """The text behind ``/cost``."""
    tracker = engine.cost_tracker
    usage = tracker.total
    lines = [
        f"Model: {engine.model}",
        f"Model calls: {tracker.calls}",
        f"Input tokens: {usage.input_tokens}",
        f"Output tokens: {usage.output_tokens}",
    ]
    if usage.cache_read_input_tokens or usage.cache_creation_input_tokens:
        lines.append(
            f"Prompt cache: {usage.cache_read_input_tokens} read, "
            f"{usage.cache_creation_input_tokens} written"
        )
    lines.append(f"Total tokens: {usage.all_tokens}")

    unpriced = tracker.unpriced_models
    if unpriced:
        known = f"${tracker.cost_usd:.4f} for the priced models, " if tracker.cost_usd else ""
        lines.append(
            f"Estimated cost: unknown ({known}no price for {', '.join(unpriced)}; "
            "add one under 'pricing' in settings.json)"
        )
    else:
        lines.append(f"Estimated cost: ${tracker.cost_usd:.4f}")

    by_model = tracker.by_model()
    if len(by_model) > 1:
        for name, entry in sorted(by_model.items()):
            cost = "no price" if entry.cost_usd is None else f"${entry.cost_usd:.4f}"
            lines.append(f"  {name}: {entry.calls} calls, {entry.usage.all_tokens} tokens, {cost}")

    limits = engine.limits
    if limits is not None:
        if limits.max_budget_usd is not None:
            lines.append(f"Budget: ${tracker.cost_usd:.4f} of ${limits.max_budget_usd:.2f}")
        if limits.max_total_tokens is not None:
            lines.append(f"Token limit: {usage.all_tokens} of {limits.max_total_tokens}")
        if limits.max_seconds is not None:
            lines.append(f"Time limit per prompt: {limits.max_seconds:g}s")
    return "\n".join(lines)
