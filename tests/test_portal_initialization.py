from __future__ import annotations

from sqlalchemy import select

from app.models.enums import SubscriptionStatus
from app.models.portal import ChildProfile, Household, Subscription
from app.providers.base import GeneratedText
from app.models.user import User
from app.services.billing import BillingService
from app.services.customer_auth import CustomerAuthService
from app.services.portal_initialization import PortalInitializationService
from app.services.portal_preview import PortalPreviewService


async def _register_customer(sqlite_session, settings, *, email: str):
    auth_service = CustomerAuthService(settings)
    user, _, _ = await auth_service.register_user(
        sqlite_session,
        email=email,
        password="setup-secure-password",
        display_name="Parent User",
        phone_number=None,
        accepted_terms=True,
        accepted_privacy=True,
        ip_address="127.0.0.1",
        user_agent="pytest",
    )
    await sqlite_session.flush()
    return user


async def test_initialization_new_account_starts_at_welcome(sqlite_session, settings):
    user = await _register_customer(sqlite_session, settings, email="welcome@example.com")
    service = PortalInitializationService(settings, BillingService(settings))

    result = await service.load_context(sqlite_session, customer_user=user)

    assert result.context.current_step == "welcome"
    assert result.context.completed_steps == []
    assert result.context.completion_ready is False


async def test_initialization_autosave_resumes_same_step(sqlite_session, settings):
    user = await _register_customer(sqlite_session, settings, email="autosave@example.com")
    service = PortalInitializationService(settings, BillingService(settings))

    await service.save_step(
        sqlite_session,
        customer_user=user,
        step="welcome",
        data={},
        validate_required=True,
        advance_step=True,
    )
    result = await service.save_step(
        sqlite_session,
        customer_user=user,
        step="household",
        data={
            "mode": "for_someone_else",
            "relationship": "guardian",
            "household_name": "Maple House",
            "timezone": "America/New_York",
        },
        validate_required=False,
        advance_step=False,
    )

    reloaded = await service.load_context(sqlite_session, customer_user=user)

    assert result.context.current_step == "household"
    assert "household" in result.context.completed_steps
    assert reloaded.context.current_step == "household"
    assert reloaded.context.snapshot["household_name"] == "Maple House"


async def test_initialization_persists_child_and_preferences(sqlite_session, settings):
    user = await _register_customer(sqlite_session, settings, email="preferences@example.com")
    service = PortalInitializationService(settings, BillingService(settings))

    await service.save_step(
        sqlite_session,
        customer_user=user,
        step="welcome",
        data={},
        validate_required=True,
        advance_step=True,
    )
    await service.save_step(
        sqlite_session,
        customer_user=user,
        step="household",
        data={
            "mode": "for_someone_else",
            "relationship": "parent",
            "household_name": "Cedar Home",
            "timezone": "America/Chicago",
        },
        validate_required=True,
        advance_step=True,
    )
    await service.save_step(
        sqlite_session,
        customer_user=user,
        step="child",
        data={
            "profile_name": "Katie",
            "child_phone_number": "+16105550123",
            "birth_year": "2010",
            "notes": "Prefers calm check-ins.",
        },
        validate_required=True,
        advance_step=True,
    )
    result = await service.save_step(
        sqlite_session,
        customer_user=user,
        step="preferences",
        data={
            "preferred_pacing": ["gentle", "steady"],
            "preferred_pacing_custom": "calm at first, then a little clearer if needed",
            "response_style": ["encouraging", "reassuring"],
            "response_style_custom": "easy to follow and never too clinical",
            "communication_notes": "Keep the reply simple and reassuring.",
            "voice_enabled": True,
            "proactive_check_ins": True,
            "parent_visibility_mode": "full_transcript",
            "alert_threshold": "medium",
            "quiet_hours_start": "20:00",
            "quiet_hours_end": "07:00",
            "daily_cadence": "evening",
        },
        validate_required=True,
        advance_step=True,
    )

    household = await sqlite_session.scalar(
        select(Household).where(Household.account_id == user.account_id)
    )
    loaded_child = await sqlite_session.scalar(
        select(ChildProfile).where(ChildProfile.account_id == user.account_id)
    )
    linked_user = await sqlite_session.scalar(select(User).where(User.phone_number == "+16105550123"))

    assert household is not None
    assert household.name == "Cedar Home"
    assert loaded_child is not None
    assert loaded_child.display_name == "Katie"
    assert loaded_child.preferences_json["onboarding_mode"] == "for_someone_else"
    assert loaded_child.preferences_json["preferred_pacing"] == ["gentle", "steady"]
    assert loaded_child.preferences_json["preferred_pacing_custom"] == "calm at first, then a little clearer if needed"
    assert loaded_child.preferences_json["response_style"] == ["encouraging", "reassuring"]
    assert loaded_child.preferences_json["response_style_custom"] == "easy to follow and never too clinical"
    assert loaded_child.preferences_json["communication_notes"] == "Keep the reply simple and reassuring."
    assert loaded_child.boundaries_json["parent_visibility_mode"] == "full_transcript"
    assert loaded_child.boundaries_json["alert_threshold"] == "medium"
    assert loaded_child.routines_json["daily_cadence"] == "evening"
    assert loaded_child.routines_json["quiet_hours"]["start"] == "20:00"
    assert linked_user is not None
    assert result.context.current_step == "plan"


