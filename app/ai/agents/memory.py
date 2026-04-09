from __future__ import annotations

from pydantic_ai import Agent

from app.ai.schemas import EntityMergeDecision, MemoryExtractionResult, VoiceSummary


def build_memory_extraction_agent(model) -> Agent[None, MemoryExtractionResult]:
    return Agent(
        model,
        output_type=MemoryExtractionResult,
        system_prompt=(
            "Extract durable memory candidates from conversation text. "
            "Prefer concrete facts, preferences, follow-ups, and safety-relevant details."
        ),
        retries=2,
        output_retries=2,
        defer_model_check=True,
        name="memory_extraction_agent",
    )


def build_memory_entity_merge_agent(model) -> Agent[None, EntityMergeDecision]:
    return Agent(
        model,
        output_type=EntityMergeDecision,
        system_prompt=(
            "Decide whether a new memory draft should merge into an existing entity-style memory and, if so, return the compact merged version."
        ),
        retries=2,
        output_retries=2,
        defer_model_check=True,
        name="memory_entity_merge_agent",
    )


def build_memory_consolidation_agent(model) -> Agent[None, VoiceSummary]:
    return Agent(
        model,
        output_type=VoiceSummary,
        system_prompt=(
            "Summarize a longer conversation into a concise internal memory summary for future retrieval."
        ),
        retries=2,
        output_retries=2,
        defer_model_check=True,
        name="memory_consolidation_agent",
    )
