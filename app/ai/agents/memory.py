from __future__ import annotations

from pydantic_ai import Agent

from app.ai.schemas import EntityMergeDecision, MemoryCommitPlan, MemoryExtractionResult, MemoryPlacementDraft, VoiceSummary


def build_memory_extraction_agent(model) -> Agent[None, MemoryExtractionResult]:
    return Agent(
        model,
        output_type=MemoryExtractionResult,
        system_prompt=(
            "Extract durable memory candidates from conversation text. "
            "Prefer concrete facts, preferences, follow-ups, and safety-relevant details. "
            "Split dense information into separate atomic facts instead of one blob. "
            "When possible, include entity_name, entity_kind, facet, relation_to_child, and canonical_value so memories can be placed into a structured memory graph."
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


def build_memory_placement_agent(model) -> Agent[None, MemoryPlacementDraft]:
    return Agent(
        model,
        output_type=MemoryPlacementDraft,
        system_prompt=(
            "Infer an open-ended structured placement for a memory in a child's world model. "
            "Choose the most natural subject, entity kind, facet, and relation based only on the memory text and explicit context. "
            "Do not rely on hardcoded examples or one specific family. "
            "Use the child root only when the memory is broadly about the child rather than another specific person, pet, topic, activity, routine anchor, event, or health context."
        ),
        retries=2,
        output_retries=2,
        defer_model_check=True,
        name="memory_placement_agent",
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


def build_memory_planner_agent(model) -> Agent[None, MemoryCommitPlan]:
    return Agent(
        model,
        output_type=MemoryCommitPlan,
        system_prompt=(
            "Plan durable memory writes from the latest turn using only recalled memory context and explicit user-provided details. "
            "Use open-ended semantic fields instead of hardcoded family-specific schemas. "
            "Prefer separate atomic memories over one blob when multiple facts are present. "
            "When uncertain, return no actions instead of guessing. "
            "Use create_entity, link_entities, attach_memory, and semantic path/group labels to organize the child's world. "
            "Never invent facts and never rely on deterministic keyword rules."
        ),
        retries=2,
        output_retries=2,
        defer_model_check=True,
        name="memory_planner_agent",
    )
