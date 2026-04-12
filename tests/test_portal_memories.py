from __future__ import annotations

import httpx
import re
from types import SimpleNamespace
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy import func, select

from app.ai.schemas import MemoryPlacementDraft, MemoryPlacementRelatedEntity
from app.db.session import get_db_session
from app.models.admin import JobRun
from app.models.enums import EntityRelationKind, HouseholdRole, JobStatus, MemoryEntityKind, MemoryFacet, MemoryRelationshipType, MemoryType
from app.models.memory import MemoryEntity, MemoryEntityRelation, MemoryItem, MemoryItemEntity, MemoryRelationship
from app.models.portal import Account, ChildProfile, CustomerUser, Household
from app.models.user import User
from app.portal.dependencies import PortalRequestContext, require_portal_context
from app.routers import portal
from app.services.config import ConfigService
from app.services.memory import MemoryService
from app.services.prompt import PromptService


class _FakeOpenAIProvider:
    enabled = False


class _PlacementAwareRuntime:
    enabled = True

    async def infer_memory_placement(self, *, prompt: str, max_tokens: int = 260):
        return SimpleNamespace(
            output=MemoryPlacementDraft(
                primary_name=None,
                primary_kind="child",
                facet="favorites",
                relation_kind="child_world",
                canonical_value="Motion Sickness",
                related_entities=[
                    MemoryPlacementRelatedEntity(
                        display_name="Phoebe Bridgers",
                        entity_kind="artist",
                        relation_kind="favorite",
                        facet="favorites",
                        canonical_value="Phoebe Bridgers",
                    )
                ],
            )
        )


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
            MemoryItem(user_id=companion.id, memory_type=MemoryType.preference, title="Evening rhythm", content="Evening routine and after school check-ins feel easiest."),
        ]
    )
    await sqlite_session.commit()

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        legacy = await client.get("/app/memory", follow_redirects=False)
        map_page = await client.get("/app/memories/map")
        routine_page = await client.get("/app/memories/daily-routine")
        library_page = await client.get("/app/memories/library")

    assert legacy.status_code == 303
    assert legacy.headers["location"] == "/app/memories/map"
    assert map_page.status_code == 200
    assert "Memory Web" in map_page.text
    assert "/static/vendor/cytoscape.min.js?v=" in map_page.text
    assert "Daily Routine" in map_page.text
    assert "Memory Library" in map_page.text
    assert "Timeline Group" not in map_page.text
    assert "Selected Node" in map_page.text
    assert "How this fits" in map_page.text
    assert "Quick jumps" in map_page.text
    assert "Connected memories" in map_page.text
    assert "data-portal-nav-toggle" in map_page.text
    assert 'id="portal-sidebar"' in map_page.text
    assert "Overview" in map_page.text
    assert "Household" in map_page.text
    assert routine_page.status_code == 200
    assert "Routine Timeline" in routine_page.text
    assert library_page.status_code == 200
    assert "Favorite song" in library_page.text


