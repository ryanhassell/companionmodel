from __future__ import annotations

from pydantic_ai import Agent

from app.ai.schemas import SpeechDictionaryCandidate, VoiceCallScript, VoiceGreeting, VoiceSummary


def build_voice_script_agent(model) -> Agent[None, VoiceCallScript]:
    return Agent(
        model,
        output_type=VoiceCallScript,
        system_prompt=(
            "Write a spoken call script that feels natural, conversational, and emotionally believable."
        ),
        retries=2,
        output_retries=2,
        defer_model_check=True,
        name="voice_script_agent",
    )


def build_voice_greeting_agent(model) -> Agent[None, VoiceGreeting]:
    return Agent(
        model,
        output_type=VoiceGreeting,
        system_prompt=(
            "Write one very short spoken greeting line for a live call."
        ),
        retries=2,
        output_retries=2,
        defer_model_check=True,
        name="voice_greeting_agent",
    )


def build_voice_summary_agent(model) -> Agent[None, VoiceSummary]:
    return Agent(
        model,
        output_type=VoiceSummary,
        system_prompt=(
            "Summarize a phone call for internal memory in a compact, useful way."
        ),
        retries=2,
        output_retries=2,
        defer_model_check=True,
        name="voice_summary_agent",
    )


def build_speech_dictionary_candidate_agent(model) -> Agent[None, SpeechDictionaryCandidate]:
    return Agent(
        model,
        output_type=SpeechDictionaryCandidate,
        system_prompt=(
            "Infer whether a short unclear spoken phrase should be confirmed against a likely intended phrase. "
            "Only suggest a candidate when the guess is strong and context-aware."
        ),
        retries=2,
        output_retries=2,
        defer_model_check=True,
        name="speech_dictionary_candidate_agent",
    )
