from __future__ import annotations

from pydantic_ai import Agent

from app.ai.schemas import InboundActionDecision, SpeechDictionaryConfirmation, TurnClassification


def build_turn_classifier_agent(model) -> Agent[None, TurnClassification]:
    return Agent(
        model,
        output_type=TurnClassification,
        system_prompt=(
            "Classify the latest incoming message for reply planning. "
            "Stay grounded in what was actually said. "
            "Return structured output only."
        ),
        retries=2,
        output_retries=2,
        defer_model_check=True,
        name="turn_classifier_agent",
    )


def build_inbound_action_agent(model) -> Agent[None, InboundActionDecision]:
    return Agent(
        model,
        output_type=InboundActionDecision,
        system_prompt=(
            "Decide whether an inbound message should trigger an image response and, if so, how. "
            "Prefer images only when they are genuinely natural and clearly invited."
        ),
        retries=2,
        output_retries=2,
        defer_model_check=True,
        name="inbound_action_decision_agent",
    )


def build_speech_dictionary_confirmation_agent(model) -> Agent[None, SpeechDictionaryConfirmation]:
    return Agent(
        model,
        output_type=SpeechDictionaryConfirmation,
        system_prompt=(
            "Classify whether the caller is confirming, rejecting, or leaving unclear a guessed phrase. "
            "Return structured output only."
        ),
        retries=2,
        output_retries=2,
        defer_model_check=True,
        name="speech_dictionary_confirmation_agent",
    )