async def test_memory_web_graph_builds_structured_entity_branches(sqlite_session, settings):
    app, _, companion = await _portal_fixture(sqlite_session, settings)
    summary = MemoryItem(
        user_id=companion.id,
        memory_type=MemoryType.summary,
        title="Creativity summary",
        content="Katie had a strong afternoon with music and drawing.",
        tags=["music"],
        embedding_vector=[1.0, 0.0, 0.0],
    )
    fact = MemoryItem(
        user_id=companion.id,
        memory_type=MemoryType.fact,
        title="Drawing memory",
        content="Katie drew for twenty minutes while listening to music.",
        consolidated_into_id=summary.id,
        tags=["music"],
        embedding_vector=[0.99, 0.01, 0.0],
    )
    similar = MemoryItem(
        user_id=companion.id,
        memory_type=MemoryType.episode,
        title="Taylor Swift memory",
        content="Katie sang while drawing in the afternoon.",
        tags=["music"],
        embedding_vector=[0.98, 0.02, 0.0],
    )
    routine = MemoryItem(
        user_id=companion.id,
        memory_type=MemoryType.preference,
        title="Evening routine",
        content="After school and bedtime routines help Katie settle in.",
        tags=["routine"],
        embedding_vector=[0.97, 0.03, 0.0],
    )
    sqlite_session.add_all([summary, fact, similar, routine])
    await sqlite_session.flush()
    fact.consolidated_into_id = summary.id
    await sqlite_session.commit()

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/app/memories/graph-data")

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    child_node = next(node for node in payload["nodes"] if node["kind"] == "child")
    assert "main anchor" in child_node["summary"]
    assert any(node["kind"] == "facet" for node in payload["nodes"])
    assert any(node["kind"] == "topic" for node in payload["nodes"])
    assert any(node["kind"] == "memory" for node in payload["nodes"])
    assert not any(node["kind"] == "week" for node in payload["nodes"])
    assert not any(node["kind"] == "day" for node in payload["nodes"])
    memory_labels = {node["label"] for node in payload["nodes"] if node["kind"] == "memory"}
    assert "Evening routine" not in memory_labels
    assert any(edge["relationship_type"] == "facet_group" for edge in payload["structural_edges"])
    assert any(edge["relationship_type"] == "facet_entity" for edge in payload["structural_edges"])
    assert any(edge["relationship_type"] == "entity_memory_primary" for edge in payload["structural_edges"])
    assert any(edge["relationship_type"] == "consolidated_into" for edge in payload["structural_edges"])
    assert payload["similarity_edges"]
    assert all(edge["kind"] == "similarity" for edge in payload["similarity_edges"])


async def test_memory_web_excludes_daily_life_entries_even_without_explicit_routine_words(sqlite_session, settings):
    app, _, companion = await _portal_fixture(sqlite_session, settings)
    daily_life = MemoryItem(
        user_id=companion.id,
        memory_type=MemoryType.episode,
        title="Sabrina daily look for 2026-04-09",
        content="For Thursday, April 09, 2026, Sabrina picked a denim jacket with silver hoops and went with a braided hairstyle.",
        summary="Today Sabrina is wearing a denim jacket with a braided hairstyle.",
        tags=["daily_life", "appearance", "outfit", "hair"],
        metadata_json={"source": "daily_life", "slot": "appearance"},
        embedding_vector=[1.0, 0.0, 0.0],
    )
    broader_memory = MemoryItem(
        user_id=companion.id,
        memory_type=MemoryType.fact,
        title="Favorite artist",
        content="Her favorite artist is Phoebe Bridgers.",
        tags=["music"],
        embedding_vector=[0.99, 0.01, 0.0],
    )
    sqlite_session.add_all([daily_life, broader_memory])
    await sqlite_session.commit()

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        map_response = await client.get("/app/memories/graph-data")
        routine_response = await client.get("/app/memories/daily-routine-data")

    assert map_response.status_code == 200
    map_payload = map_response.json()
    map_memory_labels = {node["label"] for node in map_payload["nodes"] if node["kind"] == "memory"}
    map_entity_labels = {node["label"] for node in map_payload["nodes"] if node["kind"] != "memory"}
    assert "Sabrina daily look for 2026-04-09" not in map_memory_labels
    assert "Favorite artist" in map_memory_labels
    assert "Sabrina" not in map_entity_labels
    assert "Daily_Life" not in map_entity_labels

    assert routine_response.status_code == 200
    routine_payload = routine_response.json()
    routine_labels = {node["label"] for node in routine_payload["nodes"] if node["kind"] == "memory"}
    assert "Sabrina daily look for 2026-04-09" in routine_labels


