from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import AIUnavailableError, AiRuntime
from app.ai.deps import ParentChatDeps
from app.ai.history import render_memory_hits, render_portal_chat_history, render_recent_message_snippets
from app.ai.schemas import ParentGuidanceMemoryDraft, ParentGuidanceSaveResult
from app.core.settings import RuntimeSettings
from app.models.communication import Message
from app.models.enums import Channel, MemoryRelationshipType, MemoryType
from app.models.memory import MemoryItem, MemoryRelationship
from app.models.portal import ChildProfile, CustomerUser, PortalChatMessage, PortalChatThread
from app.models.user import User
from app.schemas.site import PortalChatMessageView
from app.services.config import ConfigService
from app.services.conversation import ConversationService
from app.services.memory import MemoryService
from app.utils.text import truncate_text
from app.utils.time import utc_now


@dataclass(slots=True)
class GuidanceMemoryDraft:
    content: str
    title: str
    memory_type: MemoryType
    tags: list[str]
    importance_score: float
    entity_name: str | None = None
    entity_kind: str | None = None
    ref_key: str | None = None
    parent_ref: str | None = None


class ParentChatService:
    def __init__(
        self,
        settings: RuntimeSettings,
        ai_runtime: AiRuntime,
        config_service: ConfigService,
        conversation_service: ConversationService,
        memory_service: MemoryService,
    ) -> None:
        self.settings = settings
        self.ai_runtime = ai_runtime
        self.config_service = config_service
        self.conversation_service = conversation_service
        self.memory_service = memory_service

    async def get_or_create_thread(
        self,
        session: AsyncSession,
        *,
        account_id,
        customer_user: CustomerUser,
        child_profile: ChildProfile,
    ) -> PortalChatThread:
        thread = await session.scalar(
            select(PortalChatThread).where(
                PortalChatThread.account_id == account_id,
                PortalChatThread.customer_user_id == customer_user.id,
                PortalChatThread.child_profile_id == child_profile.id,
            )
        )
        if thread is not None:
            return thread

        thread = PortalChatThread(
            account_id=account_id,
            customer_user_id=customer_user.id,
            child_profile_id=child_profile.id,
            metadata_json={
                "kind": "parent_portal_chat",
                "child_name": _child_name(child_profile),
                "relationship_label": _relationship_label(customer_user),
            },
        )
        session.add(thread)
        await session.flush()
        return thread

    async def list_messages(
        self,
        session: AsyncSession,
        *,
        thread_id,
        limit: int = 80,
    ) -> list[PortalChatMessage]:
        messages = list(
            (
                await session.execute(
                    select(PortalChatMessage)
                    .where(PortalChatMessage.thread_id == thread_id)
                    .order_by(PortalChatMessage.created_at)
                    .limit(max(limit, 1))
                )
            )
            .scalars()
            .all()
        )
        return [message for message in messages if (message.metadata_json or {}).get("kind") != "welcome"]

    def serialize_messages(self, messages: list[PortalChatMessage]) -> list[PortalChatMessageView]:
        return [
            PortalChatMessageView(
                id=str(message.id),
                sender=message.sender,
                body=message.body,
                created_at=message.created_at.isoformat() if message.created_at else None,
                memory_saved=bool((message.metadata_json or {}).get("saved_to_memory")),
                memory_saved_label=str((message.metadata_json or {}).get("memory_saved_label") or "").strip() or None,
                memory_saved_details=_message_memory_details(message),
            )
            for message in messages
        ]

    async def send_message(
        self,
        session: AsyncSession,
        *,
        account_id,
        customer_user: CustomerUser,
        child_profile: ChildProfile,
        text: str,
    ) -> tuple[PortalChatThread, PortalChatMessage, PortalChatMessage]:
        cleaned = truncate_text(" ".join(str(text or "").split()), 2400)
        if not cleaned:
            raise ValueError("Write a message before sending.")

        thread = await self.get_or_create_thread(
            session,
            account_id=account_id,
            customer_user=customer_user,
            child_profile=child_profile,
        )
        parent_message = PortalChatMessage(
            thread_id=thread.id,
            sender="parent",
            body=cleaned,
            metadata_json={"source": "parent_portal_chat"},
        )
        session.add(parent_message)
        thread.last_parent_message_at = utc_now()
        await session.flush()

        assistant_text, model_name, usage, saved_memories = await self._respond_with_agent(
            session,
            customer_user=customer_user,
            child_profile=child_profile,
            thread=thread,
            guidance_text=cleaned,
        )
        if saved_memories:
            parent_message.metadata_json = {
                **dict(parent_message.metadata_json or {}),
                "saved_to_memory": True,
                "memory_id": str(saved_memories[0].id),
                "memory_ids": [str(item.id) for item in saved_memories],
                "memory_type": saved_memories[0].memory_type.value,
                "memory_saved_count": len(saved_memories),
                "memory_saved_label": _memory_saved_label(saved_memories),
                "memory_saved_details": _saved_memory_details(saved_memories),
            }
            await session.flush()
        assistant_message = PortalChatMessage(
            thread_id=thread.id,
            sender="assistant",
            body=assistant_text,
            metadata_json={"source": "parent_portal_chat"},
            model_name=model_name,
            tokens_in=_usage_value(usage, "input_tokens"),
            tokens_out=_usage_value(usage, "output_tokens"),
        )
        session.add(assistant_message)
        thread.last_assistant_message_at = utc_now()
        await session.flush()
        return thread, parent_message, assistant_message

    async def _respond_with_agent(
        self,
        session: AsyncSession,
        *,
        customer_user: CustomerUser,
        child_profile: ChildProfile,
        thread: PortalChatThread,
        guidance_text: str,
    ) -> tuple[str, str | None, dict[str, Any], list[MemoryItem]]:
        companion_user = await self._companion_user(session, child_profile=child_profile)
        persona = await self.conversation_service.get_active_persona(session, companion_user) if companion_user else None
        config = (
            await self.config_service.get_effective_config(session, user=companion_user, persona=persona)
            if companion_user
            else self.settings.model_dump(mode="json")
        )
        memory_hits = []
        recent_child_messages: list[Message] = []
        if companion_user is not None:
            memory_hits = await self.memory_service.retrieve(
                session,
                user_id=companion_user.id,
                persona_id=persona.id if persona else None,
                query=(await self._latest_parent_message(session, thread_id=thread.id)).body,
                top_k=min(6, int(config["memory"]["top_k"])),
                threshold=max(0.78, float(config["memory"]["similarity_threshold"])),
            )
            recent_child_messages = list(
                (
                    await session.execute(
                        select(Message)
                        .where(
                            Message.user_id == companion_user.id,
                            Message.channel != Channel.system,
                        )
                        .order_by(desc(Message.created_at))
                        .limit(6)
                    )
            )
                .scalars()
                .all()
            )

        recent_thread_messages = await self.list_messages(session, thread_id=thread.id, limit=12)
        saved_memories: list[MemoryItem] = []

        async def save_guidance_memories(
            drafts: list[ParentGuidanceMemoryDraft],
        ) -> ParentGuidanceSaveResult:
            nonlocal saved_memories
            saved_memories = await self._capture_parent_guidance_memories(
                session,
                child_profile=child_profile,
                text=guidance_text,
                drafts=drafts,
            )
            return ParentGuidanceSaveResult(
                saved_count=len(saved_memories),
                relationship_count=0,
                memory_ids=[str(item.id) for item in saved_memories],
                details=_saved_memory_detail_models(saved_memories),
            )

        prompt = "\n\n".join(
            [
                self._context_block(
                    customer_user=customer_user,
                    child_profile=child_profile,
                    recent_child_messages=list(reversed(recent_child_messages)),
                    memory_hits=memory_hits,
                ),
                "Recent parent chat history:",
                render_portal_chat_history(recent_thread_messages),
                f"Latest parent message: {guidance_text}",
            ]
        )
        deps = ParentChatDeps(
            session=session,
            customer_user=customer_user,
            child_profile=child_profile,
            config=config,
            thread_messages=recent_thread_messages,
            recent_child_messages=list(reversed(recent_child_messages)),
            memory_hits=memory_hits,
            save_guidance_memories=save_guidance_memories,
        )
        if not self.ai_runtime.enabled:
            raise AIUnavailableError("Parent chat is unavailable right now.")
        response = await self.ai_runtime.parent_chat(
            prompt=prompt,
            deps=deps,
            max_tokens=420,
            temperature=min(float(self.settings.openai.temperature), 0.8),
        )
        if not saved_memories:
            saved_memories = await self._capture_parent_guidance_memories(
                session,
                child_profile=child_profile,
                text=guidance_text,
            )
        text = truncate_text((response.output.text or "").strip(), 2200)
        if not text:
            raise AIUnavailableError("Parent chat returned an empty response.")
        return text, response.model, response.usage, saved_memories

    async def _companion_user(self, session: AsyncSession, *, child_profile: ChildProfile) -> User | None:
        if not child_profile.companion_user_id:
            return None
        return await session.get(User, child_profile.companion_user_id)

    async def _latest_parent_message(self, session: AsyncSession, *, thread_id) -> PortalChatMessage:
        return (
            await session.execute(
                select(PortalChatMessage)
                .where(
                    PortalChatMessage.thread_id == thread_id,
                    PortalChatMessage.sender == "parent",
                )
                .order_by(desc(PortalChatMessage.created_at))
                .limit(1)
            )
        ).scalars().one()

    def _context_block(
        self,
        *,
        customer_user: CustomerUser,
        child_profile: ChildProfile,
        recent_child_messages: list[Message],
        memory_hits: list[Any],
    ) -> str:
        preferences = dict(child_profile.preferences_json or {})
        boundaries = dict(child_profile.boundaries_json or {})
        routines = dict(child_profile.routines_json or {})
        pacing = ", ".join(_string_list(preferences.get("preferred_pacing"))) or "Not set"
        style = ", ".join(_string_list(preferences.get("response_style"))) or "Not set"
        memory_lines = [
            f"- {getattr(hit.memory, 'title', None) or 'Memory'}: {getattr(hit.memory, 'summary', None) or getattr(hit.memory, 'content', '')}"
            for hit in memory_hits[:5]
        ]
        recent_lines = [
            f"- {message.direction.value}: {(message.body or '').strip()[:180]}"
            for message in recent_child_messages[:5]
            if (message.body or "").strip()
        ]
        return "\n".join(
            [
                f"Child name: {_child_name(child_profile)}",
                f"Parent relationship: {_humanize_value(_relationship_label(customer_user))}",
                f"Profile notes: {(child_profile.notes or '').strip() or 'None provided'}",
                f"Preferred pacing: {_humanize_value(pacing)}",
                f"Response style: {_humanize_value(style)}",
                f"Voice enabled: {'Yes' if preferences.get('voice_enabled') else 'No'}",
                f"Parent visibility: {_humanize_value(boundaries.get('parent_visibility_mode'))}",
                f"Alert threshold: {_humanize_value(boundaries.get('alert_threshold'))}",
                f"Daily cadence: {_humanize_value(routines.get('daily_cadence'))}",
                f"Quiet hours: {str(routines.get('quiet_hours') or 'Not set')}",
                f"Extra communication notes: {str(preferences.get('communication_notes') or '').strip() or 'None'}",
                "Relevant memories:",
                *(memory_lines or ["- None surfaced for this prompt."]),
                "Recent child conversation snippets:",
                *(recent_lines or ["- No recent child transcript available."]),
            ]
        )

    async def _capture_parent_guidance_memories(
        self,
        session: AsyncSession,
        *,
        child_profile: ChildProfile,
        text: str,
        drafts: list[ParentGuidanceMemoryDraft] | None = None,
    ) -> list[MemoryItem]:
        companion_user = await self._companion_user(session, child_profile=child_profile)
        if companion_user is None:
            return []
        persona = await self.conversation_service.get_active_persona(session, companion_user)
        config = (
            await self.config_service.get_effective_config(session, user=companion_user, persona=persona)
            if companion_user
            else self.settings.model_dump(mode="json")
        )
        content = truncate_text(text.strip(), 2000)
        if not content:
            return []

        if drafts:
            candidate_facts = _guidance_drafts_from_models(drafts, child_name=_child_name(child_profile))[:6]
        else:
            candidate_facts = (
                await self._draft_parent_guidance_memories(
                    child_profile=child_profile,
                    text=content,
                )
            )[:6]
        created: list[MemoryItem] = []
        draft_to_item: dict[str, MemoryItem] = {}
        for fact in candidate_facts:
            existing = await self._matching_parent_guidance_memory(
                session,
                user=companion_user,
                text=fact.content,
            )
            if existing is not None:
                existing.title = fact.title
                existing.content = fact.content
                existing.summary = truncate_text(fact.content, 260)
                existing.tags = fact.tags
                existing.memory_type = fact.memory_type
                existing.importance_score = max(float(existing.importance_score or 0.0), fact.importance_score)
                metadata_json = dict(existing.metadata_json or {})
                metadata_json.update(
                    {
                        "source": "parent_portal_chat",
                        "source_kind": "parent_guidance",
                        "child_profile_id": str(child_profile.id),
                    }
                )
                if fact.entity_name:
                    metadata_json["entity_name"] = fact.entity_name
                if fact.entity_kind:
                    metadata_json["entity_kind"] = fact.entity_kind
                existing.metadata_json = metadata_json
                created.append(existing)
                if fact.ref_key:
                    draft_to_item[fact.ref_key] = existing
                continue

            item = MemoryItem(
                user_id=companion_user.id,
                persona_id=persona.id if persona else None,
                memory_type=fact.memory_type,
                title=fact.title,
                content=fact.content,
                summary=truncate_text(fact.content, 260),
                tags=fact.tags,
                importance_score=fact.importance_score,
                metadata_json={
                    "source": "parent_portal_chat",
                    "source_kind": "parent_guidance",
                    "child_profile_id": str(child_profile.id),
                    **({"entity_name": fact.entity_name} if fact.entity_name else {}),
                    **({"entity_kind": fact.entity_kind} if fact.entity_kind else {}),
                },
            )
            session.add(item)
            created.append(item)
            if fact.ref_key:
                draft_to_item[fact.ref_key] = item

        if not created:
            return []

        await session.flush()
        await self._create_guidance_relationships(
            session,
            user=companion_user,
            drafts=candidate_facts,
            draft_to_item=draft_to_item,
        )
        await self.memory_service.embed_items(session, created, config=config)
        await self.memory_service.sync_relationships_for_user(session, user_id=companion_user.id)
        return created

    async def _draft_parent_guidance_memories(
        self,
        *,
        child_profile: ChildProfile,
        text: str,
    ) -> list[GuidanceMemoryDraft]:
        return _extract_parent_guidance_memories(text, child_name=_child_name(child_profile))

    async def _create_guidance_relationships(
        self,
        session: AsyncSession,
        *,
        user: User,
        drafts: list[GuidanceMemoryDraft],
        draft_to_item: dict[str, MemoryItem],
    ) -> None:
        pairs: set[tuple[Any, Any]] = set()
        for draft in drafts:
            if not draft.ref_key or not draft.parent_ref:
                continue
            child_item = draft_to_item.get(draft.ref_key)
            parent_item = draft_to_item.get(draft.parent_ref)
            if child_item is None or parent_item is None or child_item.id == parent_item.id:
                continue
            pairs.add((parent_item.id, child_item.id))
        if not pairs:
            return

        involved_ids = {memory_id for pair in pairs for memory_id in pair}
        existing_rows = list(
            (
                await session.execute(
                    select(MemoryRelationship).where(
                        MemoryRelationship.user_id == user.id,
                        MemoryRelationship.relationship_type == MemoryRelationshipType.manual_child,
                        MemoryRelationship.parent_memory_id.in_(involved_ids),
                        MemoryRelationship.child_memory_id.in_(involved_ids),
                    )
                )
            )
            .scalars()
            .all()
        )
        existing_pairs = {(row.parent_memory_id, row.child_memory_id) for row in existing_rows}
        for parent_id, child_id in pairs:
            if (parent_id, child_id) in existing_pairs:
                continue
            session.add(
                MemoryRelationship(
                    user_id=user.id,
                    parent_memory_id=parent_id,
                    child_memory_id=child_id,
                    relationship_type=MemoryRelationshipType.manual_child,
                    metadata_json={"source": "parent_portal_chat", "source_kind": "parent_guidance"},
                )
            )
        await session.flush()

    async def _matching_parent_guidance_memory(
        self,
        session: AsyncSession,
        *,
        user: User,
        text: str,
    ) -> MemoryItem | None:
        normalized = _normalize_guidance_text(text)
        candidates = list(
            (
                await session.execute(
                    select(MemoryItem)
                    .where(
                        MemoryItem.user_id == user.id,
                        MemoryItem.disabled.is_(False),
                    )
                    .order_by(desc(MemoryItem.updated_at))
                    .limit(120)
                )
            )
            .scalars()
            .all()
        )
        for candidate in candidates:
            metadata_json = dict(candidate.metadata_json or {})
            if str(metadata_json.get("source_kind") or "").strip() != "parent_guidance":
                continue
            if _normalize_guidance_text(candidate.content) == normalized:
                return candidate
        return None


