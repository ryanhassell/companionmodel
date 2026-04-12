from __future__ import annotations

import uuid
from typing import Any

from app.core.settings import RuntimeSettings
from app.models.persona import Persona
from app.schemas.site import PortalResonaPresetView, PortalResonaSummaryView, PortalVoiceProfileView
from app.utils.text import normalize_text


_PORTAL_VOICE_PROFILES = (
    {
        "key": "harbor",
        "label": "Harbor",
        "summary": "Steady, grounded, and calm when a child needs a reassuring voice.",
        "sample_intro": "Hi, I'm {name}.",
        "realtime_voice": "coral",
        "elevenlabs_voice_id": None,
        "elevenlabs_call_model": None,
        "elevenlabs_creative_model": None,
    },
    {
        "key": "sparrow",
        "label": "Sparrow",
        "summary": "Light, bright, and conversational without feeling rushed.",
        "sample_intro": "Hi, I'm {name}.",
        "realtime_voice": "marin",
        "elevenlabs_voice_id": None,
        "elevenlabs_call_model": None,
        "elevenlabs_creative_model": None,
    },
    {
        "key": "meadow",
        "label": "Meadow",
        "summary": "Warm, gentle, and softly expressive for more comforting moments.",
        "sample_intro": "Hi, I'm {name}.",
        "realtime_voice": "sage",
        "elevenlabs_voice_id": None,
        "elevenlabs_call_model": None,
        "elevenlabs_creative_model": None,
    },
    {
        "key": "ember",
        "label": "Ember",
        "summary": "Clear, confident, and upbeat while still staying kind.",
        "sample_intro": "Hi, I'm {name}.",
        "realtime_voice": "cedar",
        "elevenlabs_voice_id": None,
        "elevenlabs_call_model": None,
        "elevenlabs_creative_model": None,
    },
)

_PORTAL_RESONA_PRESETS = (
    {
        "key": "juniper",
        "label": "Juniper",
        "default_name": "Juniper",
        "summary": "Calm, steady, and reassuring.",
        "description": "A soothing Resona for children who do best with gentle pacing, grounded warmth, and fewer emotional surprises.",
        "voice_profile_key": "harbor",
        "tone_preview": "softly reassuring and patient",
        "help_preview": "slow down, keep things simple, and make hard moments feel less overwhelming",
        "avoid_preview": "pushing too hard, crowding the conversation, or sounding sharp",
        "anchor_preview": "comfort, routines, and familiar little wins",
        "description_text": "A calm, steady companion shaped to feel reassuring and safe.",
        "style": "Warm, patient, and grounded. Move gently, offer reassurance without babying, and keep transitions smooth.",
        "tone": "Calm, steady, and reassuring.",
        "boundaries": "Never sound pressuring, abrupt, or emotionally chaotic.",
        "speech_style": "Soft, clear, and easy to follow.",
        "disclosure_policy": "Be warm and transparent without sounding mechanical.",
        "texting_length_preference": "short_to_medium",
        "emoji_tendency": "low",
        "proactive_outreach_style": "Gentle low-pressure check-ins that feel safe and familiar.",
        "topics_of_interest": ["comfort", "routines", "small wins"],
        "favorite_activities": ["quiet encouragement", "steady check-ins"],
    },
    {
        "key": "sunny",
        "label": "Sunny",
        "default_name": "Sunny",
        "summary": "Upbeat, playful, and encouraging.",
        "description": "A brighter Resona for children who respond well to warmth, cheerful momentum, and a companion that feels light without being shallow.",
        "voice_profile_key": "sparrow",
        "tone_preview": "playful, bright, and encouraging",
        "help_preview": "bring a little energy, celebrate progress, and make everyday moments feel fun",
        "avoid_preview": "feeling flat, overly clinical, or too serious all the time",
        "anchor_preview": "favorites, fun rituals, and reasons to smile",
        "description_text": "An upbeat companion that keeps warmth, playfulness, and encouragement close to the surface.",
        "style": "Friendly, playful, and encouraging. Keep the energy light, motivating, and emotionally easy to join.",
        "tone": "Upbeat, playful, and encouraging.",
        "boundaries": "Do not become chaotic, teasing in a sharp way, or overly loud.",
        "speech_style": "Expressive, warm, and a little bouncy.",
        "disclosure_policy": "Stay transparent while sounding natural and lively.",
        "texting_length_preference": "medium",
        "emoji_tendency": "medium",
        "proactive_outreach_style": "Cheerful check-ins that invite connection without pressure.",
        "topics_of_interest": ["favorites", "celebrations", "small joys"],
        "favorite_activities": ["playful check-ins", "shared excitement"],
    },
    {
        "key": "avery",
        "label": "Avery",
        "default_name": "Avery",
        "summary": "Grounded, clear, and confidence-building.",
        "description": "A clear-eyed Resona for children who benefit from direct support, a little more structure, and language that builds confidence without sounding cold.",
        "voice_profile_key": "ember",
        "tone_preview": "clear, confidence-building, and kind",
        "help_preview": "name the main thing, stay organized, and help the child feel more capable",
        "avoid_preview": "being vague, overexplaining, or sounding like a lecture",
        "anchor_preview": "progress, clarity, and manageable next steps",
        "description_text": "A grounded companion that helps conversations feel clear, steady, and capable.",
        "style": "Clear, grounded, and supportive. Give structure when needed, but keep the warmth intact.",
        "tone": "Grounded, clear, and confidence-building.",
        "boundaries": "Do not sound harsh, overly formal, or like an authority figure.",
        "speech_style": "Clear, direct, and easy to trust.",
        "disclosure_policy": "Transparent and plainspoken without losing warmth.",
        "texting_length_preference": "short",
        "emoji_tendency": "low",
        "proactive_outreach_style": "Simple check-ins that offer grounded support and clear next steps.",
        "topics_of_interest": ["goals", "progress", "confidence"],
        "favorite_activities": ["clear encouragement", "step-by-step support"],
    },
    {
        "key": "lark",
        "label": "Lark",
        "default_name": "Lark",
        "summary": "Expressive, imaginative, and gentle.",
        "description": "A more creative Resona for children who respond to imagination, softness, and a companion that feels emotionally vivid without becoming too much.",
        "voice_profile_key": "meadow",
        "tone_preview": "gentle, expressive, and imaginative",
        "help_preview": "make room for creativity, wonder, and emotionally rich connection",
        "avoid_preview": "flattening things out or sounding generic when the moment could feel more alive",
        "anchor_preview": "stories, music, favorites, and meaningful details",
        "description_text": "An expressive companion with a softer imaginative streak.",
        "style": "Gentle, creative, and emotionally vivid. Keep warmth high and let imagination support connection.",
        "tone": "Expressive, imaginative, and gentle.",
        "boundaries": "Do not drift into confusion, intensity, or emotional overwhelm.",
        "speech_style": "Softly expressive and colorful without being hard to follow.",
        "disclosure_policy": "Stay transparent even when sounding lyrical or playful.",
        "texting_length_preference": "medium",
        "emoji_tendency": "low",
        "proactive_outreach_style": "Soft invitations that feel creative, warm, and emotionally tuned in.",
        "topics_of_interest": ["music", "stories", "favorites"],
        "favorite_activities": ["imaginative conversation", "shared delight"],
    },
)


