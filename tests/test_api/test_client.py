from __future__ import annotations

import asyncio

from seawall.api.client import AnthropicApiClient, ApiMessageRequest
from seawall.engine.messages import ConversationMessage, ImageBlock, TextBlock


def test_anthropic_client_passes_api_key_and_base_url(monkeypatch):
    captured: dict[str, object] = {}

    class _FakeAsyncAnthropic:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("seawall.api.client.AsyncAnthropic", _FakeAsyncAnthropic)

    AnthropicApiClient(api_key="api-key", base_url="https://gateway.example.com")

    assert captured == {"api_key": "api-key", "base_url": "https://gateway.example.com"}


def test_conversation_message_serializes_image_block_for_anthropic():
    message = ConversationMessage(
        role="user",
        content=[
            TextBlock(text="Describe this."),
            ImageBlock(media_type="image/png", data="YWJj", source_path="/tmp/example.png"),
        ],
    )

    assert message.to_api_param() == {
        "role": "user",
        "content": [
            {"type": "text", "text": "Describe this."},
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": "YWJj",
                },
            },
        ],
    }


def test_anthropic_client_streams_with_plain_messages_api(monkeypatch):
    class _FakeStream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def __aiter__(self):
            if False:
                yield None
            return

        async def get_final_message(self):
            class _Usage:
                input_tokens = 1
                output_tokens = 1

            class _Message:
                usage = _Usage()
                stop_reason = "end_turn"
                role = "assistant"
                content = []

            return _Message()

    class _FakeMessages:
        def __init__(self):
            self.last_params = None

        def stream(self, **params):
            self.last_params = params
            return _FakeStream()

    class _FakeAsyncAnthropic:
        def __init__(self, **kwargs):
            self.messages = _FakeMessages()

    monkeypatch.setattr("seawall.api.client.AsyncAnthropic", _FakeAsyncAnthropic)

    client = AnthropicApiClient(api_key="api-key")

    async def _run():
        return [
            event
            async for event in client.stream_message(
                ApiMessageRequest(
                    model="claude-sonnet-4-6",
                    messages=[],
                    system_prompt="system prompt",
                )
            )
        ]

    events = asyncio.run(_run())

    assert events
    params = client._client.messages.last_params
    assert params["model"] == "claude-sonnet-4-6"
    assert params["system"] == "system prompt"
    # No subscription-style identity: no extra betas, metadata or headers are attached.
    assert "betas" not in params
    assert "metadata" not in params
    assert "extra_headers" not in params