def _child_name(child_profile: ChildProfile) -> str:
    return (child_profile.display_name or child_profile.first_name or "your child").strip()


def _relationship_label(customer_user: CustomerUser) -> str:
    return (customer_user.relationship_label or "parent").strip() or "parent"


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _usage_value(usage: dict[str, Any], key: str) -> int | None:
    value = usage.get(key)
    if isinstance(value, int):
        return value
    return None


def _memory_saved_label(saved_memories: list[MemoryItem]) -> str:
    if len(saved_memories) > 1:
        return f"Saved {len(saved_memories)} memories"
    memory_type = saved_memories[0].memory_type if saved_memories else MemoryType.operator_note
    if memory_type in {MemoryType.preference, MemoryType.fact}:
        return "Saved to memory"
    return "Saved as guidance"


def _normalize_guidance_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


def _saved_memory_details(saved_memories: list[MemoryItem]) -> list[dict[str, str]]:
    details: list[dict[str, str]] = []
    for memory in saved_memories[:8]:
        title = str(memory.title or _guidance_memory_title(memory.content)).strip() or "Saved memory"
        details.append(
            {
                "id": str(memory.id),
                "title": title,
                "content": str(memory.content or "").strip(),
                "memory_type": memory.memory_type.value,
            }
        )
    return details


