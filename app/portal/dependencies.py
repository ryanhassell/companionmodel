from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db_session
from app.models.portal import CustomerUser
from app.models.enums import HouseholdRole
from app.services.container import ServiceContainer


@dataclass(slots=True)
class PortalRequestContext:
    customer_user: CustomerUser
    account_id: str
    role: HouseholdRole
    clerk_user_id: str
    clerk_org_id: str
    mfa_verified: bool
    csrf_token: str
    container: ServiceContainer


async def get_container(request: Request) -> ServiceContainer:
    return request.app.state.container


async def get_optional_portal_context(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    container: ServiceContainer = Depends(get_container),
) -> PortalRequestContext | None:
    if not container.clerk_auth_service.enabled:
        token = request.cookies.get(container.settings.customer_portal.session_cookie_name)
        if not token:
            return None
        authd = await container.customer_auth_service.resolve_portal_session(session, raw_token=token)
        if authd is None:
            return None
        return PortalRequestContext(
            customer_user=authd.customer_user,
            account_id=str(authd.customer_user.account_id),
            role=HouseholdRole.owner,
            clerk_user_id="legacy",
            clerk_org_id="legacy",
            mfa_verified=False,
            csrf_token=authd.csrf_token,
            container=container,
        )

    existing = getattr(request.state, "portal_tenant_context", None)
    if existing is not None:
        tenant = existing
    else:
        token = container.clerk_auth_service.token_from_request(
            request.headers.get("authorization"),
            request.cookies.get(container.settings.clerk.backend_session_cookie_name),
            request.cookies.get(container.settings.clerk.session_cookie_name),
        )
        if not token:
            return None
        try:
            claims = container.clerk_auth_service.verify_token(token)
            tenant = await container.clerk_auth_service.resolve_tenant_context(session, claims)
            await session.commit()
            request.state.portal_tenant_context = tenant
        except Exception:
            await session.rollback()
            return None

    csrf_token = container.clerk_auth_service.csrf_token(
        clerk_user_id=tenant.clerk_user_id,
        clerk_org_id=tenant.clerk_org_id,
    )
    return PortalRequestContext(
        customer_user=tenant.customer_user,
        account_id=str(tenant.account.id),
        role=tenant.role,
        clerk_user_id=tenant.clerk_user_id,
        clerk_org_id=tenant.clerk_org_id,
        mfa_verified=tenant.mfa_verified,
        csrf_token=csrf_token,
        container=container,
    )


async def require_portal_context(
    context: PortalRequestContext | None = Depends(get_optional_portal_context),
) -> PortalRequestContext:
    if context is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    return context


async def require_owner_mfa_context(
    context: PortalRequestContext = Depends(require_portal_context),
) -> PortalRequestContext:
    if context.role != HouseholdRole.owner:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Owner permissions required",
        )
    if (
        context.container.clerk_auth_service.enabled
        and context.container.settings.clerk.require_owner_mfa
        and not context.mfa_verified
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="MFA required")
    return context