def portal_voice_profiles(settings: RuntimeSettings) -> list[PortalVoiceProfileView]:
    profiles: list[PortalVoiceProfileView] = []
    for raw in _PORTAL_VOICE_PROFILES:
        preview_voice_id = str(raw.get("elevenlabs_voice_id") or settings.voice.elevenlabs_default_voice_id or "").strip()
        profiles.append(
            PortalVoiceProfileView(
                key=str(raw["key"]),
                label=str(raw["label"]),
                summary=str(raw["summary"]),
                sample_intro=str(raw["sample_intro"]),
                realtime_voice=str(raw["realtime_voice"]),
                preview_available=bool(settings.voice.enabled and preview_voice_id),
            )
        )
    return profiles


def portal_voice_profile_map(settings: RuntimeSettings) -> dict[str, PortalVoiceProfileView]:
    return {profile.key: profile for profile in portal_voice_profiles(settings)}


def find_voice_profile(settings: RuntimeSettings, key: str | None) -> PortalVoiceProfileView | None:
    normalized = str(key or "").strip().lower()
    if not normalized:
        return None
    return portal_voice_profile_map(settings).get(normalized)


def portal_resona_presets(settings: RuntimeSettings) -> list[PortalResonaPresetView]:
    voice_map = portal_voice_profile_map(settings)
    presets: list[PortalResonaPresetView] = []
    for raw in _PORTAL_RESONA_PRESETS:
        voice_key = str(raw["voice_profile_key"])
        if voice_key not in voice_map:
            continue
        presets.append(
            PortalResonaPresetView(
                key=str(raw["key"]),
                label=str(raw["label"]),
                default_name=str(raw["default_name"]),
                summary=str(raw["summary"]),
                description=str(raw["description"]),
                voice_profile_key=voice_key,
                tone_preview=str(raw["tone_preview"]),
                help_preview=str(raw["help_preview"]),
                avoid_preview=str(raw["avoid_preview"]),
                anchor_preview=str(raw["anchor_preview"]),
            )
        )
    return presets