def _saved_memory_detail_models(saved_memories: list[MemoryItem]) -> list[dict[str, str]]:
    return _saved_memory_details(saved_memories)


def _message_memory_details(message: PortalChatMessage) -> list[dict[str, str]]:
    raw_details = (message.metadata_json or {}).get("memory_saved_details")
    if not isinstance(raw_details, list):
        return []
    details: list[dict[str, str]] = []
    for item in raw_details[:8]:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        content = str(item.get("content") or "").strip()
        if not title and not content:
            continue
        details.append(
            {
                "id": str(item.get("id") or "").strip(),
                "title": title or "Saved memory",
                "content": content,
                "memory_type": str(item.get("memory_type") or "").strip(),
            }
        )
    return details


def _split_parent_guidance_into_clauses(text: str) -> list[str]:
    sentences = re.split(r"(?<=[.!?])\s+|\n+", str(text or "").strip())
    clauses: list[str] = []
    for sentence in sentences:
        cleaned_sentence = sentence.strip(" \t\r\n,;")
        if not cleaned_sentence:
            continue
        because_parts = re.split(r"\s+(?:which is funny because|because)\s+", cleaned_sentence, flags=re.IGNORECASE)
        for because_part in because_parts:
            comma_parts = re.split(
                r",\s+(?=(?:she|he|they|her|his|their|please|avoid|do not|don't|does not|doesn't|katie|we|we're|we are|the kitten|the cat|the dog|[a-z]+(?:'s)?\s+bday|[a-z]+(?:'s)?\s+birthday))",
                because_part.strip(),
                flags=re.IGNORECASE,
            )
            for comma_part in comma_parts:
                and_parts = re.split(
                    r"\s+and\s+(?=(?:she|he|they|her|his|their|please|avoid|do not|don't|does not|doesn't|katie|we|we're|we are|the kitten|the cat|the dog)\b)",
                    comma_part.strip(),
                    flags=re.IGNORECASE,
                )
                for part in and_parts:
                    normalized = re.sub(r"\s+", " ", part).strip(" \t\r\n,;")
                    if len(normalized) < 6:
                        continue
                    clauses.append(normalized)

    deduped: list[str] = []
    seen: set[str] = set()
    for clause in clauses:
        key = _normalize_guidance_text(clause)
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(clause)
    return deduped or [str(text or "").strip()]


