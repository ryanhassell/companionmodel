from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import RuntimeSettings
from app.models.communication import MediaAsset, Message
from app.models.enums import MediaRole
from app.models.persona import Persona
from app.models.user import User
from app.providers.openai import OpenAIProvider
from app.services.prompt import PromptService
from app.utils.files import ensure_parent


class ImageService:
    def __init__(
        self,
        settings: RuntimeSettings,
        openai_provider: OpenAIProvider,
        prompt_service: PromptService,
    ) -> None:
        self.settings = settings
        self.openai_provider = openai_provider
        self.prompt_service = prompt_service

    async def generate_image(
        self,
        session: AsyncSession,
        *,
        persona: Persona,
        user: User | None,
        scene_hint: str,
        config: dict[str, Any],
        attach_to_message: Message | None = None,
        negative_prompt: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> MediaAsset:
        context = {
            "persona": persona,
            "user": user,
            "scene_hint": scene_hint,
            "negative_prompt": negative_prompt or "",
            "config": config,
        }
        prompt_text = await self.prompt_service.render(session, "image_scene", context)
        asset = MediaAsset(
            message_id=attach_to_message.id if attach_to_message else None,
            user_id=user.id if user else None,
            persona_id=persona.id,
            role=MediaRole.generated,
            prompt_text=prompt_text,
            negative_prompt=negative_prompt,
            generation_status="processing",
            metadata_json=metadata or {},
        )
        session.add(asset)
        await session.flush()

        if not self.openai_provider.enabled:
            asset.generation_status = "failed"
            asset.error_message = "OpenAI image provider is not configured"
            await session.flush()
            return asset

        generated = await self.openai_provider.generate_image(prompt=prompt_text)
        asset.mime_type = generated.mime_type
        asset.provider_asset_id = generated.raw_response.get("id")
        asset.remote_url = generated.remote_url
        asset.metadata_json = {
            **asset.metadata_json,
            "revised_prompt": generated.revised_prompt,
            "provider": "openai",
        }
        if generated.binary:
            filename = f"{asset.id}_{secrets.token_hex(6)}{generated.filename_suffix}"
            target = self.settings.media_root_path / "images" / filename
            ensure_parent(target)
            target.write_bytes(generated.binary)
            asset.local_path = str(target)
        asset.generation_status = "ready"
        await session.flush()
        return asset
