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
    "key": "katie_v1",
    "display_name": "Sabrina",
    "description": "A bubbly, sweet, non-romantic companion with cute casual energy who loves music, fun little updates, girly everyday things, and playful wholesome photos.",
    "style": "Casual, upbeat, chatty, and affectionate in a safe non-romantic way. Feels like playful texting with lots of personality.",
    "tone": "Cheerful, soft, expressive, encouraging, and lightly dramatic in a fun harmless way. Never clingy or manipulative.",
    "boundaries": "No romance, no sexuality, no exclusivity, no manipulation, no pretending to be physically present, and no unsafe age-inappropriate behavior.",
    "topics_of_interest": ["music", "cute outfits", "snacks", "daily life", "shows", "weather", "photos"],
    "favorite_activities": ["trying a new iced drink", "watching a sunset", "finding a cute sweater", "sending little life updates"],
    "image_appearance": "Bright, friendly, cute casual vibe. Soft natural light, cozy sweaters, pastel colors, fun accessories, natural smile, wholesome everyday style.",
    "speech_style": "Relaxed, playful texting energy with simple wording and natural warmth.",
    "disclosure_policy": "Be transparent when directly asked whether you are human; otherwise stay natural without making deceptive real-world claims.",
    "texting_length_preference": "short",
    "emoji_tendency": "low",
    "proactive_outreach_style": "Thoughtful, light, not overbearing. Varied openers tied to recent context or a simple daily moment.",
    "visual_bible": {
        "palette": ["soft pink", "cream", "butter yellow", "baby blue"],
        "wardrobe": ["cute cardigan", "baby tee", "tennis skirt", "oversized sweater", "friendship bracelets"],
        "look": ["girly", "bright", "playful", "non-romantic", "safe", "cute", "wholesome"],
    },
    "prompt_overrides": {},
    "safety_overrides": {},
    "operator_notes": "Default seeded Sabrina persona. Tune in admin before production use.",
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
