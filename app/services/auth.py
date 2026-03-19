from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password, verify_password
from app.models.admin import AdminUser
from app.utils.time import utc_now


class AuthService:
    async def bootstrap_admin(self, session: AsyncSession, *, username: str, password: str) -> AdminUser:
        existing_count = await session.scalar(select(func.count()).select_from(AdminUser))
        if existing_count and existing_count > 0:
            raise ValueError("An admin user already exists")
        admin = AdminUser(username=username, password_hash=hash_password(password))
        session.add(admin)
        await session.flush()
        return admin

    async def authenticate(
        self,
        session: AsyncSession,
        *,
        username: str,
        password: str,
    ) -> AdminUser | None:
        stmt = select(AdminUser).where(AdminUser.username == username, AdminUser.is_active.is_(True))
        admin = (await session.execute(stmt)).scalar_one_or_none()
        if admin is None:
            return None
        if not verify_password(password, admin.password_hash):
            return None
        admin.last_login_at = utc_now()
        await session.flush()
        return admin

    async def count_admins(self, session: AsyncSession) -> int:
        count = await session.scalar(select(func.count()).select_from(AdminUser))
        return int(count or 0)
