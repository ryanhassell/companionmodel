from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, Template
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import RuntimeSettings
from app.models.configuration import PromptTemplate


class PromptService:
    def __init__(self, settings: RuntimeSettings) -> None:
        self.settings = settings
        self.environment = Environment(
            loader=FileSystemLoader(str(settings.prompt_template_root_path)),
            autoescape=False,
            trim_blocks=True,
            lstrip_blocks=True,
        )

    async def render(self, session: AsyncSession, name: str, context: dict[str, Any]) -> str:
        template = await self._load_db_template(session, name)
        if template:
            return Template(template.body).render(**context).strip()
        file_name = f"{name}.j2"
        return self.environment.get_template(file_name).render(**context).strip()

    async def _load_db_template(self, session: AsyncSession, name: str) -> PromptTemplate | None:
        stmt = (
            select(PromptTemplate)
            .where(PromptTemplate.name == name, PromptTemplate.is_active.is_(True))
            .order_by(desc(PromptTemplate.version))
        )
        return (await session.execute(stmt)).scalars().first()

    def default_template_files(self) -> list[Path]:
        return sorted(self.settings.prompt_template_root_path.glob("*.j2"))