async def test_memory_web_excludes_daily_life_entries_even_if_structure_metadata_looks_non_routine(sqlite_session, settings):
    app, _, companion = await _portal_fixture(sqlite_session, settings)
    polluted_daily_life = MemoryItem(
        user_id=companion.id,
        memory_type=MemoryType.episode,
        title="Sabrina tomorrow plan for 2026-04-10",
        content="Tomorrow Sabrina is probably going to wander around a store and grab a little snack.",
        summary="Tomorrow Sabrina is probably going to wander around a store and grab a little snack.",
        tags=["daily_life", "plan"],
        metadata_json={
            "source": "daily_life",
            "slot": "tomorrow_plan",
            "facet": "events",
            "entity_kind": "family_member",
            "structured_primary_entity_kind": "family_member",
        },
        embedding_vector=[1.0, 0.0, 0.0],
    )
    stable_memory = MemoryItem(
        user_id=companion.id,
        memory_type=MemoryType.fact,
        title="Favorite game",
        content="Katie loves Mario Kart.",
        tags=["games"],
        embedding_vector=[0.99, 0.01, 0.0],
    )
    sqlite_session.add_all([polluted_daily_life, stable_memory])
    await sqlite_session.commit()

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        map_response = await client.get("/app/memories/graph-data")
        routine_response = await client.get("/app/memories/daily-routine-data")

    assert map_response.status_code == 200
    map_payload = map_response.json()
    map_memory_labels = {node["label"] for node in map_payload["nodes"] if node["kind"] == "memory"}
    assert "Sabrina tomorrow plan for 2026-04-10" not in map_memory_labels
    assert "Favorite game" in map_memory_labels

    assert routine_response.status_code == 200
    routine_payload = routine_response.json()
    routine_labels = {node["label"] for node in routine_payload["nodes"] if node["kind"] == "memory"}
    assert "Sabrina tomorrow plan for 2026-04-10" in routine_labels


async def test_memory_web_does_not_create_an_empty_profile_hub(sqlite_session, settings):
    app, context, companion = await _portal_fixture(sqlite_session, settings)
    root = await context.container.memory_service._ensure_child_root_entity(
        sqlite_session,
        user_id=companion.id,
        persona_id=None,
        child_name="Katie",
    )
    memory = MemoryItem(
        user_id=companion.id,
        memory_type=MemoryType.fact,
        title="Birthday",
        content="Katie's birthday is September 16th, 2004.",
        embedding_vector=[0.9, 0.1, 0.0],
    )
    sqlite_session.add(memory)
    await sqlite_session.flush()
    sqlite_session.add(
        MemoryItemEntity(
            memory_id=memory.id,
            entity_id=root.id,
            role="primary",
            facet=MemoryFacet.identity,
            is_primary=True,
        )
    )
    await sqlite_session.commit()

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/app/memories/graph-data")

    assert response.status_code == 200
    payload = response.json()
    assert not any(node["kind"] == "facet" and node["label"] == "Profile" for node in payload["nodes"])


async def test_recent_memories_endpoint_paginates_five_at_a_time(sqlite_session, settings):
    app, _, companion = await _portal_fixture(sqlite_session, settings)
    sqlite_session.add_all(
        [
            MemoryItem(
                user_id=companion.id,
                memory_type=MemoryType.fact,
                title=f"Recent memory {index}",
                content=f"Long-term memory {index}",
            )
            for index in range(1, 8)
        ]
    )
    await sqlite_session.commit()

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = await client.get("/app/memories/recent-list?view=map&page=1")
        second = await client.get("/app/memories/recent-list?view=map&page=2")

    assert first.status_code == 200
    first_payload = first.json()
    assert first_payload["ok"] is True
    assert first_payload["page"] == 1
    assert first_payload["page_total"] == 2
    assert first_payload["has_prev"] is False
    assert first_payload["has_next"] is True
    assert len(first_payload["items"]) == 5

    assert second.status_code == 200
    second_payload = second.json()
    assert second_payload["ok"] is True
    assert second_payload["page"] == 2
    assert second_payload["page_total"] == 2
    assert second_payload["has_prev"] is True
    assert second_payload["has_next"] is False
    assert len(second_payload["items"]) == 2