def _extract_parent_guidance_memories(text: str, *, child_name: str) -> list[GuidanceMemoryDraft]:
    working_text = str(text or "").strip()
    drafts: list[GuidanceMemoryDraft] = []

    pattern_extractors = [
        (
            re.compile(
                r"(?:i'?d say\s+)?(?:her|his|their|theirs|katie'?s?)\s+favorite\s+([a-z0-9' -]+?)\s+song\s+is\s+[\"“']?([^\"”'.,;!?]+)",
                flags=re.IGNORECASE,
            ),
            lambda match: GuidanceMemoryDraft(
                content=f"{child_name}'s favorite {_display_value(match.group(1))} song is {_display_value(match.group(2))}.",
                title=f"Favorite {_title_label(match.group(1))} Song",
                memory_type=MemoryType.preference,
                tags=["parent-guidance", "preference", "likes", "music"],
                importance_score=0.82,
            ),
        ),
        (
            re.compile(
                r"(?:she|he|they)\s+is\s+(\d{1,3})\b",
                flags=re.IGNORECASE,
            ),
            lambda match: GuidanceMemoryDraft(
                content=f"{child_name} is {match.group(1)} years old.",
                title="Age",
                memory_type=MemoryType.fact,
                tags=["parent-guidance", "age"],
                importance_score=0.84,
            ),
        ),
        (
            re.compile(
                r"(?:her|his|their)\s+(?:birthday|bday)\s+is\s+(.+?)(?=\s+and\s+(?:she|he|they)\b|[.,;!?]|$)",
                flags=re.IGNORECASE,
            ),
            lambda match: GuidanceMemoryDraft(
                content=f"{child_name}'s birthday is {_display_value(match.group(1))}.",
                title="Birthday details",
                memory_type=MemoryType.fact,
                tags=["parent-guidance", "birthday"],
                importance_score=0.84,
            ),
        ),
        (
            re.compile(
                r"(?:she|he|they)\s+loves?\s+([^.,;!?]+)",
                flags=re.IGNORECASE,
            ),
            lambda match: GuidanceMemoryDraft(
                content=f"{child_name} loves {_display_value(match.group(1))}.",
                title="Likes and preferences",
                memory_type=MemoryType.preference,
                tags=["parent-guidance", "preference", "likes"],
                importance_score=0.78,
            ),
        ),
        (
            re.compile(
                r"(?:her|his|their)\s+best\s+friends?\s+are\s+([^.;!?]+)",
                flags=re.IGNORECASE,
            ),
            lambda match: _friend_list_drafts(_display_value(match.group(1)), child_name=child_name),
        ),
        (
            re.compile(
                r"(?:we are|we're|we|i am|i'm|i)\s+getting\s+(a|an)\s+([a-z][a-z -]+?)(?:\s+(next week|this week|tomorrow|soon))?(?=[,.;!?]|$)",
                flags=re.IGNORECASE,
            ),
            lambda match: GuidanceMemoryDraft(
                content=f"The family is getting {match.group(1).lower()} {_display_value(match.group(2))}{f' {match.group(3).lower()}' if match.group(3) else ''}.",
                title=f"Getting {match.group(1).lower()} {_title_label(match.group(2))}",
                memory_type=MemoryType.fact,
                tags=["parent-guidance", "family-update"],
                importance_score=0.8,
            ),
        ),
        (
            re.compile(
                r"(?:and\s+)?(?:the\s+)?([a-z][a-z -]+?)'s\s+name\s+is\s+[\"“']?([^\"”'.,;!?]+)",
                flags=re.IGNORECASE,
            ),
            lambda match: GuidanceMemoryDraft(
                content=f"The {_display_value(match.group(1)).lower()}'s name is {_display_value(match.group(2))}.",
                title=f"{_title_label(match.group(1))} Name",
                memory_type=MemoryType.fact,
                tags=["parent-guidance", "name"],
                importance_score=0.8,
            ),
        ),
    ]

    for pattern, builder in pattern_extractors:
        while True:
            match = pattern.search(working_text)
            if match is None:
                break
            built = builder(match)
            if isinstance(built, list):
                drafts.extend(built)
            else:
                drafts.append(built)
            working_text = f"{working_text[:match.start()]} {working_text[match.end():]}".strip()

    for clause in _split_parent_guidance_into_clauses(working_text):
        normalized_clause = _normalize_generic_guidance_clause(clause, child_name=child_name)
        if not normalized_clause:
            continue
        drafts.append(
            GuidanceMemoryDraft(
                content=normalized_clause,
                title=_guidance_memory_title(normalized_clause),
                memory_type=_guidance_memory_type(normalized_clause),
                tags=_guidance_memory_tags(normalized_clause),
                importance_score=_guidance_importance_score(normalized_clause),
            )
        )

    deduped: list[GuidanceMemoryDraft] = []
    seen: set[str] = set()
    for draft in drafts:
        key = _normalize_guidance_text(draft.content)
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(draft)
    return deduped


