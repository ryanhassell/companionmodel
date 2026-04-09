from __future__ import annotations

import argparse
import asyncio
import os
from dataclasses import dataclass

import httpx
from sqlalchemy import select

from app.db.session import get_sessionmaker
from app.models.portal import Account, CustomerUser


@dataclass(slots=True)
class ClerkUserMatch:
    user_id: str
    org_id: str | None


class ClerkApiClient:
    def __init__(self, *, secret_key: str, base_url: str = "https://api.clerk.com/v1") -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={
                "Authorization": f"Bearer {secret_key}",
                "Content-Type": "application/json",
            },
            timeout=20.0,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def match_user(self, email: str) -> ClerkUserMatch | None:
        users_resp = await self._client.get("/users", params={"email_address": email, "limit": 1})
        users_resp.raise_for_status()
        users = users_resp.json()
        if not users:
            return None

        user = users[0]
        user_id = str(user.get("id") or "")
        if not user_id:
            return None

        memberships_resp = await self._client.get(
            f"/users/{user_id}/organization_memberships",
            params={"limit": 1},
        )
        memberships_resp.raise_for_status()
        memberships = memberships_resp.json()
        org_id = None
        if memberships:
            org_id = str((memberships[0].get("organization") or {}).get("id") or "") or None
        return ClerkUserMatch(user_id=user_id, org_id=org_id)


async def run(dry_run: bool) -> None:
    secret_key = os.getenv("CLERK_SECRET_KEY")
    if not secret_key:
        raise RuntimeError("CLERK_SECRET_KEY is required")
    base_url = os.getenv("CLERK_API_BASE_URL", "https://api.clerk.com/v1")

    client = ClerkApiClient(secret_key=secret_key, base_url=base_url)
    sessionmaker = get_sessionmaker()
    linked = 0
    skipped = 0
    try:
        async with sessionmaker() as session:
            users = list(
                (
                    await session.execute(
                        select(CustomerUser).where(CustomerUser.clerk_user_id.is_(None)).order_by(CustomerUser.created_at)
                    )
                )
                .scalars()
                .all()
            )
            for user in users:
                match = await client.match_user(user.email)
                if match is None:
                    skipped += 1
                    continue
                account = await session.get(Account, user.account_id)
                if account is None:
                    skipped += 1
                    continue
                user.clerk_user_id = match.user_id
                if match.org_id and not account.clerk_org_id:
                    account.clerk_org_id = match.org_id
                linked += 1
            if dry_run:
                await session.rollback()
            else:
                await session.commit()
    finally:
        await client.aclose()

    mode = "DRY-RUN" if dry_run else "APPLIED"
    print(f"[{mode}] clerk identity backfill complete: linked={linked} skipped={skipped}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill local customer users/accounts with Clerk IDs")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