async def test_initialization_hydrates_existing_partial_records(sqlite_session, settings):
    user = await _register_customer(sqlite_session, settings, email="hydrate@example.com")
    service = PortalInitializationService(settings, BillingService(settings))

    user.relationship_label = "guardian"
    household = Household(
        account_id=user.account_id,
        name="River House",
        timezone="America/New_York",
        is_self_managed=False,
    )
    sqlite_session.add(household)
    await sqlite_session.flush()
    sqlite_session.add(
        ChildProfile(
            account_id=user.account_id,
            household_id=household.id,
            first_name="Jordan",
            display_name="Jordan",
        )
    )
    await sqlite_session.flush()

    result = await service.load_context(sqlite_session, customer_user=user)

    assert result.context.completed_steps == ["welcome", "household", "child"]
    assert result.context.current_step == "preferences"
    assert result.context.snapshot["household_name"] == "River House"
    assert result.context.snapshot["profile_name"] == "Jordan"


async def test_initialization_hydrates_partial_data_and_requires_allowed_subscription(sqlite_session, settings):
    user = await _register_customer(sqlite_session, settings, email="billing@example.com")
    service = PortalInitializationService(settings, BillingService(settings))

    await service.save_step(
        sqlite_session,
        customer_user=user,
        step="welcome",
        data={},
        validate_required=True,
        advance_step=True,
    )
    await service.save_step(
        sqlite_session,
        customer_user=user,
        step="household",
        data={
            "mode": "for_someone_else",
            "relationship": "guardian",
            "household_name": "Oak Home",
            "timezone": "America/New_York",
        },
        validate_required=True,
        advance_step=True,
    )
    await service.save_step(
        sqlite_session,
        customer_user=user,
        step="child",
        data={"profile_name": "Sam", "child_phone_number": "", "birth_year": "", "notes": ""},
        validate_required=True,
        advance_step=True,
    )
    await service.save_step(
        sqlite_session,
        customer_user=user,
        step="preferences",
        data={
            "preferred_pacing": ["balanced"],
            "preferred_pacing_custom": "",
            "response_style": ["warm"],
            "response_style_custom": "",
            "communication_notes": "",
            "voice_enabled": False,
            "proactive_check_ins": True,
            "parent_visibility_mode": "summary_with_alerts",
            "alert_threshold": "high",
            "quiet_hours_start": "",
            "quiet_hours_end": "",
            "daily_cadence": "adaptive",
        },
        validate_required=True,
        advance_step=True,
    )
    result = await service.save_step(
        sqlite_session,
        customer_user=user,
        step="plan",
        data={"selected_plan_key": "chat"},
        validate_required=True,
        advance_step=True,
    )

    assert result.context.current_step == "billing"
    assert result.context.completion_ready is False

    sqlite_session.add(
        Subscription(
            account_id=user.account_id,
            status=SubscriptionStatus.incomplete,
            stripe_customer_id="cus_test_123",
            stripe_subscription_id="sub_test_123",
            stripe_price_id="chat_monthly",
        )
    )
    await sqlite_session.flush()

    incomplete = await service.load_context(sqlite_session, customer_user=user)
    assert incomplete.context.current_step == "billing"
    assert incomplete.context.completion_ready is False

    subscription = await sqlite_session.scalar(
        select(Subscription).where(Subscription.account_id == user.account_id)
    )
    assert subscription is not None
    subscription.status = SubscriptionStatus.active
    await sqlite_session.flush()

    complete = await service.load_context(sqlite_session, customer_user=user)
    assert complete.context.current_step == "complete"
    assert complete.context.completion_ready is True
    assert complete.context.billing_status == "active"


