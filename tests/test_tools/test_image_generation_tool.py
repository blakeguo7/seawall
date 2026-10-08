"""Tests for the image_generation tool."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from seawall.config.settings import ImageGenerationConfig
from seawall.tools.base import ToolExecutionContext
from seawall.tools.image_generation_tool import ImageGenerationTool, ImageGenerationToolInput


@pytest.mark.asyncio
async def test_execute_requires_api_key_for_openai(tmp_path: Path) -> None:
    tool = ImageGenerationTool()
    result = await tool.execute(
        ImageGenerationToolInput(prompt="a cat"),
        ToolExecutionContext(cwd=tmp_path, metadata={"image_generation_config": {}}),
    )
    assert result.is_error
    assert "API key is not configured" in result.output


@pytest.mark.asyncio
async def test_execute_generate_writes_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    image_bytes = b"fake-png"
    image_b64 = base64.b64encode(image_bytes).decode("ascii")

    async def fake_generate_images(arguments, model, api_key, base_url):
        assert arguments.prompt == "a cat"
        assert model == "gpt-image-2"
        assert api_key == "test-key"
        assert base_url == ""
        return [image_b64]

    monkeypatch.setattr(ImageGenerationTool, "_generate_images", staticmethod(fake_generate_images))

    tool = ImageGenerationTool()
    result = await tool.execute(
        ImageGenerationToolInput(prompt="a cat", output_path="assets/cat.png"),
        ToolExecutionContext(
            cwd=tmp_path,
            metadata={"image_generation_config": {"api_key": "test-key", "model": "gpt-image-2"}},
        ),
    )

    out = tmp_path / "assets" / "cat.png"
    assert not result.is_error
    assert out.read_bytes() == image_bytes
    assert result.metadata["paths"] == [str(out)]
    assert result.metadata["provider"] == "openai"


@pytest.mark.asyncio
async def test_execute_refuses_overwrite_by_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    out = tmp_path / "image.png"
    out.write_bytes(b"existing")
    image_b64 = base64.b64encode(b"new").decode("ascii")

    async def fake_generate_images(arguments, model, api_key, base_url):
        return [image_b64]

    monkeypatch.setattr(ImageGenerationTool, "_generate_images", staticmethod(fake_generate_images))

    tool = ImageGenerationTool()
    result = await tool.execute(
        ImageGenerationToolInput(prompt="a cat", output_path=str(out)),
        ToolExecutionContext(cwd=tmp_path, metadata={"image_generation_config": {"api_key": "test"}}),
    )

    assert result.is_error
    assert "output already exists" in result.output
    assert out.read_bytes() == b"existing"


def test_resolve_output_paths_multiple(tmp_path: Path) -> None:
    paths = ImageGenerationTool._resolve_output_paths(
        ImageGenerationToolInput(output_path="hero.png", n=2),
        tmp_path,
    )
    assert paths == [tmp_path / "hero-1.png", tmp_path / "hero-2.png"]


def test_image_generation_config_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEAWALL_IMAGE_GENERATION_MODEL", "gpt-image-1")
    monkeypatch.setenv("SEAWALL_IMAGE_GENERATION_API_KEY", "sk-test")
    monkeypatch.setenv("SEAWALL_IMAGE_GENERATION_BASE_URL", "https://example.test/v1")

    cfg = ImageGenerationConfig.from_env()

    assert cfg.model == "gpt-image-1"
    assert cfg.api_key == "sk-test"
    assert cfg.base_url == "https://example.test/v1"
    assert cfg.is_configured
