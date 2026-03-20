from __future__ import annotations

import mimetypes
import secrets
from pathlib import Path
from typing import Any

import httpx
from fastapi import UploadFile
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
        use_reference_image: bool = True,
        metadata: dict[str, Any] | None = None,
    ) -> MediaAsset:
        reference_asset = await self.get_persona_reference_asset(session, persona) if use_reference_image else None
        context = {
            "persona": persona,
            "user": user,
            "scene_hint": scene_hint,
            "negative_prompt": negative_prompt or "",
            "config": config,
            "has_reference_image": use_reference_image and reference_asset is not None,
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

        reference_paths = self._reference_paths(reference_asset)
        used_reference_image = False
        try:
            if reference_paths:
                generated = await self.openai_provider.edit_image(
                    prompt=prompt_text,
                    reference_images=reference_paths,
                )
                used_reference_image = True
            else:
                generated = await self.openai_provider.generate_image(prompt=prompt_text)
        except httpx.HTTPError as exc:
            asset.generation_status = "failed"
            asset.error_message = str(exc) or exc.__class__.__name__
            asset.metadata_json = {
                **asset.metadata_json,
                "reference_image_asset_id": str(reference_asset.id) if reference_asset else None,
                "used_reference_image": used_reference_image,
                "generation_failed": True,
                "generation_error": str(exc) or exc.__class__.__name__,
                "generation_error_type": exc.__class__.__name__,
                "generation_error_repr": repr(exc),
            }
            await session.flush()
            return asset
        asset.mime_type = generated.mime_type
        asset.provider_asset_id = generated.raw_response.get("id")
        asset.remote_url = generated.remote_url
        asset.metadata_json = {
            **asset.metadata_json,
            "revised_prompt": generated.revised_prompt,
            "provider": "openai",
            "reference_image_asset_id": str(reference_asset.id) if reference_asset else None,
            "used_reference_image": used_reference_image,
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

    async def save_persona_reference_image(
        self,
        session: AsyncSession,
        *,
        persona: Persona,
        upload: UploadFile,
    ) -> MediaAsset:
        content = await upload.read()
        mime_type = upload.content_type or mimetypes.guess_type(upload.filename or "")[0] or "image/png"
        suffix = mimetypes.guess_extension(mime_type) or Path(upload.filename or "reference.png").suffix or ".png"
        asset = MediaAsset(
            persona_id=persona.id,
            role=MediaRole.generated,
            mime_type=mime_type,
            generation_status="reference",
            metadata_json={
                "source": "persona_reference_upload",
                "is_persona_reference": True,
                "original_filename": upload.filename,
            },
        )
        session.add(asset)
        await session.flush()
        filename = f"{asset.id}_{secrets.token_hex(6)}{suffix}"
        target = self.settings.media_root_path / "reference-images" / filename
        ensure_parent(target)
        target.write_bytes(content)
        asset.local_path = str(target)
        visual_bible = dict(persona.visual_bible or {})
        visual_bible["reference_image_asset_id"] = str(asset.id)
        persona.visual_bible = visual_bible
        await session.flush()
        return asset

    async def get_persona_reference_asset(
        self,
        session: AsyncSession,
        persona: Persona | None,
    ) -> MediaAsset | None:
        if persona is None:
            return None
        asset_id = (persona.visual_bible or {}).get("reference_image_asset_id")
        if not asset_id:
            return None
        return await session.get(MediaAsset, asset_id)

    def _reference_paths(self, reference_asset: MediaAsset | None) -> list[Path]:
        if reference_asset is None or not reference_asset.local_path:
            return []
        path = Path(reference_asset.local_path)
        if not path.exists():
            return []
        return [path]