async def test_initialization_accepts_custom_preference_text_without_preset_choices(sqlite_session, settings):
    user = await _register_customer(sqlite_session, settings, email="customprefs@example.com")
    service = PortalInitializationService(settings, BillingService(settings))

    await service.save_step(
        sqlite_session,
        customer_user=user,
        step="welcome",
        data={},
        validate_required=True,
        advance_step=True,
    )
    await service.save_step(
        sqlite_session,
        customer_user=user,
        step="household",
        data={
            "mode": "for_someone_else",
            "relationship": "guardian",
            "household_name": "Harbor Home",
            "timezone": "America/New_York",
        },
        validate_required=True,
        advance_step=True,
    )
    await service.save_step(
        sqlite_session,
        customer_user=user,
        step="child",
        data={"profile_name": "Avery", "child_phone_number": "", "birth_year": "", "notes": ""},
        validate_required=True,
        advance_step=True,
    )

    result = await service.save_step(
        sqlite_session,
        customer_user=user,
        step="preferences",
        data={
            "preferred_pacing": [],
            "preferred_pacing_custom": "start very gently, then get clearer only if Avery seems ready",
            "response_style": [],
            "response_style_custom": "soft, supportive, and easy to understand",
            "communication_notes": "Short messages help.",
            "voice_enabled": False,
            "proactive_check_ins": True,
            "parent_visibility_mode": "summary_with_alerts",
            "alert_threshold": "medium",
            "quiet_hours_start": "",
            "quiet_hours_end": "",
            "daily_cadence": "adaptive",
        },
        validate_required=True,
        advance_step=True,
    )

    assert result.context.current_step == "plan"
    assert "preferences" in result.context.completed_steps


async def test_portal_preview_service_falls_back_without_openai(sqlite_session, settings):
    user = await _register_customer(sqlite_session, settings, email="preview@example.com")

    class _DisabledOpenAIProvider:
        enabled = False

    class _UnusedUsageIngestionService:
        async def record_event(self, *args, **kwargs):  # pragma: no cover
            raise AssertionError("record_event should not be called when OpenAI is disabled")

    preview_service = PortalPreviewService(
        settings,
        openai_provider=_DisabledOpenAIProvider(),  # type: ignore[arg-type]
        usage_ingestion_service=_UnusedUsageIngestionService(),  # type: ignore[arg-type]
    )

    result = await preview_service.generate_preference_preview(
        sqlite_session,
        customer_user=user,
        payload={
            "profile_name": "Katie",
            "preferred_pacing": ["gentle", "steady"],
            "preferred_pacing_custom": "",
            "response_style": ["warm", "reassuring"],
            "response_style_custom": "",
            "communication_notes": "",
            "voice_enabled": True,
            "proactive_check_ins": True,
            "daily_cadence": "evening",
        },
    )

    assert "Katie" in result["message"]
    assert "changed any time" in result["caption"]
    assert result["source"] == "fallback_disabled"


