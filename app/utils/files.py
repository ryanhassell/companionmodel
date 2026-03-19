from __future__ import annotations

import base64
from pathlib import Path


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def write_base64_file(path: Path, payload: str) -> None:
    ensure_parent(path)
    path.write_bytes(base64.b64decode(payload))
