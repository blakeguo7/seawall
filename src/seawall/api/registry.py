"""
LLM provider registry: metadata used to name the active provider.

The harness talks to two kinds of endpoints: Anthropic's Messages API and
OpenAI-compatible chat APIs. A ProviderSpec only labels a well-known provider
so status output and diagnostics can name it. The provider is detected from the
API key prefix, the base URL or the model name. Any other endpoint works
through a custom profile with a base URL and an API format.

Adding a provider: add a ProviderSpec to PROVIDERS below. Order matters, it
controls match priority.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderSpec:
    """One LLM provider's metadata.

    backend_type:
      "anthropic"    — Anthropic SDK (default for claude-* models)
      "openai_compat" — OpenAI-compatible REST API
    """

    # Identity
    name: str  # canonical name, e.g. "dashscope"
    keywords: tuple[str, ...]  # model-name substrings for detection (lowercase)
    env_key: str  # primary API key environment variable
    display_name: str = ""  # shown in status / diagnostics

    # Routing
    backend_type: str = "openai_compat"  # "anthropic" | "openai_compat"
    default_base_url: str = ""  # fallback base URL for this provider

    # Auto-detection signals
    detect_by_key_prefix: str = ""  # match api_key prefix
    detect_by_base_keyword: str = ""  # match substring in base_url

    @property
    def label(self) -> str:
        return self.display_name or self.name.title()


# ---------------------------------------------------------------------------
# PROVIDERS registry — order = detection priority.
# ---------------------------------------------------------------------------

PROVIDERS: tuple[ProviderSpec, ...] = (
    ProviderSpec(
        name="anthropic",
        keywords=("anthropic", "claude"),
        env_key="ANTHROPIC_API_KEY",
        display_name="Anthropic",
        backend_type="anthropic",
    ),
    ProviderSpec(
        name="openai",
        keywords=("openai", "gpt", "o1", "o3", "o4"),
        env_key="OPENAI_API_KEY",
        display_name="OpenAI",
    ),
    ProviderSpec(
        name="deepseek",
        keywords=("deepseek",),
        env_key="DEEPSEEK_API_KEY",
        display_name="DeepSeek",
        default_base_url="https://api.deepseek.com/v1",
        detect_by_base_keyword="deepseek",
    ),
    ProviderSpec(
        name="dashscope",
        keywords=("qwen", "dashscope"),
        env_key="DASHSCOPE_API_KEY",
        display_name="DashScope",
        default_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        detect_by_base_keyword="dashscope",
    ),
)


# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------


def find_by_name(name: str) -> ProviderSpec | None:
    """Find a provider spec by canonical name, e.g. "dashscope"."""
    for spec in PROVIDERS:
        if spec.name == name:
            return spec
    return None


def _match_by_model(model: str) -> ProviderSpec | None:
    """Match a provider by model-name keyword (case-insensitive)."""
    model_lower = model.lower()
    model_normalized = model_lower.replace("-", "_")
    model_prefix = model_lower.split("/", 1)[0] if "/" in model_lower else ""
    normalized_prefix = model_prefix.replace("-", "_")

    # Prefer an explicit provider-prefix match (e.g. "deepseek/..." → deepseek spec)
    for spec in PROVIDERS:
        if model_prefix and normalized_prefix == spec.name:
            return spec

    # Fall back to keyword scan
    for spec in PROVIDERS:
        if any(
            kw in model_lower or kw.replace("-", "_") in model_normalized
            for kw in spec.keywords
        ):
            return spec
    return None


def detect_provider_from_registry(
    model: str,
    api_key: str | None = None,
    base_url: str | None = None,
) -> ProviderSpec | None:
    """Detect the best-matching ProviderSpec for the given inputs.

    Detection priority:
      1. api_key prefix
      2. base_url keyword (e.g. "dashscope" in the URL → DashScope)
      3. model name keyword (e.g. "qwen" → DashScope)
    """
    # 1. api_key prefix
    if api_key:
        for spec in PROVIDERS:
            if spec.detect_by_key_prefix and api_key.startswith(spec.detect_by_key_prefix):
                return spec

    # 2. base_url keyword
    if base_url:
        base_lower = base_url.lower()
        for spec in PROVIDERS:
            if spec.detect_by_base_keyword and spec.detect_by_base_keyword in base_lower:
                return spec

    # 3. model keyword
    if model:
        return _match_by_model(model)

    return None
