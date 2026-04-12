from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from app.models.communication import Message
from app.models.portal import PortalChatMessage
from app.utils.text import truncate_text


def render_recent_message_snippets(messages: Sequence[Message], *, limit: int = 8) -> str:
    lines: list[str] = []
    for message in list(messages)[-limit:]:
        body = truncate_text((message.body or "").strip(), 220)
        if not body:
            continue
        lines.append(f"- {message.direction.value}: {body}")
    return "\n".join(lines) or "- none"


def render_memory_hits(memory_hits: Sequence[Any], *, limit: int = 6) -> str:
    lines: list[str] = []
    for hit in list(memory_hits)[:limit]:
        memory = hit.memory
        label = memory.title or "Memory"
        text = memory.summary or memory.content or ""
        lines.append(f"- {label}: {truncate_text(text, 220)}")
    return "\n".join(lines) or "- none"


def render_portal_chat_history(messages: Sequence[PortalChatMessage], *, limit: int = 12) -> str:
    lines: list[str] = []
    for message in list(messages)[-limit:]:
        sender = "Resona" if message.sender == "assistant" else "Parent"
        body = truncate_text((message.body or "").strip(), 320)
        if not body:
            continue
        lines.append(f"{sender}: {body}")
        metadata = dict(message.metadata_json or {})
        raw_details = metadata.get("memory_saved_details")
        if not isinstance(raw_details, list):
            continue
        for item in raw_details[:3]:
            if not isinstance(item, dict):
                continue
            title = truncate_text(str(item.get("title") or "").strip(), 80)
            content = truncate_text(str(item.get("content") or "").strip(), 180)
            if title and content:
                lines.append(f"  Saved memory: {title} — {content}")
            elif title:
                lines.append(f"  Saved memory: {title}")
    return "\n".join(lines)