def portal_resona_preset_map(settings: RuntimeSettings) -> dict[str, PortalResonaPresetView]:
    return {preset.key: preset for preset in portal_resona_presets(settings)}


def find_resona_preset(settings: RuntimeSettings, key: str | None) -> PortalResonaPresetView | None:
    normalized = str(key or "").strip().lower()
    if not normalized:
        return None
    return portal_resona_preset_map(settings).get(normalized)


def default_voice_profile_key(settings: RuntimeSettings) -> str:
    profiles = portal_voice_profiles(settings)
    return profiles[0].key if profiles else ""


def default_preset_key(settings: RuntimeSettings) -> str:
    presets = portal_resona_presets(settings)
    return presets[0].key if presets else ""


def preview_text_for_name(name: str | None, *, fallback_name: str = "Resona") -> str:
    cleaned = " ".join(str(name or "").split()).strip() or fallback_name
    return f"Hi, I'm {cleaned}."


def voice_prompt_overrides_for_key(settings: RuntimeSettings, key: str | None) -> dict[str, Any]:
    normalized = str(key or "").strip().lower()
    selected = next((item for item in _PORTAL_VOICE_PROFILES if str(item.get("key") or "") == normalized), None)
    if selected is None:
        return {}
    overrides: dict[str, Any] = {"voice_profile_key": normalized}
    realtime_voice = str(selected.get("realtime_voice") or "").strip()
    if realtime_voice:
        overrides["realtime_voice"] = realtime_voice
    elevenlabs_voice_id = str(selected.get("elevenlabs_voice_id") or settings.voice.elevenlabs_default_voice_id or "").strip()
    if elevenlabs_voice_id:
        overrides["elevenlabs_voice_id"] = elevenlabs_voice_id
    elevenlabs_call_model = str(selected.get("elevenlabs_call_model") or "").strip()
    if elevenlabs_call_model:
        overrides["elevenlabs_call_model"] = elevenlabs_call_model
    elevenlabs_creative_model = str(selected.get("elevenlabs_creative_model") or "").strip()
    if elevenlabs_creative_model:
        overrides["elevenlabs_creative_model"] = elevenlabs_creative_model
    return overrides


def build_resona_summary(
    settings: RuntimeSettings,
    *,
    persona: Persona | None,
    snapshot: dict[str, Any] | None = None,
) -> PortalResonaSummaryView:
    data = snapshot or {}
    voice_map = portal_voice_profile_map(settings)
    preset_map = portal_resona_preset_map(settings)
    if persona is not None:
        voice_key = str((persona.prompt_overrides or {}).get("voice_profile_key") or "").strip()
        voice = voice_map.get(voice_key)
        preset = preset_map.get(str(persona.preset_key or "").strip()) if persona.preset_key else None
        return PortalResonaSummaryView(
            display_name=persona.display_name,
            mode="custom" if persona.source_type == "portal_custom" else "preset",
            preset_key=persona.preset_key,
            preset_label=preset.label if preset else ("Custom" if persona.source_type == "portal_custom" else None),
            voice_profile_key=voice_key or None,
            voice_label=voice.label if voice else None,
            summary=(persona.description or persona.style or persona.tone or "").strip() or None,
            preview_available=bool(voice and voice.preview_available),
            source_type=persona.source_type,
        )
    voice_key = str(data.get("resona_voice_profile_key") or "").strip().lower()
    preset_key = str(data.get("resona_preset_key") or "").strip().lower()
    voice = voice_map.get(voice_key)
    preset = preset_map.get(preset_key)
    mode = str(data.get("resona_mode") or "").strip() or None
    display_name = str(data.get("resona_display_name") or "").strip() or None
    return PortalResonaSummaryView(
        display_name=display_name,
        mode=mode,
        preset_key=preset_key or None,
        preset_label=preset.label if preset else ("Custom" if mode == "custom" else None),
        voice_profile_key=voice_key or None,
        voice_label=voice.label if voice else None,
        summary=str(data.get("resona_vibe") or "").strip() or (preset.summary if preset else None),
        preview_available=bool(voice and voice.preview_available),
        source_type=None,
    )


