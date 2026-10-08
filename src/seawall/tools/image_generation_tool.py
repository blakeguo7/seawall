"""Generate or edit raster images with configurable image generation providers."""

from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import Any, Literal

from openai import AsyncOpenAI
from pydantic import BaseModel, Field

from seawall.api.openai_client import _normalize_openai_base_url
from seawall.tools.base import BaseTool, ToolExecutionContext, ToolResult

log = logging.getLogger(__name__)

_DEFAULT_PROMPT = (
    "Create a high-quality raster image that satisfies the user's request. "
    "Avoid watermarks, unintended text, and unrelated logos."
)
_DEFAULT_MODEL = "gpt-image-2"
_DEFAULT_OUTPUT_DIR = "generated_images"


class ImageGenerationToolInput(BaseModel):
    """Arguments for image generation or editing."""

    prompt: str = Field(default=_DEFAULT_PROMPT, description="Image generation or edit prompt.")
    image_paths: list[str] = Field(
        default_factory=list,
        description="Local image paths to edit or use as references (uses the image edit endpoint).",
    )
    mask_path: str | None = Field(default=None, description="Optional PNG mask path for OpenAI edit mode.")
    output_path: str | None = Field(
        default=None,
        description="Optional output path. For multiple images, numeric suffixes are added.",
    )
    output_dir: str = Field(
        default=_DEFAULT_OUTPUT_DIR,
        description="Output directory used when output_path is not provided.",
    )
    model: str | None = Field(default=None, description="OpenAI image model override.")
    n: int = Field(default=1, ge=1, le=10, description="Number of images to generate.")
    size: str = Field(default="auto", description="OpenAI image size, e.g. auto, 1024x1024, 1536x1024.")
    quality: str = Field(default="medium", description="OpenAI image quality, e.g. low, medium, high, auto.")
    background: Literal["transparent", "opaque", "auto"] | None = Field(
        default=None,
        description="Optional OpenAI background mode when supported by the provider.",
    )
    output_format: Literal["png", "jpeg", "webp"] = Field(default="png", description="Output image format.")
    output_compression: int | None = Field(
        default=None,
        ge=0,
        le=100,
        description="Optional OpenAI compression level for lossy output formats when supported.",
    )
    input_fidelity: Literal["low", "high"] | None = Field(
        default=None,
        description="Optional OpenAI edit input fidelity when supported by the provider.",
    )
    moderation: str | None = Field(default=None, description="Optional OpenAI moderation setting.")
    overwrite: bool = Field(default=False, description="Whether to overwrite existing output files.")


