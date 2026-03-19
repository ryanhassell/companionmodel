from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.settings import AppConfig, RuntimeSettings
from app.db.base import Base


@pytest.fixture
def settings() -> RuntimeSettings:
    return RuntimeSettings(
        _env_file=None,
        app=AppConfig(
            prompt_template_root=str(Path("app/prompts").resolve()),
            media_root=str(Path("var/test-media").resolve()),
            log_path=str(Path("var/test.log").resolve()),
        ),
    )


@pytest.fixture
async def sqlite_session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    async with sessionmaker() as session:
        yield session
    await engine.dispose()
