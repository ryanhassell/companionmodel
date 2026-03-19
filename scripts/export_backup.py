from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from sqlalchemy import select

from app.db.session import get_sessionmaker
from app.models import AdminUser, AppSetting, CallRecord, Conversation, DeliveryAttempt, MediaAsset, MemoryItem, Message, Persona, PromptTemplate, SafetyEvent, ScheduleRule, User


TABLES = [
    ("users", User),
    ("personas", Persona),
    ("conversations", Conversation),
    ("messages", Message),
    ("media_assets", MediaAsset),
    ("memory_items", MemoryItem),
    ("schedule_rules", ScheduleRule),
    ("app_settings", AppSetting),
    ("safety_events", SafetyEvent),
    ("delivery_attempts", DeliveryAttempt),
    ("call_records", CallRecord),
    ("prompt_templates", PromptTemplate),
    ("admin_users", AdminUser),
]


async def main() -> None:
    parser = argparse.ArgumentParser(description="Export a JSON backup")
    parser.add_argument("output", default="var/backups/backup.json", nargs="?")
    args = parser.parse_args()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    sessionmaker = get_sessionmaker()
    payload = {}
    async with sessionmaker() as session:
        for name, model in TABLES:
            rows = (await session.execute(select(model))).scalars().all()
            payload[name] = [row.__dict__ for row in rows]
            for row in payload[name]:
                row.pop("_sa_instance_state", None)
                for key, value in list(row.items()):
                    if hasattr(value, "value"):
                        row[key] = value.value
                    elif hasattr(value, "isoformat"):
                        row[key] = value.isoformat()
                    elif hasattr(value, "hex"):
                        row[key] = str(value)
    output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Backup exported to {output}")


if __name__ == "__main__":
    asyncio.run(main())
