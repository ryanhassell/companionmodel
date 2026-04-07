from __future__ import annotations

import httpx
import respx

from app.providers.elevenlabs import ElevenLabsProvider


@respx.mock
async def test_elevenlabs_stream_tts_uses_stream_endpoint(settings):
    settings.elevenlabs.api_key = "eleven-key"
    settings.voice.elevenlabs_call_tts_model = "eleven_flash_v2_5"
    client = httpx.AsyncClient()
    provider = ElevenLabsProvider(settings, client)
    route = respx.post("https://api.elevenlabs.io/v1/text-to-speech/voice_123/stream").mock(
        return_value=httpx.Response(200, content=b"abc123")
    )

    chunks = []
    async for chunk in provider.stream_tts(text="hello", voice_id="voice_123"):
        chunks.append(chunk)

    assert route.called
    assert b"".join(chunks) == b"abc123"
    await client.aclose()