async def test_memory_structure_promotes_specific_existing_entity_and_supporting_topic(sqlite_session, settings):
    app, context, companion = await _portal_fixture(sqlite_session, settings)
    artist_pref = MemoryItem(
        user_id=companion.id,
        memory_type=MemoryType.preference,
        title="Likes and preferences",
        content="Katie likes Taylor Swift.",
        metadata_json={
            "source": "parent_portal_chat",
            "source_kind": "parent_guidance",
            "entity_name": "Taylor Swift",
            "entity_kind": "artist",
            "facet": "favorites",
        },
    )
    favorite_song = MemoryItem(
        user_id=companion.id,
        memory_type=MemoryType.fact,
        title="Favorite Taylor Song",
        content="Katie's favorite Taylor Swift song is Ophelia.",
        metadata_json={
            "source": "parent_portal_chat",
            "source_kind": "parent_guidance",
        },
    )
    sqlite_session.add_all([artist_pref, favorite_song])
    await sqlite_session.flush()

    await context.container.memory_service.ensure_structure_for_memories(
        sqlite_session,
        user_id=companion.id,
        persona_id=None,
        memories=[artist_pref, favorite_song],
        child_name="Katie",
    )
    await sqlite_session.commit()

    entities = list((await sqlite_session.execute(select(MemoryEntity).where(MemoryEntity.user_id == companion.id))).scalars().all())
    taylor = next(entity for entity in entities if entity.display_name == "Taylor Swift")
    ophelia = next(entity for entity in entities if entity.display_name == "Ophelia")

    song_links = list(
        (
            await sqlite_session.execute(
                select(MemoryItemEntity).where(MemoryItemEntity.memory_id == favorite_song.id)
            )
        )
        .scalars()
        .all()
    )
    primary_link = next(link for link in song_links if link.is_primary)
    assert primary_link.entity_id == taylor.id
    assert any(link.entity_id == ophelia.id and not link.is_primary for link in song_links)

    relations = list(
        (
            await sqlite_session.execute(
                select(MemoryEntityRelation).where(
                    MemoryEntityRelation.parent_entity_id == taylor.id,
                    MemoryEntityRelation.child_entity_id == ophelia.id,
                )
            )
        )
        .scalars()
        .all()
    )
    assert relations


async def test_memory_structure_uses_ai_placement_without_hardcoded_artist_values(sqlite_session, settings):
    app, context, companion = await _portal_fixture(sqlite_session, settings)
    context.container.memory_service = MemoryService(settings, _PlacementAwareRuntime(), PromptService(settings))
    memory = MemoryItem(
        user_id=companion.id,
        memory_type=MemoryType.preference,
        title="Favorite song",
        content="Her favorite song is Motion Sickness by Phoebe Bridgers.",
    )
    sqlite_session.add(memory)
    await sqlite_session.commit()

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/app/memories/graph-data")

    assert response.status_code == 200
    artist = await sqlite_session.scalar(
        select(MemoryEntity).where(
            MemoryEntity.user_id == companion.id,
            MemoryEntity.display_name == "Phoebe Bridgers",
            MemoryEntity.entity_kind == MemoryEntityKind.artist,
        )
    )
    assert artist is not None
    relation = await sqlite_session.scalar(
        select(MemoryEntityRelation).where(
            MemoryEntityRelation.user_id == companion.id,
            MemoryEntityRelation.child_entity_id == artist.id,
            MemoryEntityRelation.relationship_kind == EntityRelationKind.favorite,
        )
    )
    assert relation is not None
    assert "Taylor Swift" not in response.text


async def test_daily_routine_graph_keeps_week_and_day_grouping(sqlite_session, settings):
    app, _, companion = await _portal_fixture(sqlite_session, settings)
    routine = MemoryItem(
        user_id=companion.id,
        memory_type=MemoryType.preference,
        title="Morning routine",
        content="Morning routine and getting ready for school feel easier with music.",
        tags=["routine", "morning"],
        embedding_vector=[1.0, 0.0, 0.0],
    )
    non_routine = MemoryItem(
        user_id=companion.id,
        memory_type=MemoryType.fact,
        title="Favorite artist",
        content="Katie loves Taylor Swift.",
        tags=["music"],
        embedding_vector=[0.99, 0.01, 0.0],
    )
    sqlite_session.add_all([routine, non_routine])
    await sqlite_session.commit()

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/app/memories/daily-routine-data")

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert any(node["kind"] == "week" for node in payload["nodes"])
    assert any(node["kind"] == "day" for node in payload["nodes"])
    assert any(edge["relationship_type"] == "time_week" for edge in payload["structural_edges"])
    assert any(edge["relationship_type"] == "time_day" for edge in payload["structural_edges"])
    memory_labels = {node["label"] for node in payload["nodes"] if node["kind"] == "memory"}
    assert "Morning routine" in memory_labels
    assert "Favorite artist" not in memory_labels


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
                    "subject_name": "Camp",
                    "entity_kind": "topic",
                    "facet": "events",
                    "relation_to_child": "summer memory",
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
    assert updated["primary_entity"]["display_name"] == "Camp"
    assert updated["primary_entity"]["entity_kind"] == "topic"
    assert updated["primary_entity"]["facet"] == "events"

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