class ImageGenerationTool(BaseTool):
    """Generate or edit raster images and save them to local files."""

    name = "image_generation"
    description = (
        "Generate or edit raster images using an OpenAI-compatible image API (key and base_url "
        "configured under image_generation). Use this for bitmap assets such as photos, "
        "illustrations, sprites, mockups, transparent cutouts, or edited local images."
    )
    input_model = ImageGenerationToolInput

    async def execute(self, arguments: ImageGenerationToolInput, context: ToolExecutionContext) -> ToolResult:
        config = context.metadata.get("image_generation_config", {})
        if not isinstance(config, dict):
            config = {}

        try:
            output_paths = self._resolve_output_paths(arguments, context.cwd)
            image_b64 = await self._generate_with_openai(arguments, config)
            written = self._write_images(image_b64, output_paths, overwrite=arguments.overwrite)
        except Exception as exc:
            log.exception("image_generation failed")
            return ToolResult(output=f"image_generation failed: {exc}", is_error=True)

        mode = "edit" if arguments.image_paths else "generate"
        model = (arguments.model or str(config.get("model") or _DEFAULT_MODEL)).strip()
        return ToolResult(
            output=(
                f"[Image generation via {model} ({mode}, openai)]\n"
                + "\n".join(f"Wrote {path}" for path in written)
            ),
            metadata={"paths": [str(path) for path in written], "model": model, "mode": mode, "provider": "openai"},
        )

    async def _generate_with_openai(self, arguments: ImageGenerationToolInput, config: dict[str, object]) -> list[str]:
        model = (arguments.model or str(config.get("model") or _DEFAULT_MODEL)).strip()
        api_key = str(config.get("api_key") or "").strip()
        base_url = str(config.get("base_url") or "").strip()
        if not api_key:
            raise RuntimeError(
                "OpenAI image generation API key is not configured. Set image_generation.api_key "
                "or SEAWALL_IMAGE_GENERATION_API_KEY."
            )
        if arguments.image_paths:
            return await self._edit_images(arguments, model, api_key, base_url)
        return await self._generate_images(arguments, model, api_key, base_url)

    @staticmethod
    async def _generate_images(arguments: ImageGenerationToolInput, model: str, api_key: str, base_url: str) -> list[str]:
        client = AsyncOpenAI(
            api_key=api_key,
            base_url=_normalize_openai_base_url(base_url),
            default_headers={"Authorization": f"Bearer {api_key}"},
        )
        result = await client.images.generate(**_image_payload(arguments, model))
        return _extract_b64_images(result)

    @staticmethod
    async def _edit_images(arguments: ImageGenerationToolInput, model: str, api_key: str, base_url: str) -> list[str]:
        client = AsyncOpenAI(
            api_key=api_key,
            base_url=_normalize_openai_base_url(base_url),
            default_headers={"Authorization": f"Bearer {api_key}"},
        )
        image_handles = [Path(path).expanduser().resolve().open("rb") for path in arguments.image_paths]
        mask_handle = Path(arguments.mask_path).expanduser().resolve().open("rb") if arguments.mask_path else None
        try:
            payload = _image_payload(arguments, model)
            payload["image"] = image_handles if len(image_handles) > 1 else image_handles[0]
            if mask_handle is not None:
                payload["mask"] = mask_handle
            result = await client.images.edit(**payload)
        finally:
            for handle in image_handles:
                handle.close()
            if mask_handle is not None:
                mask_handle.close()
        return _extract_b64_images(result)

    @staticmethod
    def _resolve_output_paths(arguments: ImageGenerationToolInput, cwd: Path) -> list[Path]:
        suffix = f".{arguments.output_format}"
        if arguments.output_path:
            base = Path(arguments.output_path)
            if not base.is_absolute():
                base = cwd / base
            base = base.expanduser().resolve()
        else:
            out_dir = Path(arguments.output_dir)
            if not out_dir.is_absolute():
                out_dir = cwd / out_dir
            out_dir = out_dir.expanduser().resolve()
            base = out_dir / f"image{suffix}"
        if base.suffix.lower() != suffix:
            base = base.with_suffix(suffix)
        if arguments.n == 1:
            return [base]
        return [base.with_name(f"{base.stem}-{idx}{base.suffix}") for idx in range(1, arguments.n + 1)]

    @staticmethod
    def _write_images(images: list[str], output_paths: list[Path], *, overwrite: bool) -> list[Path]:
        written: list[Path] = []
        for image_b64, output_path in zip(images, output_paths, strict=False):
            if output_path.exists() and not overwrite:
                raise FileExistsError(f"output already exists: {output_path} (set overwrite=true)")
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(base64.b64decode(image_b64))
            written.append(output_path)
        if not written:
            raise RuntimeError("provider returned no image data")
        return written


def _image_payload(arguments: ImageGenerationToolInput, model: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "prompt": arguments.prompt,
        "n": arguments.n,
        "size": arguments.size,
        "quality": arguments.quality,
        "background": arguments.background,
        "output_format": arguments.output_format,
        "output_compression": arguments.output_compression,
        "input_fidelity": arguments.input_fidelity,
        "moderation": arguments.moderation,
    }
    return {key: value for key, value in payload.items() if value is not None}


def _extract_b64_images(result: Any) -> list[str]:
    images: list[str] = []
    for item in getattr(result, "data", []) or []:
        b64 = getattr(item, "b64_json", None)
        if isinstance(b64, str) and b64:
            images.append(b64)
            continue
        url = getattr(item, "url", None)
        if isinstance(url, str) and url.startswith("data:image/") and ";base64," in url:
            images.append(url.split(";base64,", 1)[1])
    return images
