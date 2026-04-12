from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from pydantic_ai import Embedder
from pydantic_ai.embeddings.openai import OpenAIEmbeddingModel
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.models.openai import OpenAIResponsesModel
from pydantic_ai.providers.openai import OpenAIProvider as PydanticOpenAIProvider
from pydantic_ai.usage import UsageLimits

from app.ai.agents import (
    build_candidate_reply_agent,
    build_inbound_action_agent,
    build_memory_consolidation_agent,
    build_memory_entity_merge_agent,
    build_memory_extraction_agent,
    build_memory_planner_agent,
    build_memory_placement_agent,
    build_parent_chat_agent,
    build_photo_status_reply_agent,
    build_portal_preview_agent,
    build_proactive_call_opening_agent,
    build_proactive_message_agent,
    build_safety_rewrite_agent,
    build_speech_dictionary_candidate_agent,
    build_speech_dictionary_confirmation_agent,
    build_supportive_safety_reply_agent,
    build_turn_classifier_agent,
    build_voice_greeting_agent,
    build_voice_script_agent,
    build_voice_summary_agent,
)
from app.ai.deps import ParentChatDeps
from app.ai.schemas import (
    CandidateReplies,
    EntityMergeDecision,
    InboundActionDecision,
    MemoryCommitPlan,
    MemoryExtractionResult,
    MemoryPlacementDraft,
    ParentChatResponse,
    PhotoStatusReply,
    PortalPreferencePreview,
    ProactiveCallOpening,
    ProactiveMessageDraft,
    SafetyRewriteResult,
    SpeechDictionaryCandidate,
    SpeechDictionaryConfirmation,
    SupportiveSafetyReply,
    TurnClassification,
    VoiceCallScript,
    VoiceGreeting,
    VoiceSummary,
)
from app.core.settings import RuntimeSettings

OutputT = TypeVar("OutputT")


class AIUnavailableError(RuntimeError):
    pass


@dataclass(slots=True)
class AIGeneration(Generic[OutputT]):
    output: OutputT
    model: str | None
    usage: dict[str, Any]