def _normalize_generic_guidance_clause(clause: str, *, child_name: str) -> str | None:
    text = re.sub(r"\s+", " ", str(clause or "").strip())
    if not text:
        return None
    text = re.sub(r"^(?:i'?d say|i would say|i think|i feel like|it seems like)\s+", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^(?:that|just that)\s+", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^(?:she|he|they)\s+", f"{child_name} ", text, flags=re.IGNORECASE)
    text = re.sub(r"^(?:her|his|their)\s+", f"{child_name}'s ", text, flags=re.IGNORECASE)
    text = text.strip(" \t\r\n,;")
    connector_only = re.sub(r"[^a-zA-Z ]+", " ", text).strip().casefold()
    if connector_only in {"", "because", "which is funny because", "and", "and the"}:
        return None
    if len(text) < 6:
        return None
    if text[-1] not in ".!?":
        text = f"{text}."
    return text[0].upper() + text[1:]


def _coerce_guidance_memory_drafts(
    payload: dict[str, Any] | list[Any] | None,
    *,
    child_name: str,
    source_text: str,
) -> list[GuidanceMemoryDraft]:
    if isinstance(payload, dict) and isinstance(payload.get("memories"), list):
        raw_items = payload["memories"]
    elif isinstance(payload, list):
        raw_items = payload
    else:
        raw_items = []

    drafts: list[GuidanceMemoryDraft] = []
    seen: set[str] = set()
    for index, raw_item in enumerate(raw_items[:8]):
        if not isinstance(raw_item, dict):
            continue
        content = _normalize_ai_guidance_content(raw_item.get("content"), child_name=child_name)
        if not content:
            continue
        key = _normalize_guidance_text(content)
        if not key or key in seen:
            continue
        seen.add(key)
        try:
            memory_type = MemoryType(str(raw_item.get("memory_type") or "").strip().lower())
        except ValueError:
            memory_type = _guidance_memory_type(content)
        tags = _clean_tags(raw_item.get("tags"))
        if "parent-guidance" not in tags:
            tags.insert(0, "parent-guidance")
        title = str(raw_item.get("title") or "").strip() or _guidance_memory_title(content)
        importance_score = _coerce_importance(raw_item.get("importance_score"), default=_guidance_importance_score(content))
        ref_key = str(raw_item.get("ref") or raw_item.get("ref_key") or f"memory_{index + 1}").strip() or f"memory_{index + 1}"
        parent_ref = str(raw_item.get("parent_ref") or "").strip() or None
        entity_name = _display_value(str(raw_item.get("entity_name") or ""))
        entity_kind = _display_value(str(raw_item.get("entity_kind") or ""))
        drafts.append(
            GuidanceMemoryDraft(
                content=content,
                title=title,
                memory_type=memory_type,
                tags=tags,
                importance_score=importance_score,
                entity_name=entity_name or None,
                entity_kind=entity_kind.lower() or None,
                ref_key=ref_key,
                parent_ref=parent_ref,
            )
        )

    if not drafts:
        fallback = _extract_parent_guidance_memories(source_text, child_name=child_name)
        return fallback[:6]
    return drafts[:6]


