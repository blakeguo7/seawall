"""Settings model and loading logic for Seawall.

Settings are resolved with the following precedence (highest first):
1. CLI arguments
2. Environment variables (ANTHROPIC_API_KEY, SEAWALL_MODEL, etc.)
3. Config file (~/.seawall/settings.json)
4. Defaults
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, model_validator

from seawall.hooks.schemas import HookDefinition
from seawall.mcp.types import McpServerConfig
from seawall.permissions.modes import PermissionMode
from seawall.utils.file_lock import exclusive_file_lock
from seawall.utils.fs import atomic_write_text


# ANSI escape sequence pattern
_ANSI_ESCAPE_PATTERN = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi_escape_sequences(text: str) -> str:
    """Remove ANSI escape sequences from text.

    This is used to clean environment variables that may contain terminal
    formatting codes (e.g., '[1m' for bold) which can corrupt API requests.
    """
    if not text:
        return text
    return _ANSI_ESCAPE_PATTERN.sub("", text)


class PathRuleConfig(BaseModel):
    """A glob-pattern path permission rule."""

    pattern: str
    allow: bool = True


class PermissionSettings(BaseModel):
    """Permission mode configuration."""

    mode: PermissionMode = PermissionMode.DEFAULT
    allowed_tools: list[str] = Field(default_factory=list)
    denied_tools: list[str] = Field(default_factory=list)
    path_rules: list[PathRuleConfig] = Field(default_factory=list)
    denied_commands: list[str] = Field(default_factory=list)
    approval_timeout_seconds: float = Field(
        default=300.0,
        gt=0,
        description="How long an approval request waits for an answer before it is denied.",
    )


class ModelPriceConfig(BaseModel):
    """Price of one model in US dollars per million tokens."""

    input: float = Field(ge=0)
    output: float = Field(ge=0)
    cache_write: float | None = Field(default=None, ge=0, description="Default: 1.25 x input")
    cache_read: float | None = Field(default=None, ge=0, description="Default: 0.1 x input")


class LoopGuardSettings(BaseModel):
    """When an agent that keeps repeating itself is warned, then stopped."""

    enabled: bool = True
    warn_after: int = Field(default=3, ge=2, description="Same call, same result, this many times: warn")
    stop_after: int = Field(default=5, ge=3, description="...and this many times: stop the run")
    window: int = Field(default=12, ge=3, description="How many recent calls are compared")
    warn_errors_after: int = Field(default=4, ge=2, description="Failing calls in a row: warn")
    stop_errors_after: int = Field(default=8, ge=3, description="Failing calls in a row: stop")

    @model_validator(mode="after")
    def _warning_comes_before_stop(self) -> LoopGuardSettings:
        if self.stop_after <= self.warn_after:
            raise ValueError("loop_guard.stop_after must be greater than warn_after")
        if self.stop_errors_after <= self.warn_errors_after:
            raise ValueError("loop_guard.stop_errors_after must be greater than warn_errors_after")
        if self.window < self.stop_after:
            raise ValueError("loop_guard.window must be at least stop_after")
        return self


class LimitSettings(BaseModel):
    """Limits on spend, tokens and time, plus the loop guard."""

    max_budget_usd: float | None = Field(
        default=None, gt=0, description="Stop before the next model call once the estimated session cost reaches this."
    )
    max_total_tokens: int | None = Field(
        default=None, gt=0, description="Stop once the session has used this many tokens, cache traffic included."
    )
    max_seconds: float | None = Field(
        default=None, gt=0, description="Stop starting new steps once one prompt has run this long."
    )
    loop_guard: LoopGuardSettings = Field(default_factory=LoopGuardSettings)


class AuditSettings(BaseModel):
    """Audit log configuration: one tamper-evident file per session."""

    enabled: bool = True
    directory: str = Field(
        default="",
        description="Where session logs are written. Empty means <data dir>/audit.",
    )
    key_env: str = Field(
        default="SEAWALL_AUDIT_KEY",
        description=(
            "Name of the environment variable holding an HMAC key. When it is set the log "
            "is signed; when it is not, the log is a plain hash chain."
        ),
    )
    fail_closed: bool = Field(
        default=False,
        description="Refuse to run a tool when its decision cannot be written to the audit log.",
    )
    max_field_chars: int = Field(
        default=1000, ge=80, description="Longest tool-input string kept in a record."
    )


class TraceSettings(BaseModel):
    """Trace configuration: one file per session with a timed span for every step of each run."""

    enabled: bool = True
    directory: str = Field(
        default="",
        description="Where trace files are written. Empty means <data dir>/traces.",
    )
    capture_content: bool = Field(
        default=False,
        description=(
            "Keep a short preview of each model reply and tool output in the spans. Off by default: "
            "spans then hold timings, token counts, costs and statuses, and no text."
        ),
    )
    max_field_chars: int = Field(
        default=1000, ge=80, description="Longest string kept in a span attribute."
    )


class MemorySettings(BaseModel):
    """Memory system configuration."""

    enabled: bool = True
    max_files: int = 5
    max_entrypoint_lines: int = 200
    max_entrypoint_bytes: int = 25_000
    context_window_tokens: int | None = None
    auto_compact_threshold_tokens: int | None = None
    auto_extract_enabled: bool = False
    auto_extract_max_records: int = 3
    session_memory_enabled: bool = True
    auto_dream_enabled: bool = False
    auto_dream_min_hours: float = 24.0
    auto_dream_min_sessions: int = 5


class SandboxNetworkSettings(BaseModel):
    """OS-level network restrictions passed to sandbox-runtime."""

    allowed_domains: list[str] = Field(default_factory=list)
    denied_domains: list[str] = Field(default_factory=list)


class SandboxFilesystemSettings(BaseModel):
    """OS-level filesystem restrictions passed to sandbox-runtime."""

    allow_read: list[str] = Field(default_factory=list)
    deny_read: list[str] = Field(default_factory=list)
    allow_write: list[str] = Field(default_factory=lambda: ["."])
    deny_write: list[str] = Field(default_factory=list)


class DockerSandboxSettings(BaseModel):
    """Docker-specific sandbox configuration."""

    image: str = "seawall-sandbox:latest"
    auto_build_image: bool = True
    cpu_limit: float = Field(default=2.0, ge=0, description="CPUs the container may use. 0 means no limit.")
    memory_limit: str = Field(
        default="4g",
        description="Memory the container may use, as Docker writes it (512m, 4g). Swap is disabled. Empty means no limit.",
    )
    pids_limit: int = Field(
        default=512, ge=0, description="Most processes the container may run at once, which stops a fork bomb. 0 means no limit."
    )
    cap_drop_all: bool = Field(default=True, description="Remove every Linux capability from the container.")
    no_new_privileges: bool = Field(default=True, description="Stop a process in the container from gaining privileges (setuid binaries).")
    read_only_root: bool = Field(
        default=True,
        description=(
            "Mount the container's root filesystem read-only. The project directory, extra mounts and /tmp "
            "stay writable, and HOME points at /tmp."
        ),
    )
    tmp_size: str = Field(default="512m", description="Size of the writable /tmp, which is memory-backed and goes with the container.")
    extra_mounts: list[str] = Field(default_factory=list)
    extra_env: dict[str, str] = Field(default_factory=dict)


class SandboxSettings(BaseModel):
    """Sandbox-runtime integration settings."""

    enabled: bool = False
    backend: str = "srt"
    fail_if_unavailable: bool = False
    enabled_platforms: list[str] = Field(default_factory=list)
    network: SandboxNetworkSettings = Field(default_factory=SandboxNetworkSettings)
    filesystem: SandboxFilesystemSettings = Field(default_factory=SandboxFilesystemSettings)
    docker: DockerSandboxSettings = Field(default_factory=DockerSandboxSettings)


class WebSettings(BaseModel):
    """Outbound web tool configuration."""

    proxy: str | None = None
    resolution_mode: str = "auto"
    synthetic_dns_cidrs: list[str] = Field(default_factory=list)


class ProviderProfile(BaseModel):
    """Named provider workflow configuration."""

    label: str
    provider: str
    api_format: str
    auth_source: str
    default_model: str
    base_url: str | None = None
    last_model: str | None = None
    credential_slot: str | None = None
    allowed_models: list[str] = Field(default_factory=list)
    context_window_tokens: int | None = None
    auto_compact_threshold_tokens: int | None = None

    @property
    def resolved_model(self) -> str:
        """Return the active model for this profile."""
        return resolve_model_setting(
            (self.last_model or "").strip() or self.default_model,
            self.provider,
            default_model=self.default_model,
        )


@dataclass(frozen=True)
class ResolvedAuth:
    """Normalized auth material used to construct API clients."""

    provider: str
    auth_kind: str
    value: str
    source: str
    state: str = "configured"


CLAUDE_MODEL_ALIAS_OPTIONS: tuple[tuple[str, str, str], ...] = (
    ("default", "Default", "Recommended model for this profile"),
    ("best", "Best", "Most capable available model"),
    ("sonnet", "Sonnet", "Latest Sonnet for everyday coding"),
    ("opus", "Opus", "Latest Opus for complex reasoning"),
    ("haiku", "Haiku", "Fastest Claude model"),
    ("sonnet[1m]", "Sonnet (1M context)", "Latest Sonnet with 1M context"),
    ("opus[1m]", "Opus (1M context)", "Latest Opus with 1M context"),
    ("opusplan", "Opus Plan Mode", "Use Opus in plan mode and Sonnet otherwise"),
)

_CLAUDE_ALIAS_TARGETS: dict[str, str] = {
    "sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-6",
    "haiku": "claude-haiku-4-5",
    "sonnet[1m]": "claude-sonnet-4-6[1m]",
    "opus[1m]": "claude-opus-4-6[1m]",
}

_CLAUDE_TIERS = frozenset({"best", "opusplan", *_CLAUDE_ALIAS_TARGETS})


def _serves_claude(model: str | None) -> bool:
    """Whether a profile whose own model is ``model`` serves Claude models.

    Claude's tier names (sonnet, opus, haiku, best, opusplan) only mean something there. An empty
    model, ``default`` and the tier names themselves count as Claude, which is what profiles had
    before this check existed.
    """
    name = (model or "").strip().lower().split("/")[-1]
    return name in ("", "default") or name.startswith("claude") or name in _CLAUDE_TIERS


def normalize_anthropic_model_name(model: str) -> str:
    """Normalize an Anthropic model name the same way Hermes does.

    - Strips the ``anthropic/`` prefix when present.
    - Converts dotted Claude version separators to Anthropic's hyphenated form.
    """
    normalized = model.strip()
    lower = normalized.lower()
    if lower.startswith("anthropic/"):
        normalized = normalized[len("anthropic/"):]
        lower = normalized.lower()
    if lower.startswith("claude-"):
        return normalized.replace(".", "-")
    return normalized


def default_provider_profiles() -> dict[str, ProviderProfile]:
    """Return the built-in provider workflow catalog."""
    return {
        "claude-api": ProviderProfile(
            label="Anthropic-Compatible API",
            provider="anthropic",
            api_format="anthropic",
            auth_source="anthropic_api_key",
            default_model="claude-sonnet-4-6",
        ),
        "openai-compatible": ProviderProfile(
            label="OpenAI-Compatible API",
            provider="openai",
            api_format="openai",
            auth_source="openai_api_key",
            default_model="gpt-5.4",
        ),
        "qwen": ProviderProfile(
            label="Qwen (DashScope)",
            provider="dashscope",
            api_format="openai",
            auth_source="dashscope_api_key",
            default_model="qwen-plus",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        ),
    }


def builtin_provider_profile_names() -> set[str]:
    """Return the names of built-in provider profiles."""
    return set(default_provider_profiles())


def display_label_for_profile(profile_name: str, profile: ProviderProfile) -> str:
    """Return the user-facing label for a profile.

    Built-in profiles always use the current built-in catalog label so old
    persisted settings don't keep stale wording in menus.
    """
    builtin = default_provider_profiles().get(profile_name)
    if builtin is not None:
        return builtin.label
    return profile.label


def is_claude_family_provider(provider: str) -> bool:
    """Return True when the provider is a Claude/Anthropic workflow."""
    return provider == "anthropic"


def display_model_setting(profile: ProviderProfile) -> str:
    """Return the user-facing model setting for a profile."""
    configured = (profile.last_model or "").strip()
    if not configured and is_claude_family_provider(profile.provider):
        return "default"
    return configured or profile.default_model


def resolve_model_setting(
    model_setting: str,
    provider: str,
    *,
    default_model: str | None = None,
    permission_mode: str | None = None,
) -> str:
    """Resolve a user-facing model setting into the concrete runtime model ID."""
    configured = model_setting.strip()
    normalized = configured.lower()

    if not configured or normalized == "default":
        fallback = (default_model or "").strip()
        if fallback and fallback.lower() != "default":
            return resolve_model_setting(
                fallback,
                provider,
                default_model=None,
                permission_mode=permission_mode,
            )
        if is_claude_family_provider(provider):
            return _CLAUDE_ALIAS_TARGETS["sonnet"]
        return "gpt-5.4"

    if is_claude_family_provider(provider):
        if normalized in _CLAUDE_TIERS and not _serves_claude(default_model):
            # The profile serves other models: DeepSeek or a gateway behind an Anthropic-compatible
            # endpoint. A Claude name sent there is not refused; the endpoint quietly answers with a
            # model of its own, and the cost is estimated at Claude's prices. Use the profile's model.
            return (default_model or "").strip()
        if normalized == "best":
            return _CLAUDE_ALIAS_TARGETS["opus"]
        if normalized == "opusplan":
            if permission_mode == PermissionMode.PLAN.value:
                return _CLAUDE_ALIAS_TARGETS["opus"]
            return _CLAUDE_ALIAS_TARGETS["sonnet"]
        if normalized in _CLAUDE_ALIAS_TARGETS:
            return _CLAUDE_ALIAS_TARGETS[normalized]
        return normalize_anthropic_model_name(configured)

    if provider == "openai" and normalized in {"default", "best"}:
        return "gpt-5.4"

    return configured


def auth_source_provider_name(auth_source: str) -> str:
    """Map an auth source to the storage/runtime provider name."""
    mapping = {
        "anthropic_api_key": "anthropic",
        "openai_api_key": "openai",
        "dashscope_api_key": "dashscope",
    }
    return mapping.get(auth_source, auth_source)


def auth_source_uses_api_key(auth_source: str) -> bool:
    """Return True when the auth source is backed by a user-supplied API key."""
    return auth_source.endswith("_api_key")


def auth_source_env_var_candidates(auth_source: str) -> tuple[str, ...]:
    """Return env vars to probe for an auth source in precedence order."""
    mapping = {
        "anthropic_api_key": ("SEAWALL_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"),
        "openai_api_key": ("SEAWALL_OPENAI_API_KEY", "OPENAI_API_KEY"),
        "dashscope_api_key": ("SEAWALL_DASHSCOPE_API_KEY", "DASHSCOPE_API_KEY"),
    }
    return mapping.get(auth_source, ())


def resolve_auth_env_value(auth_source: str) -> tuple[str, str] | None:
    """Return the first configured env var/value pair for an auth source."""
    for env_var in auth_source_env_var_candidates(auth_source):
        env_value = os.environ.get(env_var, "")
        if env_value:
            return env_var, env_value
    return None


def credential_storage_provider_name(profile_name: str, profile: ProviderProfile) -> str:
    """Return the storage namespace used for this profile's credential.

    Built-in API-key flows continue to use provider-level storage by default.
    Custom compatible profiles can set ``credential_slot`` to bind their own key.
    """
    del profile_name
    if auth_source_uses_api_key(profile.auth_source) and profile.credential_slot:
        return f"profile:{profile.credential_slot}"
    return auth_source_provider_name(profile.auth_source)


def default_auth_source_for_provider(provider: str, api_format: str | None = None) -> str:
    """Infer the default auth source for a provider/backend."""
    if provider == "dashscope":
        return "dashscope_api_key"
    if provider == "openai" or api_format == "openai":
        return "openai_api_key"
    return "anthropic_api_key"


def _slugify_profile_name(value: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else "-" for ch in value).strip("-")
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned or "custom"


def _infer_profile_name_from_flat_settings(settings: "Settings") -> str:
    provider = (settings.provider or "").strip()
    if provider == "openai" and not settings.base_url:
        return "openai-compatible"
    if provider == "anthropic" and not settings.base_url:
        return "claude-api"
    if settings.base_url:
        return _slugify_profile_name(Path(settings.base_url).name or settings.base_url)
    if provider:
        return _slugify_profile_name(provider)
    return "claude-api"


def _profile_from_flat_settings(settings: "Settings") -> tuple[str, ProviderProfile]:
    defaults = default_provider_profiles()
    name = _infer_profile_name_from_flat_settings(settings)
    existing = defaults.get(name)
    if existing is not None and (
        existing.provider == settings.provider or not settings.provider
    ) and (
        existing.api_format == settings.api_format
    ) and (
        existing.base_url == settings.base_url
    ):
        profile = existing.model_copy(
            update={
                "last_model": settings.model or existing.resolved_model,
            }
        )
        return name, profile

    provider = settings.provider or ("openai" if settings.api_format == "openai" else "anthropic")
    profile = ProviderProfile(
        label=f"Imported {provider}",
        provider=provider,
        api_format=settings.api_format,
        auth_source=default_auth_source_for_provider(provider, settings.api_format),
        default_model=settings.model or defaults.get("claude-api", ProviderProfile(
            label="Claude API",
            provider="anthropic",
            api_format="anthropic",
            auth_source="anthropic_api_key",
            default_model="sonnet",
        )).default_model,
        last_model=settings.model or None,
        base_url=settings.base_url,
    )
    return name, profile


class ImageGenerationConfig(BaseModel):
    """Configuration for the image_generation tool."""

    model: str = "gpt-image-2"
    api_key: str = ""
    base_url: str = ""

    @classmethod
    def from_env(cls) -> "ImageGenerationConfig":
        """Load image generation config from environment variables."""
        return cls(
            model=os.environ.get("SEAWALL_IMAGE_GENERATION_MODEL", "gpt-image-2").strip()
            or "gpt-image-2",
            api_key=os.environ.get("SEAWALL_IMAGE_GENERATION_API_KEY", "").strip(),
            base_url=os.environ.get("SEAWALL_IMAGE_GENERATION_BASE_URL", "").strip(),
        )

    @property
    def is_configured(self) -> bool:
        """Return True when an image generation API key is available."""
        return bool(self.api_key)


class VisionModelConfig(BaseModel):
    """Configuration for the vision model used by the image_to_text tool.

    When the active model does not support multimodal input, the agent loop
    automatically falls back to this vision model to describe images.
    """

    model: str = ""
    api_key: str = ""
    base_url: str = ""

    @classmethod
    def from_env(cls) -> "VisionModelConfig":
        """Load vision model config from environment variables."""
        return cls(
            model=os.environ.get("SEAWALL_VISION_MODEL", "").strip(),
            api_key=os.environ.get("SEAWALL_VISION_API_KEY", "").strip(),
            base_url=os.environ.get("SEAWALL_VISION_BASE_URL", "").strip(),
        )

    @property
    def is_configured(self) -> bool:
        """Return True when both model and api_key are set."""
        return bool(self.model and self.api_key)


class Settings(BaseModel):
    """Main settings model for Seawall."""

    # API configuration
    api_key: str = ""
    model: str = "claude-sonnet-4-6"
    max_tokens: int = 16384
    base_url: str | None = None
    timeout: float = 30.0
    context_window_tokens: int | None = None
    auto_compact_threshold_tokens: int | None = None
    api_format: str = "anthropic"  # "anthropic" or "openai"
    provider: str = ""
    active_profile: str = "claude-api"
    profiles: dict[str, ProviderProfile] = Field(default_factory=default_provider_profiles)
    max_turns: int = 200

    # Behavior
    system_prompt: str | None = None
    append_system_prompt: str | None = None
    permission: PermissionSettings = Field(default_factory=PermissionSettings)
    hooks: dict[str, list[HookDefinition]] = Field(default_factory=dict)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    sandbox: SandboxSettings = Field(default_factory=SandboxSettings)
    audit: AuditSettings = Field(default_factory=AuditSettings)
    trace: TraceSettings = Field(default_factory=TraceSettings)
    limits: LimitSettings = Field(default_factory=LimitSettings)
    pricing: dict[str, ModelPriceConfig] = Field(
        default_factory=dict,
        description=(
            "Prices by model name or name prefix, in US dollars per million tokens. They take "
            "precedence over the built-in table, which is approximate and covers few models."
        ),
    )
    web: WebSettings = Field(default_factory=WebSettings)
    enabled_plugins: dict[str, bool] = Field(default_factory=dict)
    allow_project_plugins: bool = False
    allow_project_skills: bool = True
    project_skill_dirs: list[str] = Field(
        default_factory=lambda: [".seawall/skills", ".agents/skills", ".claude/skills"]
    )
    mcp_servers: dict[str, McpServerConfig] = Field(default_factory=dict)

    # UI
    fast_mode: bool = False
    effort: str = "medium"
    passes: int = 1
    verbose: bool = False

    # Vision model (image-to-text fallback)
    vision: VisionModelConfig = Field(default_factory=VisionModelConfig)

    # Image generation model
    image_generation: ImageGenerationConfig = Field(default_factory=ImageGenerationConfig)

    def merged_profiles(self) -> dict[str, ProviderProfile]:
        """Return the saved profiles merged over the built-in catalog."""
        merged = default_provider_profiles()
        for name, raw_profile in self.profiles.items():
            profile = (
                raw_profile.model_copy(deep=True)
                if isinstance(raw_profile, ProviderProfile)
                else ProviderProfile.model_validate(raw_profile)
            )
            builtin = merged.get(name)
            if builtin is not None and profile.base_url is None and builtin.base_url is not None:
                profile = profile.model_copy(update={"base_url": builtin.base_url})
            merged[name] = profile
        return merged

    def resolve_profile(self, name: str | None = None) -> tuple[str, ProviderProfile]:
        """Return the active provider profile."""
        profiles = self.merged_profiles()
        profile_name = (name or self.active_profile or os.environ.get("SEAWALL_PROFILE") or "").strip() or "claude-api"
        if profile_name not in profiles:
            fallback_name, fallback = _profile_from_flat_settings(self)
            profiles[fallback_name] = fallback
            profile_name = fallback_name
        return profile_name, profiles[profile_name].model_copy(deep=True)

    def materialize_active_profile(self) -> Settings:
        """Project the active profile back onto legacy flat settings fields."""
        profile_name, profile = self.resolve_profile()
        configured_model = (profile.last_model or "").strip() or profile.default_model
        return self.model_copy(
            update={
                "active_profile": profile_name,
                "profiles": self.merged_profiles(),
                "provider": profile.provider,
                "api_format": profile.api_format,
                "base_url": profile.base_url,
                "context_window_tokens": profile.context_window_tokens,
                "auto_compact_threshold_tokens": profile.auto_compact_threshold_tokens,
                "model": resolve_model_setting(
                    configured_model,
                    profile.provider,
                    default_model=profile.default_model,
                    permission_mode=self.permission.mode.value,
                ),
            }
        )

    def sync_active_profile_from_flat_fields(self) -> Settings:
        """Fold legacy flat provider fields back into the active profile.

        This preserves compatibility for callers that still construct `Settings`
        by setting top-level `provider` / `api_format` / `base_url` / `model`
        directly before the profile layer is used everywhere.
        """
        profile_name, profile = self.resolve_profile()
        profile_from_env = bool(os.environ.get("SEAWALL_PROFILE"))
        flat_profile_fields_match_profile = profile_from_env or (
            (self.provider or "").strip() == profile.provider
            and (self.api_format or "").strip() == profile.api_format
            and self.base_url == profile.base_url
        )
        next_provider = profile.provider if flat_profile_fields_match_profile else (self.provider or "").strip() or profile.provider
        next_api_format = profile.api_format if flat_profile_fields_match_profile else (self.api_format or "").strip() or profile.api_format
        next_base_url = profile.base_url if flat_profile_fields_match_profile else (self.base_url if self.base_url is not None else profile.base_url)
        next_context_window_tokens = (
            self.context_window_tokens
            if self.context_window_tokens is not None
            else profile.context_window_tokens
        )
        next_auto_compact_threshold_tokens = (
            self.auto_compact_threshold_tokens
            if self.auto_compact_threshold_tokens is not None
            else profile.auto_compact_threshold_tokens
        )
        flat_model = (self.model or "").strip()
        resolved_profile_model = resolve_model_setting(
            (profile.last_model or "").strip() or profile.default_model,
            profile.provider,
            default_model=profile.default_model,
            permission_mode=self.permission.mode.value,
        )
        if flat_model and flat_model != resolved_profile_model:
            next_model = flat_model
        else:
            next_model = profile.last_model
        current_default_auth = default_auth_source_for_provider(profile.provider, profile.api_format)
        next_auth_source = profile.auth_source
        if not next_auth_source or next_auth_source == current_default_auth:
            next_auth_source = default_auth_source_for_provider(next_provider, next_api_format)

        updated_profile = profile.model_copy(
            update={
                "provider": next_provider,
                "api_format": next_api_format,
                "base_url": next_base_url,
                "auth_source": next_auth_source,
                "last_model": next_model,
                "context_window_tokens": next_context_window_tokens,
                "auto_compact_threshold_tokens": next_auto_compact_threshold_tokens,
            }
        )
        profiles = self.merged_profiles()
        profiles[profile_name] = updated_profile
        return self.model_copy(
            update={
                "active_profile": profile_name,
                "profiles": profiles,
            }
        )

    def resolve_api_key(self) -> str:
        """Resolve API key with precedence: instance value > env var > empty.

        Returns the API key string. Raises ValueError if no key is found.
        """
        profile_name, profile = self.resolve_profile()
        del profile_name

        if self.api_key:
            return self.api_key

        env_resolved = resolve_auth_env_value(profile.auth_source)
        if env_resolved:
            _, env_value = env_resolved
            return env_value

        raise ValueError(
            "No API key found. Set an SEAWALL_* provider API key "
            "(preferred) or the matching native provider environment variable, "
            "or configure api_key in ~/.seawall/settings.json"
        )

    def resolve_auth(self) -> ResolvedAuth:
        """Resolve the API key for the current provider profile."""
        profile_name, profile = self.resolve_profile()
        provider = profile.provider.strip()
        auth_source = profile.auth_source.strip() or default_auth_source_for_provider(provider, profile.api_format)
        storage_provider = auth_source_provider_name(auth_source)

        from seawall.auth.storage import load_credential

        if profile.credential_slot:
            scoped_storage_provider = f"profile:{profile.credential_slot}"
            scoped = load_credential(scoped_storage_provider, "api_key", use_keyring=False)
            if scoped is None:
                scoped = load_credential(scoped_storage_provider, "api_key")
            if scoped:
                return ResolvedAuth(
                    provider=provider or auth_source_provider_name(auth_source),
                    auth_kind="api_key",
                    value=scoped,
                    source=f"file:{scoped_storage_provider}",
                    state="configured",
                )

        storage_provider = credential_storage_provider_name(profile_name, profile)

        env_resolved = resolve_auth_env_value(auth_source)
        if env_resolved:
            env_var, env_value = env_resolved
            return ResolvedAuth(
                provider=provider or storage_provider,
                auth_kind="api_key",
                value=env_value,
                source=f"env:{env_var}",
                state="configured",
            )

        explicit_key = "" if profile.credential_slot else self.api_key
        if explicit_key:
            return ResolvedAuth(
                provider=provider or storage_provider,
                auth_kind="api_key",
                value=explicit_key,
                source="settings_or_env",
                state="configured",
            )

        stored = load_credential(storage_provider, "api_key")
        if stored:
            return ResolvedAuth(
                provider=provider or auth_source_provider_name(auth_source),
                auth_kind="api_key",
                value=stored,
                source=f"file:{storage_provider}",
                state="configured",
            )

        raise ValueError(
            f"No credentials found for auth source '{auth_source}'. "
            "Configure the matching provider or environment variable first."
        )

    def merge_cli_overrides(self, **overrides: Any) -> Settings:
        """Return a new Settings with CLI overrides applied (non-None values only)."""
        updates = {k: v for k, v in overrides.items() if v is not None}
        permission_mode = updates.pop("permission_mode", None)
        allowed_tools = normalize_tool_names(updates.pop("allowed_tools", None))
        disallowed_tools = normalize_tool_names(updates.pop("disallowed_tools", None))
        limit_overrides = {
            name: updates.pop(name)
            for name in ("max_budget_usd", "max_total_tokens", "max_seconds")
            if name in updates
        }

        def apply_permission_mode(settings: Settings) -> Settings:
            if limit_overrides:
                settings = settings.model_copy(
                    update={"limits": settings.limits.model_copy(update=limit_overrides)}
                )
            permission = settings.permission
            permission_updates: dict[str, Any] = {}
            if permission_mode is not None:
                permission_updates["mode"] = PermissionMode(str(permission_mode))
            if allowed_tools:
                permission_updates["allowed_tools"] = _extend_unique(permission.allowed_tools, allowed_tools)
            if disallowed_tools:
                permission_updates["denied_tools"] = _extend_unique(permission.denied_tools, disallowed_tools)
            if not permission_updates:
                return settings
            return settings.model_copy(
                update={"permission": permission.model_copy(update=permission_updates)}
            )

        # Strip ANSI escape sequences from model name if present
        if "model" in updates and isinstance(updates["model"], str):
            updates["model"] = strip_ansi_escape_sequences(updates["model"])
        if "effort" in updates and isinstance(updates["effort"], str):
            updates["effort"] = "xhigh" if updates["effort"].strip().lower() == "max" else updates["effort"].strip().lower()
        merged = apply_permission_mode(self.model_copy(update=updates))
        if not updates:
            return merged
        profile_keys = {
            "model",
            "base_url",
            "api_format",
            "provider",
            "api_key",
            "active_profile",
            "profiles",
            "context_window_tokens",
            "auto_compact_threshold_tokens",
        }
        profile_updates = profile_keys.intersection(updates)
        if not profile_updates:
            return merged
        if "active_profile" in profile_updates:
            switch_updates = {
                key: value
                for key, value in updates.items()
                if key not in profile_keys or key in {"active_profile", "profiles"}
            }
            switched = apply_permission_mode(self.model_copy(update=switch_updates)).materialize_active_profile()
            remaining_profile_updates = {
                key: value
                for key, value in updates.items()
                if key in profile_keys and key not in {"active_profile", "profiles"}
            }
            if not remaining_profile_updates:
                return switched
            return (
                switched.model_copy(update=remaining_profile_updates)
                .sync_active_profile_from_flat_fields()
                .materialize_active_profile()
            )
        return merged.sync_active_profile_from_flat_fields().materialize_active_profile()


def normalize_tool_names(values: Any) -> list[str]:
    """Split ``--allowed-tools`` style input ("a,b c" or ["a", "b,c"]) into tool names."""
    if not values:
        return []
    if isinstance(values, str):
        values = [values]
    names: list[str] = []
    for value in values:
        for name in re.split(r"[,\s]+", str(value)):
            if name and name not in names:
                names.append(name)
    return names


def _extend_unique(existing: list[str], extra: list[str]) -> list[str]:
    return [*existing, *(name for name in extra if name not in existing)]


def _apply_env_overrides(settings: Settings) -> Settings:
    """Apply supported environment variable overrides over loaded settings.

    Provider-scoped env vars (``ANTHROPIC_BASE_URL``, ``ANTHROPIC_MODEL``,
    ``OPENAI_BASE_URL``) only apply when the active profile does *not*
    explicitly configure the corresponding field.  ``SEAWALL_*`` env vars
    always override (explicit user intent).
    """
    updates: dict[str, Any] = {}

    # Resolve the active profile to check for explicit settings.
    _, active_profile = settings.resolve_profile()
    profile_has_base_url = active_profile.base_url is not None
    profile_explicit_model = (active_profile.last_model or "").strip()
    profile_has_explicit_model = bool(profile_explicit_model) and profile_explicit_model.lower() not in {"", "default"}

    # --- model ---
    seawall_model = os.environ.get("SEAWALL_MODEL")
    if seawall_model:
        updates["model"] = strip_ansi_escape_sequences(seawall_model)
    elif not profile_has_explicit_model:
        anthropic_model = os.environ.get("ANTHROPIC_MODEL")
        if anthropic_model:
            updates["model"] = strip_ansi_escape_sequences(anthropic_model)

    # --- base_url ---
    seawall_base = os.environ.get("SEAWALL_BASE_URL")
    if seawall_base:
        updates["base_url"] = seawall_base
    elif not profile_has_base_url:
        generic_base = os.environ.get("ANTHROPIC_BASE_URL") or os.environ.get("OPENAI_BASE_URL")
        if generic_base:
            updates["base_url"] = generic_base

    max_tokens = os.environ.get("SEAWALL_MAX_TOKENS")
    if max_tokens:
        updates["max_tokens"] = int(max_tokens)

    timeout = os.environ.get("SEAWALL_TIMEOUT")
    if timeout:
        updates["timeout"] = float(timeout)

    max_turns = os.environ.get("SEAWALL_MAX_TURNS")
    if max_turns:
        updates["max_turns"] = int(max_turns)

    context_window_tokens = os.environ.get("SEAWALL_CONTEXT_WINDOW_TOKENS")
    if context_window_tokens:
        updates["context_window_tokens"] = int(context_window_tokens)

    auto_compact_threshold_tokens = os.environ.get("SEAWALL_AUTO_COMPACT_THRESHOLD_TOKENS")
    if auto_compact_threshold_tokens:
        updates["auto_compact_threshold_tokens"] = int(auto_compact_threshold_tokens)

    provider = os.environ.get("SEAWALL_PROVIDER")
    api_format = os.environ.get("SEAWALL_API_FORMAT")
    env_auth_source = active_profile.auth_source
    if provider or api_format:
        env_auth_source = default_auth_source_for_provider(
            provider or active_profile.provider,
            api_format or active_profile.api_format,
        )

    env_resolved = resolve_auth_env_value(env_auth_source)
    if env_resolved:
        _, api_key = env_resolved
        updates["api_key"] = api_key

    if api_format:
        updates["api_format"] = api_format

    if provider:
        updates["provider"] = provider

    sandbox_enabled = os.environ.get("SEAWALL_SANDBOX_ENABLED")
    sandbox_fail = os.environ.get("SEAWALL_SANDBOX_FAIL_IF_UNAVAILABLE")
    sandbox_backend = os.environ.get("SEAWALL_SANDBOX_BACKEND")
    sandbox_docker_image = os.environ.get("SEAWALL_SANDBOX_DOCKER_IMAGE")
    sandbox_updates: dict[str, Any] = {}
    if sandbox_enabled is not None:
        sandbox_updates["enabled"] = _parse_bool_env(sandbox_enabled)
    if sandbox_fail is not None:
        sandbox_updates["fail_if_unavailable"] = _parse_bool_env(sandbox_fail)
    if sandbox_backend is not None:
        sandbox_updates["backend"] = sandbox_backend
    if sandbox_docker_image is not None:
        sandbox_updates["docker"] = settings.sandbox.docker.model_copy(
            update={"image": sandbox_docker_image}
        )
    if sandbox_updates:
        updates["sandbox"] = settings.sandbox.model_copy(update=sandbox_updates)

    web_updates: dict[str, Any] = {}
    web_proxy = os.environ.get("SEAWALL_WEB_PROXY")
    if web_proxy:
        web_updates["proxy"] = web_proxy
    web_resolution_mode = os.environ.get("SEAWALL_WEB_RESOLUTION_MODE")
    if web_resolution_mode:
        web_updates["resolution_mode"] = web_resolution_mode
    web_synthetic_dns_cidrs = os.environ.get("SEAWALL_WEB_SYNTHETIC_DNS_CIDRS")
    if web_synthetic_dns_cidrs:
        web_updates["synthetic_dns_cidrs"] = [
            entry.strip()
            for entry in web_synthetic_dns_cidrs.split(",")
            if entry.strip()
        ]
    if web_updates:
        updates["web"] = settings.web.model_copy(update=web_updates)

    if not updates:
        return settings
    return settings.model_copy(update=updates)


def _parse_bool_env(value: str) -> bool:
    """Parse a boolean environment override."""
    return value.strip().lower() in {"1", "true", "yes", "on"}


def load_settings(config_path: Path | None = None) -> Settings:
    """Load settings from config file, merging with defaults.

    Args:
        config_path: Path to settings.json. If None, uses the default location.

    Returns:
        Settings instance with file values merged over defaults.
    """
    if config_path is None:
        from seawall.config.paths import get_config_file_path

        config_path = get_config_file_path()

    if config_path.exists():
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        settings = Settings.model_validate(raw)
        env_profile = os.environ.get("SEAWALL_PROFILE")
        if env_profile:
            settings = settings.model_copy(update={"active_profile": env_profile.strip()})
        if "profiles" not in raw or "active_profile" not in raw:
            profile_name, profile = _profile_from_flat_settings(settings)
            merged_profiles = settings.merged_profiles()
            merged_profiles[profile_name] = profile
            settings = settings.model_copy(
                update={
                    "active_profile": profile_name,
                    "profiles": merged_profiles,
                }
            )
        return _apply_env_overrides(settings.materialize_active_profile())

    settings = Settings()
    env_profile = os.environ.get("SEAWALL_PROFILE")
    if env_profile:
        settings = settings.model_copy(update={"active_profile": env_profile.strip()})
    return _apply_env_overrides(settings.materialize_active_profile())


def save_settings(settings: Settings, config_path: Path | None = None) -> None:
    """Persist settings to the config file.

    Args:
        settings: Settings instance to save.
        config_path: Path to write. If None, uses the default location.
    """
    if config_path is None:
        from seawall.config.paths import get_config_file_path

        config_path = get_config_file_path()

    settings = settings.sync_active_profile_from_flat_fields().materialize_active_profile()
    lock_path = config_path.with_suffix(config_path.suffix + ".lock")
    with exclusive_file_lock(lock_path):
        atomic_write_text(
            config_path,
            settings.model_dump_json(indent=2) + "\n",
        )