async def test_memory_page_renders_clear_store_danger_zone(sqlite_session, settings):
    app, _, _ = await _portal_fixture(sqlite_session, settings)

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/app/memories/library")

    assert response.status_code == 200
    assert "Danger Zone" in response.text
    assert "Clear Memory Store" in response.text
    assert "Permanently Clear Memory Store" in response.text


async def test_memory_graph_data_echoes_query_state_and_node_metadata(sqlite_session, settings):
    app, _, companion = await _portal_fixture(sqlite_session, settings)
    sqlite_session.add(
        MemoryItem(
            user_id=companion.id,
            memory_type=MemoryType.fact,
            title="Favorite color",
            content="Katie's favorite color is bright blue.",
            summary="Katie's favorite color is bright blue.",
            embedding_vector=[1.0, 0.0, 0.0],
        )
    )
    await sqlite_session.commit()

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = await client.get("/app/memories/graph-data")
        selected_memory = next(node for node in first.json()["nodes"] if node["kind"] == "memory")
        response = await client.get(f"/app/memories/graph-data?q=color&node={selected_memory['id']}&similar=0")

    assert response.status_code == 200
    payload = response.json()
    assert payload["query"] == "color"
    assert payload["selected_node_id"] == selected_memory["id"]
    assert payload["show_similarity"] is False
    memory_node = next(node for node in payload["nodes"] if node["id"] == selected_memory["id"])
    assert memory_node["breadcrumb"]
    assert memory_node["icon_key"]


async def test_memory_library_branch_scope_filters_to_selected_node(sqlite_session, settings):
    app, _, companion = await _portal_fixture(sqlite_session, settings)
    first_memory = MemoryItem(
        user_id=companion.id,
        memory_type=MemoryType.fact,
        title="Favorite color",
        content="Katie's favorite color is blue.",
        embedding_vector=[1.0, 0.0, 0.0],
    )
    second_memory = MemoryItem(
        user_id=companion.id,
        memory_type=MemoryType.fact,
        title="Favorite snack",
        content="Katie likes vanilla ice cream cones.",
        embedding_vector=[0.0, 1.0, 0.0],
    )
    sqlite_session.add_all([first_memory, second_memory])
    await sqlite_session.commit()

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/app/memories/library?branch={first_memory.id}")

    assert response.status_code == 200
    assert "Branch View" in response.text
    assert "Favorite color" in response.text
    assert "Favorite snack" not in response.text


async def test_memory_recent_changes_includes_daily_cleanup_entries(sqlite_session, settings):
    app, _, companion = await _portal_fixture(sqlite_session, settings)
    memory = MemoryItem(
        user_id=companion.id,
        memory_type=MemoryType.preference,
        title="Favorite color",
        content="Katie's favorite color is blue.",
        embedding_vector=[1.0, 0.0, 0.0],
    )
    sqlite_session.add(memory)
    await sqlite_session.flush()
    sqlite_session.add(
        JobRun(
            job_name="memory_health",
            status=JobStatus.success,
            details_json={
                "changes": [
                    {
                        "id": "cleanup-test",
                        "user_id": str(companion.id),
                        "change_type": "cleanup",
                        "title": "Merged duplicate memory into Favorite color",
                        "summary": "Archived a duplicate memory copy.",
                        "occurred_at": "2026-04-11T03:15:00+00:00",
                        "memory_id": str(memory.id),
                        "node_id": str(memory.id),
                        "href": f"/app/memories/map?node={memory.id}",
                        "tone": "warning",
                    }
                ]
            },
        )
    )
    await sqlite_session.commit()

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/app/memories/recent-changes")

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    titles = {item["title"] for item in payload["changes"]}
    assert "Favorite color" in titles
    assert "Merged duplicate memory into Favorite color" in titles


