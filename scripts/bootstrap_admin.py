from __future__ import annotations

import argparse
import asyncio
import getpass

from app.core.settings import get_settings
from app.db.session import get_sessionmaker
from app.services.container import ServiceContainer


async def main() -> None:
    parser = argparse.ArgumentParser(description="Bootstrap the first admin user")
    parser.add_argument("--username", default=None)
    parser.add_argument("--password", default=None)
    args = parser.parse_args()

    settings = get_settings()
    username = args.username or settings.admin.bootstrap_username or input("Admin username: ").strip()
    password = args.password or settings.admin.bootstrap_password or getpass.getpass("Admin password: ")
    if len(password) < 12:
        raise SystemExit("Password must be at least 12 characters")

    container = ServiceContainer.build(settings)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        try:
            admin = await container.auth_service.bootstrap_admin(session, username=username, password=password)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        await session.commit()
        print(f"Created admin user {admin.username} ({admin.id})")
    await container.aclose()


if __name__ == "__main__":
    asyncio.run(main())
