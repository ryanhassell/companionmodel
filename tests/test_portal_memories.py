from __future__ import annotations

import httpx
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy import select

from app.db.session import get_db_session
from app.models.enums import HouseholdRole, MemoryRelationshipType, MemoryType
from app.models.memory import MemoryItem, MemoryRelationship
from app.models.portal import Account, ChildProfile, CustomerUser, Household
from app.models.user import User
from app.portal.dependencies import PortalRequestContext, require_portal_context
from app.routers import portal
from app.services.config import ConfigService
from app.services.memory import MemoryService
from app.services.prompt import PromptService


class _FakeOpenAIProvider:
    enabled = False


class _FakeClerkAuthService:
    enabled = True


class _FakeContainer:
    def __init__(self, settings) -> None:
        self.settings = settings
        self.clerk_auth_service = _FakeClerkAuthService()
        self.config_service = ConfigService(settings)
        self.memory_service = MemoryService(settings, _FakeOpenAIProvider(), PromptService(settings))


async def _portal_fixture(sqlite_session, settings):
    account = Account(name="Resona Family", slug="resona-family", clerk_org_id="org_test")
    sqlite_session.add(account)
    await sqlite_session.flush()

    customer_user = CustomerUser(
        account_id=account.id,
        email="parent@example.com",
        password_hash="clerk:test",
        display_name="Parent",
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
    return app, context, companion


async def test_memory_routes_render_grouped_navigation_and_redirect_legacy(sqlite_session, settings):
    app, _, companion = await _portal_fixture(sqlite_session, settings)
    sqlite_session.add_all(
        [
            MemoryItem(user_id=companion.id, memory_type=MemoryType.fact, title="Favorite song", content="Katie loves singing in the kitchen."),
            MemoryItem(user_id=companion.id, memory_type=MemoryType.preference, title="Evening rhythm", content="Evening check-ins feel easiest."),
        ]
    )
    await sqlite_session.commit()

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        legacy = await client.get("/app/memory", follow_redirects=False)
        map_page = await client.get("/app/memories/map")
        library_page = await client.get("/app/memories/library")

    assert legacy.status_code == 303
    assert legacy.headers["location"] == "/app/memories/map"
    assert map_page.status_code == 200
    assert "Memory Map" in map_page.text
    assert "Memory Library" in map_page.text
    assert "Overview" in map_page.text
    assert "Household" in map_page.text
    assert library_page.status_code == 200
    assert "Favorite song" in library_page.text


async def test_graph_data_separates_structural_and_similarity_edges(sqlite_session, settings):
    app, _, companion = await _portal_fixture(sqlite_session, settings)
    summary = MemoryItem(
        user_id=companion.id,
        memory_type=MemoryType.summary,
        title="After-school summary",
        content="Katie had a strong afternoon with music and drawing.",
        embedding_vector=[1.0, 0.0, 0.0],
    )
    fact = MemoryItem(
        user_id=companion.id,
        memory_type=MemoryType.fact,
        title="Drawing memory",
        content="Katie drew for twenty minutes after school.",
        consolidated_into_id=summary.id,
        embedding_vector=[0.99, 0.01, 0.0],
    )
    similar = MemoryItem(
        user_id=companion.id,
        memory_type=MemoryType.episode,
        title="Music memory",
        content="Katie sang while drawing in the afternoon.",
        embedding_vector=[0.98, 0.02, 0.0],
    )
    sqlite_session.add_all([summary, fact, similar])
    await sqlite_session.flush()
    fact.consolidated_into_id = summary.id
    await sqlite_session.commit()

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/app/memories/graph-data")

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert len(payload["nodes"]) == 3
    assert any(edge["relationship_type"] == "consolidated_into" for edge in payload["structural_edges"])
    assert payload["similarity_edges"]
    assert all(edge["kind"] == "similarity" for edge in payload["similarity_edges"])


async def test_memory_update_and_delete_preview_respect_structural_orphans(sqlite_session, settings):
    app, context, companion = await _portal_fixture(sqlite_session, settings)
    root = MemoryItem(
        user_id=companion.id,
        memory_type=MemoryType.fact,
        title="Camp memory",
        content="Katie talked about camp today.",
        summary="Katie talked about camp today.",
        embedding_vector=[1.0, 0.0, 0.0],
    )
    orphan_child = MemoryItem(
        user_id=companion.id,
        memory_type=MemoryType.follow_up,
        title="Camp follow-up",
        content="Resona checked in again about camp.",
        summary="Resona checked in again about camp.",
        embedding_vector=[0.99, 0.01, 0.0],
    )
    preserved_child = MemoryItem(
        user_id=companion.id,
        memory_type=MemoryType.preference,
        title="Music bridge",
        content="Music helped the conversation feel easier.",
        summary="Music helped the conversation feel easier.",
        embedding_vector=[0.5, 0.5, 0.0],
    )
    other_parent = MemoryItem(
        user_id=companion.id,
        memory_type=MemoryType.fact,
        title="Music memory",
        content="Katie likes singing and musical games.",
        summary="Katie likes singing and musical games.",
        embedding_vector=[0.49, 0.51, 0.0],
    )
    similar_only = MemoryItem(
        user_id=companion.id,
        memory_type=MemoryType.episode,
        title="Camp song",
        content="Katie sang a camp song again later.",
        summary="Katie sang a camp song again later.",
        embedding_vector=[1.0, 0.0, 0.01],
    )
    sqlite_session.add_all([root, orphan_child, preserved_child, other_parent, similar_only])
    await sqlite_session.flush()
    sqlite_session.add_all(
        [
            MemoryRelationship(
                user_id=companion.id,
                parent_memory_id=root.id,
                child_memory_id=orphan_child.id,
                relationship_type=MemoryRelationshipType.manual_child,
            ),
            MemoryRelationship(
                user_id=companion.id,
                parent_memory_id=root.id,
                child_memory_id=preserved_child.id,
                relationship_type=MemoryRelationshipType.manual_child,
            ),
            MemoryRelationship(
                user_id=companion.id,
                parent_memory_id=other_parent.id,
                child_memory_id=preserved_child.id,
                relationship_type=MemoryRelationshipType.manual_child,
            ),
        ]
    )
    await sqlite_session.commit()

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        update_response = await client.post(
            f"/app/memories/{root.id}",
            json={
                "csrf_token": context.csrf_token,
                "data": {
                    "title": "Camp memory updated",
                    "content": "Katie talked about camp and music today.",
                    "summary": "Katie talked about camp and music today.",
                    "tags": "camp, music",
                    "pinned": True,
                    "archived": False,
                },
            },
        )
        preview_response = await client.post(
            f"/app/memories/{root.id}/delete-preview",
            json={"csrf_token": context.csrf_token},
        )
        delete_response = await client.post(
            f"/app/memories/{root.id}/delete",
            json={"csrf_token": context.csrf_token},
        )

    assert update_response.status_code == 200
    updated = update_response.json()["memory"]
    assert updated["title"] == "Camp memory updated"
    assert updated["pinned"] is True
    assert updated["tags"] == ["camp", "music"]

    preview = preview_response.json()["preview"]
    preview_ids = {entry["id"] for entry in preview["affected"]}
    assert str(root.id) in preview_ids
    assert str(orphan_child.id) in preview_ids
    assert str(preserved_child.id) not in preview_ids
    assert str(similar_only.id) not in preview_ids

    assert delete_response.status_code == 200
    remaining_ids = {
        str(item_id)
        for item_id in (
            await sqlite_session.execute(select(MemoryItem.id).where(MemoryItem.user_id == companion.id))
        )
        .scalars()
        .all()
    }
    assert str(root.id) not in remaining_ids
    assert str(orphan_child.id) not in remaining_ids
    assert str(preserved_child.id) in remaining_ids
    assert str(similar_only.id) in remaining_ids
