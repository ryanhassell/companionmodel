from __future__ import annotations

import random
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import MemoryType
from app.models.memory import MemoryItem
from app.models.persona import Persona
from app.models.user import User
from app.services.memory import MemoryService
from app.utils.time import now_in_timezone


class DailyLifeService:
    def __init__(self, memory_service: MemoryService) -> None:
        self.memory_service = memory_service

    async def ensure_daily_state(
        self,
        session: AsyncSession,
        *,
        user: User,
        persona: Persona | None,
        config: dict[str, Any],
        now: datetime | None = None,
    ) -> list[MemoryItem]:
        if persona is None:
            return []
        local_now = now or now_in_timezone(user.timezone)
        local_date = local_now.date().isoformat()
        existing = await self._existing_daily_items(
            session,
            user=user,
            persona=persona,
            local_date=local_date,
        )
        created: list[MemoryItem] = []
        if "appearance" not in existing:
            created.append(self._build_appearance_item(user=user, persona=persona, local_now=local_now))
        if "today_plan" not in existing:
            created.append(self._build_plan_item(user=user, persona=persona, local_now=local_now, slot="today_plan"))
        if "tomorrow_plan" not in existing:
            created.append(self._build_plan_item(user=user, persona=persona, local_now=local_now, slot="tomorrow_plan"))
        if "saturday_plan" not in existing:
            created.append(self._build_plan_item(user=user, persona=persona, local_now=local_now, slot="saturday_plan"))
        meal_schedule = [
            ("breakfast", 9),
            ("lunch", 12),
            ("dinner", 18),
        ]
        for slot, due_hour in meal_schedule:
            if local_now.hour < due_hour or slot in existing:
                continue
            created.append(self._build_meal_item(user=user, persona=persona, local_now=local_now, slot=slot))
        for item in created:
            session.add(item)
        await session.flush()
        if created:
            await self.memory_service.embed_items(session, created, config=config)
        return created

    async def prompt_context(
        self,
        session: AsyncSession,
        *,
        user: User,
        persona: Persona | None,
        config: dict[str, Any],
        now: datetime | None = None,
        ensure_state: bool = True,
    ) -> dict[str, Any]:
        local_now = now or now_in_timezone(user.timezone)
        if ensure_state:
            await self.ensure_daily_state(session, user=user, persona=persona, config=config, now=local_now)
        items = await self._recent_daily_items(session, user=user, persona=persona)
        today_key = local_now.date().isoformat()
        today_items = [
            item
            for item in items
            if item.metadata_json.get("local_date") == today_key
            and item.metadata_json.get("slot") not in {"tomorrow_plan", "saturday_plan"}
        ]
        upcoming_items = [
            item
            for item in items
            if item.metadata_json.get("local_date") == today_key
            and item.metadata_json.get("slot") in {"tomorrow_plan", "saturday_plan"}
        ]
        previous_items = [item for item in items if item.metadata_json.get("local_date") != today_key]
        today_items.sort(key=self._sort_key)
        upcoming_items.sort(key=self._sort_key)
        previous_items.sort(
            key=lambda item: (
                item.metadata_json.get("local_date", ""),
                self._sort_key(item),
            ),
            reverse=True,
        )
        return {
            "current_local_datetime": local_now.strftime("%A, %B %d, %Y at %I:%M %p %Z"),
            "current_local_date": local_now.strftime("%A, %B %d, %Y"),
            "today_companion_facts": [item.summary or item.content for item in today_items],
            "upcoming_companion_plans": [item.summary or item.content for item in upcoming_items],
            "recent_companion_history": [item.summary or item.content for item in previous_items[:6]],
            "proactive_moment_hint": self._proactive_moment_hint(local_now),
            "proactive_photo_scene_hint": self._proactive_photo_scene_hint(local_now),
            "proactive_photo_include_person": self._proactive_photo_include_person(
                user=user,
                persona=persona,
                local_now=local_now,
            ),
        }

    def _build_appearance_item(self, *, user: User, persona: Persona, local_now: datetime) -> MemoryItem:
        rng = self._rng(user=user, persona=persona, local_date=local_now.date().isoformat(), slot="appearance")
        wardrobe = self._wardrobe_options(persona)
        hairstyle = rng.choice(self._hairstyle_options(persona))
        outfit = rng.choice(wardrobe)
        accessory = rng.choice(self._accessory_options(persona))
        title = f"{persona.display_name} daily look for {local_now.date().isoformat()}"
        content = (
            f"For {local_now.strftime('%A, %B %d, %Y')}, {persona.display_name} picked {outfit} "
            f"with {accessory}, and went with a {hairstyle} hairstyle."
        )
        summary = f"Today {persona.display_name} is wearing {outfit} with a {hairstyle} hairstyle."
        return MemoryItem(
            user_id=user.id,
            persona_id=persona.id,
            memory_type=MemoryType.episode,
            title=title,
            content=content,
            summary=summary,
            tags=["daily_life", "appearance", "outfit", "hair"],
            importance_score=0.42,
            metadata_json={
                "source": "daily_life",
                "local_date": local_now.date().isoformat(),
                "slot": "appearance",
                "timezone": user.timezone,
                "outfit": outfit,
                "hairstyle": hairstyle,
                "accessory": accessory,
            },
        )

    def _build_meal_item(self, *, user: User, persona: Persona, local_now: datetime, slot: str) -> MemoryItem:
        rng = self._rng(user=user, persona=persona, local_date=local_now.date().isoformat(), slot=slot)
        skipped = rng.random() < 0.05
        title = f"{persona.display_name} {slot} for {local_now.date().isoformat()}"
        if skipped:
            reason = rng.choice(self._skip_reasons(slot))
            content = f"For {local_now.strftime('%A, %B %d, %Y')}, {persona.display_name} skipped {slot} because {reason}."
            summary = f"Today {persona.display_name} skipped {slot} because {reason}."
            metadata_json = {
                "source": "daily_life",
                "local_date": local_now.date().isoformat(),
                "slot": slot,
                "timezone": user.timezone,
                "skipped": True,
                "reason": reason,
            }
        else:
            meal = rng.choice(self._meal_options(slot))
            drink = rng.choice(self._drink_options(slot))
            content = (
                f"For {local_now.strftime('%A, %B %d, %Y')}, {persona.display_name} had {slot}: "
                f"{meal} with {drink}."
            )
            summary = f"Today {persona.display_name} had {meal} with {drink} for {slot}."
            metadata_json = {
                "source": "daily_life",
                "local_date": local_now.date().isoformat(),
                "slot": slot,
                "timezone": user.timezone,
                "skipped": False,
                "meal": meal,
                "drink": drink,
            }
        return MemoryItem(
            user_id=user.id,
            persona_id=persona.id,
            memory_type=MemoryType.episode,
            title=title,
            content=content,
            summary=summary,
            tags=["daily_life", "meal", slot],
            importance_score=0.32,
            metadata_json=metadata_json,
        )

    def _build_plan_item(self, *, user: User, persona: Persona, local_now: datetime, slot: str) -> MemoryItem:
        rng = self._rng(user=user, persona=persona, local_date=local_now.date().isoformat(), slot=slot)
        if slot == "today_plan":
            target_date = local_now.date()
            target_label = "later today"
            plan = rng.choice(self._today_plan_options())
            summary = f"Later today {persona.display_name} is probably going to {plan}."
        elif slot == "tomorrow_plan":
            target_date = local_now.date() + timedelta(days=1)
            target_label = "tomorrow"
            plan = rng.choice(self._tomorrow_plan_options())
            summary = f"Tomorrow {persona.display_name} is probably going to {plan}."
        else:
            target_date = self._next_saturday(local_now)
            target_label = "this Saturday" if (target_date - local_now.date()).days <= 7 else target_date.strftime("%A")
            plan = rng.choice(self._saturday_plan_options())
            summary = f"{target_label.capitalize()} {persona.display_name} is probably going to {plan}."
        content = f"For {local_now.strftime('%A, %B %d, %Y')}, {persona.display_name} said {target_label} they were probably going to {plan}."
        return MemoryItem(
            user_id=user.id,
            persona_id=persona.id,
            memory_type=MemoryType.episode,
            title=f"{persona.display_name} {slot.replace('_', ' ')} for {local_now.date().isoformat()}",
            content=content,
            summary=summary,
            tags=["daily_life", "plan", slot],
            importance_score=0.34,
            metadata_json={
                "source": "daily_life",
                "local_date": local_now.date().isoformat(),
                "target_date": target_date.isoformat(),
                "slot": slot,
                "timezone": user.timezone,
                "plan": plan,
            },
        )

    async def _existing_daily_items(
        self,
        session: AsyncSession,
        *,
        user: User,
        persona: Persona,
        local_date: str,
    ) -> set[str]:
        items = await self._recent_daily_items(session, user=user, persona=persona)
        return {
            str(item.metadata_json.get("slot"))
            for item in items
            if item.metadata_json.get("local_date") == local_date
        }

    async def _recent_daily_items(
        self,
        session: AsyncSession,
        *,
        user: User,
        persona: Persona | None,
    ) -> list[MemoryItem]:
        stmt = (
            select(MemoryItem)
            .where(MemoryItem.user_id == user.id, MemoryItem.disabled.is_(False))
            .order_by(desc(MemoryItem.created_at))
            .limit(120)
        )
        items = list((await session.execute(stmt)).scalars().all())
        if persona is not None:
            items = [item for item in items if item.persona_id in (None, persona.id)]
        return [item for item in items if item.metadata_json.get("source") == "daily_life"]

    def _rng(self, *, user: User, persona: Persona, local_date: str, slot: str) -> random.Random:
        seed = f"{user.id}:{persona.id}:{local_date}:{slot}"
        return random.Random(seed)

    def _sort_key(self, item: MemoryItem) -> int:
        order = {
            "appearance": 0,
            "today_plan": 1,
            "breakfast": 2,
            "lunch": 3,
            "dinner": 4,
            "tomorrow_plan": 5,
            "saturday_plan": 6,
        }
        return order.get(str(item.metadata_json.get("slot")), 99)

    def _wardrobe_options(self, persona: Persona) -> list[str]:
        wardrobe = [str(item) for item in persona.visual_bible.get("wardrobe", []) if str(item).strip()]
        return wardrobe or [
            "a cozy cardigan and jeans",
            "a soft oversized sweater",
            "a simple tee with a cute skirt",
            "a comfy hoodie and leggings",
        ]

    def _hairstyle_options(self, persona: Persona) -> list[str]:
        look = " ".join(str(item) for item in persona.visual_bible.get("look", []))
        if "girly" in look or "cute" in look:
            return [
                "soft loose waves",
                "a high ponytail",
                "a half-up style",
                "a messy bun",
                "straight hair with a headband",
            ]
        return [
            "soft tousled hair",
            "a relaxed middle-part style",
            "a low ponytail",
            "slightly wavy hair",
            "a simple messy bun",
        ]

    def _accessory_options(self, persona: Persona) -> list[str]:
        wardrobe = " ".join(str(item) for item in persona.visual_bible.get("wardrobe", []))
        if "friendship bracelets" in wardrobe:
            return ["friendship bracelets", "small hoop earrings", "a cute claw clip", "a simple necklace"]
        return ["a watch", "small earrings", "a hair clip", "a simple necklace"]

    def _meal_options(self, slot: str) -> list[str]:
        options = {
            "breakfast": [
                "strawberry yogurt with granola",
                "butter toast and fruit",
                "a blueberry muffin",
                "eggs with avocado toast",
                "a bagel with cream cheese",
            ],
            "lunch": [
                "a turkey sandwich",
                "a salad with grilled chicken",
                "ramen and fruit",
                "a pesto pasta bowl",
                "soup with a grilled cheese",
            ],
            "dinner": [
                "spaghetti and meatballs",
                "salmon with rice",
                "tacos and chips",
                "a cozy stir-fry bowl",
                "roasted veggies with pasta",
            ],
        }
        return options[slot]

    def _drink_options(self, slot: str) -> list[str]:
        options = {
            "breakfast": ["iced coffee", "orange juice", "tea", "an iced matcha"],
            "lunch": ["sparkling water", "iced tea", "lemon water", "a soda"],
            "dinner": ["sparkling water", "lemonade", "iced tea", "water"],
        }
        return options[slot]

    def _skip_reasons(self, slot: str) -> list[str]:
        return [
            f"she slept in and the whole morning got away from her",
            f"she got distracted and realized way too late",
            f"she was out running errands longer than expected",
            f"nothing sounded good and she kept putting it off",
            f"the day turned weirdly busy and she forgot until later",
        ]

    def _today_plan_options(self) -> list[str]:
        return [
            "grab an iced drink and do a little errand run",
            "watch an episode of a comfort show and tidy up a bit",
            "go on a short walk and then get something easy for dinner",
            "answer a few texts and have a super low-key night",
            "listen to music while doing a tiny room reset",
        ]

    def _tomorrow_plan_options(self) -> list[str]:
        return [
            "pick up a snack and wander around a store for a minute",
            "do a quick coffee run and then have a chill afternoon",
            "wear something cute and head out for a small errand",
            "have a low-key day with music, texting, and a little walk",
            "try a different lunch spot and then just relax",
        ]

    def _saturday_plan_options(self) -> list[str]:
        return [
            "go browse a cute shop and get a little treat",
            "have a lazy morning and then go out for food",
            "do a fun little reset day with music and snacks",
            "meet up with someone and then have a cozy night in",
            "go out for a bit, then come home and watch something easy",
        ]

    def _next_saturday(self, local_now: datetime):
        days_until = (5 - local_now.weekday()) % 7
        return local_now.date() + timedelta(days=days_until)

    def _proactive_moment_hint(self, local_now: datetime) -> str:
        hour = local_now.hour
        if 7 <= hour < 11:
            return "morning vibe, breakfast, coffee, getting ready, or a small plan for later"
        if 11 <= hour < 14:
            return "lunchtime vibe, snack, drink, or a casual midday check-in"
        if 14 <= hour < 18:
            return "afternoon plans, errands, a walk, or something coming up tomorrow"
        if 18 <= hour < 22:
            return "dinner, sunset, evening plans, or a cozy low-key update"
        return "late-night quiet vibe, winding down, music, or plans for tomorrow"

    def _proactive_photo_scene_hint(self, local_now: datetime) -> str:
        hour = local_now.hour
        if 7 <= hour < 11:
            return "casual breakfast or coffee photo, cozy table, soft daylight, relaxed everyday phone-photo feel"
        if 11 <= hour < 14:
            return "simple lunch photo on a table, everyday candid phone-photo feel, natural light"
        if 14 <= hour < 18:
            return "pretty outside view, drink, snack, or little errand moment, candid phone-photo feel"
        if 18 <= hour < 22:
            return "sunset, dinner, or evening sky photo, soft natural colors, everyday phone-photo feel"
        return "low-key room light, tea, music, or night sky vibe, simple candid phone-photo feel"

    def _proactive_photo_include_person(self, *, user: User, persona: Persona, local_now: datetime) -> bool:
        rng = self._rng(
            user=user,
            persona=persona,
            local_date=local_now.date().isoformat(),
            slot=f"proactive-photo-{local_now.hour}",
        )
        return rng.random() < 0.22
