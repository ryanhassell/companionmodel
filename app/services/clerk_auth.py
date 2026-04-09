from __future__ import annotations

import hashlib
import hmac
import secrets
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx
import jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.settings import RuntimeSettings
from app.models.portal import Account, AuthIdentityEvent, CustomerUser, RoleAssignment
from app.models.enums import HouseholdRole
from app.utils.time import utc_now

logger = get_logger(__name__)
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_CLERK_LOCAL_EMAIL_RE = re.compile(r"^[^@\s]+@clerk\.local$", re.IGNORECASE)
_CLERK_API_BASE_URL = "https://api.clerk.com/v1"


@dataclass(slots=True)
class ClerkClaims:
    user_id: str
    org_id: str | None
    org_role: str | None
    email: str | None
    mfa_verified: bool
    raw: dict[str, Any]


@dataclass(slots=True)
class TenantContext:
    account: Account
    customer_user: CustomerUser
    role: HouseholdRole
    clerk_user_id: str
    clerk_org_id: str
    mfa_verified: bool


class ClerkAuthService:
    def __init__(self, settings: RuntimeSettings) -> None:
        self.settings = settings
        jwks_url = settings.clerk.jwks_url
        if not jwks_url and settings.clerk.issuer:
            jwks_url = settings.clerk.issuer.rstrip("/") + "/.well-known/jwks.json"
        self._jwks_client = jwt.PyJWKClient(jwks_url) if jwks_url else None

    @property
    def enabled(self) -> bool:
        cfg = self.settings.clerk
        return bool(cfg.enabled and cfg.issuer and self._jwks_client)

    def verify_token(self, token: str) -> ClerkClaims:
        if not self.enabled:
            raise ValueError("Clerk auth is not configured")
        assert self._jwks_client is not None
        cfg = self.settings.clerk
        signing_key = self._jwks_client.get_signing_key_from_jwt(token)
        kwargs: dict[str, Any] = {
            "algorithms": ["RS256"],
            "issuer": cfg.issuer,
            "options": {"require": ["exp", "iat", "sub"]},
        }
        if cfg.audience:
            kwargs["audience"] = cfg.audience
        decoded = jwt.decode(token, signing_key.key, **kwargs)

        user_id = str(decoded.get("sub") or "")
        if not user_id:
            raise ValueError("Missing user subject")
        org_id = decoded.get("org_id")
        org_role = decoded.get("org_role")
        email = self._extract_email(decoded)
        return ClerkClaims(
            user_id=user_id,
            org_id=str(org_id) if org_id else None,
            org_role=str(org_role) if org_role else None,
            email=email,
            mfa_verified=self._mfa_verified(decoded),
            raw=decoded,
        )

    def token_from_request(self, authorization: str | None, *session_cookies: str | None) -> str | None:
        if authorization and authorization.lower().startswith("bearer "):
            return authorization[7:].strip()
        for session_cookie in session_cookies:
            if session_cookie:
                return session_cookie
        return None

    async def resolve_tenant_context(self, session: AsyncSession, claims: ClerkClaims) -> TenantContext:
        effective_org_id = claims.org_id or f"user:{claims.user_id}"
        if self.settings.clerk.require_org and not claims.org_id:
            logger.info(
                "clerk_personal_tenant_fallback",
                extra={"clerk_user_id": claims.user_id, "effective_org_id": effective_org_id},
            )

        account = await session.scalar(select(Account).where(Account.clerk_org_id == effective_org_id))
        display_name = self._extract_display_name(claims.raw)
        if account is None:
            slug = self._safe_slug(effective_org_id)
            account = Account(
                name=(f"Organization {claims.org_id}" if claims.org_id else (display_name or claims.email or "Resona Household")),
                slug=f"org-{slug[:32]}",
                clerk_org_id=effective_org_id,
            )
            session.add(account)
            await session.flush()

        customer_user = await session.scalar(select(CustomerUser).where(CustomerUser.clerk_user_id == claims.user_id))
        if customer_user is None and claims.email:
            customer_user = await session.scalar(
                select(CustomerUser).where(
                    CustomerUser.account_id == account.id,
                    CustomerUser.email == claims.email,
                )
            )
        if customer_user is None:
            email = claims.email or f"{claims.user_id}@clerk.local"
            customer_user = CustomerUser(
                account_id=account.id,
                email=email,
                password_hash=f"clerk:{secrets.token_hex(16)}",
                display_name=display_name,
                clerk_user_id=claims.user_id,
                verification_level="verified",
            )
            session.add(customer_user)
            await session.flush()
        else:
            customer_user.account_id = account.id
            customer_user.clerk_user_id = claims.user_id
            if claims.email and _EMAIL_RE.match(claims.email):
                customer_user.email = claims.email
            if display_name and (
                not customer_user.display_name
                or self._looks_like_clerk_placeholder(customer_user.display_name)
            ):
                customer_user.display_name = display_name

        customer_user.last_clerk_auth_at = utc_now()
        role = HouseholdRole.owner if not claims.org_id else self._map_role(claims.org_role)

        session.add(
            AuthIdentityEvent(
                account_id=account.id,
                customer_user_id=customer_user.id,
                event_type="clerk_auth_seen",
                details_json={
                    "org_id": claims.org_id,
                    "effective_org_id": effective_org_id,
                    "org_role": claims.org_role,
                    "mfa_verified": claims.mfa_verified,
                    "personal_tenant": claims.org_id is None,
                },
                created_at=utc_now(),
            )
        )

        assignment = await session.scalar(
            select(RoleAssignment).where(
                RoleAssignment.account_id == account.id,
                RoleAssignment.customer_user_id == customer_user.id,
            )
        )
        if assignment is not None:
            assignment.role = role

        await session.flush()
        return TenantContext(
            account=account,
            customer_user=customer_user,
            role=role,
            clerk_user_id=claims.user_id,
            clerk_org_id=effective_org_id,
            mfa_verified=claims.mfa_verified,
        )

    def csrf_token(self, *, clerk_user_id: str, clerk_org_id: str) -> str:
        payload = f"{clerk_user_id}:{clerk_org_id}".encode("utf-8")
        key = self.settings.app.secret_key.encode("utf-8")
        return hmac.new(key, payload, hashlib.sha256).hexdigest()

    async def verify_current_password(self, *, clerk_user_id: str, password: str) -> bool:
        secret_key = str(self.settings.clerk.secret_key or "").strip()
        if not secret_key:
            raise RuntimeError("Clerk secret key is not configured")
        if not clerk_user_id or not password:
            return False

        endpoint = f"{_CLERK_API_BASE_URL}/users/{quote(clerk_user_id, safe='')}/verify_password"
        headers = {
            "Authorization": f"Bearer {secret_key}",
            "Content-Type": "application/json",
        }
        payload = {"password": password}

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(endpoint, headers=headers, json=payload)
        except httpx.HTTPError as exc:
            logger.warning(
                "clerk_password_verify_transport_error",
                clerk_user_id=clerk_user_id,
                error=str(exc),
            )
            raise RuntimeError("Clerk password verification failed") from exc

        if response.status_code == 200:
            data = response.json() if response.content else {}
            verified = data.get("verified", True)
            return bool(verified)

        if response.status_code in {400, 404, 422}:
            logger.info(
                "clerk_password_verify_rejected",
                clerk_user_id=clerk_user_id,
                status_code=response.status_code,
            )
            return False

        if response.status_code in {401, 403, 429}:
            logger.warning(
                "clerk_password_verify_unavailable",
                clerk_user_id=clerk_user_id,
                status_code=response.status_code,
            )
            raise RuntimeError("Clerk password verification is temporarily unavailable")

        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "clerk_password_verify_failed",
                clerk_user_id=clerk_user_id,
                status_code=response.status_code,
                error=str(exc),
            )
            raise RuntimeError("Clerk password verification failed") from exc
        return False

    def _extract_email(self, claims: dict[str, Any]) -> str | None:
        email = claims.get("email")
        if isinstance(email, str) and _EMAIL_RE.match(email):
            return email.lower()
        emails = claims.get("email_addresses")
        if isinstance(emails, list):
            for item in emails:
                if isinstance(item, dict):
                    value = item.get("email_address")
                    if isinstance(value, str) and _EMAIL_RE.match(value):
                        return value.lower()
        return None

    def _extract_display_name(self, claims: dict[str, Any]) -> str | None:
        candidates = [
            claims.get("name"),
            claims.get("full_name"),
            claims.get("fullName"),
            self._join_name_parts(claims.get("first_name"), claims.get("last_name")),
            self._join_name_parts(claims.get("firstName"), claims.get("lastName")),
            self._join_name_parts(claims.get("given_name"), claims.get("family_name")),
            claims.get("username"),
        ]
        for candidate in candidates:
            if not isinstance(candidate, str):
                continue
            cleaned = candidate.strip()
            if not cleaned:
                continue
            if self._looks_like_clerk_placeholder(cleaned):
                continue
            return cleaned
        return None

    def _join_name_parts(self, first: Any, last: Any) -> str | None:
        first_clean = str(first or "").strip()
        last_clean = str(last or "").strip()
        full = " ".join(part for part in [first_clean, last_clean] if part)
        return full or None

    def _looks_like_clerk_placeholder(self, value: str | None) -> bool:
        if not value:
            return True
        cleaned = value.strip()
        if not cleaned:
            return True
        lowered = cleaned.lower()
        if _CLERK_LOCAL_EMAIL_RE.match(lowered):
            return True
        if lowered.startswith("user_") and "@" not in lowered:
            return True
        return False

    def _mfa_verified(self, claims: dict[str, Any]) -> bool:
        amr = claims.get("amr")
        if isinstance(amr, list):
            if any(str(item).lower() in {"mfa", "totp", "otp", "webauthn"} for item in amr):
                return True
        fva = claims.get("fva")
        if isinstance(fva, list) and len(fva) >= 2:
            try:
                return int(fva[1]) >= 0
            except (TypeError, ValueError):
                return False
        return False

    def _map_role(self, org_role: str | None) -> HouseholdRole:
        value = (org_role or "").lower()
        if value in {"org:admin", "admin", "owner"}:
            return HouseholdRole.owner
        if value in {"org:member", "member", "guardian"}:
            return HouseholdRole.guardian
        if value in {"caregiver"}:
            return HouseholdRole.caregiver
        return HouseholdRole.viewer

    def _safe_slug(self, value: str) -> str:
        cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
        return cleaned or "org"
