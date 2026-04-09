from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.dependencies import get_optional_admin_context
from app.core.security import create_session_token
from app.core.templating import templates
from app.db.session import get_db_session

router = APIRouter(tags=["auth"])


@router.get("/login")
async def login_page(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    admin_context: object | None = Depends(get_optional_admin_context),
):
    if admin_context is not None:
        return RedirectResponse(url="/admin", status_code=303)
    container = request.app.state.container
    admin_count = await container.auth_service.count_admins(session)
    if admin_count == 0:
        return RedirectResponse(url="/bootstrap", status_code=303)
    return templates.TemplateResponse(
        "auth/login.html",
        {"request": request, "has_admin": admin_count > 0},
    )


@router.post("/login")
async def login_submit(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    form = await request.form()
    container = request.app.state.container
    admin = await container.auth_service.authenticate(
        session,
        username=str(form.get("username", "")),
        password=str(form.get("password", "")),
    )
    if admin is None:
        return templates.TemplateResponse(
            "auth/login.html",
            {"request": request, "error": "Invalid username or password"},
            status_code=400,
        )
    await session.commit()
    token = create_session_token(str(admin.id), container.settings)
    response = RedirectResponse(url="/admin", status_code=303)
    response.set_cookie(
        container.settings.admin.session_cookie_name,
        token,
        httponly=True,
        secure=container.settings.admin.secure_cookies,
        samesite="lax",
        max_age=container.settings.admin.session_max_age_seconds,
    )
    return response


@router.get("/bootstrap")
async def bootstrap_page(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    container = request.app.state.container
    admin_count = await container.auth_service.count_admins(session)
    if admin_count > 0:
        return RedirectResponse(url="/login", status_code=303)
    return templates.TemplateResponse("auth/bootstrap.html", {"request": request})


@router.post("/bootstrap")
async def bootstrap_submit(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    form = await request.form()
    container = request.app.state.container
    admin = await container.auth_service.bootstrap_admin(
        session,
        username=str(form.get("username", "")).strip(),
        password=str(form.get("password", "")),
    )
    await session.commit()
    token = create_session_token(str(admin.id), container.settings)
    response = RedirectResponse(url="/admin", status_code=303)
    response.set_cookie(
        container.settings.admin.session_cookie_name,
        token,
        httponly=True,
        secure=container.settings.admin.secure_cookies,
        samesite="lax",
        max_age=container.settings.admin.session_max_age_seconds,
    )
    return response


@router.post("/logout")
async def logout(request: Request):
    container = request.app.state.container
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(container.settings.admin.session_cookie_name)
    return response
