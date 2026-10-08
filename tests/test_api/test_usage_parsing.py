"""Reading token counts, including prompt-cache hits, from provider responses."""

from __future__ import annotations

from types import SimpleNamespace

from seawall.api.openai_client import _usage_data


def test_plain_usage() -> None:
    usage = SimpleNamespace(prompt_tokens=120, completion_tokens=30)
    assert _usage_data(usage) == {"input_tokens": 120, "output_tokens": 30, "cache_read_input_tokens": 0}


def test_openai_cached_tokens_are_split_out_of_the_prompt() -> None:
    usage = SimpleNamespace(
        prompt_tokens=1000,
        completion_tokens=50,
        prompt_tokens_details=SimpleNamespace(cached_tokens=800),
    )
    assert _usage_data(usage) == {
        "input_tokens": 200,
        "output_tokens": 50,
        "cache_read_input_tokens": 800,
    }


def test_deepseek_cache_hits_are_split_out_too() -> None:
    usage = SimpleNamespace(prompt_tokens=500, completion_tokens=10, prompt_cache_hit_tokens=300)
    data = _usage_data(usage)
    assert (data["input_tokens"], data["cache_read_input_tokens"]) == (200, 300)


def test_missing_or_nonsense_values_do_not_go_negative() -> None:
    usage = SimpleNamespace(prompt_tokens=10, completion_tokens=None, prompt_cache_hit_tokens=999)
    data = _usage_data(usage)
    assert data == {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 10}
    assert _usage_data(SimpleNamespace(prompt_tokens=None, completion_tokens=None))["input_tokens"] == 0
