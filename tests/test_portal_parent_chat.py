from __future__ import annotations

import uuid

import httpx
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy import func, select

from app.ai.runtime import AIGeneration
from app.ai.schemas import ParentChatResponse
from app.db.session import get_db_session
from app.models.enums import HouseholdRole, MemoryRelationshipType, MemoryType
from app.models.memory import MemoryItem, MemoryRelationship
from app.models.portal import Account, ChildProfile, CustomerUser, Household, PortalChatMessage, PortalChatThread
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
    enabled = True

    def __init__(self) -> None:
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

    async def embed_documents(self, texts):
        return [[1.0, 0.0, 0.0] for _ in texts]

    async def embed_query(self, text):
        return [1.0, 0.0, 0.0]


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

    companion = User(display_name="Katie", phone_number="+16105550111")
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
    assert "Shape how Resona shows up" not in response.text
    assert "Good Uses" not in response.text


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
    assert "Preferred pacing: Gentle" in container.ai_runtime.prompt
    assert "Latest parent message:" in container.ai_runtime.prompt

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
    assert emma.metadata_json.get("entity_kind") == "person"
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
