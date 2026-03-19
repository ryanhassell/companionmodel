from __future__ import annotations

import json
import re
import unicodedata
from difflib import SequenceMatcher
from hashlib import sha256
from typing import Any


WHITESPACE_RE = re.compile(r"\s+")


def normalize_text(text: str | None) -> str:
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text).strip().lower()
    return WHITESPACE_RE.sub(" ", text)


def similarity_score(left: str | None, right: str | None) -> float:
    if not left or not right:
        return 0.0
    return SequenceMatcher(a=normalize_text(left), b=normalize_text(right)).ratio()


def truncate_text(text: str, max_length: int) -> str:
    if len(text) <= max_length:
        return text
    clipped = text[: max_length - 1].rsplit(" ", 1)[0].strip()
    return (clipped or text[: max_length - 1]).strip() + "…"


def make_idempotency_key(*parts: Any) -> str:
    data = "||".join(str(part or "") for part in parts)
    return sha256(data.encode("utf-8")).hexdigest()


def extract_json_block(text: str) -> dict[str, Any] | list[Any] | None:
    stripped = text.strip()
    if not stripped:
        return None
    for candidate in [stripped, *re.findall(r"(\{.*\}|\[.*\])", stripped, re.DOTALL)]:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None
