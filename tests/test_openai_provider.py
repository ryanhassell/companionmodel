from __future__ import annotations

import httpx
import respx

from app.providers.openai import OpenAIProvider


@respx.mock
async def test_openai_generate_text_parses_output_text(settings):
    settings.openai.api_key = "key"
    client = httpx.AsyncClient()
    provider = OpenAIProvider(settings, client)
    route = respx.post("https://api.openai.com/v1/responses").mock(
        return_value=httpx.Response(
            200,
            json={"model": "gpt-test", "output_text": "hello world", "usage": {"total_tokens": 3}},
        )
    )
    response = await provider.generate_text(input_items="hello")
    assert route.called
    assert response.text == "hello world"
    await client.aclose()
