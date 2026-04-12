from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory="app/templates")

_STATIC_ROOT = Path("app/static")


def static_asset(path: str) -> str:
    normalized = str(path or "").lstrip("/")
    if not normalized:
        return "/static"
    candidate = _STATIC_ROOT / normalized
    try:
        version = int(candidate.stat().st_mtime)
    except OSError:
        return f"/static/{normalized}"
    return f"/static/{normalized}?v={version}"


templates.env.globals["static_asset"] = static_asset
