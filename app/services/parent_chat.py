from __future__ import annotations

import asyncio
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from sqlalchemy import delete, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import AIUnavailableError, AiRuntime
from app.ai.deps import ParentChatDeps
from app.ai.history import render_memory_hits, render_portal_chat_history, render_recent_message_snippets
from app.ai.schemas import MemoryFactDraft, MemoryPlacementRelatedEntity, ParentGuidanceMemoryDraft, ParentGuidanceSaveResult
from app.core.settings import RuntimeSettings
from app.models.communication import Message
from app.models.enums import Channel, MemoryRelationshipType, MemoryType, PortalChatMessageKind, PortalChatRunStatus
from app.models.memory import MemoryItem, MemoryRelationship
from app.models.persona import Persona
from app.models.portal import ChildProfile, CustomerUser, PortalChatMessage, PortalChatRun, PortalChatThread
from app.models.user import User
from app.schemas.site import PortalChatActivityView, PortalChatMessageView, PortalChatSavedMemoryView, PortalChatThreadView
from app.services.config import ConfigService
from app.services.conversation import ConversationService
from app.services.memory import MemoryService
from app.utils.text import similarity_score, truncate_text
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
    facet: str | None = None
    relation_to_child: str | None = None
    canonical_value: str | None = None
    related_entities: list[MemoryPlacementRelatedEntity] = field(default_factory=list)
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
        thread_id=None,
    ) -> PortalChatThread:
        thread = await self.resolve_thread(
            session,
            account_id=account_id,
            customer_user=customer_user,
            child_profile=child_profile,
            thread_id=thread_id,
            create_if_missing=False,
        )
        if thread is not None:
            return thread

        return await self.create_thread(
            session,
            account_id=account_id,
            customer_user=customer_user,
            child_profile=child_profile,
        )

    async def create_thread(
        self,
        session: AsyncSession,
        *,
        account_id,
        customer_user: CustomerUser,
        child_profile: ChildProfile,
    ) -> PortalChatThread:
        thread = PortalChatThread(
            account_id=account_id,
            customer_user_id=customer_user.id,
            child_profile_id=child_profile.id,
            metadata_json={
                "kind": "parent_portal_chat",
                "child_name": _child_name(child_profile),
                "relationship_label": _relationship_label(customer_user),
                "title": "New chat",
                "preview": "",
                "message_count": 0,
            },
        )
        session.add(thread)
        await session.flush()
        return thread

    async def resolve_thread(
        self,
        session: AsyncSession,
        *,
        account_id,
        customer_user: CustomerUser,
        child_profile: ChildProfile,
        thread_id=None,
        create_if_missing: bool = True,
    ) -> PortalChatThread | None:
        if thread_id:
            try:
                normalized_thread_id = thread_id if isinstance(thread_id, uuid.UUID) else uuid.UUID(str(thread_id))
            except (TypeError, ValueError):
                normalized_thread_id = None
        else:
            normalized_thread_id = None

        if normalized_thread_id:
            candidate = await session.scalar(
                select(PortalChatThread).where(
                    PortalChatThread.id == normalized_thread_id,
                    PortalChatThread.account_id == account_id,
                    PortalChatThread.customer_user_id == customer_user.id,
                    PortalChatThread.child_profile_id == child_profile.id,
                )
            )
            if candidate is not None:
                return candidate

        thread = await session.scalar(
            select(PortalChatThread)
            .where(
                PortalChatThread.account_id == account_id,
                PortalChatThread.customer_user_id == customer_user.id,
                PortalChatThread.child_profile_id == child_profile.id,
            )
            .order_by(
                desc(func.coalesce(PortalChatThread.last_assistant_message_at, PortalChatThread.last_parent_message_at, PortalChatThread.updated_at)),
                desc(PortalChatThread.updated_at),
            )
        )
        if thread is not None or not create_if_missing:
            return thread
        return await self.create_thread(
            session,
            account_id=account_id,
            customer_user=customer_user,
            child_profile=child_profile,
        )

    async def list_threads(
        self,
        session: AsyncSession,
        *,
        account_id,
        customer_user: CustomerUser,
        child_profile: ChildProfile,
        limit: int = 24,
    ) -> list[PortalChatThread]:
        return list(
            (
                await session.execute(
                    select(PortalChatThread)
                    .where(
                        PortalChatThread.account_id == account_id,
                        PortalChatThread.customer_user_id == customer_user.id,
                        PortalChatThread.child_profile_id == child_profile.id,
                    )
                    .order_by(
                        desc(func.coalesce(PortalChatThread.last_assistant_message_at, PortalChatThread.last_parent_message_at, PortalChatThread.updated_at)),
                        desc(PortalChatThread.updated_at),
                    )
                    .limit(max(limit, 1))
                )
            )
            .scalars()
            .all()
        )

    async def clear_thread(
        self,
        session: AsyncSession,
        *,
        thread: PortalChatThread,
    ) -> None:
        await session.execute(delete(PortalChatMessage).where(PortalChatMessage.thread_id == thread.id))
        await session.execute(delete(PortalChatRun).where(PortalChatRun.thread_id == thread.id))
        thread.last_parent_message_at = None
        thread.last_assistant_message_at = None
        thread.metadata_json = {
            **dict(thread.metadata_json or {}),
            "title": "New chat",
            "preview": "",
            "message_count": 0,
        }
        await session.flush()

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
        views: list[PortalChatMessageView] = []
        carried_activity: list[PortalChatActivityView] = []
        for message in messages:
            metadata = dict(message.metadata_json or {})
            message_activity = _message_activity_events(message)
            if message.sender == "parent" and message_activity:
                carried_activity.extend(message_activity)
            view = PortalChatMessageView(
                id=str(message.id),
                sender=message.sender,
                body=message.body,
                kind=message.message_kind.value if hasattr(message.message_kind, "value") else str(message.message_kind or "message"),
                run_id=str(message.run_id) if message.run_id else None,
                created_at=message.created_at.isoformat() if message.created_at else None,
                memory_saved=bool(metadata.get("saved_to_memory")),
                memory_saved_label=str(metadata.get("memory_saved_label") or "").strip() or None,
                memory_saved_details=_message_memory_details(message),
                activity_events=[],
            )
            if message.sender == "assistant":
                combined_events = [*carried_activity, *message_activity]
                deduped_events: list[PortalChatActivityView] = []
                seen_event_keys: set[tuple[str, str, str | None, str | None, str, str | None]] = set()
                for item in combined_events:
                    event_key = (
                        item.kind,
                        item.label,
                        item.detail,
                        item.memory_id,
                        ",".join(item.memory_ids or []),
                        item.href,
                    )
                    if event_key in seen_event_keys:
                        continue
                    seen_event_keys.add(event_key)
                    deduped_events.append(item)
                view.activity_events = deduped_events
                carried_activity = []
            views.append(view)
        return views

    def serialize_threads(
        self,
        threads: list[PortalChatThread],
        *,
        active_thread_id=None,
    ) -> list[PortalChatThreadView]:
        return [
            PortalChatThreadView(
                id=str(thread.id),
                title=_thread_title(thread),
                preview=_thread_preview(thread),
                created_at=thread.created_at.isoformat() if thread.created_at else None,
                updated_at=thread.updated_at.isoformat() if thread.updated_at else None,
                message_count=int((thread.metadata_json or {}).get("message_count") or 0),
                href=f"/app/parent-chat?thread={thread.id}",
                is_active=str(thread.id) == str(active_thread_id),
            )
            for thread in threads
        ]

    async def send_message(
        self,
        session: AsyncSession,
        *,
        account_id,
        customer_user: CustomerUser,
        child_profile: ChildProfile,
        text: str,
        question_context: str | None = None,
        thread: PortalChatThread | None = None,
    ) -> tuple[PortalChatThread, PortalChatMessage, PortalChatMessage]:
        cleaned = truncate_text(" ".join(str(text or "").split()), 2400)
        if not cleaned:
            raise ValueError("Write a message before sending.")

        thread = thread or await self.get_or_create_thread(
            session,
            account_id=account_id,
            customer_user=customer_user,
            child_profile=child_profile,
        )
        parent_message = PortalChatMessage(
            thread_id=thread.id,
            sender="parent",
            message_kind=PortalChatMessageKind.message,
            body=cleaned,
            metadata_json={
                "source": "parent_portal_chat",
                "question_context": question_context,
            },
        )
        session.add(parent_message)
        thread.last_parent_message_at = utc_now()
        self._update_thread_metadata_after_parent_message(thread, cleaned)
        await session.flush()
        run = await self._create_run(
            session,
            thread=thread,
            customer_user=customer_user,
            child_profile=child_profile,
        )

        assistant_text, model_name, usage, saved_memories = await self._respond_with_agent(
            session,
            customer_user=customer_user,
            child_profile=child_profile,
            thread=thread,
            guidance_text=cleaned,
            question_context=question_context,
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
        activity_events = _activity_events_from_saved_memories(saved_memories)
        assistant_message = PortalChatMessage(
            thread_id=thread.id,
            run_id=run.id,
            sender="assistant",
            message_kind=PortalChatMessageKind.message,
            body=assistant_text,
            metadata_json={
                "source": "parent_portal_chat",
                "activity_events": [item.model_dump(mode="json") for item in activity_events],
                "saved_to_memory": bool(saved_memories),
                "memory_saved_label": _memory_saved_label(saved_memories) if saved_memories else None,
                "memory_saved_details": _saved_memory_details(saved_memories),
            },
            model_name=model_name,
            tokens_in=_usage_value(usage, "input_tokens"),
            tokens_out=_usage_value(usage, "output_tokens"),
        )
        session.add(assistant_message)
        run.status = PortalChatRunStatus.completed
        run.model_name = model_name
        run.completed_at = utc_now()
        run.metadata_json = {
            **dict(run.metadata_json or {}),
            "activity_events": [item.model_dump(mode="json") for item in activity_events],
            "memory_ids": [str(item.id) for item in saved_memories],
        }
        thread.last_assistant_message_at = utc_now()
        self._update_thread_metadata_after_assistant_message(thread, assistant_text)
        await session.flush()
        return thread, parent_message, assistant_message

    async def stream_message(
        self,
        session: AsyncSession,
        *,
        account_id,
        customer_user: CustomerUser,
        child_profile: ChildProfile,
        text: str,
        question_context: str | None = None,
        thread: PortalChatThread | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        cleaned = truncate_text(" ".join(str(text or "").split()), 2400)
        if not cleaned:
            raise ValueError("Write a message before sending.")

        thread = thread or await self.get_or_create_thread(
            session,
            account_id=account_id,
            customer_user=customer_user,
            child_profile=child_profile,
        )
        parent_message = PortalChatMessage(
            thread_id=thread.id,
            sender="parent",
            message_kind=PortalChatMessageKind.message,
            body=cleaned,
            metadata_json={
                "source": "parent_portal_chat",
                "question_context": question_context,
            },
        )
        session.add(parent_message)
        thread.last_parent_message_at = utc_now()
        self._update_thread_metadata_after_parent_message(thread, cleaned)
        await session.flush()

        run = await self._create_run(
            session,
            thread=thread,
            customer_user=customer_user,
            child_profile=child_profile,
        )
        await session.commit()

        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        activity_events: list[PortalChatActivityView] = []

        async def _push(event: dict[str, Any]) -> None:
            await queue.put(event)

        async def _runner() -> None:
            nonlocal activity_events
            stream_session = session
            try:
                companion_user = await self._companion_user(stream_session, child_profile=child_profile)
                persona = await self.conversation_service.get_active_persona(stream_session, companion_user) if companion_user else None
                config = (
                    await self.config_service.get_effective_config(stream_session, user=companion_user, persona=persona)
                    if companion_user
                    else self.settings.model_dump(mode="json")
                )
                recent_thread_messages = await self.list_messages(stream_session, thread_id=thread.id, limit=12)
                memory_inventory_requested = _is_memory_reflection_request(cleaned, child_name=_child_name(child_profile))
                retrieval_query = _parent_chat_memory_query(
                    _message_with_question_context(cleaned, question_context=question_context),
                    recent_thread_messages=recent_thread_messages,
                    child_name=_child_name(child_profile),
                )
                memory_hits = []
                memory_inventory: list[MemoryItem] = []
                recent_child_messages: list[Message] = []
                if companion_user is not None:
                    memory_hits = await self.memory_service.retrieve(
                        stream_session,
                        user_id=companion_user.id,
                        persona_id=persona.id if persona else None,
                        query=retrieval_query,
                        top_k=min(6, int(config["memory"]["top_k"])),
                        threshold=max(0.78, float(config["memory"]["similarity_threshold"])),
                    )
                    recent_child_messages = list(
                        (
                            await stream_session.execute(
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
                    if memory_inventory_requested:
                        memory_inventory = await self._memory_inventory_snapshot(
                            stream_session,
                            user=companion_user,
                        )

                saved_memories: list[MemoryItem] = []
                save_result: ParentGuidanceSaveResult | None = None
                save_lock = asyncio.Lock()

                async def save_guidance_memories(
                    drafts: list[ParentGuidanceMemoryDraft],
                ) -> ParentGuidanceSaveResult:
                    nonlocal saved_memories, save_result, activity_events
                    async with save_lock:
                        if save_result is not None:
                            return save_result
                        saved_memories = await self._capture_parent_guidance_memories(
                            stream_session,
                            child_profile=child_profile,
                            text=cleaned,
                            drafts=drafts,
                        )
                        activity_events = _activity_events_from_saved_memories(saved_memories)
                        for event in activity_events:
                            await _push(
                                {
                                    "type": event.kind,
                                    "label": event.label,
                                    "detail": event.detail,
                                    "memory_id": event.memory_id,
                                    "memory_ids": list(event.memory_ids or []),
                                    "count": event.count,
                                    "href": event.href,
                                    "details": [item.model_dump(mode="json") for item in event.details],
                                }
                            )
                        save_result = ParentGuidanceSaveResult(
                            saved_count=len(saved_memories),
                            relationship_count=0,
                            memory_ids=[str(item.id) for item in saved_memories],
                            details=_saved_memory_detail_models(saved_memories),
                        )
                        return save_result

                prompt = "\n\n".join(
                    [
                        self._context_block(
                            customer_user=customer_user,
                            child_profile=child_profile,
                            persona=persona,
                            recent_child_messages=list(reversed(recent_child_messages)),
                            memory_hits=memory_hits,
                            memory_inventory=memory_inventory,
                            include_memory_inventory=memory_inventory_requested,
                        ),
                        "Recent parent chat history:",
                        render_portal_chat_history(recent_thread_messages),
                        *(
                            [f"Parent is answering this portal question: {question_context}"]
                            if question_context
                            else []
                        ),
                        f"Latest parent message: {cleaned}",
                    ]
                )
                deps = ParentChatDeps(
                    session=stream_session,
                    customer_user=customer_user,
                    child_profile=child_profile,
                    config=config,
                    **_persona_dep_values(persona),
                    thread_messages=recent_thread_messages,
                    recent_child_messages=list(reversed(recent_child_messages)),
                    memory_hits=memory_hits,
                    save_guidance_memories=save_guidance_memories,
                )

                await _push({"type": "status", "label": "Resona is thinking..."})
                response = await self.ai_runtime.parent_chat(
                    prompt=prompt,
                    deps=deps,
                    max_tokens=420,
                    temperature=min(float(self.settings.openai.temperature), 0.8),
                )
                if not saved_memories:
                    saved_memories = await self._capture_parent_guidance_memories(
                        stream_session,
                        child_profile=child_profile,
                        text=cleaned,
                    )
                    if saved_memories:
                        activity_events = _activity_events_from_saved_memories(saved_memories)
                assistant_text = truncate_text((response.output.text or "").strip(), 2200)
                if not assistant_text:
                    raise AIUnavailableError("Parent chat returned an empty response.")
                assistant_text = _refocus_parent_chat_response(
                    assistant_text,
                    guidance_text=cleaned,
                    child_name=_child_name(child_profile),
                )
                await _push({"type": "status", "label": "Resona is replying..."})
                for chunk in _assistant_stream_chunks(assistant_text):
                    await _push({"type": "assistant_delta", "text": chunk})
                parent_message.metadata_json = {
                    **dict(parent_message.metadata_json or {}),
                    "saved_to_memory": bool(saved_memories),
                    "memory_saved_label": _memory_saved_label(saved_memories) if saved_memories else None,
                    "memory_saved_details": _saved_memory_details(saved_memories),
                }
                assistant_message = PortalChatMessage(
                    thread_id=thread.id,
                    run_id=run.id,
                    sender="assistant",
                    message_kind=PortalChatMessageKind.message,
                    body=assistant_text,
                    metadata_json={
                        "source": "parent_portal_chat",
                        "activity_events": [item.model_dump(mode="json") for item in activity_events],
                        "saved_to_memory": bool(saved_memories),
                        "memory_saved_label": _memory_saved_label(saved_memories) if saved_memories else None,
                        "memory_saved_details": _saved_memory_details(saved_memories),
                    },
                    model_name=response.model,
                    tokens_in=_usage_value(response.usage, "input_tokens"),
                    tokens_out=_usage_value(response.usage, "output_tokens"),
                )
                stream_session.add(assistant_message)
                thread.last_assistant_message_at = utc_now()
                self._update_thread_metadata_after_assistant_message(thread, assistant_text)
                run.status = PortalChatRunStatus.completed
                run.model_name = assistant_message.model_name
                run.completed_at = utc_now()
                run.metadata_json = {
                    **dict(run.metadata_json or {}),
                    "activity_events": [item.model_dump(mode="json") for item in activity_events],
                    "memory_ids": [str(item.id) for item in saved_memories],
                }
                await stream_session.commit()
                await _push(
                    {
                        "type": "assistant_message",
                        "message": self.serialize_messages([assistant_message])[0].model_dump(mode="json"),
                        "thread_id": str(thread.id),
                        "run_id": str(run.id),
                    }
                )
                await _push({"type": "run_complete", "thread_id": str(thread.id), "run_id": str(run.id)})
            except Exception as exc:
                await stream_session.rollback()
                existing_run_metadata = dict(run.__dict__.get("metadata_json") or {})
                run.status = PortalChatRunStatus.failed
                run.error_code = exc.__class__.__name__
                run.completed_at = utc_now()
                run.metadata_json = {
                    **existing_run_metadata,
                    "error": str(exc),
                }
                await stream_session.commit()
                await _push({"type": "run_error", "detail": str(exc), "retryable": True})
            finally:
                await queue.put(None)

        runner = asyncio.create_task(_runner())
        try:
            yield {"type": "thread_ready", "thread_id": str(thread.id), "run_id": str(run.id)}
            while True:
                event = await queue.get()
                if event is None:
                    break
                yield event
            await runner
        finally:
            if not runner.done():
                runner.cancel()

    async def _create_run(
        self,
        session: AsyncSession,
        *,
        thread: PortalChatThread,
        customer_user: CustomerUser,
        child_profile: ChildProfile,
    ) -> PortalChatRun:
        run = PortalChatRun(
            account_id=thread.account_id,
            customer_user_id=customer_user.id,
            child_profile_id=child_profile.id,
            thread_id=thread.id,
            status=PortalChatRunStatus.running,
            metadata_json={"source": "parent_portal_chat"},
            started_at=utc_now(),
        )
        session.add(run)
        await session.flush()
        return run

    def _update_thread_metadata_after_parent_message(self, thread: PortalChatThread, text: str) -> None:
        metadata = dict(thread.metadata_json or {})
        current_title = str(metadata.get("title") or "").strip()
        if not current_title or current_title == "New chat":
            metadata["title"] = _thread_title_from_text(text)
        metadata["preview"] = truncate_text(text, 100)
        metadata["message_count"] = int(metadata.get("message_count") or 0) + 1
        thread.metadata_json = metadata

    def _update_thread_metadata_after_assistant_message(self, thread: PortalChatThread, text: str) -> None:
        metadata = dict(thread.metadata_json or {})
        if text.strip():
            metadata["preview"] = truncate_text(text, 120)
        metadata["message_count"] = int(metadata.get("message_count") or 0) + 1
        thread.metadata_json = metadata

    async def _respond_with_agent(
        self,
        session: AsyncSession,
        *,
        customer_user: CustomerUser,
        child_profile: ChildProfile,
        thread: PortalChatThread,
        guidance_text: str,
        question_context: str | None = None,
    ) -> tuple[str, str | None, dict[str, Any], list[MemoryItem]]:
        companion_user = await self._companion_user(session, child_profile=child_profile)
        persona = await self.conversation_service.get_active_persona(session, companion_user) if companion_user else None
        config = (
            await self.config_service.get_effective_config(session, user=companion_user, persona=persona)
            if companion_user
            else self.settings.model_dump(mode="json")
        )
        recent_thread_messages = await self.list_messages(session, thread_id=thread.id, limit=12)
        memory_inventory_requested = _is_memory_reflection_request(guidance_text, child_name=_child_name(child_profile))
        retrieval_query = _parent_chat_memory_query(
            _message_with_question_context(guidance_text, question_context=question_context),
            recent_thread_messages=recent_thread_messages,
            child_name=_child_name(child_profile),
        )
        memory_hits = []
        memory_inventory: list[MemoryItem] = []
        recent_child_messages: list[Message] = []
        if companion_user is not None:
            memory_hits = await self.memory_service.retrieve(
                session,
                user_id=companion_user.id,
                persona_id=persona.id if persona else None,
                query=retrieval_query,
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
            if memory_inventory_requested:
                memory_inventory = await self._memory_inventory_snapshot(
                    session,
                    user=companion_user,
                )
        saved_memories: list[MemoryItem] = []
        save_result: ParentGuidanceSaveResult | None = None
        save_lock = asyncio.Lock()

        async def save_guidance_memories(
            drafts: list[ParentGuidanceMemoryDraft],
        ) -> ParentGuidanceSaveResult:
            nonlocal saved_memories, save_result
            async with save_lock:
                if save_result is not None:
                    return save_result
                saved_memories = await self._capture_parent_guidance_memories(
                    session,
                    child_profile=child_profile,
                    text=guidance_text,
                    drafts=drafts,
                )
                save_result = ParentGuidanceSaveResult(
                    saved_count=len(saved_memories),
                    relationship_count=0,
                    memory_ids=[str(item.id) for item in saved_memories],
                    details=_saved_memory_detail_models(saved_memories),
                )
                return save_result

        prompt = "\n\n".join(
            [
                self._context_block(
                    customer_user=customer_user,
                    child_profile=child_profile,
                    persona=persona,
                    recent_child_messages=list(reversed(recent_child_messages)),
                    memory_hits=memory_hits,
                    memory_inventory=memory_inventory,
                    include_memory_inventory=memory_inventory_requested,
                ),
                "Recent parent chat history:",
                render_portal_chat_history(recent_thread_messages),
                *(
                    [f"Parent is answering this portal question: {question_context}"]
                    if question_context
                    else []
                ),
                f"Latest parent message: {guidance_text}",
            ]
        )
        deps = ParentChatDeps(
            session=session,
            customer_user=customer_user,
            child_profile=child_profile,
            config=config,
            **_persona_dep_values(persona),
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
        text = _refocus_parent_chat_response(
            text,
            guidance_text=guidance_text,
            child_name=_child_name(child_profile),
        )
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
        persona: Persona | None,
        recent_child_messages: list[Message],
        memory_hits: list[Any],
        memory_inventory: list[MemoryItem] | None = None,
        include_memory_inventory: bool = False,
    ) -> str:
        preferences = dict(child_profile.preferences_json or {})
        boundaries = dict(child_profile.boundaries_json or {})
        routines = dict(child_profile.routines_json or {})
        pacing = ", ".join(_string_list(preferences.get("preferred_pacing"))) or "Not set"
        style = ", ".join(_string_list(preferences.get("response_style"))) or "Not set"
        persona_topics = ", ".join(_string_list(getattr(persona, "topics_of_interest", []) or [])) or "Not set"
        persona_activities = ", ".join(_string_list(getattr(persona, "favorite_activities", []) or [])) or "Not set"
        memory_lines = [
            f"- {getattr(hit.memory, 'title', None) or 'Memory'}: {getattr(hit.memory, 'summary', None) or getattr(hit.memory, 'content', '')}"
            for hit in memory_hits[:5]
        ]
        memory_inventory_lines = [
            f"- {memory.title or 'Memory'}: {truncate_text((memory.summary or memory.content or '').strip(), 220)}"
            for memory in list(memory_inventory or [])[:8]
            if (memory.summary or memory.content or "").strip()
        ]
        recent_lines = [
            f"- {message.direction.value}: {(message.body or '').strip()[:180]}"
            for message in recent_child_messages[:5]
            if (message.body or "").strip()
        ]
        lines = [
            f"Child name: {_child_name(child_profile)}",
            f"Parent relationship: {_humanize_value(_relationship_label(customer_user))}",
            f"Profile notes: {(child_profile.notes or '').strip() or 'None provided'}",
            f"Active companion persona: {getattr(persona, 'display_name', None) or 'Resona'}",
            f"Persona description: {(getattr(persona, 'description', None) or '').strip() or 'Warm, kind, playful, emotionally supportive, and steady.'}",
            f"Persona style: {(getattr(persona, 'style', None) or '').strip() or 'Friendly, calm, and affectionate in a non-romantic way.'}",
            f"Persona tone: {(getattr(persona, 'tone', None) or '').strip() or 'Gentle, grounded, upbeat when appropriate.'}",
            f"Persona speech style: {(getattr(persona, 'speech_style', None) or '').strip() or 'Natural, easy to read, and conversational.'}",
            f"Persona topics of interest: {_humanize_value(persona_topics)}",
            f"Persona favorite activities: {_humanize_value(persona_activities)}",
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
        ]
        if include_memory_inventory:
            lines.extend(
                [
                    "Memory inventory snapshot:",
                    *(memory_inventory_lines or ["- No durable memories are stored yet."]),
                ]
            )
        lines.extend(
            [
                "Recent child conversation snippets:",
                *(recent_lines or ["- No recent child transcript available."]),
            ]
        )
        return "\n".join(lines)

    async def _memory_inventory_snapshot(
        self,
        session: AsyncSession,
        *,
        user: User,
    ) -> list[MemoryItem]:
        candidates = await self.memory_service.list_memories_for_user(
            session,
            user_id=user.id,
            limit=48,
        )
        inventory: list[MemoryItem] = []
        seen: set[tuple[str, str]] = set()
        for memory in candidates:
            if self.memory_service.is_routine_memory(memory):
                continue
            title_key = _normalize_guidance_text(memory.title or "")
            content_key = _normalize_guidance_text(memory.summary or memory.content or "")
            dedupe_key = (title_key, content_key)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            inventory.append(memory)
            if len(inventory) >= 8:
                break
        return inventory

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
        decision = await self.memory_service.plan_and_commit_text(
            session,
            user=companion_user,
            persona=persona,
            latest_content=content,
            config=config,
            source_kind="parent_guidance",
            source_channel="parent_chat",
            origin_key=f"parent-guidance:{child_profile.id}:{_normalize_guidance_text(content)[:96]}",
            recent_snippets=[
                f"parent: {content}",
                *[
                    f"draft hint: {truncate_text(' '.join(filter(None, [draft.title, draft.content])), 240)}"
                    for draft in list(drafts or [])[:6]
                    if str(getattr(draft, 'content', '') or '').strip()
                ],
            ],
            extra_metadata={
                "child_profile_id": str(child_profile.id),
                "source": "parent_portal_chat",
                "draft_count": len(drafts or []),
            },
        )
        if decision.status != "applied" or not decision.memory_ids:
            return []
        valid_ids: list[uuid.UUID] = []
        for item_id in decision.memory_ids:
            try:
                valid_ids.append(item_id if isinstance(item_id, uuid.UUID) else uuid.UUID(str(item_id)))
            except (TypeError, ValueError):
                continue
        if not valid_ids:
            return []
        by_id = {
            item.id: item
            for item in (
                await session.execute(select(MemoryItem).where(MemoryItem.id.in_(valid_ids)))
            )
            .scalars()
            .all()
        }
        return [by_id[item_id] for item_id in valid_ids if item_id in by_id]

    async def _draft_parent_guidance_memories(
        self,
        *,
        session: AsyncSession,
        child_profile: ChildProfile,
        text: str,
    ) -> list[GuidanceMemoryDraft]:
        if getattr(self.ai_runtime, "enabled", False) and hasattr(self.ai_runtime, "extract_memories"):
            ai_drafts = await self._draft_parent_guidance_memories_with_ai(
                session,
                child_profile=child_profile,
                text=text,
            )
            if ai_drafts:
                return ai_drafts
        return _extract_parent_guidance_memories(text, child_name=_child_name(child_profile))

    async def _draft_parent_guidance_memories_with_ai(
        self,
        session: AsyncSession,
        *,
        child_profile: ChildProfile,
        text: str,
    ) -> list[GuidanceMemoryDraft]:
        child_name = _child_name(child_profile)
        heuristic_facts = _extract_parent_guidance_memories(text, child_name=child_name)[:6]
        try:
            prompt = await self.memory_service.prompt_service.render(
                session,
                "parent_guidance_memory_fallback",
                {
                    "child_name": child_name,
                    "message": text,
                },
            )
            response = await self.ai_runtime.extract_memories(
                prompt=prompt,
                max_tokens=self.settings.openai.memory_max_output_tokens,
            )
        except Exception:
            return []
        return _guidance_drafts_from_memory_fact_models(
            list(response.output.facts or []),
            child_name=child_name,
            source_text=text,
            heuristic_facts=heuristic_facts,
        )

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
        draft: GuidanceMemoryDraft,
    ) -> MemoryItem | None:
        normalized = _normalize_guidance_text(draft.content)
        semantic_key = _guidance_semantic_key(
            title=draft.title,
            content=draft.content,
            entity_name=draft.entity_name,
            entity_kind=draft.entity_kind,
            facet=draft.facet,
            relation_to_child=draft.relation_to_child,
            canonical_value=draft.canonical_value,
        )
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
            if semantic_key:
                candidate_key = _guidance_semantic_key(
                    title=candidate.title or "",
                    content=candidate.content or "",
                    entity_name=metadata_json.get("entity_name"),
                    entity_kind=metadata_json.get("entity_kind"),
                    facet=metadata_json.get("facet"),
                    relation_to_child=metadata_json.get("relation_to_child"),
                    canonical_value=metadata_json.get("canonical_value"),
                )
                if candidate_key and candidate_key == semantic_key:
                    return candidate
            if _normalize_guidance_text(candidate.content) == normalized:
                return candidate
            if _guidance_memories_look_equivalent(candidate, draft):
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


def _normalize_guidance_fragment(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", _normalize_guidance_text(str(value or ""))).strip()


def _persona_dep_values(persona: Persona | None) -> dict[str, Any]:
    return {
        "persona_name": (getattr(persona, "display_name", None) or "Resona").strip() or "Resona",
        "persona_description": (getattr(persona, "description", None) or "").strip() or None,
        "persona_style": (getattr(persona, "style", None) or "").strip() or None,
        "persona_tone": (getattr(persona, "tone", None) or "").strip() or None,
        "persona_boundaries": (getattr(persona, "boundaries", None) or "").strip() or None,
        "persona_speech_style": (getattr(persona, "speech_style", None) or "").strip() or None,
        "persona_disclosure_policy": (getattr(persona, "disclosure_policy", None) or "").strip() or None,
        "persona_operator_notes": (getattr(persona, "operator_notes", None) or "").strip() or None,
        "persona_topics_of_interest": tuple(_string_list(getattr(persona, "topics_of_interest", []) or [])),
        "persona_favorite_activities": tuple(_string_list(getattr(persona, "favorite_activities", []) or [])),
    }


def _is_memory_reflection_request(text: str, *, child_name: str) -> bool:
    lowered = _normalize_guidance_text(text)
    child_name_lower = _normalize_guidance_text(child_name)
    direct_markers = (
        "what do you remember",
        "what do u remember",
        "what do you know",
        "what do u know",
        "check your memory",
        "search your memory",
        "look in your memory",
        "what's in your memory",
        "whats in your memory",
        "tell me what you remember",
        "tell me what you know",
    )
    if any(marker in lowered for marker in direct_markers):
        return True
    if ("memory" in lowered or "remember" in lowered or "know about" in lowered) and ("?" in text or child_name_lower in lowered):
        return True
    return False


def _guidance_semantic_key(
    *,
    title: str,
    content: str,
    entity_name: Any = None,
    entity_kind: Any = None,
    facet: Any = None,
    relation_to_child: Any = None,
    canonical_value: Any = None,
) -> tuple[str, ...] | None:
    subject = _normalize_guidance_fragment(entity_name)
    subject_kind = _normalize_guidance_fragment(entity_kind)
    facet_key = _normalize_guidance_fragment(facet)
    relation_key = _normalize_guidance_fragment(relation_to_child)
    canonical = _normalize_guidance_fragment(canonical_value)
    if canonical:
        return (
            "structured",
            subject_kind or "child",
            subject or "child",
            facet_key or "general",
            relation_key or "related",
            canonical,
        )
    if subject and facet_key:
        return (
            "structured-subject",
            subject_kind or "topic",
            subject,
            facet_key,
            relation_key or "related",
        )
    return None


def _guidance_focus_tokens(text: str) -> set[str]:
    stopwords = {
        "child",
        "parent",
        "the",
        "and",
        "for",
        "with",
        "into",
        "from",
        "that",
        "this",
        "what",
        "when",
        "where",
        "about",
        "they",
        "them",
        "their",
        "she",
        "her",
        "his",
        "its",
        "would",
        "could",
        "should",
        "really",
        "very",
        "just",
        "one",
        "best",
        "friend",
        "friends",
        "family",
        "brother",
        "sister",
        "mom",
        "dad",
        "mother",
        "father",
        "name",
        "like",
        "likes",
        "love",
        "loves",
        "enjoy",
        "enjoys",
        "prefer",
        "prefers",
        "favorite",
        "favorites",
    }
    tokens: set[str] = set()
    for token in re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)?", _normalize_guidance_text(text)):
        if len(token) < 3 or token in stopwords:
            continue
        tokens.add(token)
    return tokens


def _guidance_memories_look_equivalent(candidate: MemoryItem, draft: GuidanceMemoryDraft) -> bool:
    if candidate.memory_type != draft.memory_type:
        return False
    title_similarity = similarity_score(candidate.title or "", draft.title or "")
    content_similarity = similarity_score(candidate.content or "", draft.content or "")
    candidate_tokens = _guidance_focus_tokens(candidate.content or "")
    draft_tokens = _guidance_focus_tokens(draft.content or "")
    if not candidate_tokens or not draft_tokens:
        return False
    overlap = candidate_tokens & draft_tokens
    overlap_ratio = len(overlap) / max(min(len(candidate_tokens), len(draft_tokens)), 1)
    if content_similarity >= 0.9 and overlap_ratio >= 0.7:
        return True
    if title_similarity < 0.84:
        return False
    return overlap_ratio >= 0.7


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


def _activity_events_from_saved_memories(saved_memories: list[MemoryItem]) -> list[PortalChatActivityView]:
    details = _saved_memory_details(saved_memories)
    if len(details) > 1:
        return [
            PortalChatActivityView(
                kind="memory_batch_added",
                label=f"Learned {len(details)} things",
                detail=None,
                memory_id=None,
                memory_ids=[str(item.get("id") or "").strip() for item in details if str(item.get("id") or "").strip()],
                count=len(details),
                href="/app/memories/library",
                details=[
                    PortalChatSavedMemoryView(
                        id=str(item.get("id") or "").strip() or None,
                        title=str(item.get("title") or "Saved memory"),
                        content=str(item.get("content") or "").strip(),
                        memory_type=str(item.get("memory_type") or "").strip() or None,
                    )
                    for item in details
                ],
            )
        ]
    events: list[PortalChatActivityView] = []
    for detail in details:
        title = str(detail.get("title") or "Memory").strip()
        events.append(
            PortalChatActivityView(
                kind="memory_added",
                label=f"Added memory: {title}",
                detail=str(detail.get("content") or "").strip() or None,
                memory_id=str(detail.get("id") or "").strip() or None,
                memory_ids=[str(detail.get("id") or "").strip()] if str(detail.get("id") or "").strip() else [],
                count=1,
                href=(f"/app/memories/map?node={detail.get('id')}" if str(detail.get("id") or "").strip() else None),
            )
        )
    return events


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


def _message_activity_events(message: PortalChatMessage) -> list[PortalChatActivityView]:
    raw_events = (message.metadata_json or {}).get("activity_events")
    if not isinstance(raw_events, list):
        return []
    events: list[PortalChatActivityView] = []
    for item in raw_events:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()
        if not label:
            continue
        events.append(
            PortalChatActivityView(
                kind=str(item.get("kind") or "activity").strip() or "activity",
                label=label,
                detail=str(item.get("detail") or "").strip() or None,
                memory_id=str(item.get("memory_id") or "").strip() or None,
                memory_ids=[
                    str(memory_id).strip()
                    for memory_id in list(item.get("memory_ids") or [])
                    if str(memory_id).strip()
                ],
                count=int(item.get("count") or 0) or None,
                href=str(item.get("href") or "").strip() or None,
                details=[
                    PortalChatSavedMemoryView(
                        id=str(detail.get("id") or "").strip() or None,
                        title=str(detail.get("title") or "Saved memory"),
                        content=str(detail.get("content") or "").strip(),
                        memory_type=str(detail.get("memory_type") or "").strip() or None,
                    )
                    for detail in list(item.get("details") or [])
                    if isinstance(detail, dict)
                ],
            )
        )
    return _condense_activity_events(events)


def _condense_activity_events(events: list[PortalChatActivityView]) -> list[PortalChatActivityView]:
    memory_added = [item for item in events if item.kind == "memory_added"]
    if len(memory_added) <= 1:
        return events
    condensed: list[PortalChatActivityView] = [
        item for item in events if item.kind != "memory_added"
    ]
    condensed.insert(
        0,
        PortalChatActivityView(
            kind="memory_batch_added",
            label=f"Learned {len(memory_added)} things",
            detail=None,
            memory_id=None,
            memory_ids=[item.memory_id for item in memory_added if item.memory_id],
            count=len(memory_added),
            href="/app/memories/library",
            details=[
                PortalChatSavedMemoryView(
                    id=item.memory_id,
                    title=item.label.replace("Added memory:", "").strip() or "Saved memory",
                    content=item.detail or "",
                    memory_type=None,
                )
                for item in memory_added
            ],
        ),
    )
    return condensed


def _thread_title(thread: PortalChatThread) -> str:
    metadata = dict(thread.metadata_json or {})
    title = str(metadata.get("title") or "").strip()
    return title or "New chat"


def _thread_preview(thread: PortalChatThread) -> str:
    metadata = dict(thread.metadata_json or {})
    preview = str(metadata.get("preview") or "").strip()
    return preview


def _thread_title_from_text(text: str) -> str:
    cleaned = " ".join(str(text or "").split()).strip()
    if not cleaned:
        return "New chat"
    title = truncate_text(cleaned, 54).rstrip(" .,!?:;")
    return title or "New chat"


def _split_parent_guidance_into_clauses(text: str, *, child_name: str) -> list[str]:
    sentences = re.split(r"(?<=[.!?])\s+|\n+", str(text or "").strip())
    clauses: list[str] = []
    child_name_pattern = re.escape(str(child_name or "").strip())
    child_name_branch = f"|{child_name_pattern}" if child_name_pattern else ""
    for sentence in sentences:
        cleaned_sentence = sentence.strip(" \t\r\n,;")
        if not cleaned_sentence:
            continue
        because_parts = re.split(r"\s+(?:which is funny because|because)\s+", cleaned_sentence, flags=re.IGNORECASE)
        for because_part in because_parts:
            comma_parts = re.split(
                rf",\s+(?=(?:she|he|they|her|his|their|please|avoid|do not|don't|does not|doesn't{child_name_branch}|we|we're|we are|the kitten|the cat|the dog|[a-z]+(?:'s)?\s+bday|[a-z]+(?:'s)?\s+birthday))",
                because_part.strip(),
                flags=re.IGNORECASE,
            )
            for comma_part in comma_parts:
                and_parts = re.split(
                    rf"\s+and\s+(?=(?:she|he|they|her|his|their|please|avoid|do not|don't|does not|doesn't{child_name_branch}|we|we're|we are|the kitten|the cat|the dog)\b)",
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

    child_name_pattern = re.escape(child_name)
    pattern_extractors = [
        (
            re.compile(
                rf"(?:i'?d say\s+)?(?:her|his|their|theirs|{child_name_pattern}'?s?)\s+favorite\s+([a-z0-9' -]+?)\s+song\s+is\s+[\"“']?([^\"”'.,;!?]+)",
                flags=re.IGNORECASE,
            ),
            lambda match: GuidanceMemoryDraft(
                content=f"{child_name}'s favorite {_display_value(match.group(1))} song is {_display_value(match.group(2))}.",
                title=f"Favorite {_title_label(match.group(1))} Song",
                memory_type=MemoryType.preference,
                tags=["parent-guidance", "preference", "likes", "music"],
                importance_score=0.82,
                facet="favorites",
                canonical_value=_display_value(match.group(2)),
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
                facet="identity",
                canonical_value=match.group(1),
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
                facet="milestones",
                canonical_value=_display_value(match.group(1)),
            ),
        ),
        (
            re.compile(
                r"(?:she|he|they)\s+(?:really\s+|absolutely\s+|truly\s+)?loves?\s+([^.,;!?]+)",
                flags=re.IGNORECASE,
            ),
            lambda match: GuidanceMemoryDraft(
                content=f"{child_name} loves {_display_value(match.group(1))}.",
                title="Likes and preferences",
                memory_type=MemoryType.preference,
                tags=["parent-guidance", "preference", "likes"],
                importance_score=0.78,
                facet="preferences",
                canonical_value=_display_value(match.group(1)),
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
                r"(?:her|his|their)\s+(brother|sister|mom|mother|dad|father)\s*,?\s*([a-z][a-z' -]+?)\s*,?\s+likes?\s+([^.;!?]+)",
                flags=re.IGNORECASE,
            ),
            lambda match: _family_member_interest_drafts(
                relation=_display_value(match.group(1)),
                name=_display_value(match.group(2)),
                interests=_display_value(match.group(3)),
                child_name=child_name,
            ),
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
                facet="events",
                canonical_value=f"{match.group(1).lower()} {_display_value(match.group(2))}",
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
                facet="pets" if _display_value(match.group(1)).lower() in {"kitten", "cat", "dog", "pet"} else "events",
                entity_name=_display_value(match.group(2)) if _display_value(match.group(1)).lower() in {"kitten", "cat", "dog", "pet"} else None,
                entity_kind="pet" if _display_value(match.group(1)).lower() in {"kitten", "cat", "dog", "pet"} else None,
                relation_to_child=_display_value(match.group(1)).lower() if _display_value(match.group(1)).lower() in {"kitten", "cat", "dog", "pet"} else None,
                canonical_value=_display_value(match.group(2)),
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

    for clause in _split_parent_guidance_into_clauses(working_text, child_name=child_name):
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
        if _is_low_value_guidance_memory_content(draft.content, child_name=child_name):
            continue
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
    lowered = _normalize_guidance_text(text)
    lowered_stripped = re.sub(r"[^a-z0-9 ]+", "", lowered).strip()
    if lowered_stripped in {
        "yeah of course",
        "of course",
        "yes of course",
        "okay",
        "ok",
        "sounds good",
        "got it",
        "thank you",
        "thanks",
        "hi",
        "hello",
    }:
        return None
    if text.endswith("?") and not any(
        marker in lowered
        for marker in (
            child_name.casefold(),
            " she ",
            " he ",
            " they ",
            " her ",
            " his ",
            " their ",
            " remember ",
            " avoid ",
            " prefers ",
            " likes ",
            " loves ",
            " favorite ",
            " birthday ",
            " bday ",
            " best friend",
            " friends are ",
            " hates ",
            " doesn't like ",
            " does not like ",
        )
    ):
        return None
    if _looks_like_contextual_followup_turn(text, child_name=child_name) and not _contains_durable_guidance_signal(text, child_name=child_name):
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
    heuristic_facts: list[GuidanceMemoryDraft] | None = None,
) -> list[GuidanceMemoryDraft]:
    heuristic_facts = list(heuristic_facts or _extract_parent_guidance_memories(source_text, child_name=child_name))[:6]
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
        if _is_low_value_guidance_memory_content(content, child_name=child_name):
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
        facet = _display_value(str(raw_item.get("facet") or ""))
        relation_to_child = _display_value(str(raw_item.get("relation_to_child") or ""))
        canonical_value = _display_value(str(raw_item.get("canonical_value") or ""))
        related_entities = _normalize_related_entities(raw_item.get("related_entities"))
        drafts.append(
            GuidanceMemoryDraft(
                content=content,
                title=title,
                memory_type=memory_type,
                tags=tags,
                importance_score=importance_score,
                entity_name=entity_name or None,
                entity_kind=entity_kind.lower() or None,
                facet=facet.lower() or None,
                relation_to_child=relation_to_child.lower() or None,
                canonical_value=canonical_value or None,
                related_entities=related_entities,
                ref_key=ref_key,
                parent_ref=parent_ref,
            )
        )

    if drafts:
        drafts = _filter_grounded_guidance_drafts(
            drafts,
            source_text=source_text,
            child_name=child_name,
            heuristic_facts=heuristic_facts,
        )

    if not drafts:
        return heuristic_facts[:6]
    if _looks_like_bad_guidance_extraction(drafts, source_text=source_text):
        return heuristic_facts[:6]
    if _prefer_heuristic_guidance_drafts(drafts, heuristic_facts=heuristic_facts):
        return heuristic_facts[:6]
    return drafts[:6]


def _filter_candidate_guidance_drafts_for_turn(
    drafts: list[GuidanceMemoryDraft],
    *,
    source_text: str,
    child_name: str,
) -> list[GuidanceMemoryDraft]:
    if not drafts:
        return []
    if not _looks_like_contextual_followup_turn(source_text, child_name=child_name):
        return drafts
    filtered = [
        draft
        for draft in drafts
        if (
            draft.memory_type in {MemoryType.fact, MemoryType.preference}
            and (
                bool(draft.entity_name)
                or bool(draft.entity_kind)
                or bool(draft.facet)
                or bool(draft.relation_to_child)
                or bool(draft.canonical_value)
                or _contains_durable_guidance_signal(draft.content, child_name=child_name)
            )
        )
    ]
    return filtered


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


def _parent_chat_memory_query(
    latest_text: str,
    *,
    recent_thread_messages: list[PortalChatMessage],
    child_name: str,
) -> str:
    cleaned = " ".join(str(latest_text or "").split()).strip()
    if not cleaned:
        return ""
    if not _looks_like_contextual_followup_turn(cleaned, child_name=child_name):
        return cleaned

    snippets: list[str] = []
    seen: set[str] = {_normalize_guidance_text(cleaned)}
    for message in reversed(list(recent_thread_messages)):
        body = " ".join(str(message.body or "").split()).strip()
        normalized_body = _normalize_guidance_text(body)
        if body and normalized_body and normalized_body not in seen:
            snippets.append(body)
            seen.add(normalized_body)
        for detail in _message_memory_details(message):
            title = str(detail.get("title") or "").strip()
            content = str(detail.get("content") or "").strip()
            snippet = f"{title}: {content}" if title and content else title or content
            normalized_snippet = _normalize_guidance_text(snippet)
            if snippet and normalized_snippet and normalized_snippet not in seen:
                snippets.append(snippet)
                seen.add(normalized_snippet)
        if len(snippets) >= 6:
            break
    if not snippets:
        return cleaned
    return "\n".join(
        [
            cleaned,
            "Recent thread context:",
            *[f"- {truncate_text(item, 220)}" for item in snippets[:6]],
        ]
    )


def _normalize_related_entities(value: Any) -> list[MemoryPlacementRelatedEntity]:
    if not isinstance(value, list):
        return []
    related_entities: list[MemoryPlacementRelatedEntity] = []
    seen: set[tuple[str, str]] = set()
    for item in value[:6]:
        if not isinstance(item, dict):
            continue
        display_name = _display_value(str(item.get("display_name") or ""))
        entity_kind = _display_value(str(item.get("entity_kind") or ""))
        if not display_name or not entity_kind:
            continue
        key = (display_name.casefold(), entity_kind.casefold())
        if key in seen:
            continue
        seen.add(key)
        related_entities.append(
            MemoryPlacementRelatedEntity(
                display_name=display_name,
                entity_kind=entity_kind.lower(),
                relation_kind=_display_value(str(item.get("relation_kind") or "related")).lower() or "related",
                relation_to_child=_display_value(str(item.get("relation_to_child") or "")) or None,
                facet=_display_value(str(item.get("facet") or "")) or None,
                canonical_value=_display_value(str(item.get("canonical_value") or "")) or None,
            )
        )
    return related_entities


def _guidance_structured_override(draft: GuidanceMemoryDraft) -> dict[str, Any] | None:
    related_entities = [
        {
            "display_name": item.display_name,
            "entity_kind": item.entity_kind,
            "relation_kind": item.relation_kind,
            **({"relation_to_child": item.relation_to_child} if item.relation_to_child else {}),
            **({"facet": item.facet} if item.facet else {}),
            **({"canonical_value": item.canonical_value} if item.canonical_value else {}),
        }
        for item in list(draft.related_entities or [])
        if getattr(item, "display_name", None) and getattr(item, "entity_kind", None)
    ]
    payload = {
        **({"subject_name": draft.entity_name} if draft.entity_name else {}),
        **({"entity_kind": draft.entity_kind} if draft.entity_kind else {}),
        **({"facet": draft.facet} if draft.facet else {}),
        **({"relation_to_child": draft.relation_to_child} if draft.relation_to_child else {}),
        **({"canonical_value": draft.canonical_value} if draft.canonical_value else {}),
        **({"related_entities": related_entities} if related_entities else {}),
    }
    return payload or None


def _is_low_value_guidance_memory_content(text: str, *, child_name: str) -> bool:
    lowered = _normalize_guidance_text(text)
    stripped = re.sub(r"[^a-z0-9 ]+", " ", lowered).strip()
    if not stripped:
        return True
    if re.fullmatch(r"(?:[a-z]{3,9}\s+\d{1,2}\s+\d{1,2}\s+\d{2}\s+[ap]m\s+)?(?:hi|hello|hey)\b", stripped):
        return True
    child_name_norm = re.escape(_normalize_guidance_text(child_name))
    if re.fullmatch(
        rf"(?:i am|im|i m)\s+{child_name_norm}(?:s| s)\s+(?:mom|mother|dad|father|parent|guardian|caregiver)\b",
        stripped,
    ):
        return True
    return False


def _guidance_drafts_from_models(
    drafts: list[ParentGuidanceMemoryDraft],
    *,
    child_name: str,
    source_text: str,
    heuristic_facts: list[GuidanceMemoryDraft] | None = None,
) -> list[GuidanceMemoryDraft]:
    payload = [draft.model_dump(mode="json") for draft in drafts]
    return _coerce_guidance_memory_drafts(
        payload,
        child_name=child_name,
        source_text=source_text,
        heuristic_facts=heuristic_facts,
    )


def _guidance_drafts_from_memory_fact_models(
    drafts: list[MemoryFactDraft],
    *,
    child_name: str,
    source_text: str,
    heuristic_facts: list[GuidanceMemoryDraft] | None = None,
) -> list[GuidanceMemoryDraft]:
    payload: list[dict[str, Any]] = []
    for draft in drafts:
        payload.append(
            {
                "title": draft.title,
                "content": draft.content or draft.summary,
                "memory_type": draft.memory_type.value if hasattr(draft.memory_type, "value") else str(draft.memory_type),
                "tags": list(draft.tags or []),
                "importance_score": draft.importance_score,
                "entity_name": draft.entity_name,
                "entity_kind": draft.entity_kind,
                "facet": draft.facet,
                "relation_to_child": draft.relation_to_child,
                "canonical_value": draft.canonical_value,
                "related_entities": [item.model_dump(mode="json") for item in list(getattr(draft, "related_entities", []) or [])],
            }
        )
    return _coerce_guidance_memory_drafts(
        payload,
        child_name=child_name,
        source_text=source_text,
        heuristic_facts=heuristic_facts,
    )


def _filter_grounded_guidance_drafts(
    drafts: list[GuidanceMemoryDraft],
    *,
    source_text: str,
    child_name: str,
    heuristic_facts: list[GuidanceMemoryDraft],
) -> list[GuidanceMemoryDraft]:
    source_tokens = _grounding_tokens(source_text, child_name=child_name)
    heuristic_keys = {_normalize_guidance_text(item.content) for item in heuristic_facts}
    grounded: list[GuidanceMemoryDraft] = []
    for draft in drafts:
        if _normalize_guidance_text(draft.content) in heuristic_keys:
            grounded.append(draft)
            continue
        if _draft_is_grounded_in_latest_message(draft, source_tokens=source_tokens, child_name=child_name):
            grounded.append(draft)
    return grounded


def _draft_is_grounded_in_latest_message(
    draft: GuidanceMemoryDraft,
    *,
    source_tokens: set[str],
    child_name: str,
) -> bool:
    draft_tokens = _grounding_tokens(draft.content, child_name=child_name)
    if not draft_tokens or not source_tokens:
        return False
    overlap = draft_tokens & source_tokens
    if not overlap:
        return False
    overlap_ratio = len(overlap) / max(len(draft_tokens), 1)
    return overlap_ratio >= 0.45 or len(overlap) >= 2


def _grounding_tokens(text: str, *, child_name: str) -> set[str]:
    stopwords = {
        "the",
        "and",
        "for",
        "with",
        "that",
        "this",
        "from",
        "have",
        "has",
        "just",
        "they",
        "them",
        "their",
        "there",
        "about",
        "would",
        "could",
        "should",
        "because",
        "really",
        "absolutely",
        "please",
        "your",
        "what",
        "whats",
        "name",
        "yeah",
        "course",
        "like",
        "said",
        "tell",
        "into",
        "when",
        "while",
        "next",
        "week",
        "love",
        "loves",
    }
    child_tokens = {
        token
        for token in re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)?", child_name.casefold())
        if token
    }
    tokens: set[str] = set()
    for token in re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)?", str(text or "").casefold()):
        if token in child_tokens or token in stopwords:
            continue
        if len(token) >= 4 or token.isdigit():
            tokens.add(token)
    return tokens


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
        if _draft_bundles_multiple_facts(draft.content):
            return True
        if len(draft.content) > 140 and lower_content in lower_source and "," in draft.content:
            return True
    return False


def _prefer_heuristic_guidance_drafts(
    drafts: list[GuidanceMemoryDraft],
    *,
    heuristic_facts: list[GuidanceMemoryDraft],
) -> bool:
    if not heuristic_facts:
        return False
    if len(heuristic_facts) > len(drafts) and any(_draft_bundles_multiple_facts(item.content) for item in drafts):
        return True
    return False


def _draft_bundles_multiple_facts(text: str) -> bool:
    lower = _normalize_guidance_text(text)
    markers = (
        " years old",
        " birthday ",
        " bday ",
        " best friend",
        " friends are ",
        " favorite ",
        " song is ",
        " syndrome",
        " cat named ",
        " dog named ",
        " brother ",
        " sister ",
        " mom ",
        " dad ",
        " name is ",
    )
    marker_count = sum(1 for marker in markers if marker in lower)
    if marker_count >= 2:
        return True
    if any(token in lower for token in (" brother ", " sister ", " mom ", " mother ", " dad ", " father ")) and any(
        token in lower for token in (" likes ", " loves ", " enjoys ", " favorite ", " prefers ")
    ):
        return True
    if text.count(",") >= 2:
        return True
    if len(re.split(r"(?<=[.!?])\s+", text.strip())) > 1:
        return True
    return False


def _friend_list_drafts(value: str, *, child_name: str) -> list[GuidanceMemoryDraft]:
    summary = GuidanceMemoryDraft(
        content=f"{child_name}'s best friends are {_display_value(value)}.",
        title="Best friends",
        memory_type=MemoryType.fact,
        tags=["parent-guidance", "friends"],
        importance_score=0.84,
        facet="friends",
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
                entity_kind="friend",
                facet="friends",
                relation_to_child="friend",
                canonical_value=name,
                ref_key=f"best_friend_{index}",
                parent_ref=summary.ref_key,
            )
        )
    return drafts


def _family_member_interest_drafts(
    *,
    relation: str,
    name: str,
    interests: str,
    child_name: str,
) -> list[GuidanceMemoryDraft]:
    display_relation = _family_relation_label(relation)
    display_name = _display_value(name)
    if not display_name:
        return []
    summary = GuidanceMemoryDraft(
        content=f"{child_name}'s {display_relation.lower()} is {display_name}.",
        title=f"{display_relation}: {display_name}",
        memory_type=MemoryType.fact,
        tags=["parent-guidance", "family", "person"],
        importance_score=0.82,
        entity_name=display_name,
        entity_kind="family_member",
        facet="family",
        relation_to_child=display_relation.lower(),
        canonical_value=display_name,
        ref_key=f"family_{display_relation.lower()}_{_slug_fragment(display_name)}",
    )
    normalized_interests = _normalize_relative_interest(interests)
    drafts = [summary]
    if normalized_interests:
        drafts.append(
            GuidanceMemoryDraft(
                content=f"{display_name} likes {normalized_interests}.",
                title=f"{display_name}'s interests",
                memory_type=MemoryType.fact,
                tags=["parent-guidance", "family", "interests"],
                importance_score=0.76,
                entity_name=display_name,
                entity_kind="family_member",
                facet="interests",
                relation_to_child=display_relation.lower(),
                canonical_value=normalized_interests,
                ref_key=f"{summary.ref_key}_interests",
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


def _family_relation_label(value: str) -> str:
    normalized = _normalize_guidance_text(value)
    mapping = {
        "mom": "Mom",
        "mother": "Mother",
        "dad": "Dad",
        "father": "Father",
        "brother": "Brother",
        "sister": "Sister",
    }
    return mapping.get(normalized, _title_label(value))


def _normalize_relative_interest(value: str) -> str:
    text = _display_value(value)
    if not text:
        return ""
    text = re.sub(r"\betc\.?$", "", text, flags=re.IGNORECASE).strip(" ,.;")
    text = re.sub(r",\s*like\s+", " like ", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*,\s*", ", ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""
    if " like " in text.lower():
        head, _, tail = text.partition(" like ")
        list_items = _split_named_list(tail)
        if list_items:
            return f"{head.strip()} like {' and '.join(list_items) if len(list_items) == 2 else ', '.join(list_items[:-1]) + ', and ' + list_items[-1] if len(list_items) > 2 else list_items[0]}"
    return text


def _slug_fragment(value: str) -> str:
    fragment = re.sub(r"[^a-z0-9]+", "_", _normalize_guidance_text(value)).strip("_")
    return fragment or "person"


def _refocus_parent_chat_response(text: str, *, guidance_text: str, child_name: str) -> str:
    focused = text.strip()
    family_names = _family_member_names_in_guidance(guidance_text)
    for name in family_names:
        focused = re.sub(
            rf"about\s+{re.escape(child_name)}\s+or\s+{re.escape(name)}\b",
            f"about {child_name} or the people around her",
            focused,
            flags=re.IGNORECASE,
        )
        focused = re.sub(
            rf"about\s+{re.escape(name)}\b",
            f"about {child_name}'s world",
            focused,
            flags=re.IGNORECASE,
        )
    return focused


def _family_member_names_in_guidance(text: str) -> set[str]:
    names: set[str] = set()
    patterns = (
        re.compile(r"(?:her|his|their)\s+(?:brother|sister|mom|mother|dad|father)\s*,?\s*([a-z][a-z' -]+)", flags=re.IGNORECASE),
        re.compile(r"(?:brother|sister|mom|mother|dad|father)\s+([a-z][a-z' -]+)", flags=re.IGNORECASE),
    )
    for pattern in patterns:
        for match in pattern.finditer(str(text or "")):
            value = _display_value(match.group(1))
            if value:
                names.add(value)
    return names


def _looks_like_contextual_followup_turn(text: str, *, child_name: str) -> bool:
    lowered = _normalize_guidance_text(text)
    stripped = re.sub(r"[^a-z0-9 ]+", " ", lowered).strip()
    if not stripped:
        return False
    openers = (
        "no i mean",
        "i mean",
        "wait",
        "hold on",
        "what ",
        "whats ",
        "what s ",
        "who ",
        "who s ",
        "how ",
        "can ",
        "could ",
        "would ",
        "do ",
        "does ",
        "did ",
        "is ",
        "are ",
        "tell me ",
        "remind me ",
    )
    has_question_shape = str(text or "").strip().endswith("?") or any(stripped.startswith(opener) for opener in openers)
    if " what is it " in f" {stripped} " or " what it is " in f" {stripped} ":
        has_question_shape = True
    ambiguous_pronoun = any(token in f" {stripped} " for token in (" it ", " that ", " this ", " them ", " those ", " one "))
    if has_question_shape:
        return True
    return ambiguous_pronoun and not _contains_durable_guidance_signal(text, child_name=child_name)


def _contains_durable_guidance_signal(text: str, *, child_name: str) -> bool:
    lowered = f" {_normalize_guidance_text(text)} "
    child_token = _normalize_guidance_text(child_name)
    markers = (
        " likes ",
        " loves ",
        " enjoys ",
        " prefers ",
        " favorite ",
        " birthday ",
        " bday ",
        " years old",
        " best friend",
        " friends are ",
        " friend is ",
        " brother ",
        " sister ",
        " mom ",
        " mother ",
        " dad ",
        " father ",
        " cat ",
        " dog ",
        " pet ",
        " named ",
        " name is ",
        " called ",
        " avoid ",
        " hates ",
        " doesn't like ",
        " does not like ",
        " after school",
        " bedtime",
        " morning",
        " evening",
        " routine",
        " overwhelmed",
        " calm ",
        " syndrome",
        " diagnosis",
        " allergy",
        " allergic",
        " getting a ",
        " getting an ",
    )
    if any(marker in lowered for marker in markers):
        return True
    if child_token and f" {child_token} is " in lowered:
        return True
    return False


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
    return "Parent guidance"


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


def _assistant_stream_chunks(text: str, *, target_size: int = 72) -> list[str]:
    cleaned = str(text or "").strip()
    if not cleaned:
        return []
    parts = re.split(r"(\s+)", cleaned)
    chunks: list[str] = []
    current = ""
    for part in parts:
        candidate = f"{current}{part}"
        if current and len(candidate) > target_size:
            chunks.append(current)
            current = part.lstrip()
            continue
        current = candidate
    if current:
        chunks.append(current)
    return chunks


def _message_with_question_context(text: str, *, question_context: str | None = None) -> str:
    cleaned = str(text or "").strip()
    question = str(question_context or "").strip()
    if not question:
        return cleaned
    return f"{question}\n{cleaned}".strip()


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