def _normalize_ai_guidance_content(value: Any, *, child_name: str) -> str | None:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    if not text:
        return None
    text = re.sub(r"^(?:i'?d say|i would say|i think|i feel like|it sounds like|it seems like)\s+", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^(?:that|just that)\s+", "", text, flags=re.IGNORECASE)
    text = text.replace("“", '"').replace("”", '"').replace("’", "'")
    text = re.sub(r"^(?:she|he|they)\s+", f"{child_name} ", text, flags=re.IGNORECASE)
    text = re.sub(r"^(?:her|his|their)\s+", f"{child_name}'s ", text, flags=re.IGNORECASE)
    text = text.strip(" \t\r\n,;")
    if len(text) < 6:
        return None
    if text[-1] not in ".!?":
        text = f"{text}."
    return text[0].upper() + text[1:]


def _guidance_drafts_from_models(
    drafts: list[ParentGuidanceMemoryDraft],
    *,
    child_name: str,
) -> list[GuidanceMemoryDraft]:
    payload = [draft.model_dump(mode="json") for draft in drafts]
    return _coerce_guidance_memory_drafts(payload, child_name=child_name, source_text="")


def _clean_tags(raw_tags: Any) -> list[str]:
    if not isinstance(raw_tags, list):
        return []
    tags: list[str] = []
    for tag in raw_tags:
        cleaned = re.sub(r"[^a-z0-9_-]+", "-", str(tag or "").strip().lower()).strip("-")
        if cleaned and cleaned not in tags:
            tags.append(cleaned)
    return tags


def _coerce_importance(value: Any, *, default: float) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, numeric))


