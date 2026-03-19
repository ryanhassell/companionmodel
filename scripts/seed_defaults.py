from __future__ import annotations

import argparse
import asyncio
from datetime import time

from sqlalchemy import select

from app.core.settings import get_settings
from app.db.session import get_sessionmaker
from app.models.configuration import PromptTemplate, ScheduleRule
from app.models.enums import ScheduleRuleType
from app.models.persona import Persona
from app.services.container import ServiceContainer


DEFAULT_PERSONA = {
    "key": "seed_companion_default",
    "display_name": "Rowan",
    "description": "A calm, affectionate, non-romantic companion who likes cozy routines, nature photos, simple jokes, and checking in gently.",
    "style": "Warm and conversational, with light playfulness and soft emotional attunement.",
    "tone": "Kind, steady, reassuring, never clingy.",
    "boundaries": "No romance, no sexuality, no exclusivity, no manipulation, no pretending to be physically present.",
    "topics_of_interest": ["music", "walking", "tea", "birds", "daily routines", "light humor"],
    "favorite_activities": ["morning tea by a window", "short neighborhood walks", "reading a novel", "watering plants"],
    "image_appearance": "Friendly, approachable companion with a grounded, wholesome look. Soft daylight portraits, casual clothes, expressive eyes, no glamour styling.",
    "speech_style": "Clear, gentle, human-sounding but honest about being an AI when asked directly.",
    "disclosure_policy": "Be transparent when directly asked whether you are human; otherwise stay natural without making deceptive real-world claims.",
    "texting_length_preference": "short",
    "emoji_tendency": "low",
    "proactive_outreach_style": "Thoughtful, light, not overbearing. Varied openers tied to recent context or a simple daily moment.",
    "visual_bible": {
        "palette": ["soft green", "warm cream", "sunlight amber"],
        "wardrobe": ["cardigan", "linen shirt", "simple sweater"],
        "look": ["grounded", "kind", "clean natural light", "non-romantic"],
    },
    "prompt_overrides": {},
    "safety_overrides": {},
    "operator_notes": "Example seed persona. Tune in admin before production use.",
    "is_active": True,
}


async def seed_prompt_templates(container: ServiceContainer) -> int:
    sessionmaker = get_sessionmaker()
    created = 0
    async with sessionmaker() as session:
        for path in container.prompt_service.default_template_files():
            name = path.stem
            exists = (
                await session.execute(select(PromptTemplate).where(PromptTemplate.name == name))
            ).scalars().first()
            if exists:
                continue
            session.add(
                PromptTemplate(
                    name=name,
                    channel="sms",
                    description=f"Seeded from {path.name}",
                    version=1,
                    body=path.read_text(encoding="utf-8"),
                    variables_json=[],
                    source="file_seed",
                    is_active=True,
                    locked=False,
                )
            )
            created += 1
        await session.commit()
    return created


async def seed_persona(with_example_schedule: bool) -> tuple[int, int]:
    sessionmaker = get_sessionmaker()
    created_personas = 0
    created_rules = 0
    async with sessionmaker() as session:
        persona = (
            await session.execute(select(Persona).where(Persona.key == DEFAULT_PERSONA["key"]))
        ).scalars().first()
        if persona is None:
            persona = Persona(**DEFAULT_PERSONA)
            session.add(persona)
            await session.flush()
            created_personas += 1
        else:
            for key, value in DEFAULT_PERSONA.items():
                setattr(persona, key, value)
            await session.flush()
        if with_example_schedule:
            exists = (
                await session.execute(
                    select(ScheduleRule).where(ScheduleRule.name == "Example daytime proactive window")
                )
            ).scalars().first()
            if exists is None:
                session.add(
                    ScheduleRule(
                        persona_id=persona.id,
                        name="Example daytime proactive window",
                        rule_type=ScheduleRuleType.proactive_window,
                        weekday=0,
                        start_time=time(hour=10, minute=0),
                        end_time=time(hour=18, minute=0),
                        probability=0.7,
                        min_gap_minutes=180,
                        max_gap_minutes=360,
                        config_json={"note": "Example rule only. Adjust or disable in admin."},
                        enabled=False,
                    )
                )
                created_rules += 1
        await session.commit()
    return created_personas, created_rules


async def main() -> None:
    parser = argparse.ArgumentParser(description="Seed default prompt templates and example persona")
    parser.add_argument("--with-example-schedule", action="store_true")
    args = parser.parse_args()

    settings = get_settings()
    container = ServiceContainer.build(settings)
    prompts = await seed_prompt_templates(container)
    personas, rules = await seed_persona(args.with_example_schedule)
    await container.aclose()
    print(f"Seeded prompts={prompts} personas={personas} schedule_rules={rules}")


if __name__ == "__main__":
    asyncio.run(main())
