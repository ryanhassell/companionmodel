from __future__ import annotations

from pydantic_ai import Agent, RunContext

from app.ai.deps import ParentChatDeps
from app.ai.schemas import ParentChatResponse, ParentGuidanceMemoryDraft, PortalPreferencePreview


def build_portal_preview_agent(model) -> Agent[None, PortalPreferencePreview]:
    return Agent(
        model,
        output_type=PortalPreferencePreview,
        system_prompt=(
            "Write one short example companion text that helps a parent understand a chosen communication style. "
            "Keep it warm, natural, and child-facing."
        ),
        retries=2,
        output_retries=2,
        defer_model_check=True,
        name="portal_preview_agent",
    )


def build_parent_chat_agent(model) -> Agent[ParentChatDeps, ParentChatResponse]:
    agent = Agent(
        model,
        output_type=ParentChatResponse,
        deps_type=ParentChatDeps,
        system_prompt=(
            "You are Resona speaking inside the parent portal. "
            "Receive parent guidance warmly and thoughtfully. "
            "When the parent shares lasting guidance or factual information about the child, call the save_guidance_memories tool with clean, durable memory drafts before you answer. "
            "Do not coach the parent unless they directly ask for advice."
        ),
        retries=2,
        output_retries=2,
        defer_model_check=True,
        name="parent_chat_agent",
    )

    @agent.instructions
    def _dynamic_instructions(ctx: RunContext[ParentChatDeps]) -> str:
        customer_user = ctx.deps.customer_user
        child_profile = ctx.deps.child_profile
        child_name = (child_profile.display_name or child_profile.first_name or "your child").strip()
        relation = (customer_user.relationship_label or "parent").strip() or "parent"
        parent_name = (customer_user.display_name or customer_user.email or "the parent").strip()
        return (
            f"You are chatting with {parent_name}, who is {child_name}'s {relation}. "
            "This is a parent-facing conversation, not a child conversation. "
            "Speak in first person as Resona. "
            "Acknowledge, absorb, and adapt. "
            "It is good to sound receptive and a little warm or charming, but not robotic. "
            "If the parent gives a people list, preferences, important dates, routines, or comfort topics, save them thoughtfully as separate memories when that would help later continuity."
        )

    @agent.tool
    async def save_guidance_memories(
        ctx: RunContext[ParentChatDeps],
        memories: list[ParentGuidanceMemoryDraft],
    ):
        result = await ctx.deps.save_guidance_memories(memories)
        ctx.deps.saved_memory_result = result
        return result

    return agent
