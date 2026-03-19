from __future__ import annotations

from sqlalchemy import select

from app.models.persona import Persona
from app.models.user import User


async def test_basic_db_persistence(sqlite_session):
    persona = Persona(key="seed", display_name="Rowan", is_active=True)
    user = User(phone_number="+15555550104", display_name="Alex")
    sqlite_session.add_all([persona, user])
    await sqlite_session.commit()

    loaded = (await sqlite_session.execute(select(User).where(User.phone_number == "+15555550104"))).scalar_one()
    assert loaded.display_name == "Alex"
