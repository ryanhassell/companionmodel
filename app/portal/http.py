from __future__ import annotations

from urllib.parse import urlencode

from fastapi import Request
from fastapi.responses import JSONResponse

from app.core.logging import get_logger

logger = get_logger(__name__)


def current_path_with_query(request: Request) -> str:
    query = request.url.query
    return f"{request.url.path}?{query}" if query else request.url.path


def safe_portal_resume_url(value: str | None, *, default: str = "/app/landing") -> str:
    candidate = str(value or "").strip()
    if not candidate:
        return default
    if not candidate.startswith("/") or candidate.startswith("//"):
        return default
    return candidate


def portal_login_url(*, reason: str, resume_url: str | None) -> str:
    safe_resume = safe_portal_resume_url(resume_url)
    query = {"reason": reason}
    if safe_resume != "/app/landing":
        query["resume"] = safe_resume
    return f"/app/login?{urlencode(query)}"


def is_portal_interactive_request(request: Request) -> bool:
    interactive_paths = {
        "/app/initialize/save",
        "/app/initialize/preview",
        "/app/initialize/billing/checkout",
        "/app/initialize/draft-event",
        "/app/parent-chat/send",
        "/app/parent-chat/stream",
    }
    if request.url.path in interactive_paths:
        return True
    if request.headers.get("x-resona-request", "").strip().lower() == "fetch":
        return True
    return "application/json" in request.headers.get("accept", "").lower()



def portal_json_error_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    detail: str,
    login_reason: str,
    retryable: bool = False,
    resume_url: str | None = None,
) -> JSONResponse:
    safe_resume = safe_portal_resume_url(
        resume_url or request.headers.get("x-resona-resume-url") or current_path_with_query(request)
    )
    login_url = portal_login_url(reason=login_reason, resume_url=safe_resume)
    logger.info(
        "portal_json_session_response",
        code=code,
        status_code=status_code,
        path=request.url.path,
        resume_url=safe_resume,
    )
    return JSONResponse(
        {
            "ok": False,
            "code": code,
            "detail": detail,
            "login_url": login_url,
            "resume_url": safe_resume,
            "retryable": retryable,
        },
        status_code=status_code,
    )
