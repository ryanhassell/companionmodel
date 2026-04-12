from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy import func, select

from app.ai.runtime import AIGeneration
from app.ai.schemas import (
    MemoryCommitPlan,
    MemoryExtractionResult,
    MemoryPlanAction,
    MemoryPlanEntityDraft,
    MemoryPlanMemoryDraft,
    MemorySemanticPayload,
    ParentChatResponse,
    ParentGuidanceMemoryDraft,
)
from app.db.session import get_db_session
from app.models.enums import HouseholdRole, MemoryRelationshipType, MemoryType
from app.models.memory import MemoryItem, MemoryRelationship
from app.models.persona import Persona
from app.models.portal import Account, ChildProfile, CustomerUser, Household, PortalChatMessage, PortalChatRun, PortalChatThread
from app.models.user import User
from app.portal.dependencies import PortalRequestContext, require_portal_context
from app.routers import portal
from app.services.config import ConfigService
from app.services.conversation import ConversationService
from app.services.memory import MemoryService
from app.services.parent_chat import ParentChatService
from app.services.prompt import PromptService
from app.services.rate_limiter import RateLimiterService


class _FakeAiRuntime:
    def __init__(self) -> None:
        self.enabled = True
        self.prompt = ""
        self.deps = None

    async def parent_chat(self, *, prompt, deps, temperature=None, max_tokens=None):
        self.prompt = prompt
        self.deps = deps
        return AIGeneration(
            output=ParentChatResponse(
                text="I understand. I'll remember that for Katie and keep it in mind when I respond. If there's anything specific to avoid, tell me and I'll hold onto that too."
            ),
            model="gpt-test",
            usage={"input_tokens": 12, "output_tokens": 22},
        )

    async def extract_memories(self, *, prompt, max_tokens=None):
        return AIGeneration(
            output=MemoryExtractionResult(facts=[]),
            model="gpt-test",
            usage={"input_tokens": 8, "output_tokens": 10},
        )

    async def plan_memory_commit(self, *, prompt, max_tokens=None, request_limit=None):
        latest = _latest_content_from_memory_prompt(prompt)
        actions = _memory_plan_actions_for_test_text(latest, prompt=prompt)
        return AIGeneration(
            output=MemoryCommitPlan(summary="test plan", actions=actions),
            model="gpt-test",
            usage={"input_tokens": 18, "output_tokens": 26},
        )

    async def embed_documents(self, texts):
        return [[1.0, 0.0, 0.0] for _ in texts]

    async def embed_query(self, text):
        return [1.0, 0.0, 0.0]

    @asynccontextmanager
    async def parent_chat_stream(self, *, prompt, deps, temperature=None, max_tokens=None):
        result = await self.parent_chat(prompt=prompt, deps=deps, temperature=temperature, max_tokens=max_tokens)

        class _FakeStreamResult:
            def __init__(self, generation):
                self._generation = generation
                self.response = type("Resp", (), {"model_name": generation.model})()

            async def stream_text(self, delta=True, debounce_by=0.03):
                text = self._generation.output.text
                midpoint = max(len(text) // 2, 1)
                for chunk in [text[:midpoint], text[midpoint:]]:
                    if chunk:
                        yield chunk

            async def get_output(self):
                return self._generation.output

            def usage(self):
                return type("Usage", (), self._generation.usage)()

        yield _FakeStreamResult(result)


def _latest_content_from_memory_prompt(prompt: str) -> str:
    marker = "Latest content:\n"
    if marker not in prompt:
        return ""
    trailing = prompt.split(marker, 1)[1]
    for end_marker in ("\n\nRecent snippets:", "\nRecent snippets:"):
        if end_marker in trailing:
            trailing = trailing.split(end_marker, 1)[0]
            break
    return " ".join(trailing.split()).strip()


def _memory_action(
    *,
    title: str,
    content: str,
    memory_type: str = "fact",
    tags: list[str] | None = None,
    importance: float = 0.72,
    world_section: str = "memories",
    group: str | None = None,
    path: list[str] | None = None,
    entity_name: str | None = None,
    entity_kind: str | None = None,
    relation_to_child: str | None = None,
    canonical_value: str | None = None,
    ref: str | None = None,
    target_memory_ref: str | None = None,
    target_memory_id: str | None = None,
) -> MemoryPlanAction:
    entity_draft = None
    if entity_name:
        entity_draft = MemoryPlanEntityDraft(
            display_name=entity_name,
            relation_to_child=relation_to_child,
            canonical_value=canonical_value,
            entity_kind_legacy=entity_kind,
            semantic=MemorySemanticPayload(
                world_section=world_section,
                kind=entity_kind or "topic",
                group=group,
                label=entity_name,
                relation=relation_to_child,
                path=list(path or []),
                confidence=0.9,
            ),
        )
    return MemoryPlanAction(
        action="create_memory",
        ref=ref,
        target_memory_ref=target_memory_ref,
        target_memory_id=target_memory_id,
        memory=MemoryPlanMemoryDraft(
            title=title,
            content=content,
            summary=content,
            memory_type=memory_type,
            tags=list(tags or []),
            importance_score=importance,
            semantic=MemorySemanticPayload(
                world_section=world_section,
                kind=memory_type,
                group=group,
                label=title,
                relation=relation_to_child,
                path=list(path or []),
                confidence=0.9,
            ),
        ),
        entity=entity_draft,
    )


def _memory_plan_actions_for_test_text(text: str, *, prompt: str) -> list[MemoryPlanAction]:
    normalized = text.lower()
    if not normalized:
        return []
    if "what's your name" in normalized or "what is your name" in normalized:
        return []
    if "no i mean like what is it to katie" in normalized:
        return []
    if "katie loves birthdays" in normalized and "september 16th 2004" not in normalized:
        if "taylor swift" in normalized:
            return [
                _memory_action(
                    title="Katie loves birthdays",
                    content="Katie loves birthdays.",
                    memory_type="preference",
                    tags=["birthdays"],
                    group="preferences",
                    path=["Likes and preferences"],
                ),
                _memory_action(
                    title="Likes and preferences",
                    content="Katie likes Taylor Swift.",
                    memory_type="preference",
                    tags=["music"],
                    group="preferences",
                    path=["Likes and preferences", "Music"],
                ),
            ]
        return [
            _memory_action(
                title="Katie loves birthdays",
                content="Katie loves birthdays.",
                memory_type="preference",
                tags=["birthdays"],
                group="preferences",
                path=["Likes and preferences"],
            )
        ]
    if "please keep things more gentle after school" in normalized:
        return [
            _memory_action(
                title="After-school pacing",
                content="Please keep things more gentle after school and don't push too hard if she's overwhelmed.",
                memory_type="operator_note",
                tags=["parent-guidance", "after-school"],
                group="guidance",
                path=["Parent guidance"],
            )
        ]
    if "she is 21" in normalized and "emma and zoe" in normalized:
        return [
            _memory_action(title="Katie's age", content="Katie is 21 years old.", tags=["profile"], group="identity", path=["Profile"]),
            _memory_action(title="Katie's birthday", content="Katie's birthday is September 16th 2004.", tags=["birthday"], group="identity", path=["Profile"]),
            _memory_action(title="Katie loves birthdays", content="Katie loves birthdays.", memory_type="preference", tags=["birthdays"], group="preferences", path=["Likes and preferences"]),
            _memory_action(title="Best friends", content="Katie's best friends are Emma and Zoe.", tags=["friends"], group="friends", path=["Social world", "Friends"], ref="friends_summary"),
            _memory_action(title="Friend: Emma", content="Emma is one of Katie's best friends.", tags=["friends"], group="friends", path=["Social world", "Friends"], entity_name="Emma", entity_kind="friend", relation_to_child="best friend", target_memory_ref="friends_summary"),
            _memory_action(title="Friend: Zoe", content="Zoe is one of Katie's best friends.", tags=["friends"], group="friends", path=["Social world", "Friends"], entity_name="Zoe", entity_kind="friend", relation_to_child="best friend", target_memory_ref="friends_summary"),
        ]
    if "favorite taylor swift song" in normalized and "kitten" in normalized and "ophelia" in normalized:
        return [
            _memory_action(title="Favorite Taylor Swift Song", content="Katie's favorite Taylor Swift song is Ophelia.", memory_type="preference", tags=["music", "favorite"], group="favorites", path=["Likes and preferences", "Music"]),
            _memory_action(title="Getting a Kitten", content="The family is getting a kitten next week.", tags=["pets"], group="pets", path=["Home life", "Pets"]),
            _memory_action(title="Kitten Name", content="The kitten's name is Ophelia.", tags=["pets"], group="pets", path=["Home life", "Pets"], entity_name="Ophelia", entity_kind="pet", relation_to_child="family pet"),
        ]
    if "katie loves ice cream" in normalized or "katie really enjoys ice cream" in normalized:
        if "Memory ID:" in prompt and "really enjoys" in normalized:
            target_memory_id = prompt.split("Memory ID:", 1)[1].splitlines()[0].strip()
            return [
                MemoryPlanAction(
                    action="update_memory",
                    target_memory_id=target_memory_id,
                    memory=MemoryPlanMemoryDraft(
                        title="Ice Cream Preference",
                        content="Katie really enjoys ice cream.",
                        summary="Katie really enjoys ice cream.",
                        memory_type="preference",
                        tags=["ice cream"],
                        importance_score=0.78,
                        semantic=MemorySemanticPayload(
                            world_section="memories",
                            kind="preference",
                            group="preferences",
                            label="Ice Cream Preference",
                            path=["Likes and preferences"],
                            confidence=0.9,
                        ),
                    ),
                    entity=MemoryPlanEntityDraft(
                        display_name="ice cream",
                        canonical_value="ice cream",
                        entity_kind_legacy="topic",
                        semantic=MemorySemanticPayload(
                            world_section="memories",
                            kind="topic",
                            group="preferences",
                            label="ice cream",
                            path=["Likes and preferences"],
                            confidence=0.9,
                        ),
                    ),
                )
            ]
        return [
            _memory_action(
                title="Favorite Color" if "favorite color" in normalized else "Ice Cream Preference",
                content="Katie's favorite color is blue." if "favorite color" in normalized else "Katie loves ice cream.",
                memory_type="preference",
                tags=["favorite", "color"] if "favorite color" in normalized else ["ice cream"],
                group="preferences",
                path=["Likes and preferences"],
                entity_name="ice cream" if "ice cream" in normalized else None,
                entity_kind="topic" if "ice cream" in normalized else None,
                canonical_value="ice cream" if "ice cream" in normalized else None,
            )
        ]
    if "hi! i am katie's mom" in normalized and "katie likes taylor swift" in normalized:
        return [
            _memory_action(title="Likes and preferences", content="Katie likes Taylor Swift.", memory_type="preference", tags=["music"], group="preferences", path=["Likes and preferences"])
        ]
    if "rain sounds" in normalized and "lavender lotion" in normalized:
        return [
            _memory_action(title="Comfort sound", content="Rain sounds help Katie feel calm.", memory_type="preference", tags=["comfort", "sound"], group="preferences", path=["Comfort"]),
            _memory_action(title="Comfort scent", content="Lavender lotion helps Katie feel calm.", memory_type="preference", tags=["comfort", "scent"], group="preferences", path=["Comfort"]),
        ]
    if "her best friends are emma and zoe" in normalized:
        return [
            _memory_action(title="Best friends", content="Katie's best friends are Emma and Zoe.", tags=["friends"], group="friends", path=["Social world", "Friends"], ref="friends_summary"),
            _memory_action(title="Friend: Emma", content="Emma is one of Katie's best friends.", tags=["friends"], group="friends", path=["Social world", "Friends"], entity_name="Emma", entity_kind="friend", relation_to_child="best friend", target_memory_ref="friends_summary"),
            _memory_action(title="Friend: Zoe", content="Zoe is one of Katie's best friends.", tags=["friends"], group="friends", path=["Social world", "Friends"], entity_name="Zoe", entity_kind="friend", relation_to_child="best friend", target_memory_ref="friends_summary"),
        ]
    if "her brother, ryan" in normalized and "video games" in normalized:
        return [
            _memory_action(title="Brother: Ryan", content="Katie's brother is Ryan.", tags=["family"], group="family", path=["Family"], entity_name="Ryan", entity_kind="family_member", relation_to_child="brother", ref="ryan"),
            _memory_action(title="Ryan's interests", content="Ryan likes video games like Zelda and Pokemon.", memory_type="preference", tags=["family", "games"], group="interests", path=["Family", "Ryan"], entity_name="Ryan", entity_kind="family_member", relation_to_child="brother", target_memory_ref="ryan"),
        ]
    if "katie's favorite color is blue" in normalized:
        return [
            _memory_action(title="Favorite Color", content="Katie's favorite color is blue.", memory_type="preference", tags=["favorite", "color"], group="preferences", path=["Likes and preferences"])
        ]
    return []


class _FakeClerkAuthService:
    enabled = True


class _FakeContainer:
    def __init__(self, settings) -> None:
        self.settings = settings
        self.clerk_auth_service = _FakeClerkAuthService()
        self.rate_limiter_service = RateLimiterService(settings)
        self.ai_runtime = _FakeAiRuntime()
        self.config_service = ConfigService(settings)
        self.conversation_service = ConversationService()
        self.memory_service = MemoryService(settings, self.ai_runtime, PromptService(settings))
        self.parent_chat_service = ParentChatService(
            settings,
            self.ai_runtime,
            self.config_service,
            self.conversation_service,
            self.memory_service,
        )


async def _portal_fixture(sqlite_session, settings):
    account = Account(name="Resona Family", slug="resona-family", clerk_org_id="org_test")
    sqlite_session.add(account)
    await sqlite_session.flush()

    customer_user = CustomerUser(
        account_id=account.id,
        email="parent@example.com",
        password_hash="clerk:test",
        display_name="Ryan",
        relationship_label="mom",
    )
    sqlite_session.add(customer_user)
    await sqlite_session.flush()

    household = Household(
        account_id=account.id,
        name="Resona Home",
        timezone="America/New_York",
    )
    sqlite_session.add(household)
    await sqlite_session.flush()

    persona = Persona(
        key="marigold",
        display_name="Marigold",
        description="Bright, warm, and playful with a steady emotional center.",
        style="Curious, natural, and gently encouraging.",
        tone="Calm, affectionate, and lightly playful.",
        speech_style="Short, casual, text-friendly replies.",
        topics_of_interest=["music", "pets"],
        favorite_activities=["singing", "sharing little moments"],
        is_active=True,
    )
    sqlite_session.add(persona)
    await sqlite_session.flush()

    companion = User(display_name="Katie", phone_number="+16105550111")
    companion.preferred_persona_id = persona.id
    sqlite_session.add(companion)
    await sqlite_session.flush()

    child = ChildProfile(
        account_id=account.id,
        household_id=household.id,
        first_name="Katie",
        display_name="Katie",
        companion_user_id=companion.id,
        preferences_json={"preferred_pacing": ["gentle"], "response_style": ["encouraging"], "voice_enabled": True},
        boundaries_json={"parent_visibility_mode": "full_transcript", "alert_threshold": "low"},
        routines_json={"daily_cadence": "adaptive"},
    )
    sqlite_session.add(child)
    await sqlite_session.commit()

    container = _FakeContainer(settings)
    context = PortalRequestContext(
        customer_user=customer_user,
        account_id=str(account.id),
        role=HouseholdRole.owner,
        clerk_user_id="user_test",
        clerk_org_id="org_test",
        mfa_verified=True,
        csrf_token="csrf-test",
        container=container,
    )

    app = FastAPI()
    app.include_router(portal.router)
    app.state.container = container

    async def _context_override():
        return context

    async def _session_override():
        return sqlite_session

    app.dependency_overrides[require_portal_context] = _context_override
    app.dependency_overrides[get_db_session] = _session_override
    return app, context, child, container


async def test_parent_chat_page_renders(settings, sqlite_session):
    app, context, child, container = await _portal_fixture(sqlite_session, settings)
    await container.parent_chat_service.get_or_create_thread(
        sqlite_session,
        account_id=context.customer_user.account_id,
        customer_user=context.customer_user,
        child_profile=child,
    )
    await sqlite_session.commit()

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/app/parent-chat")

    assert response.status_code == 200
    assert "Parent Chat" in response.text
    assert "Katie" in response.text
    assert "New chat" in response.text
    assert "Clear chat history" in response.text
    assert 'name="csrf_token" value="csrf-test"' in response.text
    assert "Shape how Resona shows up" not in response.text
    assert "Good Uses" not in response.text


async def test_parent_chat_history_shows_added_memory_subtext_under_resona(settings, sqlite_session):
    app, context, child, container = await _portal_fixture(sqlite_session, settings)
    thread = await container.parent_chat_service.get_or_create_thread(
        sqlite_session,
        account_id=context.customer_user.account_id,
        customer_user=context.customer_user,
        child_profile=child,
    )
    await container.parent_chat_service.send_message(
        sqlite_session,
        account_id=context.customer_user.account_id,
        customer_user=context.customer_user,
        child_profile=child,
        text="Katie loves birthdays.",
        thread=thread,
    )
    await sqlite_session.commit()

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/app/parent-chat")

    assert response.status_code == 200
    assert "Added memory:" in response.text
    assert "Tool" not in response.text


async def test_parent_chat_send_creates_messages_and_uses_parent_context(settings, sqlite_session):
    app, context, child, container = await _portal_fixture(sqlite_session, settings)

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/app/parent-chat/send",
            json={
                "csrf_token": context.csrf_token,
                "message": "Please keep things more gentle after school and don't push too hard if she's overwhelmed.",
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert [message["sender"] for message in payload["messages"]] == ["parent", "assistant"]
    assert payload["messages"][0]["memory_saved"] is True
    assert str(payload["messages"][0]["memory_saved_label"]).startswith("Saved")
    assert payload["messages"][0]["memory_saved_details"]
    assert "Parent relationship: Mom" in container.ai_runtime.prompt
    assert "Active companion persona: Marigold" in container.ai_runtime.prompt
    assert "Preferred pacing: Gentle" in container.ai_runtime.prompt
    assert "Latest parent message:" in container.ai_runtime.prompt
    assert container.ai_runtime.deps.persona_name == "Marigold"
    assert container.ai_runtime.deps.persona_style == "Curious, natural, and gently encouraging."

    stored_count = await sqlite_session.scalar(select(func.count()).select_from(PortalChatMessage))
    assert stored_count == 2

    stored_messages = list(
        (
            await sqlite_session.execute(
                select(PortalChatMessage).order_by(PortalChatMessage.created_at)
            )
        )
        .scalars()
        .all()
    )
    assert stored_messages[0].sender == "parent"
    assert stored_messages[1].sender == "assistant"
    assert stored_messages[0].metadata_json.get("saved_to_memory") is True

    guidance_query = (
        select(MemoryItem)
        .where(MemoryItem.user_id == child.companion_user_id)
        .order_by(MemoryItem.created_at.desc())
    )
    guidance_memories = list((await sqlite_session.execute(guidance_query)).scalars().all())
    assert guidance_memories
    assert all(memory.memory_type in {MemoryType.preference, MemoryType.operator_note} for memory in guidance_memories)
    assert any("Please keep things more gentle after school" in memory.content for memory in guidance_memories)


async def test_parent_chat_stream_endpoint_emits_live_events_and_persists_run(settings, sqlite_session):
    app, context, child, _ = await _portal_fixture(sqlite_session, settings)

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test", timeout=10.0) as client:
        async with client.stream(
            "POST",
            "/app/parent-chat/stream",
            json={
                "csrf_token": context.csrf_token,
                "message": "Katie loves birthdays and Taylor Swift.",
            },
        ) as response:
            assert response.status_code == 200
            body = await response.aread()

    text = body.decode("utf-8")
    assert "event: thread_ready" in text
    assert "event: assistant_delta" in text
    assert "event: assistant_message" in text
    assert "event: run_complete" in text
    assert "Learned 2 things" in text

    run_count = await sqlite_session.scalar(select(func.count()).select_from(PortalChatRun))
    assert run_count == 1
    message_count = await sqlite_session.scalar(select(func.count()).select_from(PortalChatMessage))
    assert message_count == 2


async def test_parent_chat_rich_message_creates_multiple_memories(settings, sqlite_session):
    app, context, child, _ = await _portal_fixture(sqlite_session, settings)

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/app/parent-chat/send",
            json={
                "csrf_token": context.csrf_token,
                "message": "She is 21, her birthday is September 16th 2004 and she loves birthdays. Her best friends are Emma and Zoe.",
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["messages"][0]["memory_saved"] is True
    assert payload["messages"][0]["memory_saved_label"] == "Saved 6 memories"
    assert payload["messages"][1]["activity_events"][0]["label"] == "Learned 6 things"
    assert payload["messages"][1]["activity_events"][0]["details"]
    assert payload["messages"][1]["activity_events"][0]["href"] == "/app/memories/library"

    stored_memories = list(
        (
            await sqlite_session.execute(
                select(MemoryItem)
                .where(MemoryItem.user_id == child.companion_user_id)
                .order_by(MemoryItem.created_at)
            )
        )
        .scalars()
        .all()
    )
    contents = [memory.content for memory in stored_memories]
    titles = [memory.title for memory in stored_memories]
    assert len(stored_memories) == 6
    assert "Katie is 21 years old." in contents
    assert "Katie's birthday is September 16th 2004." in contents
    assert "Katie loves birthdays." in contents
    assert "Katie's best friends are Emma and Zoe." in contents
    assert "Emma is one of Katie's best friends." in contents
    assert "Zoe is one of Katie's best friends." in contents
    assert "Friend: Emma" in titles
    assert "Friend: Zoe" in titles

    relationships = list((await sqlite_session.execute(select(MemoryRelationship))).scalars().all())
    manual_edges = [row for row in relationships if row.relationship_type == MemoryRelationshipType.manual_child]
    assert len(manual_edges) == 2


async def test_parent_chat_turns_one_message_into_clean_fact_memories(settings, sqlite_session):
    app, context, child, _ = await _portal_fixture(sqlite_session, settings)

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/app/parent-chat/send",
            json={
                "csrf_token": context.csrf_token,
                "message": "I'd say her favorite Taylor Swift song is 'Ophelia', which is funny because we are getting a kitten next week, and the kitten's name is Ophelia.",
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["messages"][0]["memory_saved"] is True
    assert payload["messages"][0]["memory_saved_label"] == "Saved 3 memories"
    assert [item["title"] for item in payload["messages"][0]["memory_saved_details"]] == [
        "Favorite Taylor Swift Song",
        "Getting a Kitten",
        "Kitten Name",
    ]

    stored_memories = list(
        (
            await sqlite_session.execute(
                select(MemoryItem)
                .where(MemoryItem.user_id == child.companion_user_id)
                .order_by(MemoryItem.created_at)
            )
        )
        .scalars()
        .all()
    )
    contents = [memory.content for memory in stored_memories]
    titles = [memory.title for memory in stored_memories]
    assert "Katie's favorite Taylor Swift song is Ophelia." in contents
    assert "The family is getting a kitten next week." in contents
    assert "The kitten's name is Ophelia." in contents
    assert "Favorite Taylor Swift Song" in titles
    assert "Getting a Kitten" in titles
    assert "Kitten Name" in titles
    assert all("I'd say" not in content for content in contents)


async def test_parent_chat_follow_up_question_does_not_create_memory(settings, sqlite_session):
    app, context, child, _ = await _portal_fixture(sqlite_session, settings)

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first_response = await client.post(
            "/app/parent-chat/send",
            json={
                "csrf_token": context.csrf_token,
                "message": "She is 21, her birthday is September 16th 2004 and she loves birthdays.",
            },
        )
        assert first_response.status_code == 200

        first_count = await sqlite_session.scalar(select(func.count()).select_from(MemoryItem))

        second_response = await client.post(
            "/app/parent-chat/send",
            json={
                "csrf_token": context.csrf_token,
                "message": "Yeah of course. What's your name?",
            },
        )

    assert second_response.status_code == 200
    payload = second_response.json()
    assert payload["messages"][0]["memory_saved"] is False
    second_count = await sqlite_session.scalar(select(func.count()).select_from(MemoryItem))
    assert second_count == first_count


async def test_parent_chat_clarifying_follow_up_without_question_mark_does_not_create_memory_and_keeps_recent_memory_context(
    settings,
    sqlite_session,
):
    app, context, child, container = await _portal_fixture(sqlite_session, settings)

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        seeded = await client.post(
            "/app/parent-chat/send",
            json={
                "csrf_token": context.csrf_token,
                "message": "I'd say her favorite Taylor Swift song is 'Ophelia', which is funny because we are getting a kitten next week, and the kitten's name is Ophelia.",
            },
        )
        assert seeded.status_code == 200

        first_count = await sqlite_session.scalar(select(func.count()).select_from(MemoryItem))

        follow_up = await client.post(
            "/app/parent-chat/send",
            json={
                "csrf_token": context.csrf_token,
                "message": "No i mean like what is it to Katie.",
            },
        )

    assert follow_up.status_code == 200
    payload = follow_up.json()
    assert payload["messages"][0]["memory_saved"] is False
    second_count = await sqlite_session.scalar(select(func.count()).select_from(MemoryItem))
    assert second_count == first_count
    assert "Favorite Taylor Swift Song" in container.ai_runtime.prompt
    assert "Katie's favorite Taylor Swift song is Ophelia." in container.ai_runtime.prompt


async def test_parent_chat_memory_reflection_questions_include_memory_inventory(settings, sqlite_session):
    app, context, child, container = await _portal_fixture(sqlite_session, settings)

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        seeded = await client.post(
            "/app/parent-chat/send",
            json={
                "csrf_token": context.csrf_token,
                "message": "Katie loves birthdays.",
            },
        )
        assert seeded.status_code == 200

        reflected = await client.post(
            "/app/parent-chat/send",
            json={
                "csrf_token": context.csrf_token,
                "message": "What do you remember about Katie?",
            },
        )

    assert reflected.status_code == 200
    assert "Memory inventory snapshot:" in container.ai_runtime.prompt
    assert "Katie loves birthdays." in container.ai_runtime.prompt


async def test_parent_chat_semantic_duplicate_preferences_merge_instead_of_stacking(settings, sqlite_session):
    app, context, child, container = await _portal_fixture(sqlite_session, settings)
    saved_contents: list[str] = []

    async def _dedupe_saves(*, prompt, deps, temperature=None, max_tokens=None):
        draft = ParentGuidanceMemoryDraft(
            title="Likes and preferences",
            content=saved_contents.pop(0),
            memory_type="preference",
            tags=["parent-guidance", "preference", "likes"],
            importance_score=0.78,
            facet="preferences",
            canonical_value="ice cream",
        )
        await deps.save_guidance_memories([draft])
        return AIGeneration(
            output=ParentChatResponse(text="I’ve got that and I’ll remember it."),
            model="gpt-test",
            usage={"input_tokens": 10, "output_tokens": 12},
        )

    container.ai_runtime.parent_chat = _dedupe_saves

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        saved_contents[:] = ["Katie loves ice cream."]
        first = await client.post(
            "/app/parent-chat/send",
            json={"csrf_token": context.csrf_token, "message": "Katie loves ice cream."},
        )
        assert first.status_code == 200

        saved_contents[:] = ["Katie really enjoys ice cream."]
        second = await client.post(
            "/app/parent-chat/send",
            json={"csrf_token": context.csrf_token, "message": "Katie really enjoys ice cream."},
        )

    assert second.status_code == 200
    stored_memories = list(
        (
            await sqlite_session.execute(
                select(MemoryItem)
                .where(MemoryItem.user_id == child.companion_user_id)
                .order_by(MemoryItem.created_at)
            )
        )
        .scalars()
        .all()
    )
    assert len(stored_memories) == 1
    assert stored_memories[0].metadata_json.get("canonical_value") == "ice cream"


async def test_parent_chat_filters_parent_self_intro_and_greeting_from_memories(settings, sqlite_session):
    app, context, child, _ = await _portal_fixture(sqlite_session, settings)

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/app/parent-chat/send",
            json={
                "csrf_token": context.csrf_token,
                "message": "Hi! I am Katie's mom. Katie likes Taylor Swift.",
            },
        )

    assert response.status_code == 200
    payload = response.json()
    details = payload["messages"][0]["memory_saved_details"]
    assert len(details) == 1
    assert details[0]["content"] == "Katie likes Taylor Swift."

    stored_memories = list(
        (
            await sqlite_session.execute(
                select(MemoryItem)
                .where(MemoryItem.user_id == child.companion_user_id)
                .order_by(MemoryItem.created_at)
            )
        )
        .scalars()
        .all()
    )
    assert len(stored_memories) == 1
    assert stored_memories[0].content == "Katie likes Taylor Swift."


async def test_parent_chat_uses_ai_fallback_drafting_before_regex(settings, sqlite_session):
    app, context, child, container = await _portal_fixture(sqlite_session, settings)

    async def _ai_extract(*, prompt, max_tokens=None):
        return AIGeneration(
            output=MemoryExtractionResult(
                facts=[
                    {
                        "title": "Comfort sound",
                        "content": "Rain sounds help Katie feel calm.",
                        "summary": "Rain sounds help Katie feel calm.",
                        "memory_type": "preference",
                        "tags": ["comfort", "sound"],
                        "importance_score": 0.76,
                        "entity_name": "Rain sounds",
                        "entity_kind": "topic",
                        "facet": "preferences",
                        "canonical_value": "rain sounds",
                    },
                    {
                        "title": "Comfort scent",
                        "content": "Lavender lotion helps Katie feel calm.",
                        "summary": "Lavender lotion helps Katie feel calm.",
                        "memory_type": "preference",
                        "tags": ["comfort", "scent"],
                        "importance_score": 0.74,
                        "entity_name": "Lavender lotion",
                        "entity_kind": "topic",
                        "facet": "preferences",
                        "canonical_value": "lavender lotion",
                    },
                ]
            ),
            model="gpt-test",
            usage={"input_tokens": 10, "output_tokens": 24},
        )

    container.ai_runtime.extract_memories = _ai_extract

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/app/parent-chat/send",
            json={
                "csrf_token": context.csrf_token,
                "message": "She calms down with rain sounds and lavender lotion.",
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["messages"][0]["memory_saved_label"] == "Saved 2 memories"
    assert [item["title"] for item in payload["messages"][0]["memory_saved_details"]] == [
        "Comfort sound",
        "Comfort scent",
    ]


async def test_parent_chat_rejects_context_only_memory_drafts(settings, sqlite_session):
    app, context, child, container = await _portal_fixture(sqlite_session, settings)

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        seeded = await client.post(
            "/app/parent-chat/send",
            json={
                "csrf_token": context.csrf_token,
                "message": "She is 21, her birthday is September 16th 2004 and she loves birthdays. Her best friends are Marissa and Alyssa.",
            },
        )
        assert seeded.status_code == 200

    initial_count = await sqlite_session.scalar(select(func.count()).select_from(MemoryItem))

    async def _context_bleed(*, prompt, deps, temperature=None, max_tokens=None):
        await deps.save_guidance_memories(
            [
                ParentGuidanceMemoryDraft(
                    title="Katie's Profile",
                    content=(
                        "Katie is 21 years old, her birthday is September 16th, 2004. "
                        "She loves birthdays and has best friends named Marissa and Alyssa."
                    ),
                    memory_type="fact",
                    tags=["profile"],
                    importance_score=0.8,
                )
            ]
        )
        return AIGeneration(
            output=ParentChatResponse(
                text="My name is Resona, and I'm here for Katie."
            ),
            model="gpt-test",
            usage={"input_tokens": 12, "output_tokens": 18},
        )

    container.ai_runtime.parent_chat = _context_bleed

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/app/parent-chat/send",
            json={
                "csrf_token": context.csrf_token,
                "message": "What's your name?",
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["messages"][0]["memory_saved"] is False
    final_count = await sqlite_session.scalar(select(func.count()).select_from(MemoryItem))
    assert final_count == initial_count


async def test_parent_chat_ai_guidance_drafts_can_create_friend_summary_and_person_links(settings, sqlite_session):
    app, context, child, _ = await _portal_fixture(sqlite_session, settings)

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/app/parent-chat/send",
            json={
                "csrf_token": context.csrf_token,
                "message": "Her best friends are Emma and Zoe.",
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["messages"][0]["memory_saved_label"] == "Saved 3 memories"
    assert [item["title"] for item in payload["messages"][0]["memory_saved_details"]] == [
        "Best friends",
        "Friend: Emma",
        "Friend: Zoe",
    ]

    stored_memories = list(
        (
            await sqlite_session.execute(
                select(MemoryItem)
                .where(MemoryItem.user_id == child.companion_user_id)
                .order_by(MemoryItem.created_at)
            )
        )
        .scalars()
        .all()
    )
    assert len(stored_memories) == 3
    emma = next(memory for memory in stored_memories if memory.title == "Friend: Emma")
    zoe = next(memory for memory in stored_memories if memory.title == "Friend: Zoe")
    assert emma.metadata_json.get("entity_name") == "Emma"
    assert emma.metadata_json.get("entity_kind") == "friend"
    assert zoe.metadata_json.get("entity_name") == "Zoe"

    relationships = list(
        (
            await sqlite_session.execute(
                select(MemoryRelationship).where(
                    MemoryRelationship.relationship_type == MemoryRelationshipType.manual_child
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(relationships) == 2


async def test_parent_chat_family_member_interest_creates_structured_family_memories(settings, sqlite_session):
    app, context, child, _ = await _portal_fixture(sqlite_session, settings)

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/app/parent-chat/send",
            json={
                "csrf_token": context.csrf_token,
                "message": "Her brother, Ryan, likes video games, like zelda, pokemon, etc.",
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["messages"][0]["memory_saved_label"] == "Saved 2 memories"
    assert [item["title"] for item in payload["messages"][0]["memory_saved_details"]] == [
        "Brother: Ryan",
        "Ryan's interests",
    ]

    stored_memories = list(
        (
            await sqlite_session.execute(
                select(MemoryItem)
                .where(MemoryItem.user_id == child.companion_user_id)
                .order_by(MemoryItem.created_at)
            )
        )
        .scalars()
        .all()
    )
    assert len(stored_memories) == 2
    assert stored_memories[0].content == "Katie's brother is Ryan."
    assert stored_memories[0].metadata_json.get("entity_name") == "Ryan"
    assert stored_memories[0].metadata_json.get("entity_kind") == "family_member"
    assert stored_memories[1].content == "Ryan likes video games like Zelda and Pokemon."
    assert stored_memories[1].metadata_json.get("entity_name") == "Ryan"
    assert stored_memories[1].metadata_json.get("entity_kind") == "family_member"

    relationships = list(
        (
            await sqlite_session.execute(
                select(MemoryRelationship).where(
                    MemoryRelationship.relationship_type == MemoryRelationshipType.manual_child
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(relationships) == 1


async def test_parent_chat_serializes_duplicate_save_tool_calls_with_one_result(settings, sqlite_session):
    app, context, child, container = await _portal_fixture(sqlite_session, settings)

    async def _double_save(*, prompt, deps, temperature=None, max_tokens=None):
        drafts = [
            ParentGuidanceMemoryDraft(
                title="Favorite Color",
                content="Katie's favorite color is blue.",
                memory_type="preference",
                tags=["favorite", "color"],
                importance_score=0.7,
            )
        ]
        first, second = await asyncio.gather(
            deps.save_guidance_memories(drafts),
            deps.save_guidance_memories(drafts),
        )
        assert first.saved_count == second.saved_count == 1
        assert first.memory_ids == second.memory_ids
        return AIGeneration(
            output=ParentChatResponse(
                text="I understand. I'll remember that Katie's favorite color is blue."
            ),
            model="gpt-test",
            usage={"input_tokens": 12, "output_tokens": 18},
        )

    container.ai_runtime.parent_chat = _double_save

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/app/parent-chat/send",
            json={
                "csrf_token": context.csrf_token,
                "message": "Katie's favorite color is blue.",
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    stored_memories = list(
        (
            await sqlite_session.execute(
                select(MemoryItem)
                .where(MemoryItem.user_id == child.companion_user_id)
                .order_by(MemoryItem.created_at)
            )
        )
        .scalars()
        .all()
    )
    assert len(stored_memories) == 1
    assert stored_memories[0].content == "Katie's favorite color is blue."


async def test_parent_chat_send_form_fallback_redirects_back_to_chat(settings, sqlite_session):
    app, context, _, _ = await _portal_fixture(sqlite_session, settings)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        response = await client.post(
            "/app/parent-chat/send",
            data={
                "csrf_token": context.csrf_token,
                "message": "Please keep things light after dinner.",
            },
        )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/app/parent-chat?thread=")
    assert response.headers["location"].endswith("&sent=1")


async def test_parent_chat_can_create_new_thread_and_switch_between_threads(settings, sqlite_session):
    app, context, child, container = await _portal_fixture(sqlite_session, settings)

    first_thread = await container.parent_chat_service.get_or_create_thread(
        sqlite_session,
        account_id=context.customer_user.account_id,
        customer_user=context.customer_user,
        child_profile=child,
    )
    await container.parent_chat_service.send_message(
        sqlite_session,
        account_id=context.customer_user.account_id,
        customer_user=context.customer_user,
        child_profile=child,
        text="Keep things gentle after school.",
        thread=first_thread,
    )
    await sqlite_session.commit()

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        created = await client.post(
            "/app/parent-chat/new",
            data={"csrf_token": context.csrf_token},
        )

    assert created.status_code == 303
    location = created.headers["location"]
    assert location.startswith("/app/parent-chat?thread=")

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        switched = await client.get(location)

    assert switched.status_code == 200
    assert "Keep things gentle after school." not in switched.text
    assert "Parent conversations" in switched.text

    thread_count = await sqlite_session.scalar(select(func.count()).select_from(PortalChatThread))
    assert thread_count == 2


async def test_parent_chat_can_clear_current_thread_history(settings, sqlite_session):
    app, context, child, container = await _portal_fixture(sqlite_session, settings)

    thread = await container.parent_chat_service.get_or_create_thread(
        sqlite_session,
        account_id=context.customer_user.account_id,
        customer_user=context.customer_user,
        child_profile=child,
    )
    await container.parent_chat_service.send_message(
        sqlite_session,
        account_id=context.customer_user.account_id,
        customer_user=context.customer_user,
        child_profile=child,
        text="Katie loves birthdays.",
        thread=thread,
    )
    await sqlite_session.commit()

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        cleared = await client.post(
            f"/app/parent-chat/{thread.id}/clear",
            json={"csrf_token": context.csrf_token},
        )

    assert cleared.status_code == 200
    payload = cleared.json()
    assert payload["ok"] is True
    assert payload["messages"] == []

    message_count = await sqlite_session.scalar(select(func.count()).select_from(PortalChatMessage))
    assert message_count == 0
    run_count = await sqlite_session.scalar(select(func.count()).select_from(PortalChatRun))
    assert run_count == 0


async def test_parent_chat_send_returns_structured_unavailable_when_ai_is_disabled(settings, sqlite_session):
    app, context, _, container = await _portal_fixture(sqlite_session, settings)
    container.ai_runtime.enabled = False

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/app/parent-chat/send",
            json={
                "csrf_token": context.csrf_token,
                "message": "Please remember that Katie loves birthdays.",
            },
        )

    assert response.status_code == 503
    payload = response.json()
    assert payload["ok"] is False
    assert payload["code"] == "ai_unavailable"
    assert "unavailable" in payload["detail"].lower()


async def test_parent_chat_send_form_redirects_with_error_when_ai_is_disabled(settings, sqlite_session):
    app, context, _, container = await _portal_fixture(sqlite_session, settings)
    container.ai_runtime.enabled = False

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        response = await client.post(
            "/app/parent-chat/send",
            data={
                "csrf_token": context.csrf_token,
                "message": "Please remember that Katie loves birthdays.",
            },
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/app/parent-chat?error=unavailable"
