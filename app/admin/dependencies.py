from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_session_token, validate_csrf
from app.db.session import get_db_session
from app.models.admin import AdminUser
from app.services.container import ServiceContainer


@dataclass(slots=True)
class AdminRequestContext:
    admin_user: AdminUser
    csrf_token_value: str
    container: ServiceContainer

    @property
    def csrf_token(self) -> str:
        return self.csrf_token_value


def get_container(request: Request) -> ServiceContainer:
    return request.app.state.container


async def get_optional_admin_context(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    container: ServiceContainer = Depends(get_container),
) -> AdminRequestContext | None:
    cookie_name = container.settings.admin.session_cookie_name
    token = request.cookies.get(cookie_name)
    if token:
        session_payload = decode_session_token(token, container.settings)
        if session_payload is not None:
            try:
                admin_id = UUID(session_payload.admin_user_id)
            except ValueError:
                admin_id = None
            if admin_id is not None:
                admin_user = await session.get(AdminUser, admin_id)
                if admin_user is not None and admin_user.is_active:
                    return AdminRequestContext(
                        admin_user=admin_user,
                        csrf_token_value=session_payload.csrf_token,
                        container=container,
                    )

    if container.settings.admin.clerk_enabled:
        authz = await container.admin_authz_service.authenticate_request(
            session,
            authorization=request.headers.get("Authorization"),
            session_cookie=request.cookies.get(container.settings.clerk.backend_session_cookie_name)
            or request.cookies.get(container.settings.clerk.session_cookie_name),
        )
        if authz is not None:
            return AdminRequestContext(
                admin_user=authz.admin_user,
                csrf_token_value=authz.csrf_token,
                container=container,
            )
    return None


async def require_admin_context(
    context: AdminRequestContext | None = Depends(get_optional_admin_context),
) -> AdminRequestContext:
    if context is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    return context


async def verify_csrf_or_403(request: Request, context: AdminRequestContext) -> None:
    if not context.container.settings.admin.csrf_protection_enabled:
        return
    form = await request.form()
    csrf_token = form.get("csrf_token")
    if not validate_csrf(context.csrf_token, str(csrf_token) if csrf_token else None):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")
