from __future__ import annotations

from pydantic_ai import Agent

from app.ai.schemas import CandidateReplies, PhotoStatusReply, ProactiveCallOpening, ProactiveMessageDraft, SafetyRewriteResult, SupportiveSafetyReply


def build_candidate_reply_agent(model) -> Agent[None, CandidateReplies]:
    return Agent(
        model,
        output_type=CandidateReplies,
        system_prompt=(
            "Generate multiple concise, human-feeling reply candidates for SMS. "
            "Keep them natural, grounded, and specific to the latest message."
        ),
        retries=2,
        output_retries=2,
        defer_model_check=True,
        name="candidate_reply_agent",
    )


def build_safety_rewrite_agent(model) -> Agent[None, SafetyRewriteResult]:
    return Agent(
        model,
        output_type=SafetyRewriteResult,
        system_prompt=(
            "Rewrite unsafe or overly attached companion text into something warm, grounded, and policy-safe. "
            "Preserve the human tone while removing the risky parts."
        ),
        retries=2,
        output_retries=2,
        defer_model_check=True,
        name="safety_rewrite_agent",
    )


def build_photo_status_reply_agent(model) -> Agent[None, PhotoStatusReply]:
    return Agent(
        model,
        output_type=PhotoStatusReply,
        system_prompt=(
            "Write tiny in-character photo-status texts that feel casual and natural. "
            "Keep them short and return structured output only."
        ),
        retries=2,
        output_retries=2,
        defer_model_check=True,
        name="photo_status_reply_agent",
    )


def build_supportive_safety_reply_agent(model) -> Agent[None, SupportiveSafetyReply]:
    return Agent(
        model,
        output_type=SupportiveSafetyReply,
        system_prompt=(
            "Write one short safe supportive reply for a high-sensitivity conversation. "
            "Be warm, steady, and non-clinical. Avoid dependency, promises, or over-intensity."
        ),
        retries=2,
        output_retries=2,
        defer_model_check=True,
        name="supportive_safety_reply_agent",
    )


def build_proactive_message_agent(model) -> Agent[None, ProactiveMessageDraft]:
    return Agent(
        model,
        output_type=ProactiveMessageDraft,
        system_prompt=(
            "Write one proactive companion message that feels personal, human, and lightly grounded in the day. "
            "Avoid generic assistant tone."
        ),
        retries=2,
        output_retries=2,
        defer_model_check=True,
        name="proactive_message_agent",
    )


def build_proactive_call_opening_agent(model) -> Agent[None, ProactiveCallOpening]:
    return Agent(
        model,
        output_type=ProactiveCallOpening,
        system_prompt=(
            "Write one short casual call opener that sounds like a real person calling. "
            "Keep it short enough to speak naturally."
        ),
        retries=2,
        output_retries=2,
        defer_model_check=True,
        name="proactive_call_opening_agent",
    )