class AiRuntime:
    def __init__(self, settings: RuntimeSettings, http_client) -> None:
        self.settings = settings
        self.http_client = http_client
        self.provider = PydanticOpenAIProvider(
            base_url=settings.openai.base_url,
            api_key=settings.openai.api_key,
            http_client=http_client,
        )
        self.chat_model = OpenAIResponsesModel(settings.openai.chat_model, provider=self.provider)
        self.portal_model = OpenAIResponsesModel(settings.openai.portal_preview_model, provider=self.provider)
        self.voice_model = OpenAIResponsesModel(settings.voice.text_model, provider=self.provider)
        self.embedding_model = OpenAIEmbeddingModel(settings.openai.embedding_model, provider=self.provider)
        self.embedder = Embedder(self.embedding_model, defer_model_check=True, instrument=False)

        self.turn_classifier_agent = build_turn_classifier_agent(self.chat_model)
        self.inbound_action_agent = build_inbound_action_agent(self.chat_model)
        self.candidate_reply_agent = build_candidate_reply_agent(self.chat_model)
        self.safety_rewrite_agent = build_safety_rewrite_agent(self.chat_model)
        self.photo_status_reply_agent = build_photo_status_reply_agent(self.chat_model)
        self.supportive_safety_reply_agent = build_supportive_safety_reply_agent(self.chat_model)
        self.memory_extraction_agent = build_memory_extraction_agent(self.chat_model)
        self.memory_placement_agent = build_memory_placement_agent(self.chat_model)
        self.memory_entity_merge_agent = build_memory_entity_merge_agent(self.chat_model)
        self.memory_consolidation_agent = build_memory_consolidation_agent(self.chat_model)
        self.memory_planner_agent = build_memory_planner_agent(self.chat_model)
        self.portal_preview_agent = build_portal_preview_agent(self.portal_model)
        self.parent_chat_agent = build_parent_chat_agent(self.portal_model)
        self.proactive_message_agent = build_proactive_message_agent(self.chat_model)
        self.proactive_call_opening_agent = build_proactive_call_opening_agent(self.voice_model)
        self.voice_script_agent = build_voice_script_agent(self.voice_model)
        self.voice_greeting_agent = build_voice_greeting_agent(self.voice_model)
        self.voice_summary_agent = build_voice_summary_agent(self.voice_model)
        self.speech_dictionary_candidate_agent = build_speech_dictionary_candidate_agent(self.voice_model)
        self.speech_dictionary_confirmation_agent = build_speech_dictionary_confirmation_agent(self.voice_model)

    @property
    def enabled(self) -> bool:
        return bool(self.settings.openai.api_key)

    def default_usage_limits(
        self,
        *,
        request_limit: int | None = None,
        tool_calls_limit: int | None = None,
    ) -> UsageLimits:
        effective_request_limit = request_limit if request_limit is not None else max(int(self.settings.openai.max_retries), 1)
        return UsageLimits(request_limit=max(int(effective_request_limit), 1), tool_calls_limit=tool_calls_limit)

    def model_settings(self, *, model_name: str, max_output_tokens: int | None = None, temperature: float | None = None) -> dict[str, Any]:
        settings: dict[str, Any] = {}
        if max_output_tokens is not None:
            settings["max_tokens"] = max_output_tokens
        if temperature is not None and self._supports_temperature(model_name):
            settings["temperature"] = temperature
        if self._supports_reasoning(model_name):
            settings["extra_body"] = {"reasoning": {"effort": self.settings.openai.reasoning_effort}}
        return settings

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not self.enabled or not texts:
            return []
        result = await self.embedder.embed_documents(texts)
        return [list(item) for item in result.embeddings]

    async def embed_query(self, text: str) -> list[float]:
        if not self.enabled or not text.strip():
            return []
        result = await self.embedder.embed_query(text)
        return list(result.embeddings[0]) if result.embeddings else []

    async def classify_turn(self, *, instructions: str, prompt: str) -> AIGeneration[TurnClassification]:
        return await self._run(self.turn_classifier_agent, prompt, instructions=instructions, max_tokens=220)

    async def decide_inbound_action(self, *, instructions: str, prompt: str) -> AIGeneration[InboundActionDecision]:
        return await self._run(self.inbound_action_agent, prompt, instructions=instructions, max_tokens=220)

    async def candidate_replies(self, *, instructions: str, prompt: str, temperature: float | None = None, max_tokens: int = 480) -> AIGeneration[CandidateReplies]:
        return await self._run(self.candidate_reply_agent, prompt, instructions=instructions, max_tokens=max_tokens, temperature=temperature)

    async def rewrite_safely(self, *, instructions: str, prompt: str, temperature: float | None = None, max_tokens: int = 120) -> AIGeneration[SafetyRewriteResult]:
        return await self._run(self.safety_rewrite_agent, prompt, instructions=instructions, max_tokens=max_tokens, temperature=temperature)

    async def photo_status_reply(self, *, instructions: str, prompt: str, temperature: float | None = None, max_tokens: int = 80) -> AIGeneration[PhotoStatusReply]:
        return await self._run(self.photo_status_reply_agent, prompt, instructions=instructions, max_tokens=max_tokens, temperature=temperature)

    async def supportive_safety_reply(self, *, prompt: str, model_name: str | None = None, max_tokens: int = 120) -> AIGeneration[SupportiveSafetyReply]:
        return await self._run(self.supportive_safety_reply_agent, prompt, max_tokens=max_tokens, model_name=model_name or self.chat_model.model_name)

    async def extract_memories(self, *, prompt: str, max_tokens: int) -> AIGeneration[MemoryExtractionResult]:
        return await self._run(self.memory_extraction_agent, prompt, instructions="Return structured memory extraction only.", max_tokens=max_tokens)

    async def infer_memory_placement(self, *, prompt: str, max_tokens: int = 260) -> AIGeneration[MemoryPlacementDraft]:
        return await self._run(
            self.memory_placement_agent,
            prompt,
            instructions="Return only the structured placement.",
            max_tokens=max_tokens,
        )

    async def merge_entity_memory(self, *, prompt: str, max_tokens: int) -> AIGeneration[EntityMergeDecision]:
        return await self._run(self.memory_entity_merge_agent, prompt, instructions="Return the structured merge decision only.", max_tokens=max_tokens)

    async def consolidate_memory(self, *, prompt: str, max_tokens: int) -> AIGeneration[VoiceSummary]:
        return await self._run(self.memory_consolidation_agent, prompt, instructions="Return only the concise summary.", max_tokens=max_tokens)

    async def plan_memory_commit(self, *, prompt: str, max_tokens: int = 1200, request_limit: int = 6) -> AIGeneration[MemoryCommitPlan]:
        return await self._run(
            self.memory_planner_agent,
            prompt,
            instructions="Return only the structured memory commit plan.",
            max_tokens=max_tokens,
            request_limit=request_limit,
        )

    async def portal_preview(self, *, prompt: str, temperature: float | None = None, max_tokens: int = 90) -> AIGeneration[PortalPreferencePreview]:
        return await self._run(self.portal_preview_agent, prompt, max_tokens=max_tokens, temperature=temperature, model_name=self.portal_model.model_name)

    async def parent_chat(self, *, prompt: str, deps: ParentChatDeps, temperature: float | None = None, max_tokens: int = 420) -> AIGeneration[ParentChatResponse]:
        return await self._run(
            self.parent_chat_agent,
            prompt,
            deps=deps,
            max_tokens=max_tokens,
            temperature=temperature,
            request_limit=15,
            tool_calls_limit=12,
            model_name=self.portal_model.model_name,
        )

    @asynccontextmanager
    async def parent_chat_stream(
        self,
        *,
        prompt: str,
        deps: ParentChatDeps,
        temperature: float | None = None,
        max_tokens: int = 420,
        event_stream_handler=None,
    ):
        if not self.enabled:
            raise AIUnavailableError("OpenAI is not configured")
        try:
            async with self.parent_chat_agent.run_stream(
                prompt,
                deps=deps,
                model=self.portal_model,
                model_settings=self.model_settings(
                    model_name=self.portal_model.model_name,
                    max_output_tokens=max_tokens,
                    temperature=temperature,
                ),
                usage_limits=self.default_usage_limits(request_limit=15, tool_calls_limit=12),
                event_stream_handler=event_stream_handler,
            ) as result:
                yield result
        except UsageLimitExceeded as exc:
            raise AIUnavailableError(f"AI request exceeded its safe working limit: {exc}") from exc

    async def proactive_message(self, *, instructions: str, prompt: str, temperature: float | None = None, max_tokens: int = 160) -> AIGeneration[ProactiveMessageDraft]:
        return await self._run(self.proactive_message_agent, prompt, instructions=instructions, max_tokens=max_tokens, temperature=temperature)

    async def proactive_call_opening(self, *, instructions: str, prompt: str, max_tokens: int = 70) -> AIGeneration[ProactiveCallOpening]:
        return await self._run(self.proactive_call_opening_agent, prompt, instructions=instructions, max_tokens=max_tokens, model_name=self.voice_model.model_name)

    async def voice_script(self, *, instructions: str, prompt: str, max_tokens: int = 500) -> AIGeneration[VoiceCallScript]:
        return await self._run(self.voice_script_agent, prompt, instructions=instructions, max_tokens=max_tokens, model_name=self.voice_model.model_name)

    async def voice_greeting(self, *, instructions: str, prompt: str, max_tokens: int = 35) -> AIGeneration[VoiceGreeting]:
        return await self._run(self.voice_greeting_agent, prompt, instructions=instructions, max_tokens=max_tokens, model_name=self.voice_model.model_name)

    async def voice_summary(self, *, instructions: str, prompt: str, max_tokens: int = 70) -> AIGeneration[VoiceSummary]:
        return await self._run(self.voice_summary_agent, prompt, instructions=instructions, max_tokens=max_tokens, model_name=self.voice_model.model_name)

    async def speech_dictionary_candidate(self, *, prompt: str, max_tokens: int = 80) -> AIGeneration[SpeechDictionaryCandidate]:
        return await self._run(self.speech_dictionary_candidate_agent, prompt, max_tokens=max_tokens, model_name=self.voice_model.model_name)

    async def speech_dictionary_confirmation(self, *, prompt: str, max_tokens: int = 20) -> AIGeneration[SpeechDictionaryConfirmation]:
        return await self._run(self.speech_dictionary_confirmation_agent, prompt, max_tokens=max_tokens, model_name=self.voice_model.model_name)

    async def _run(
        self,
        agent,
        prompt: str,
        *,
        instructions: str | None = None,
        deps=None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        request_limit: int | None = None,
        tool_calls_limit: int | None = None,
        model_name: str | None = None,
    ):
        if not self.enabled:
            raise AIUnavailableError("OpenAI is not configured")
        model = agent.model if model_name is None else self._model_for_name(model_name)
        try:
            result = await agent.run(
                prompt,
                deps=deps,
                model=model,
                instructions=instructions,
                model_settings=self.model_settings(
                    model_name=getattr(model, "model_name", model_name or self.settings.openai.chat_model),
                    max_output_tokens=max_tokens,
                    temperature=temperature,
                ),
                usage_limits=self.default_usage_limits(request_limit=request_limit, tool_calls_limit=tool_calls_limit),
            )
        except UsageLimitExceeded as exc:
            raise AIUnavailableError(f"AI request exceeded its safe working limit: {exc}") from exc
        return AIGeneration(
            output=result.output,
            model=getattr(result.response, "model_name", None),
            usage=dict(getattr(result.usage(), "__dict__", {})),
        )

    def _model_for_name(self, model_name: str):
        if model_name == self.portal_model.model_name:
            return self.portal_model
        if model_name == self.voice_model.model_name:
            return self.voice_model
        if model_name == self.chat_model.model_name:
            return self.chat_model
        return OpenAIResponsesModel(model_name, provider=self.provider)

    def _supports_temperature(self, model_name: str) -> bool:
        normalized = model_name.strip().lower()
        if normalized.startswith("gpt-5") and self.settings.openai.reasoning_effort not in (None, "", "none"):
            return False
        return True

    def _supports_reasoning(self, model_name: str) -> bool:
        normalized = model_name.strip().lower()
        if self.settings.openai.reasoning_effort in (None, "", "none"):
            return False
        return normalized.startswith("gpt-5")