async def test_portal_preview_service_caches_generated_result(sqlite_session, settings):
    user = await _register_customer(sqlite_session, settings, email="preview-cache@example.com")

    class _FakeOpenAIProvider:
        enabled = True

        def __init__(self):
            self.calls = []

        async def generate_text(self, **kwargs):
            self.calls.append(kwargs)
            return GeneratedText(
                text="Hey Katie, tell me what happened. I'm here with you.",
                model=kwargs.get("model"),
                usage={"input_tokens": 11, "output_tokens": 9, "total_tokens": 20},
            )

    class _RecordingUsageIngestionService:
        def __init__(self):
            self.calls = 0

        async def record_event(self, *args, **kwargs):
            self.calls += 1

    fake_openai = _FakeOpenAIProvider()
    usage_ingestion = _RecordingUsageIngestionService()
    preview_service = PortalPreviewService(
        settings,
        openai_provider=fake_openai,  # type: ignore[arg-type]
        usage_ingestion_service=usage_ingestion,  # type: ignore[arg-type]
    )
    payload = {
        "profile_name": "Katie",
        "preferred_pacing": ["gentle", "steady"],
        "preferred_pacing_custom": "start soft, then get a little clearer if needed",
        "response_style": ["warm", "reassuring"],
        "response_style_custom": "never too clinical",
        "communication_notes": "Short messages help.",
        "voice_enabled": True,
        "proactive_check_ins": True,
        "daily_cadence": "evening",
    }

    generated = await preview_service.generate_preference_preview(
        sqlite_session,
        customer_user=user,
        payload=payload,
    )
    cached = await preview_service.get_cached_preference_preview(
        sqlite_session,
        customer_user=user,
        payload=payload,
    )
    init_context = await PortalInitializationService(settings, BillingService(settings)).load_context(
        sqlite_session,
        customer_user=user,
    )

    assert fake_openai.calls[0]["model"] == settings.openai.portal_preview_model
    assert usage_ingestion.calls == 1
    assert cached is not None
    assert cached["message"] == generated["message"]
    assert cached["cached"] is True
    assert "_preference_preview_cache" not in init_context.context.snapshot


async def test_portal_preview_service_falls_back_when_remote_preview_errors(sqlite_session, settings):
    user = await _register_customer(sqlite_session, settings, email="preview-remote-failure@example.com")

    class _BrokenOpenAIProvider:
        enabled = True

        async def generate_text(self, **kwargs):
            raise RuntimeError("preview backend unavailable")

    class _UnusedUsageIngestionService:
        async def record_event(self, *args, **kwargs):  # pragma: no cover
            raise AssertionError("record_event should not be called when the preview provider fails")

    preview_service = PortalPreviewService(
        settings,
        openai_provider=_BrokenOpenAIProvider(),  # type: ignore[arg-type]
        usage_ingestion_service=_UnusedUsageIngestionService(),  # type: ignore[arg-type]
    )

    result = await preview_service.generate_preference_preview(
        sqlite_session,
        customer_user=user,
        payload={
            "profile_name": "Katie",
            "preferred_pacing": ["gentle"],
            "preferred_pacing_custom": "",
            "response_style": ["warm"],
            "response_style_custom": "",
            "communication_notes": "",
            "voice_enabled": True,
            "proactive_check_ins": True,
            "daily_cadence": "evening",
        },
    )
    cached = await preview_service.get_cached_preference_preview(
        sqlite_session,
        customer_user=user,
        payload={
            "profile_name": "Katie",
            "preferred_pacing": ["gentle"],
            "preferred_pacing_custom": "",
            "response_style": ["warm"],
            "response_style_custom": "",
            "communication_notes": "",
            "voice_enabled": True,
            "proactive_check_ins": True,
            "daily_cadence": "evening",
        },
    )

    assert result["source"] == "fallback_remote_unavailable"
    assert "temporarily unavailable" in result["caption"]
    assert cached is None