async def test_clear_memory_store_requires_exact_phrase(sqlite_session, settings):
    app, context, companion = await _portal_fixture(sqlite_session, settings)
    sqlite_session.add(
        MemoryItem(
            user_id=companion.id,
            memory_type=MemoryType.fact,
            title="Favorite snack",
            content="Katie likes popcorn.",
        )
    )
    await sqlite_session.commit()

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        page = await client.get("/app/memories/library")
        html = page.text
        captcha_token = re.search(r'name="captcha_token" value="([^"]+)"', html).group(1)
        phrase = re.search(r"<code>([^<]+)</code>", html).group(1)
        captcha_payload = portal._memory_clear_captcha_serializer(settings).loads(captcha_token)
        response = await client.post(
            "/app/memories/clear-store",
            data={
                "csrf_token": context.csrf_token,
                "next": "/app/memories/library",
                "captcha_token": captcha_token,
                "captcha_answer": str(captcha_payload["answer"]),
                "confirmation_text": f"{phrase} please",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert "memory_store_error=confirmation" in response.headers["location"]
    remaining_count = int(
        await sqlite_session.scalar(
            select(func.count()).select_from(MemoryItem).where(MemoryItem.user_id == companion.id)
        )
    )
    assert remaining_count == 1


async def test_clear_memory_store_deletes_memories_and_structure(sqlite_session, settings):
    app, context, companion = await _portal_fixture(sqlite_session, settings)
    memories = [
        MemoryItem(
            user_id=companion.id,
            memory_type=MemoryType.fact,
            title="Favorite artist",
            content="Her favorite artist is Phoebe Bridgers.",
        ),
        MemoryItem(
            user_id=companion.id,
            memory_type=MemoryType.fact,
            title="Brother",
            content="Her brother is Ryan.",
        ),
    ]
    sqlite_session.add_all(memories)
    await sqlite_session.flush()
    sqlite_session.add(
        MemoryRelationship(
            user_id=companion.id,
            parent_memory_id=memories[0].id,
            child_memory_id=memories[1].id,
            relationship_type=MemoryRelationshipType.manual_child,
        )
    )
    await sqlite_session.commit()
    await context.container.memory_service.ensure_structure_for_memories(
        sqlite_session,
        user_id=companion.id,
        persona_id=None,
        memories=memories,
        child_name="Katie",
    )
    await sqlite_session.commit()

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        page = await client.get("/app/memories/map")
        html = page.text
        captcha_token = re.search(r'name="captcha_token" value="([^"]+)"', html).group(1)
        phrase = re.search(r"<code>([^<]+)</code>", html).group(1)
        captcha_payload = portal._memory_clear_captcha_serializer(settings).loads(captcha_token)
        response = await client.post(
            "/app/memories/clear-store",
            data={
                "csrf_token": context.csrf_token,
                "next": "/app/memories/map",
                "captcha_token": captcha_token,
                "captcha_answer": str(captcha_payload["answer"]),
                "confirmation_text": phrase,
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert "memory_store_status=cleared" in response.headers["location"]
    memory_count = int(
        await sqlite_session.scalar(
            select(func.count()).select_from(MemoryItem).where(MemoryItem.user_id == companion.id)
        )
    )
    relationship_count = int(
        await sqlite_session.scalar(
            select(func.count()).select_from(MemoryRelationship).where(MemoryRelationship.user_id == companion.id)
        )
    )
    entity_count = int(
        await sqlite_session.scalar(
            select(func.count()).select_from(MemoryEntity).where(MemoryEntity.user_id == companion.id)
        )
    )
    entity_link_count = int(
        await sqlite_session.scalar(
            select(func.count()).select_from(MemoryItemEntity).where(
                MemoryItemEntity.memory_id.in_(select(MemoryItem.id).where(MemoryItem.user_id == companion.id))
            )
        )
    )
    entity_relation_count = int(
        await sqlite_session.scalar(
            select(func.count()).select_from(MemoryEntityRelation).where(MemoryEntityRelation.user_id == companion.id)
        )
    )
    assert memory_count == 0
    assert relationship_count == 0
    assert entity_count == 0
    assert entity_link_count == 0
    assert entity_relation_count == 0