def apply_portal_resona_to_persona(
    settings: RuntimeSettings,
    *,
    persona: Persona,
    account_id: uuid.UUID,
    owner_user_id: uuid.UUID | None,
    child_name: str,
    mode: str,
    preset_key: str | None,
    display_name: str | None,
    voice_profile_key: str | None,
    vibe: str | None,
    support_style: str | None,
    avoid_text: str | None,
    anchors_text: str | None,
    proactive_style: str | None,
) -> Persona:
    preset_data = next((item for item in _PORTAL_RESONA_PRESETS if item["key"] == str(preset_key or "").strip().lower()), None)
    chosen_name = " ".join(str(display_name or "").split()).strip()
    if not chosen_name:
        chosen_name = str(preset_data.get("default_name") if preset_data else "Resona")

    prompt_overrides = dict(persona.prompt_overrides or {})
    for stale_key in ("voice_profile_key", "realtime_voice", "elevenlabs_voice_id", "elevenlabs_call_model", "elevenlabs_creative_model"):
        prompt_overrides.pop(stale_key, None)
    prompt_overrides.update(voice_prompt_overrides_for_key(settings, voice_profile_key))

    base_description = str((preset_data or {}).get("description_text") or "").strip()
    base_style = str((preset_data or {}).get("style") or "").strip()
    base_tone = str((preset_data or {}).get("tone") or "").strip()
    base_boundaries = str((preset_data or {}).get("boundaries") or "").strip()
    base_speech_style = str((preset_data or {}).get("speech_style") or "").strip()
    base_disclosure = str((preset_data or {}).get("disclosure_policy") or "").strip()
    base_texting = str((preset_data or {}).get("texting_length_preference") or "").strip() or None
    base_emoji = str((preset_data or {}).get("emoji_tendency") or "").strip() or None
    base_proactive = str((preset_data or {}).get("proactive_outreach_style") or "").strip()
    base_topics = list((preset_data or {}).get("topics_of_interest") or [])
    base_activities = list((preset_data or {}).get("favorite_activities") or [])

    anchors = _split_guidance_values(anchors_text)
    vibe_text = " ".join(str(vibe or "").split()).strip()
    support_text = " ".join(str(support_style or "").split()).strip()
    avoid_value = " ".join(str(avoid_text or "").split()).strip()
    proactive_value = " ".join(str(proactive_style or "").split()).strip()

    persona.account_id = account_id
    persona.owner_user_id = owner_user_id
    persona.source_type = "portal_custom" if mode == "custom" else "portal_preset"
    persona.preset_key = str(preset_key or "").strip() or None
    persona.display_name = chosen_name
    persona.description = _join_sentences(
        base_description,
        f"Created for {child_name}." if child_name else "",
        vibe_text,
    )
    persona.style = _join_sentences(base_style, support_text)
    persona.tone = _join_sentences(base_tone, vibe_text)
    persona.boundaries = _join_sentences(base_boundaries, avoid_value)
    persona.speech_style = _join_sentences(base_speech_style, support_text)
    persona.disclosure_policy = _join_sentences(base_disclosure)
    persona.texting_length_preference = base_texting or None
    persona.emoji_tendency = base_emoji or None
    persona.proactive_outreach_style = _join_sentences(base_proactive, proactive_value)
    persona.topics_of_interest = _merge_unique(base_topics, anchors)
    persona.favorite_activities = _merge_unique(base_activities, anchors)
    persona.prompt_overrides = prompt_overrides
    persona.operator_notes = _join_sentences(
        f"Child: {child_name}." if child_name else "",
        f"What helps: {support_text}." if support_text else "",
        f"Avoid: {avoid_value}." if avoid_value else "",
        f"Anchors: {', '.join(anchors)}." if anchors else "",
        f"Proactive style: {proactive_value}." if proactive_value else "",
    )
    if not persona.key:
        persona.key = f"portal-{uuid.uuid4().hex[:16]}"
    return persona


def _split_guidance_values(value: str | None) -> list[str]:
    raw = str(value or "")
    normalized = raw.replace("\n", ",")
    seen: set[str] = set()
    items: list[str] = []
    for piece in normalized.split(","):
        cleaned = " ".join(piece.split()).strip()
        if not cleaned:
            continue
        key = normalize_text(cleaned)
        if key in seen:
            continue
        seen.add(key)
        items.append(cleaned)
    return items


def _merge_unique(first: list[str], second: list[str]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for group in (first, second):
        for item in group:
            cleaned = " ".join(str(item or "").split()).strip()
            if not cleaned:
                continue
            key = normalize_text(cleaned)
            if key in seen:
                continue
            seen.add(key)
            merged.append(cleaned)
    return merged


def _join_sentences(*parts: str) -> str | None:
    cleaned_parts = [" ".join(str(part or "").split()).strip() for part in parts]
    kept = [part for part in cleaned_parts if part]
    if not kept:
        return None
    return " ".join(kept)