def _looks_like_bad_guidance_extraction(drafts: list[GuidanceMemoryDraft], *, source_text: str) -> bool:
    lower_source = _normalize_guidance_text(source_text)
    bad_markers = (
        "i'd say",
        "i would say",
        "which is funny because",
        "you might consider",
        "would you like guidance",
    )
    for draft in drafts:
        lower_content = _normalize_guidance_text(draft.content)
        if any(marker in lower_content for marker in bad_markers):
            return True
        if len(draft.content) > 140 and lower_content in lower_source and "," in draft.content:
            return True
    return False


def _friend_list_drafts(value: str, *, child_name: str) -> list[GuidanceMemoryDraft]:
    summary = GuidanceMemoryDraft(
        content=f"{child_name}'s best friends are {_display_value(value)}.",
        title="Best friends",
        memory_type=MemoryType.fact,
        tags=["parent-guidance", "friends"],
        importance_score=0.84,
        ref_key="best_friends_summary",
    )
    drafts = [summary]
    for index, name in enumerate(_split_named_list(value), start=1):
        drafts.append(
            GuidanceMemoryDraft(
                content=f"{name} is one of {child_name}'s best friends.",
                title=f"Friend: {name}",
                memory_type=MemoryType.fact,
                tags=["parent-guidance", "friends", "person"],
                importance_score=0.8,
                entity_name=name,
                entity_kind="person",
                ref_key=f"best_friend_{index}",
                parent_ref=summary.ref_key,
            )
        )
    return drafts


