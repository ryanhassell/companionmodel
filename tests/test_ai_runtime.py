from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.embeddings.test import TestEmbeddingModel
from pydantic_ai.models.test import TestModel

from app.ai.runtime import AIUnavailableError, AiRuntime
from app.providers.openai import OpenAIProvider


async def test_ai_runtime_candidate_agent_uses_structured_pydantic_output(settings):
    settings.openai.api_key = "test-key"
    client = httpx.AsyncClient()
    runtime = AiRuntime(settings, client)
    try:
        with runtime.candidate_reply_agent.override(
            model=TestModel(custom_output_args={"candidates": ["one", "two", "three"]})
        ):
            result = await runtime.candidate_replies(
                instructions="Test instructions",
                prompt="Generate some replies",
            )
        assert result.output.candidates == ["one", "two", "three"]
        assert result.model == "test"
        assert result.usage["requests"] >= 1
    finally:
        await client.aclose()


async def test_ai_runtime_embedder_uses_pydantic_embedding_model(settings):
    settings.openai.api_key = "test-key"
    client = httpx.AsyncClient()
    runtime = AiRuntime(settings, client)
    test_embedding_model = TestEmbeddingModel(dimensions=6)
    try:
        with runtime.embedder.override(model=test_embedding_model):
            query = await runtime.embed_query("hello world")
            docs = await runtime.embed_documents(["a", "b"])
        assert len(query) == 6
        assert len(docs) == 2
        assert all(len(item) == 6 for item in docs)
    finally:
        await client.aclose()


async def test_ai_runtime_wraps_usage_limit_exceeded_as_ai_unavailable(settings, monkeypatch):
    settings.openai.api_key = "test-key"
    client = httpx.AsyncClient()
    runtime = AiRuntime(settings, client)

    async def _boom(*args, **kwargs):
        raise UsageLimitExceeded("tool limit")

    monkeypatch.setattr(runtime.parent_chat_agent, "run", _boom)
    try:
        with pytest.raises(AIUnavailableError, match="safe working limit"):
            await runtime.parent_chat(prompt="hello", deps=None)
    finally:
        await client.aclose()


async def test_parent_chat_uses_higher_request_limit(settings, monkeypatch):
    settings.openai.api_key = "test-key"
    client = httpx.AsyncClient()
    runtime = AiRuntime(settings, client)

    async def _capture_run(*args, **kwargs):
        usage_limits = kwargs.get("usage_limits")
        assert usage_limits is not None
        assert usage_limits.request_limit == 15
        assert usage_limits.tool_calls_limit == 12

        class _FakeUsage:
            requests = 1

        class _FakeResponse:
            model_name = "test"

        class _FakeResult:
            output = type("Output", (), {"text": "ok"})()
            response = _FakeResponse()

            def usage(self):
                return _FakeUsage()

        return _FakeResult()

    monkeypatch.setattr(runtime.parent_chat_agent, "run", _capture_run)
    try:
        result = await runtime.parent_chat(prompt="hello", deps=None)
        assert result.output.text == "ok"
    finally:
        await client.aclose()


def test_services_do_not_call_legacy_openai_text_json_or_embedding_methods_directly():
    forbidden = ("generate_text(", "generate_json(", "embed_texts(")
    allowed_files = {Path("app/providers/openai.py")}
    offenders: list[str] = []
    for path in Path("app/services").rglob("*.py"):
        if path in allowed_files:
            continue
        text = path.read_text()
        for token in forbidden:
            if token in text:
                offenders.append(f"{path}:{token}")
    assert offenders == []


def test_openai_provider_is_transport_only_after_pydantic_migration():
    assert not hasattr(OpenAIProvider, "generate_text")
    assert not hasattr(OpenAIProvider, "generate_json")
    assert not hasattr(OpenAIProvider, "embed_texts")
