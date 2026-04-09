from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import AIUnavailableError, AiRuntime
from app.core.logging import get_logger
from app.core.settings import RuntimeSettings
from app.models.portal import AccountInitialization, CustomerUser
from app.services.usage_ingestion import UsageIngestionService, UsageRecordInput
from app.utils.text import make_idempotency_key, normalize_text, truncate_text
from app.utils.time import utc_now

logger = get_logger(__name__)


class PortalPreviewService:
    CACHE_FIELD = "_preference_preview_cache"
    MAX_CACHE_ENTRIES = 24
    PACING_OPTIONS = {
        "gentle": "slow, patient, and low-pressure",
        "balanced": "steady without feeling too slow or too abrupt",
        "direct": "clear and simple without extra buildup",
        "reflective": "thoughtful and feeling-aware",
        "playful": "light and a little more upbeat",
        "steady": "ordered, predictable, and easy to follow",
    }
    STYLE_OPTIONS = {
        "warm": "kind and caring",
        "calm": "settled and low-intensity",
        "encouraging": "supportive and confidence-building",
        "reassuring": "comforting and grounding",
        "upbeat": "lightly positive without being hyper",
        "straightforward": "plain-spoken and clear",
    }
    MAX_CUSTOM_CHARS = 160
    MAX_NOTES_CHARS = 280

    def __init__(
        self,
        settings: RuntimeSettings,
        ai_runtime: AiRuntime,
        usage_ingestion_service: UsageIngestionService,
    ) -> None:
        self.settings = settings
        self.ai_runtime = ai_runtime
        self.usage_ingestion_service = usage_ingestion_service

    def normalize_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        pacing = self._normalize_options(payload.get("preferred_pacing"), allowed=self.PACING_OPTIONS)
        styles = self._normalize_options(payload.get("response_style"), allowed=self.STYLE_OPTIONS)
        return {
            "profile_name": truncate_text(str(payload.get("profile_name") or "").strip(), 80),
            "preferred_pacing": pacing,
            "preferred_pacing_custom": truncate_text(
                str(payload.get("preferred_pacing_custom") or "").strip(),
                self.MAX_CUSTOM_CHARS,
            ),
            "response_style": styles,
            "response_style_custom": truncate_text(
                str(payload.get("response_style_custom") or "").strip(),
                self.MAX_CUSTOM_CHARS,
            ),
            "communication_notes": truncate_text(
                str(payload.get("communication_notes") or "").strip(),
                self.MAX_NOTES_CHARS,
            ),
            "voice_enabled": self._as_bool(payload.get("voice_enabled")),
            "proactive_check_ins": self._as_bool(payload.get("proactive_check_ins")),
            "daily_cadence": str(payload.get("daily_cadence") or "").strip(),
        }

    async def get_cached_preference_preview(
        self,
        session: AsyncSession,
        *,
        customer_user: CustomerUser,
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        normalized = self.normalize_payload(payload)
        cached = await self._lookup_cached_preview(
            session,
            account_id=customer_user.account_id,
            payload=normalized,
        )
        if cached is None:
            return None
        source = str(cached.get("source") or "cache")
        if source.startswith("unavailable") and self.ai_runtime.enabled:
            return None
        return {
            "message": str(cached.get("message") or ""),
            "caption": str(cached.get("caption") or ""),
            "detail": str(cached.get("detail") or ""),
            "source": source,
            "cached": True,
        }

    async def generate_preference_preview(
        self,
        session: AsyncSession,
        *,
        customer_user: CustomerUser,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        normalized = self.normalize_payload(payload)
        if not normalized["profile_name"]:
            raise ValueError("Add a child or profile name first.")
        if not normalized["preferred_pacing"] and not normalized["preferred_pacing_custom"]:
            raise ValueError("Choose at least one pacing preference or describe one in your own words.")
        if not normalized["response_style"] and not normalized["response_style_custom"]:
            raise ValueError("Choose at least one response style or describe one in your own words.")

        caption = self._build_caption(normalized)
        if not self.ai_runtime.enabled:
            return self._unavailable_response(configured=False)

        try:
            response = await self.ai_runtime.portal_preview(
                prompt=self._input_prompt(normalized),
                temperature=0.7,
                max_tokens=90,
            )
            message = self._clean_message(response.output.message)
            if not message:
                raise AIUnavailableError("Empty portal preview output")
            await self._record_usage(
                session,
                customer_user=customer_user,
                payload=normalized,
                usage=response.usage,
            )
            await self._cache_preview(
                session,
                account_id=customer_user.account_id,
                payload=normalized,
                message=message,
                caption=caption,
                source="openai",
            )
            return {"message": message, "caption": caption, "source": "openai"}
        except Exception as exc:
            logger.warning(
                "portal_preview_unavailable",
                account_id=str(customer_user.account_id),
                reason=type(exc).__name__,
            )
            return self._unavailable_response(configured=True)

    def _instructions(self) -> str:
        return (
            "Write one short sample text message from a supportive AI companion to a child. "
            "The message is only to help a parent understand the selected communication preferences. "
            "Keep it casual, warm, and easy to process. "
            "Use the child's first name at most once. "
            "Avoid therapy jargon, complex metaphors, robotic wording, and corporate tone. "
            "Do not mention settings, preferences, parents, monitoring, safety systems, or that this is an example. "
            "Keep it to 1 or 2 short sentences and no more than 32 words total. "
            "Return only the message text."
        )

    def _input_prompt(self, payload: dict[str, Any]) -> str:
        pacing_lines = [f"- {item}: {self.PACING_OPTIONS[item]}" for item in payload["preferred_pacing"]]
        style_lines = [f"- {item}: {self.STYLE_OPTIONS[item]}" for item in payload["response_style"]]
        sections = [
            f"Child name: {payload['profile_name']}",
            "Situation: The child just said something felt off or upsetting today and the companion is replying.",
            "Selected pacing preferences:",
            "\n".join(pacing_lines) or "- none selected",
            f"Custom pacing guidance: {payload['preferred_pacing_custom'] or 'none'}",
            "Selected response style preferences:",
            "\n".join(style_lines) or "- none selected",
            f"Custom response style guidance: {payload['response_style_custom'] or 'none'}",
            f"Other communication guidance: {payload['communication_notes'] or 'none'}",
            f"Voice continuity enabled: {'yes' if payload['voice_enabled'] else 'no'}",
            f"Proactive check-ins enabled: {'yes' if payload['proactive_check_ins'] else 'no'}",
            f"Daily cadence: {payload['daily_cadence'] or 'adaptive'}",
        ]
        return "\n".join(sections)

    async def _record_usage(
        self,
        session: AsyncSession,
        *,
        customer_user: CustomerUser,
        payload: dict[str, Any],
        usage: dict[str, Any],
    ) -> None:
        prompt_tokens = self._usage_int(usage, "input_tokens", "prompt_tokens")
        completion_tokens = self._usage_int(usage, "output_tokens", "completion_tokens")
        total_tokens = self._usage_int(usage, "total_tokens")
        if total_tokens <= 0:
            total_tokens = prompt_tokens + completion_tokens
        if total_tokens <= 0:
            return
        await self.usage_ingestion_service.record_event(
            session,
            UsageRecordInput(
                account_id=customer_user.account_id,
                user_id=None,
                conversation_id=None,
                provider="openai",
                product_surface="portal",
                event_type="openai.responses.portal_initialize_preview",
                external_id=None,
                idempotency_key=make_idempotency_key(
                    "openai",
                    "portal_initialize_preview",
                    customer_user.account_id,
                    normalize_text(payload.get("profile_name")),
                    normalize_text("|".join(payload.get("preferred_pacing") or [])),
                    normalize_text(payload.get("preferred_pacing_custom")),
                    normalize_text("|".join(payload.get("response_style") or [])),
                    normalize_text(payload.get("response_style_custom")),
                    normalize_text(payload.get("communication_notes")),
                    utc_now().isoformat(timespec="seconds"),
                ),
                quantity=float(total_tokens),
                unit="token",
                occurred_at=utc_now(),
                metadata_json={
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": total_tokens,
                    "preview_scope": "portal_initialize",
                },
            ),
        )

    def _build_caption(self, payload: dict[str, Any]) -> str:
        pacing_summary = self._list_summary(payload["preferred_pacing"], payload["preferred_pacing_custom"])
        style_summary = self._list_summary(payload["response_style"], payload["response_style_custom"])
        return (
            "Example only. "
            f"This blends {pacing_summary} pacing with {style_summary} tone, "
            "and you can come back and change these setup preferences any time."
        )

    def _unavailable_response(self, *, configured: bool) -> dict[str, str]:
        detail = (
            "Live AI wording is unavailable right now."
            if configured
            else "Add your OpenAI key to turn on live AI wording."
        )
        return {
            "message": "",
            "caption": "",
            "detail": detail,
            "source": "unavailable_remote" if configured else "unavailable_disabled",
        }

    def _clean_message(self, text: str) -> str:
        cleaned = " ".join(str(text or "").strip().split())
        cleaned = cleaned.strip("\"' ")
        return truncate_text(cleaned, 160) if cleaned else ""

    def cache_key_for_payload(self, payload: dict[str, Any]) -> str:
        normalized = self.normalize_payload(payload)
        return self._cache_key(normalized)

    def _normalize_options(self, raw: Any, *, allowed: dict[str, str]) -> list[str]:
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

    def _list_summary(self, selected: list[str], custom: str) -> str:
        items = [item.replace("_", " ") for item in selected]
        if custom:
            items.append(custom)
        if not items:
            return "flexible"
        if len(items) == 1:
            return items[0]
        if len(items) == 2:
            return f"{items[0]} and {items[1]}"
        return f"{', '.join(items[:-1])}, and {items[-1]}"

    async def _lookup_cached_preview(
        self,
        session: AsyncSession,
        *,
        account_id: Any,
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        state = await session.scalar(select(AccountInitialization).where(AccountInitialization.account_id == account_id))
        if state is None:
            return None
        snapshot = state.snapshot_json or {}
        cache = snapshot.get(self.CACHE_FIELD)
        if not isinstance(cache, dict):
            return None
        entry = cache.get(self._cache_key(payload))
        return entry if isinstance(entry, dict) else None

    async def _cache_preview(
        self,
        session: AsyncSession,
        *,
        account_id: Any,
        payload: dict[str, Any],
        message: str,
        caption: str,
        source: str,
    ) -> None:
        state = await self._get_or_create_state(session, account_id=account_id)
        snapshot = dict(state.snapshot_json or {})
        raw_cache = snapshot.get(self.CACHE_FIELD)
        cache = dict(raw_cache) if isinstance(raw_cache, dict) else {}
        cache[self._cache_key(payload)] = {
            "message": truncate_text(message.strip(), 160),
            "caption": truncate_text(caption.strip(), 240),
            "source": source,
            "saved_at": utc_now().isoformat(),
        }
        ordered_entries = sorted(
            cache.items(),
            key=lambda item: str((item[1] or {}).get("saved_at") or ""),
            reverse=True,
        )[: self.MAX_CACHE_ENTRIES]
        snapshot[self.CACHE_FIELD] = {key: value for key, value in ordered_entries}
        state.snapshot_json = snapshot
        await session.flush()

    async def _get_or_create_state(self, session: AsyncSession, *, account_id: Any) -> AccountInitialization:
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

    def _cache_key(self, payload: dict[str, Any]) -> str:
        return make_idempotency_key(
            "portal_initialize_preview_cache",
            normalize_text(payload.get("profile_name")),
            normalize_text("|".join(payload.get("preferred_pacing") or [])),
            normalize_text(payload.get("preferred_pacing_custom")),
            normalize_text("|".join(payload.get("response_style") or [])),
            normalize_text(payload.get("response_style_custom")),
            normalize_text(payload.get("communication_notes")),
            "1" if payload.get("voice_enabled") else "0",
            "1" if payload.get("proactive_check_ins") else "0",
            normalize_text(payload.get("daily_cadence")),
        )

    def _as_bool(self, value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return False
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    def _usage_int(self, payload: dict[str, Any], *keys: str) -> int:
        for key in keys:
            value = payload.get(key)
            if isinstance(value, int):
                return value
            if isinstance(value, float):
                return int(value)
            if isinstance(value, str) and value.strip().isdigit():
                return int(value.strip())
        return 0