def _split_named_list(value: str) -> list[str]:
    raw_value = _display_value(value)
    if not raw_value:
        return []
    normalized = re.sub(r"\s+(?:who|that)\b.*$", "", raw_value, flags=re.IGNORECASE).strip(" ,.")
    parts = re.split(r"\s*(?:,| and | & )\s*", normalized)
    names: list[str] = []
    for part in parts:
        cleaned = re.sub(r"[^A-Za-z' -]+", "", part).strip()
        if not cleaned:
            continue
        title_cased = " ".join(piece.capitalize() for piece in cleaned.split())
        if title_cased and title_cased not in names:
            names.append(title_cased)
    return names


def _guidance_memory_type(text: str) -> MemoryType:
    lower = f" {_normalize_guidance_text(text)} "
    if any(token in lower for token in [" likes ", " loves ", " enjoys ", " prefers ", " favorite ", " hates ", " doesn't like ", " does not like "]):
        return MemoryType.preference
    if any(token in lower for token in [" birthday ", " bday ", " years old", " best friend", " friends are ", " name is ", " getting a ", " getting an "]):
        return MemoryType.fact
    return MemoryType.operator_note


def _guidance_memory_title(text: str) -> str:
    lower = _normalize_guidance_text(text)
    if "birthday" in lower or "bday" in lower:
        return "Birthday details"
    if "best friend" in lower or "friends are" in lower or "friend is" in lower:
        return "Best friends"
    if any(token in lower for token in [" years old", " is 21", " is 20", " is 19", " age ", "turned "]):
        return "Age"
    if "favorite" in lower and "song" in lower:
        return "Favorite song"
    if "getting a " in lower or "getting an " in lower:
        match = re.search(r"getting\s+(a|an)\s+([a-z][a-z -]+)", lower)
        if match:
            return f"Getting {match.group(1)} {_title_label(match.group(2))}"
        return "Family update"
    if "name is" in lower:
        match = re.search(r"(?:the\s+)?([a-z][a-z -]+)'s\s+name\s+is", lower)
        if match:
            return f"{_title_label(match.group(1))} Name"
        return "Name"
    if any(token in lower for token in [" likes ", " loves ", " enjoys ", " favorite ", " prefers "]):
        return "Likes and preferences"
    if any(token in lower for token in [" avoid ", " don't ", " do not ", " doesn't like ", " does not like ", " hates "]):
        return "What to avoid"
    return truncate_text(f"Parent noted: {text}", 110)


def _guidance_memory_tags(text: str) -> list[str]:
    lower = _normalize_guidance_text(text)
    tags = ["parent-guidance"]
    memory_type = _guidance_memory_type(text)
    if memory_type == MemoryType.preference:
        tags.append("preference")
    if memory_type == MemoryType.fact:
        tags.append("fact")
    if "birthday" in lower or "bday" in lower:
        tags.append("birthday")
    if any(token in lower for token in ["best friend", "friends are", "friend is"]):
        tags.append("friends")
    if any(token in lower for token in [" likes ", " loves ", " enjoys ", " favorite ", " prefers "]):
        tags.append("likes")
    if any(token in lower for token in [" avoid ", " don't ", " do not ", " doesn't like ", " does not like ", " hates "]):
        tags.append("avoid")
    deduped: list[str] = []
    for tag in tags:
        if tag not in deduped:
            deduped.append(tag)
    return deduped


def _guidance_importance_score(text: str) -> float:
    lower = _normalize_guidance_text(text)
    if "birthday" in lower or "bday" in lower or "best friend" in lower or "friends are" in lower:
        return 0.84
    if _guidance_memory_type(text) == MemoryType.preference:
        return 0.78
    return 0.68


def _display_value(value: str) -> str:
    cleaned = re.sub(r"\s+", " ", str(value or "").strip(" \t\r\n,;\"'"))
    if not cleaned:
        return ""
    return cleaned


def _title_label(value: str) -> str:
    cleaned = _display_value(value)
    if not cleaned:
        return ""
    return cleaned.title()


def _humanize_value(value: Any) -> str:
    cleaned = str(value or "").strip()
    if not cleaned:
        return "Not set"
    return cleaned.replace("_", " ").title()
