from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any
from zoneinfo import available_timezones

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import RuntimeSettings
from app.models.enums import HouseholdRole, SubscriptionStatus
from app.models.persona import Persona
from app.models.portal import (
    Account,
    AccountInitialization,
    ChildProfile,
    CustomerUser,
    Household,
    RoleAssignment,
    Subscription,
)
from app.models.user import User
from app.schemas.site import (
    PortalInitializationContext,
    PortalInitializationStep,
    PortalInitializationSummary,
)
from app.services.billing import BillingService
from app.services.portal_resona import (
    apply_portal_resona_to_persona,
    build_resona_summary,
    default_preset_key,
    default_voice_profile_key,
    portal_resona_presets,
    portal_voice_profiles,
)
from app.utils.time import utc_now

AVAILABLE_TIMEZONE_IDS = available_timezones()


@dataclass(slots=True)
class PortalInitializationResult:
    state: AccountInitialization
    context: PortalInitializationContext


class InitializationValidationError(ValueError):
    def __init__(self, errors: dict[str, str]) -> None:
        self.errors = errors
        super().__init__("Initialization step validation failed")


class PortalInitializationService:
    STEP_ORDER = ["welcome", "household", "child", "resona", "preferences", "plan", "billing", "complete"]
    PACING_OPTIONS = ("gentle", "balanced", "direct", "reflective", "playful", "steady")
    STYLE_OPTIONS = ("warm", "calm", "encouraging", "reassuring", "upbeat", "straightforward")
    PLAN_OPTIONS = {
        "chat": {
            "label": "Resona Chat",
            "summary": "Text-first support, parent visibility, and monthly included usage credits.",
            "included_credits_usd": 10,
        },
        "voice": {
            "label": "Resona Voice",
            "summary": "Everything in Chat, plus voice calls and voice continuity across channels.",
            "included_credits_usd": 30,
        },
    }
    REQUIRED_PREFERENCE_KEYS = {"onboarding_mode", "preferred_pacing", "response_style", "voice_enabled"}
    REQUIRED_BOUNDARY_KEYS = {"proactive_check_ins", "parent_visibility_mode", "alert_threshold"}
    TIMEZONE_OPTIONS = [
        ("America/New_York", "Eastern Time (America/New_York)"),
        ("America/Chicago", "Central Time (America/Chicago)"),
        ("America/Denver", "Mountain Time (America/Denver)"),
        ("America/Phoenix", "Arizona Time (America/Phoenix)"),
        ("America/Los_Angeles", "Pacific Time (America/Los_Angeles)"),
        ("America/Anchorage", "Alaska Time (America/Anchorage)"),
        ("Pacific/Honolulu", "Hawaii Time (Pacific/Honolulu)"),
        ("UTC", "UTC"),
        ("Europe/London", "United Kingdom (Europe/London)"),
        ("Europe/Paris", "Central Europe (Europe/Paris)"),
        ("Asia/Tokyo", "Japan (Asia/Tokyo)"),
        ("Australia/Sydney", "Australia East (Australia/Sydney)"),
    ]

    def __init__(self, settings: RuntimeSettings, billing_service: BillingService) -> None:
        self.settings = settings
        self.billing_service = billing_service

    def steps(self) -> list[PortalInitializationStep]:
        return [
            PortalInitializationStep(
                key="welcome",
                label="Welcome",
                description="A short overview of what setup will cover before you unlock the full portal.",
            ),
            PortalInitializationStep(
                key="household",
                label="Household Setup",
                description="Choose the use case, relationship, household name, and timezone.",
            ),
            PortalInitializationStep(
                key="child",
                label="Child Profile",
                description="Create one child/profile with the core details needed to start using Resona.",
            ),
            PortalInitializationStep(
                key="resona",
                label="Choose Resona",
                description="Pick the companion style, name, voice, and a few high-value guidance notes for this child.",
            ),
            PortalInitializationStep(
                key="preferences",
                label="Preferences",
                description="Set a few high-value communication and safety defaults.",
            ),
            PortalInitializationStep(
                key="plan",
                label="Plan",
                description="Choose between the Resona Chat and Resona Voice plan.",
            ),
            PortalInitializationStep(
                key="billing",
                label="Billing",
                description="Complete secure Stripe checkout to unlock the full parent portal.",
            ),
            PortalInitializationStep(
                key="complete",
                label="Complete",
                description="Review readiness and enter the dashboard.",
            ),
        ]

    def timezone_options(self) -> list[dict[str, str]]:
        return [
            {"value": value, "label": label}
            for value, label in self.TIMEZONE_OPTIONS
            if value == "UTC" or value in AVAILABLE_TIMEZONE_IDS
        ]

    async def get_or_create_state(
        self,
        session: AsyncSession,
        *,
        account_id: Any,
    ) -> AccountInitialization:
        state = await session.scalar(select(AccountInitialization).where(AccountInitialization.account_id == account_id))
        if state is not None:
            return state
        state = AccountInitialization(
            account_id=account_id,
            status="in_progress",
            current_step="welcome",
            completed_steps_json=[],
            snapshot_json={},
            started_at=utc_now(),
        )
        session.add(state)
        await session.flush()
        return state

    async def load_context(
        self,
        session: AsyncSession,
        *,
        customer_user: CustomerUser,
    ) -> PortalInitializationResult:
        account = await session.get(Account, customer_user.account_id)
        if account is None:
            raise ValueError("Account not found")
        state = await self.get_or_create_state(session, account_id=account.id)
        household = await session.scalar(select(Household).where(Household.account_id == account.id))
        child = await session.scalar(select(ChildProfile).where(ChildProfile.account_id == account.id))
        companion_user = await session.get(User, child.companion_user_id) if child and child.companion_user_id else None
        resona_persona = await self._resolve_child_persona(session, child=child, companion_user=companion_user)
        subscription = await self.billing_service.get_account_subscription(session, account_id=account.id)

        snapshot = self._build_snapshot(
            customer_user=customer_user,
            state=state,
            household=household,
            child=child,
            companion_user=companion_user,
            persona=resona_persona,
            subscription=subscription,
        )
        completed_steps = self._derive_completed_steps(
            state=state,
            customer_user=customer_user,
            snapshot=snapshot,
            household=household,
            child=child,
            persona=resona_persona,
            subscription=subscription,
        )
        completion_ready = self._is_subscription_allowed(subscription) and all(
            step in completed_steps for step in ["welcome", "household", "child", "resona", "preferences", "plan", "billing"]
        )
        first_incomplete = self._first_incomplete_step(completed_steps)
        candidate_step = state.current_step if state.current_step in self.STEP_ORDER else None
        if not (state.completed_steps_json or state.snapshot_json) and candidate_step == "welcome" and first_incomplete != "welcome":
            candidate_step = first_incomplete
        if completion_ready:
            current_step = "complete"
        elif candidate_step and candidate_step != "complete":
            candidate_idx = self.STEP_ORDER.index(candidate_step)
            first_idx = self.STEP_ORDER.index(first_incomplete)
            current_step = candidate_step if candidate_idx <= first_idx else first_incomplete
        else:
            current_step = first_incomplete

        state.snapshot_json = snapshot
        state.selected_plan_key = str(snapshot.get("selected_plan_key") or "") or None
        state.completed_steps_json = completed_steps
        state.current_step = current_step
        state.status = "completed" if completion_ready else "in_progress"
        state.completed_at = utc_now() if completion_ready else None
        await session.flush()

        context = PortalInitializationContext(
            current_step=current_step,
            step_order=self.STEP_ORDER,
            completed_steps=completed_steps,
            selected_plan_key=state.selected_plan_key,
            billing_status=subscription.status.value if subscription else "incomplete",
            completion_ready=completion_ready,
            snapshot=snapshot,
            summary=self._build_summary(snapshot=snapshot, subscription=subscription),
            steps=self.steps(),
            resona_summary=build_resona_summary(self.settings, persona=resona_persona, snapshot=snapshot),
        )
        return PortalInitializationResult(state=state, context=context)

    async def save_step(
        self,
        session: AsyncSession,
        *,
        customer_user: CustomerUser,
        step: str,
        data: dict[str, Any],
        validate_required: bool,
        advance_step: bool,
    ) -> PortalInitializationResult:
        if step not in self.STEP_ORDER:
            raise InitializationValidationError({"step": "Unknown initialization step."})
        result = await self.load_context(session, customer_user=customer_user)
        state = result.state
        snapshot = dict(state.snapshot_json or {})
        snapshot.update(self._normalize_step_data(step=step, data=data))
        state.snapshot_json = snapshot
        state.current_step = self.next_step_for(step) if advance_step else step
        await session.flush()

        if step == "welcome":
            state.completed_steps_json = self._merge_completed(state.completed_steps_json, "welcome")
        elif step == "household":
            errors = self._validate_household(snapshot) if validate_required else {}
            if errors:
                raise InitializationValidationError(errors)
            await self._persist_household_step(session, customer_user=customer_user, snapshot=snapshot)
        elif step == "child":
            errors = self._validate_child(snapshot) if validate_required else {}
            if errors:
                raise InitializationValidationError(errors)
            await self._persist_child_step(session, customer_user=customer_user, snapshot=snapshot)
        elif step == "resona":
            errors = self._validate_resona(snapshot) if validate_required else {}
            if errors:
                raise InitializationValidationError(errors)
            await self._persist_resona_step(session, customer_user=customer_user, snapshot=snapshot)
        elif step == "preferences":
            errors = self._validate_preferences(snapshot) if validate_required else {}
            if errors:
                raise InitializationValidationError(errors)
            await self._persist_preferences_step(session, customer_user=customer_user, snapshot=snapshot)
        elif step == "plan":
            errors = self._validate_plan(snapshot) if validate_required else {}
            if errors:
                raise InitializationValidationError(errors)
            state.selected_plan_key = str(snapshot.get("selected_plan_key") or "").strip() or None
        elif step in {"billing", "complete"}:
            # Billing completion is derived from Stripe state; completion is read-only.
            pass

        await session.flush()
        return await self.load_context(session, customer_user=customer_user)

    def next_step_for(self, current_step: str) -> str:
        try:
            idx = self.STEP_ORDER.index(current_step)
        except ValueError:
            return "welcome"
        return self.STEP_ORDER[min(idx + 1, len(self.STEP_ORDER) - 1)]

    def previous_step_for(self, current_step: str) -> str:
        try:
            idx = self.STEP_ORDER.index(current_step)
        except ValueError:
            return "welcome"
        return self.STEP_ORDER[max(idx - 1, 0)]

    def plan_options(self) -> list[dict[str, Any]]:
        return [
            {"key": key, **value}
            for key, value in self.PLAN_OPTIONS.items()
        ]

    def resona_preset_options(self) -> list[dict[str, Any]]:
        return [item.model_dump(mode="json") for item in portal_resona_presets(self.settings)]

    def voice_profile_options(self) -> list[dict[str, Any]]:
        return [item.model_dump(mode="json") for item in portal_voice_profiles(self.settings)]

    def requires_initialization(self, context: PortalInitializationContext) -> bool:
        return not context.completion_ready

    def _build_snapshot(
        self,
        *,
        customer_user: CustomerUser,
        state: AccountInitialization,
        household: Household | None,
        child: ChildProfile | None,
        companion_user: User | None,
        persona: Persona | None,
        subscription: Subscription | None,
    ) -> dict[str, Any]:
        snapshot = {
            "mode": "for_someone_else",
            "relationship": "parent",
            "household_name": "",
            "timezone": "America/New_York",
            "profile_name": "",
            "child_phone_number": "",
            "birth_year": "",
            "notes": "",
            "resona_mode": "preset",
            "resona_preset_key": default_preset_key(self.settings),
            "resona_display_name": "",
            "resona_voice_profile_key": default_voice_profile_key(self.settings),
            "resona_vibe": "",
            "resona_support_style": "",
            "resona_avoid": "",
            "resona_anchors": "",
            "resona_proactive_style": "",
            "preferred_pacing": [],
            "preferred_pacing_custom": "",
            "response_style": [],
            "response_style_custom": "",
            "communication_notes": "",
            "voice_enabled": True,
            "proactive_check_ins": True,
            "parent_visibility_mode": "",
            "alert_threshold": "",
            "quiet_hours_start": "",
            "quiet_hours_end": "",
            "daily_cadence": "",
            "selected_plan_key": state.selected_plan_key or None,
        }
        snapshot.update(self._public_snapshot_data(state.snapshot_json))
        if customer_user.relationship_label and customer_user.relationship_label != "pending":
            snapshot["relationship"] = customer_user.relationship_label
        if household is not None:
            snapshot["mode"] = "for_myself" if household.is_self_managed else snapshot.get("mode", "for_someone_else")
            snapshot["household_name"] = household.name
            snapshot["timezone"] = household.timezone
        if child is not None:
            snapshot["profile_name"] = child.display_name or child.first_name
            snapshot["birth_year"] = str(child.birth_year or "")
            snapshot["notes"] = child.notes or ""
            if companion_user is not None and companion_user.phone_number:
                snapshot["child_phone_number"] = companion_user.phone_number
            prefs = child.preferences_json or {}
            boundaries = child.boundaries_json or {}
            routines = child.routines_json or {}
            snapshot["mode"] = prefs.get("onboarding_mode", snapshot.get("mode", "for_someone_else"))
            snapshot["preferred_pacing"] = self._normalize_multi_value(
                prefs.get("preferred_pacing", snapshot["preferred_pacing"]),
                allowed=self.PACING_OPTIONS,
            ) or self._normalize_multi_value(
                prefs.get("preferred_pacing_options"),
                allowed=self.PACING_OPTIONS,
            )
            snapshot["preferred_pacing_custom"] = str(
                prefs.get("preferred_pacing_custom", snapshot["preferred_pacing_custom"])
            ).strip()
            snapshot["response_style"] = self._normalize_multi_value(
                prefs.get("response_style", snapshot["response_style"]),
                allowed=self.STYLE_OPTIONS,
            ) or self._normalize_multi_value(
                prefs.get("response_style_options"),
                allowed=self.STYLE_OPTIONS,
            )
            snapshot["response_style_custom"] = str(
                prefs.get("response_style_custom", snapshot["response_style_custom"])
            ).strip()
            snapshot["communication_notes"] = str(
                prefs.get("communication_notes", snapshot["communication_notes"])
            ).strip()
            if "voice_enabled" in prefs:
                snapshot["voice_enabled"] = bool(prefs.get("voice_enabled"))
            if "proactive_check_ins" in boundaries:
                snapshot["proactive_check_ins"] = bool(boundaries.get("proactive_check_ins"))
            snapshot["parent_visibility_mode"] = boundaries.get(
                "parent_visibility_mode",
                snapshot["parent_visibility_mode"],
            )
            snapshot["alert_threshold"] = boundaries.get("alert_threshold", snapshot["alert_threshold"])
            quiet_hours = routines.get("quiet_hours", {}) if isinstance(routines.get("quiet_hours"), dict) else {}
            snapshot["quiet_hours_start"] = quiet_hours.get("start", snapshot["quiet_hours_start"])
            snapshot["quiet_hours_end"] = quiet_hours.get("end", snapshot["quiet_hours_end"])
            snapshot["daily_cadence"] = routines.get("daily_cadence", snapshot["daily_cadence"])
            resona_data = prefs.get("resona_profile", {}) if isinstance(prefs.get("resona_profile"), dict) else {}
            snapshot["resona_mode"] = str(resona_data.get("mode") or snapshot["resona_mode"])
            snapshot["resona_preset_key"] = str(resona_data.get("preset_key") or snapshot["resona_preset_key"])
            snapshot["resona_display_name"] = str(resona_data.get("display_name") or snapshot["resona_display_name"])
            snapshot["resona_voice_profile_key"] = str(
                resona_data.get("voice_profile_key") or snapshot["resona_voice_profile_key"]
            )
            snapshot["resona_vibe"] = str(resona_data.get("vibe") or snapshot["resona_vibe"]).strip()
            snapshot["resona_support_style"] = str(
                resona_data.get("support_style") or snapshot["resona_support_style"]
            ).strip()
            snapshot["resona_avoid"] = str(resona_data.get("avoid") or snapshot["resona_avoid"]).strip()
            snapshot["resona_anchors"] = str(resona_data.get("anchors") or snapshot["resona_anchors"]).strip()
            snapshot["resona_proactive_style"] = str(
                resona_data.get("proactive_style") or snapshot["resona_proactive_style"]
            ).strip()
        if persona is not None:
            snapshot["resona_mode"] = "custom" if persona.source_type == "portal_custom" else "preset"
            snapshot["resona_preset_key"] = str(persona.preset_key or snapshot["resona_preset_key"] or "")
            snapshot["resona_display_name"] = persona.display_name or snapshot["resona_display_name"]
            snapshot["resona_voice_profile_key"] = str(
                (persona.prompt_overrides or {}).get("voice_profile_key") or snapshot["resona_voice_profile_key"] or ""
            )
            snapshot["resona_vibe"] = str(persona.tone or snapshot["resona_vibe"]).strip()
            snapshot["resona_support_style"] = str(persona.speech_style or snapshot["resona_support_style"]).strip()
            snapshot["resona_avoid"] = str(persona.boundaries or snapshot["resona_avoid"]).strip()
            topics = list(persona.topics_of_interest or [])
            activities = list(persona.favorite_activities or [])
            snapshot["resona_anchors"] = ", ".join([item for item in topics + activities if item]).strip() or snapshot["resona_anchors"]
            snapshot["resona_proactive_style"] = str(
                persona.proactive_outreach_style or snapshot["resona_proactive_style"]
            ).strip()
        if subscription is not None:
            derived_plan_key = self.billing_service.plan_key_for_subscription(subscription)
            if derived_plan_key:
                snapshot["selected_plan_key"] = derived_plan_key
        snapshot["preferred_pacing"] = self._normalize_multi_value(
            snapshot.get("preferred_pacing"),
            allowed=self.PACING_OPTIONS,
        )
        snapshot["response_style"] = self._normalize_multi_value(
            snapshot.get("response_style"),
            allowed=self.STYLE_OPTIONS,
        )
        snapshot["preferred_pacing_custom"] = str(snapshot.get("preferred_pacing_custom") or "").strip()
        snapshot["response_style_custom"] = str(snapshot.get("response_style_custom") or "").strip()
        snapshot["communication_notes"] = str(snapshot.get("communication_notes") or "").strip()
        snapshot["resona_mode"] = "custom" if str(snapshot.get("resona_mode") or "").strip() == "custom" else "preset"
        snapshot["resona_preset_key"] = str(snapshot.get("resona_preset_key") or default_preset_key(self.settings)).strip()
        snapshot["resona_display_name"] = str(snapshot.get("resona_display_name") or "").strip()
        snapshot["resona_voice_profile_key"] = str(
            snapshot.get("resona_voice_profile_key") or default_voice_profile_key(self.settings)
        ).strip()
        snapshot["resona_vibe"] = str(snapshot.get("resona_vibe") or "").strip()
        snapshot["resona_support_style"] = str(snapshot.get("resona_support_style") or "").strip()
        snapshot["resona_avoid"] = str(snapshot.get("resona_avoid") or "").strip()
        snapshot["resona_anchors"] = str(snapshot.get("resona_anchors") or "").strip()
        snapshot["resona_proactive_style"] = str(snapshot.get("resona_proactive_style") or "").strip()
        return snapshot

    def _public_snapshot_data(self, data: dict[str, Any] | None) -> dict[str, Any]:
        raw = data or {}
        return {
            key: value
            for key, value in raw.items()
            if not str(key).startswith("_")
        }

    def _derive_completed_steps(
        self,
        *,
        state: AccountInitialization,
        customer_user: CustomerUser,
        snapshot: dict[str, Any],
        household: Household | None,
        child: ChildProfile | None,
        persona: Persona | None,
        subscription: Subscription | None,
    ) -> list[str]:
        completed = set(state.completed_steps_json or [])
        if completed or household is not None or child is not None or subscription is not None:
            completed.add("welcome")
        if household is not None and customer_user.relationship_label and customer_user.relationship_label != "pending":
            completed.add("household")
        if child is not None and (child.display_name or child.first_name):
            completed.add("child")
        child_preferences = child.preferences_json or {} if child is not None else {}
        stored_resona = child_preferences.get("resona_profile")
        has_resona = bool(
            persona is not None
            or str(child_preferences.get("pending_persona_id") or "").strip()
            or (
                isinstance(stored_resona, dict)
                and (
                    str(stored_resona.get("display_name") or "").strip()
                    or str(stored_resona.get("preset_key") or "").strip()
                    or str(stored_resona.get("voice_profile_key") or "").strip()
                )
            )
        )
        if child is not None and has_resona and not self._validate_resona(snapshot):
            completed.add("resona")
        if child is not None and not self._validate_preferences(snapshot):
            completed.add("preferences")
        if str(snapshot.get("selected_plan_key") or "").strip() in self.PLAN_OPTIONS:
            completed.add("plan")
        if self._is_subscription_allowed(subscription):
            completed.add("billing")
        if all(step in completed for step in ["welcome", "household", "child", "resona", "preferences", "plan", "billing"]):
            completed.add("complete")
        ordered = [step for step in self.STEP_ORDER if step in completed]
        return ordered

    def _build_summary(
        self,
        *,
        snapshot: dict[str, Any],
        subscription: Subscription | None,
    ) -> PortalInitializationSummary:
        quiet_hours = ""
        if snapshot.get("quiet_hours_start") and snapshot.get("quiet_hours_end"):
            quiet_hours = f"{snapshot['quiet_hours_start']} - {snapshot['quiet_hours_end']}"
        return PortalInitializationSummary(
            household_name=str(snapshot.get("household_name") or "").strip() or None,
            relationship_label=str(snapshot.get("relationship") or "").strip() or None,
            child_name=str(snapshot.get("profile_name") or "").strip() or None,
            child_phone_number=str(snapshot.get("child_phone_number") or "").strip() or None,
            resona_name=str(snapshot.get("resona_display_name") or "").strip() or None,
            resona_voice_label=build_resona_summary(self.settings, persona=None, snapshot=snapshot).voice_label,
            preferred_pacing=self._communication_summary(
                selections=self._normalize_multi_value(snapshot.get("preferred_pacing"), allowed=self.PACING_OPTIONS),
                custom_text=str(snapshot.get("preferred_pacing_custom") or "").strip(),
                label_suffix="pacing",
            ),
            response_style=self._communication_summary(
                selections=self._normalize_multi_value(snapshot.get("response_style"), allowed=self.STYLE_OPTIONS),
                custom_text=str(snapshot.get("response_style_custom") or "").strip(),
                label_suffix="tone",
            ),
            voice_enabled=bool(snapshot.get("voice_enabled", False)),
            proactive_check_ins=bool(snapshot.get("proactive_check_ins", True)),
            parent_visibility_mode=str(snapshot.get("parent_visibility_mode") or "").strip() or None,
            alert_threshold=str(snapshot.get("alert_threshold") or "").strip() or None,
            quiet_hours=quiet_hours or None,
            daily_cadence=str(snapshot.get("daily_cadence") or "").strip() or None,
            selected_plan_key=str(snapshot.get("selected_plan_key") or "").strip() or None,
            subscription_status=subscription.status.value if subscription else "incomplete",
        )

    def _first_incomplete_step(self, completed_steps: list[str]) -> str:
        completed = set(completed_steps)
        for step in self.STEP_ORDER:
            if step not in completed:
                return step
        return "complete"

    def _merge_completed(self, completed_steps: list[str], step: str) -> list[str]:
        merged = set(completed_steps)
        merged.add(step)
        return [item for item in self.STEP_ORDER if item in merged]

    def _normalize_step_data(self, *, step: str, data: dict[str, Any]) -> dict[str, Any]:
        normalized: dict[str, Any] = {}
        if step == "household":
            normalized = {
                "mode": str(data.get("mode") or "").strip() or "for_someone_else",
                "relationship": str(data.get("relationship") or "").strip() or "parent",
                "household_name": str(data.get("household_name") or "").strip(),
                "timezone": str(data.get("timezone") or "").strip() or "America/New_York",
            }
        elif step == "child":
            normalized = {
                "profile_name": str(data.get("profile_name") or "").strip(),
                "child_phone_number": str(data.get("child_phone_number") or "").strip(),
                "birth_year": str(data.get("birth_year") or "").strip(),
                "notes": str(data.get("notes") or "").strip(),
            }
        elif step == "resona":
            normalized = {
                "resona_mode": "custom" if str(data.get("resona_mode") or "").strip() == "custom" else "preset",
                "resona_preset_key": str(data.get("resona_preset_key") or "").strip(),
                "resona_display_name": str(data.get("resona_display_name") or "").strip(),
                "resona_voice_profile_key": str(data.get("resona_voice_profile_key") or "").strip(),
                "resona_vibe": str(data.get("resona_vibe") or "").strip(),
                "resona_support_style": str(data.get("resona_support_style") or "").strip(),
                "resona_avoid": str(data.get("resona_avoid") or "").strip(),
                "resona_anchors": str(data.get("resona_anchors") or "").strip(),
                "resona_proactive_style": str(data.get("resona_proactive_style") or "").strip(),
            }
        elif step == "preferences":
            normalized = {
                "preferred_pacing": self._normalize_multi_value(data.get("preferred_pacing"), allowed=self.PACING_OPTIONS),
                "preferred_pacing_custom": str(data.get("preferred_pacing_custom") or "").strip(),
                "response_style": self._normalize_multi_value(data.get("response_style"), allowed=self.STYLE_OPTIONS),
                "response_style_custom": str(data.get("response_style_custom") or "").strip(),
                "communication_notes": str(data.get("communication_notes") or "").strip(),
                "voice_enabled": self._as_bool(data.get("voice_enabled")),
                "proactive_check_ins": self._as_bool(data.get("proactive_check_ins")),
                "parent_visibility_mode": str(data.get("parent_visibility_mode") or "").strip(),
                "alert_threshold": str(data.get("alert_threshold") or "").strip(),
                "quiet_hours_start": str(data.get("quiet_hours_start") or "").strip(),
                "quiet_hours_end": str(data.get("quiet_hours_end") or "").strip(),
                "daily_cadence": str(data.get("daily_cadence") or "").strip(),
            }
        elif step == "plan":
            normalized = {"selected_plan_key": str(data.get("selected_plan_key") or "").strip()}
        return normalized

    async def _persist_household_step(
        self,
        session: AsyncSession,
        *,
        customer_user: CustomerUser,
        snapshot: dict[str, Any],
    ) -> None:
        household = await session.scalar(select(Household).where(Household.account_id == customer_user.account_id))
        if household is None:
            household = Household(
                account_id=customer_user.account_id,
                name=str(snapshot["household_name"]),
                timezone=str(snapshot["timezone"]),
                is_self_managed=str(snapshot["mode"]) == "for_myself",
            )
            session.add(household)
            await session.flush()
        else:
            household.name = str(snapshot["household_name"])
            household.timezone = str(snapshot["timezone"])
            household.is_self_managed = str(snapshot["mode"]) == "for_myself"

        account = await session.get(Account, customer_user.account_id)
        if account is not None:
            account.name = str(snapshot["household_name"])

        customer_user.relationship_label = str(snapshot["relationship"])
        desired_role = HouseholdRole.owner if str(snapshot["mode"]) == "for_myself" else HouseholdRole.guardian
        assignment = await session.scalar(
            select(RoleAssignment).where(
                RoleAssignment.customer_user_id == customer_user.id,
                RoleAssignment.household_id == household.id,
            )
        )
        if assignment is None:
            session.add(
                RoleAssignment(
                    account_id=customer_user.account_id,
                    household_id=household.id,
                    customer_user_id=customer_user.id,
                    role=desired_role,
                )
            )
        else:
            assignment.role = desired_role

    async def _persist_child_step(
        self,
        session: AsyncSession,
        *,
        customer_user: CustomerUser,
        snapshot: dict[str, Any],
    ) -> None:
        household = await session.scalar(select(Household).where(Household.account_id == customer_user.account_id))
        if household is None:
            raise InitializationValidationError({"profile_name": "Complete household setup first."})

        child_profile = await session.scalar(
            select(ChildProfile)
            .where(ChildProfile.account_id == customer_user.account_id)
            .order_by(ChildProfile.created_at.desc())
        )
        companion_user = await session.get(User, child_profile.companion_user_id) if child_profile and child_profile.companion_user_id else None
        normalized_phone = str(snapshot.get("child_phone_number") or "").strip()
        if normalized_phone:
            linked_user = await session.scalar(select(User).where(User.phone_number == normalized_phone))
            if linked_user is not None:
                companion_user = linked_user
            elif companion_user is not None:
                companion_user.phone_number = normalized_phone
            else:
                companion_user = User(
                    display_name=str(snapshot["profile_name"]),
                    phone_number=normalized_phone,
                    timezone=str(snapshot.get("timezone") or "America/New_York"),
                )
                session.add(companion_user)
                await session.flush()
        if companion_user is None and child_profile and child_profile.companion_user_id:
            companion_user = await session.get(User, child_profile.companion_user_id)
        if companion_user is not None:
            companion_user.display_name = str(snapshot["profile_name"])
            companion_user.timezone = str(snapshot.get("timezone") or "America/New_York")

        birth_year = int(snapshot["birth_year"]) if str(snapshot.get("birth_year") or "").strip() else None
        if child_profile is None:
            child_profile = ChildProfile(
                account_id=customer_user.account_id,
                household_id=household.id,
                companion_user_id=companion_user.id if companion_user else None,
                first_name=str(snapshot["profile_name"]),
                display_name=str(snapshot["profile_name"]),
                birth_year=birth_year,
                notes=str(snapshot.get("notes") or "").strip() or None,
            )
            session.add(child_profile)
        else:
            child_profile.household_id = household.id
            child_profile.first_name = str(snapshot["profile_name"])
            child_profile.display_name = str(snapshot["profile_name"])
            child_profile.birth_year = birth_year
            child_profile.notes = str(snapshot.get("notes") or "").strip() or None
            if companion_user is not None:
                child_profile.companion_user_id = companion_user.id

        pending_persona_id = str((child_profile.preferences_json or {}).get("pending_persona_id") or "").strip()
        if companion_user is not None and pending_persona_id:
            try:
                pending_persona = await session.get(Persona, uuid.UUID(pending_persona_id))
            except ValueError:
                pending_persona = None
            if pending_persona is not None:
                companion_user.preferred_persona_id = pending_persona.id
                pending_persona.owner_user_id = companion_user.id
                preferences = dict(child_profile.preferences_json or {})
                preferences.pop("pending_persona_id", None)
                child_profile.preferences_json = preferences

    async def _persist_preferences_step(
        self,
        session: AsyncSession,
        *,
        customer_user: CustomerUser,
        snapshot: dict[str, Any],
    ) -> None:
        child = await session.scalar(
            select(ChildProfile)
            .where(ChildProfile.account_id == customer_user.account_id)
            .order_by(ChildProfile.created_at.desc())
        )
        if child is None:
            raise InitializationValidationError({"preferred_pacing": "Complete the child profile step first."})

        preferences = dict(child.preferences_json or {})
        boundaries = dict(child.boundaries_json or {})
        routines = dict(child.routines_json or {})

        preferences.update(
            {
                "onboarding_mode": str(snapshot.get("mode") or "for_someone_else"),
                "preferred_pacing": list(self._normalize_multi_value(snapshot["preferred_pacing"], allowed=self.PACING_OPTIONS)),
                "preferred_pacing_custom": str(snapshot.get("preferred_pacing_custom") or "").strip(),
                "response_style": list(self._normalize_multi_value(snapshot["response_style"], allowed=self.STYLE_OPTIONS)),
                "response_style_custom": str(snapshot.get("response_style_custom") or "").strip(),
                "communication_notes": str(snapshot.get("communication_notes") or "").strip(),
                "voice_enabled": bool(snapshot["voice_enabled"]),
            }
        )
        boundaries.update(
            {
                "proactive_check_ins": bool(snapshot["proactive_check_ins"]),
                "parent_visibility_mode": str(snapshot["parent_visibility_mode"]),
                "alert_threshold": str(snapshot["alert_threshold"]),
            }
        )
        quiet_hours: dict[str, str] = {}
        if snapshot.get("quiet_hours_start") and snapshot.get("quiet_hours_end"):
            quiet_hours = {
                "start": str(snapshot["quiet_hours_start"]),
                "end": str(snapshot["quiet_hours_end"]),
            }
        routines.update({"daily_cadence": str(snapshot["daily_cadence"])})
        if quiet_hours:
            routines["quiet_hours"] = quiet_hours
        elif "quiet_hours" in routines:
            routines.pop("quiet_hours", None)

        child.preferences_json = preferences
        child.boundaries_json = boundaries
        child.routines_json = routines

    async def _persist_resona_step(
        self,
        session: AsyncSession,
        *,
        customer_user: CustomerUser,
        snapshot: dict[str, Any],
    ) -> None:
        child = await session.scalar(
            select(ChildProfile)
            .where(ChildProfile.account_id == customer_user.account_id)
            .order_by(ChildProfile.created_at.desc())
        )
        if child is None:
            raise InitializationValidationError({"resona_display_name": "Complete the child profile step first."})

        companion_user = await session.get(User, child.companion_user_id) if child.companion_user_id else None
        persona = await self._resolve_child_persona(session, child=child, companion_user=companion_user)
        if persona is None or persona.source_type == "admin" or (persona.account_id and persona.account_id != customer_user.account_id):
            persona = Persona(key=f"portal-{uuid.uuid4().hex[:16]}", display_name="Resona")
            session.add(persona)
            await session.flush()

        apply_portal_resona_to_persona(
            self.settings,
            persona=persona,
            account_id=customer_user.account_id,
            owner_user_id=companion_user.id if companion_user else None,
            child_name=str(child.display_name or child.first_name or snapshot.get("profile_name") or "").strip(),
            mode=str(snapshot.get("resona_mode") or "preset"),
            preset_key=str(snapshot.get("resona_preset_key") or "").strip() or None,
            display_name=str(snapshot.get("resona_display_name") or "").strip() or None,
            voice_profile_key=str(snapshot.get("resona_voice_profile_key") or "").strip() or None,
            vibe=str(snapshot.get("resona_vibe") or "").strip() or None,
            support_style=str(snapshot.get("resona_support_style") or "").strip() or None,
            avoid_text=str(snapshot.get("resona_avoid") or "").strip() or None,
            anchors_text=str(snapshot.get("resona_anchors") or "").strip() or None,
            proactive_style=str(snapshot.get("resona_proactive_style") or "").strip() or None,
        )

        preferences = dict(child.preferences_json or {})
        preferences["resona_profile"] = {
            "mode": str(snapshot.get("resona_mode") or "preset"),
            "preset_key": str(snapshot.get("resona_preset_key") or "").strip() or None,
            "display_name": persona.display_name,
            "voice_profile_key": str(snapshot.get("resona_voice_profile_key") or "").strip() or None,
            "vibe": str(snapshot.get("resona_vibe") or "").strip(),
            "support_style": str(snapshot.get("resona_support_style") or "").strip(),
            "avoid": str(snapshot.get("resona_avoid") or "").strip(),
            "anchors": str(snapshot.get("resona_anchors") or "").strip(),
            "proactive_style": str(snapshot.get("resona_proactive_style") or "").strip(),
        }
        if companion_user is not None:
            companion_user.preferred_persona_id = persona.id
            persona.owner_user_id = companion_user.id
            preferences.pop("pending_persona_id", None)
        else:
            preferences["pending_persona_id"] = str(persona.id)
        child.preferences_json = preferences

    def _validate_household(self, snapshot: dict[str, Any]) -> dict[str, str]:
        errors: dict[str, str] = {}
        if str(snapshot.get("mode") or "") not in {"for_myself", "for_someone_else"}:
            errors["mode"] = "Choose whether this is for yourself or someone else."
        if str(snapshot.get("relationship") or "") not in {"owner", "parent", "guardian", "caregiver", "other"}:
            errors["relationship"] = "Choose a relationship for this account."
        if not str(snapshot.get("household_name") or "").strip():
            errors["household_name"] = "Enter a household name."
        timezone = str(snapshot.get("timezone") or "").strip()
        if not timezone:
            errors["timezone"] = "Choose a timezone."
        elif timezone != "UTC" and timezone not in AVAILABLE_TIMEZONE_IDS:
            errors["timezone"] = "Choose a standardized timezone from the list."
        return errors

    def _validate_child(self, snapshot: dict[str, Any]) -> dict[str, str]:
        errors: dict[str, str] = {}
        if not str(snapshot.get("profile_name") or "").strip():
            errors["profile_name"] = "Enter a child or profile name."
        birth_year = str(snapshot.get("birth_year") or "").strip()
        if birth_year:
            try:
                value = int(birth_year)
            except ValueError:
                errors["birth_year"] = "Birth year must be a number."
            else:
                if value < 1900 or value > 2100:
                    errors["birth_year"] = "Enter a realistic birth year."
        return errors

    def _validate_resona(self, snapshot: dict[str, Any]) -> dict[str, str]:
        errors: dict[str, str] = {}
        if str(snapshot.get("resona_mode") or "preset").strip() not in {"preset", "custom"}:
            errors["resona_mode"] = "Choose a preset or custom Resona."
        preset_keys = {item.key for item in portal_resona_presets(self.settings)}
        if str(snapshot.get("resona_mode") or "preset").strip() == "preset":
            preset_key = str(snapshot.get("resona_preset_key") or "").strip()
            if not preset_key or preset_key not in preset_keys:
                errors["resona_preset_key"] = "Choose one of the preset Resonas."
        voice_keys = {item.key for item in portal_voice_profiles(self.settings)}
        voice_key = str(snapshot.get("resona_voice_profile_key") or "").strip()
        if not voice_key or voice_key not in voice_keys:
            errors["resona_voice_profile_key"] = "Choose a voice profile."
        display_name = str(snapshot.get("resona_display_name") or "").strip()
        if len(display_name) > 120:
            errors["resona_display_name"] = "Keep the Resona name under 120 characters."
        for key in ("resona_vibe", "resona_support_style", "resona_avoid", "resona_anchors", "resona_proactive_style"):
            value = str(snapshot.get(key) or "").strip()
            if len(value) > 280:
                errors[key] = "Keep this under 280 characters."
        return errors

    def _validate_preferences(self, snapshot: dict[str, Any]) -> dict[str, str]:
        errors: dict[str, str] = {}
        preferred_pacing = self._normalize_multi_value(snapshot.get("preferred_pacing"), allowed=self.PACING_OPTIONS)
        response_style = self._normalize_multi_value(snapshot.get("response_style"), allowed=self.STYLE_OPTIONS)
        preferred_pacing_custom = str(snapshot.get("preferred_pacing_custom") or "").strip()
        response_style_custom = str(snapshot.get("response_style_custom") or "").strip()
        communication_notes = str(snapshot.get("communication_notes") or "").strip()
        if not preferred_pacing and not preferred_pacing_custom:
            errors["preferred_pacing"] = "Choose one or more pacing preferences, or describe one in your own words."
        if not response_style and not response_style_custom:
            errors["response_style"] = "Choose one or more response styles, or describe one in your own words."
        if len(preferred_pacing_custom) > 160:
            errors["preferred_pacing_custom"] = "Keep the custom pacing guidance under 160 characters."
        if len(response_style_custom) > 160:
            errors["response_style_custom"] = "Keep the custom response style guidance under 160 characters."
        if len(communication_notes) > 280:
            errors["communication_notes"] = "Keep additional communication notes under 280 characters."
        if str(snapshot.get("parent_visibility_mode") or "") not in {"full_transcript", "summary_with_alerts"}:
            errors["parent_visibility_mode"] = "Choose a parent visibility mode."
        if str(snapshot.get("alert_threshold") or "") not in {"low", "medium", "high"}:
            errors["alert_threshold"] = "Choose an alert threshold."
        return errors

    def _validate_plan(self, snapshot: dict[str, Any]) -> dict[str, str]:
        plan_key = str(snapshot.get("selected_plan_key") or "").strip()
        if plan_key not in self.PLAN_OPTIONS:
            return {"selected_plan_key": "Choose a Resona plan to continue."}
        return {}

    def _is_subscription_allowed(self, subscription: Subscription | None) -> bool:
        return bool(
            subscription is not None
            and subscription.status in {SubscriptionStatus.trialing, SubscriptionStatus.active, SubscriptionStatus.past_due}
        )

    def _as_bool(self, value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return False
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    def _normalize_multi_value(self, raw: Any, *, allowed: tuple[str, ...]) -> list[str]:
        if isinstance(raw, list):
            items = raw
        elif isinstance(raw, (tuple, set)):
            items = list(raw)
        elif raw in (None, ""):
            items = []
        else:
            items = [raw]
        normalized: list[str] = []
        for item in items:
            value = str(item or "").strip().lower()
            if value and value in allowed and value not in normalized:
                normalized.append(value)
        return normalized

    def _communication_summary(
        self,
        *,
        selections: list[str],
        custom_text: str,
        label_suffix: str,
    ) -> str | None:
        items = [item.replace("_", " ") for item in selections]
        if custom_text:
            items.append(custom_text)
        if not items:
            return None
        lead = items[0].title()
        if len(items) == 1:
            return f"{lead} {label_suffix}"
        if len(items) == 2:
            return f"{lead} and {items[1]} {label_suffix}"
        remainder = ", ".join(items[1:-1])
        if remainder:
            return f"{lead}, {remainder}, and {items[-1]} {label_suffix}"
        return f"{lead} and {items[-1]} {label_suffix}"

    async def _resolve_child_persona(
        self,
        session: AsyncSession,
        *,
        child: ChildProfile | None,
        companion_user: User | None,
    ) -> Persona | None:
        if companion_user and companion_user.preferred_persona_id:
            persona = await session.get(Persona, companion_user.preferred_persona_id)
            if persona is not None:
                return persona
        if child is None:
            return None
        pending_persona_id = str((child.preferences_json or {}).get("pending_persona_id") or "").strip()
        if not pending_persona_id:
            return None
        try:
            return await session.get(Persona, uuid.UUID(pending_persona_id))
        except ValueError:
            return None
