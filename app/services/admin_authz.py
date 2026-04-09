from __future__ import annotations

import secrets
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import RuntimeSettings
from app.models.admin import AdminIdentity, AdminUser
from app.services.clerk_auth import ClerkAuthService
from app.utils.time import utc_now


@dataclass(slots=True)
class AdminAuthResult:
    admin_user: AdminUser
    csrf_token: str


class AdminAuthzService:
    def __init__(self, settings: RuntimeSettings, clerk_auth_service: ClerkAuthService) -> None:
        self.settings = settings
        self.clerk_auth_service = clerk_auth_service

    async def authenticate_request(
        self,
        session: AsyncSession,
        *,
        authorization: str | None,
        session_cookie: str | None,
    ) -> AdminAuthResult | None:
        if not self.settings.admin.clerk_enabled:
            return None
        if not self.clerk_auth_service.enabled:
            return None

        token = self.clerk_auth_service.token_from_request(authorization, session_cookie)
        if not token:
            return None
        claims = self.clerk_auth_service.verify_token(token)

        allowed_roles = {item.lower() for item in _as_list(self.settings.admin.clerk_role_allowlist)}
        claim_role = str(claims.org_role or "").lower()
        if allowed_roles and claim_role not in allowed_roles:
            return None

        allowlisted_users = {item.strip() for item in _as_list(self.settings.admin.clerk_user_allowlist) if item.strip()}
        allowlisted_emails = {item.strip().lower() for item in _as_list(self.settings.admin.clerk_email_allowlist) if item.strip()}
        normalized_email = (claims.email or "").strip().lower()
        allowlisted = claims.user_id in allowlisted_users or (normalized_email and normalized_email in allowlisted_emails)
        if not allowlisted_users and not allowlisted_emails:
            allowlisted = True
        if not allowlisted:
            return None

        if self.settings.admin.require_clerk_mfa and not claims.mfa_verified:
            return None

        identity = await session.scalar(select(AdminIdentity).where(AdminIdentity.clerk_user_id == claims.user_id))
        if identity is None:
            username = (normalized_email or f"clerk-{claims.user_id[:18]}")[:80]
            admin_user = AdminUser(username=username, password_hash=f"clerk:{secrets.token_hex(16)}", is_active=True)
            session.add(admin_user)
            await session.flush()
            identity = AdminIdentity(
                admin_user_id=admin_user.id,
                provider="clerk",
                clerk_user_id=claims.user_id,
                clerk_org_id=claims.org_id,
                email=normalized_email or None,
                org_role=claims.org_role,
                mfa_verified=claims.mfa_verified,
                allowlisted=True,
                is_active=True,
                last_auth_at=utc_now(),
                metadata_json={"source": "clerk_admin"},
            )
            session.add(identity)
            await session.flush()
        else:
            admin_user = await session.get(AdminUser, identity.admin_user_id)
            if admin_user is None or not admin_user.is_active:
                return None
            identity.clerk_org_id = claims.org_id
            identity.email = normalized_email or identity.email
            identity.org_role = claims.org_role
            identity.mfa_verified = claims.mfa_verified
            identity.allowlisted = allowlisted
            identity.last_auth_at = utc_now()
            await session.flush()

        csrf_token = self.clerk_auth_service.csrf_token(
            clerk_user_id=claims.user_id,
            clerk_org_id=claims.org_id or "admin",
        )
        admin_user.last_login_at = utc_now()
        await session.flush()
        return AdminAuthResult(admin_user=admin_user, csrf_token=csrf_token)


def _as_list(value: list[str] | str | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return [item.strip() for item in str(value).split(",") if item.strip()]
