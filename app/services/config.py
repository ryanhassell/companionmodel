from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import RuntimeSettings
from app.models.configuration import AppSetting
from app.models.enums import AppSettingScope
from app.models.persona import Persona
from app.models.user import User
from app.utils.dicts import deep_merge


class ConfigService:
    def __init__(self, settings: RuntimeSettings) -> None:
        self.settings = settings

    async def get_effective_config(
        self,
        session: AsyncSession,
        *,
        user: User | None = None,
        persona: Persona | None = None,
    ) -> dict[str, Any]:
        effective = self.settings.model_dump(mode="json")
        settings_rows = await self._load_settings(
            session,
            user_id=user.id if user else None,
            persona_id=persona.id if persona else None,
        )
        for row in settings_rows:
            effective.setdefault(row.namespace, {})
            if not isinstance(effective[row.namespace], dict):
                effective[row.namespace] = {}
            effective[row.namespace][row.key] = row.value_json

        if persona and persona.safety_overrides:
            effective.setdefault("safety", {})
            effective["safety"] = deep_merge(effective["safety"], persona.safety_overrides)
        if persona and persona.prompt_overrides:
            effective.setdefault("prompt_overrides", {})
            effective["prompt_overrides"] = deep_merge(effective.get("prompt_overrides", {}), persona.prompt_overrides)
        if user and user.safety_overrides:
            effective.setdefault("safety", {})
            effective["safety"] = deep_merge(effective["safety"], user.safety_overrides)
        if user and user.schedule_overrides:
            effective.setdefault("scheduling", {})
            effective["scheduling"] = deep_merge(effective["scheduling"], user.schedule_overrides)
        return effective

    async def upsert_setting(
        self,
        session: AsyncSession,
        *,
        namespace: str,
        key: str,
        value_json: Any,
        description: str | None = None,
        scope: AppSettingScope = AppSettingScope.global_scope,
        user_id: UUID | None = None,
        persona_id: UUID | None = None,
    ) -> AppSetting:
        stmt = select(AppSetting).where(
            AppSetting.namespace == namespace,
            AppSetting.key == key,
            AppSetting.scope == scope,
            AppSetting.user_id == user_id,
            AppSetting.persona_id == persona_id,
        )
        record = (await session.execute(stmt)).scalar_one_or_none()
        if record is None:
            record = AppSetting(
                namespace=namespace,
                key=key,
                scope=scope,
                user_id=user_id,
                persona_id=persona_id,
            )
            session.add(record)
        record.value_json = value_json
        record.description = description
        await session.flush()
        return record

    async def _load_settings(
        self,
        session: AsyncSession,
        *,
        user_id: UUID | None,
        persona_id: UUID | None,
    ) -> list[AppSetting]:
        stmt = select(AppSetting).where(AppSetting.scope == AppSettingScope.global_scope)
        rows = list((await session.execute(stmt)).scalars().all())
        if persona_id:
            stmt = select(AppSetting).where(
                AppSetting.scope == AppSettingScope.persona,
                AppSetting.persona_id == persona_id,
            )
            rows.extend((await session.execute(stmt)).scalars().all())
        if user_id:
            stmt = select(AppSetting).where(
                AppSetting.scope == AppSettingScope.user,
                AppSetting.user_id == user_id,
            )
            rows.extend((await session.execute(stmt)).scalars().all())
        return rows
