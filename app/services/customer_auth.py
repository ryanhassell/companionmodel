from __future__ import annotations

import re
import secrets
import string
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import Select, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import generate_csrf_token, generate_session_secret, hash_password, stable_token_hash, verify_password
from app.core.settings import RuntimeSettings
from app.models.portal import (
    Account,
    ChildProfile,
    ConsentRecord,
    CustomerUser,
    EmailVerificationToken,
    Household,
    PhoneOtpChallenge,
    PortalSession,
    RoleAssignment,
    VerificationCase,
)
from app.models.enums import HouseholdRole, VerificationCaseStatus
from app.models.user import User
from app.utils.time import utc_now

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@dataclass(slots=True)
class AuthenticatedPortalUser:
    customer_user: CustomerUser
    portal_session: PortalSession
    csrf_token: str


class CustomerAuthService:
    def __init__(self, settings: RuntimeSettings) -> None:
        self.settings = settings

    def _normalize_email(self, value: str) -> str:
        return value.strip().lower()

    def _build_slug(self, source: str) -> str:
        normalized = re.sub(r"[^a-z0-9]+", "-", source.lower()).strip("-")
        core = normalized[:40] if normalized else "account"
        return f"{core}-{secrets.token_hex(3)}"

    async def register_user(
        self,
        session: AsyncSession,
        *,
        email: str,
        password: str,
        display_name: str,
        phone_number: str | None,
        accepted_terms: bool,
        accepted_privacy: bool,
        ip_address: str | None,
        user_agent: str | None,
    ) -> tuple[CustomerUser, str, str | None]:
        if not _EMAIL_RE.match(email.strip()):
            raise ValueError("Please provide a valid email address")
        normalized_email = self._normalize_email(email)
        existing = await session.scalar(select(CustomerUser).where(CustomerUser.email == normalized_email))
        if existing is not None:
            raise ValueError("An account with this email already exists")
        if len(password) < 12:
            raise ValueError("Password must be at least 12 characters")
        if not accepted_terms or not accepted_privacy:
            raise ValueError("You must accept terms and privacy policy")

        account = Account(name=f"{display_name or normalized_email} Account", slug=self._build_slug(normalized_email))
        session.add(account)
        await session.flush()

        customer_user = CustomerUser(
            account_id=account.id,
            email=normalized_email,
            phone_number=phone_number.strip() if phone_number else None,
            password_hash=hash_password(password),
            display_name=display_name.strip() or None,
            relationship_label="pending",
            verification_level="unverified",
        )
        session.add(customer_user)
        await session.flush()

        session.add_all(
            [
                ConsentRecord(
                    account_id=account.id,
                    customer_user_id=customer_user.id,
                    policy_type="terms",
                    policy_version=self.settings.customer_portal.policy_version,
                    accepted_at=utc_now(),
                    ip_address=ip_address,
                    user_agent=user_agent,
                ),
                ConsentRecord(
                    account_id=account.id,
                    customer_user_id=customer_user.id,
                    policy_type="privacy",
                    policy_version=self.settings.customer_portal.policy_version,
                    accepted_at=utc_now(),
                    ip_address=ip_address,
                    user_agent=user_agent,
                ),
            ]
        )

        verification_case = VerificationCase(
            account_id=account.id,
            customer_user_id=customer_user.id,
            status=VerificationCaseStatus.pending,
            risk_score=15 if phone_number else 35,
            reason_codes=[] if phone_number else ["phone_missing"],
            attestation_accepted_at=utc_now(),
        )
        session.add(verification_case)

        email_token = await self.issue_email_verification_token(session, customer_user=customer_user)
        otp_code = None
        if customer_user.phone_number:
            otp_code = await self.issue_phone_otp(session, customer_user=customer_user)
        await session.flush()
        return customer_user, email_token, otp_code

    async def authenticate(
        self,
        session: AsyncSession,
        *,
        email: str,
        password: str,
    ) -> CustomerUser | None:
        normalized_email = self._normalize_email(email)
        user = await session.scalar(select(CustomerUser).where(CustomerUser.email == normalized_email))
        if user is None or not user.is_active:
            return None

        now = utc_now()
        if user.locked_until and user.locked_until > now:
            return None

        if not verify_password(password, user.password_hash):
            user.failed_login_attempts += 1
            if user.failed_login_attempts >= self.settings.customer_portal.max_login_failures:
                user.locked_until = now + timedelta(minutes=self.settings.customer_portal.lockout_minutes)
            await session.flush()
            return None

        user.failed_login_attempts = 0
        user.locked_until = None
        user.last_login_at = now
        await session.flush()
        return user

    async def issue_email_verification_token(self, session: AsyncSession, *, customer_user: CustomerUser) -> str:
        token = generate_session_secret()
        record = EmailVerificationToken(
            customer_user_id=customer_user.id,
            token_hash=stable_token_hash(token, self.settings),
            expires_at=utc_now() + timedelta(minutes=self.settings.customer_portal.email_token_minutes),
        )
        session.add(record)
        await session.flush()
        return token

    async def verify_email_token(self, session: AsyncSession, *, token: str) -> CustomerUser | None:
        hashed = stable_token_hash(token, self.settings)
        record = await session.scalar(
            select(EmailVerificationToken).where(EmailVerificationToken.token_hash == hashed)
        )
        if record is None or record.consumed_at is not None or record.expires_at <= utc_now():
            return None
        record.consumed_at = utc_now()
        user = await session.get(CustomerUser, record.customer_user_id)
        if user is None:
            return None
        user.email_verified_at = utc_now()
        await self._refresh_verification_level(session, user)
        await session.flush()
        return user

    async def issue_phone_otp(self, session: AsyncSession, *, customer_user: CustomerUser) -> str:
        if not customer_user.phone_number:
            raise ValueError("Phone number is required for OTP")
        code = "".join(secrets.choice(string.digits) for _ in range(6))
        session.add(
            PhoneOtpChallenge(
                customer_user_id=customer_user.id,
                phone_number=customer_user.phone_number,
                code_hash=stable_token_hash(code, self.settings),
                expires_at=utc_now() + timedelta(minutes=self.settings.customer_portal.otp_code_minutes),
            )
        )
        await session.flush()
        return code

    async def verify_phone_otp(self, session: AsyncSession, *, customer_user: CustomerUser, code: str) -> bool:
        stmt: Select[tuple[PhoneOtpChallenge]] = (
            select(PhoneOtpChallenge)
            .where(PhoneOtpChallenge.customer_user_id == customer_user.id)
            .order_by(desc(PhoneOtpChallenge.created_at))
            .limit(1)
        )
        challenge = (await session.execute(stmt)).scalar_one_or_none()
        if challenge is None:
            return False
        if challenge.verified_at is not None or challenge.expires_at <= utc_now():
            return False
        challenge.attempts += 1
        if challenge.attempts > self.settings.customer_portal.otp_max_attempts:
            return False
        if challenge.code_hash != stable_token_hash(code.strip(), self.settings):
            return False
        challenge.verified_at = utc_now()
        customer_user.phone_verified_at = utc_now()
        await self._refresh_verification_level(session, customer_user)
        await session.flush()
        return True

    async def _refresh_verification_level(self, session: AsyncSession, customer_user: CustomerUser) -> None:
        if customer_user.email_verified_at and (customer_user.phone_verified_at or not customer_user.phone_number):
            customer_user.verification_level = "verified"
        elif customer_user.email_verified_at:
            customer_user.verification_level = "limited"
        else:
            customer_user.verification_level = "unverified"

        latest_case = await session.scalar(
            select(VerificationCase)
            .where(VerificationCase.customer_user_id == customer_user.id)
            .order_by(desc(VerificationCase.created_at))
        )
        if latest_case:
            if customer_user.verification_level == "verified":
                latest_case.status = VerificationCaseStatus.approved
            elif customer_user.verification_level == "limited":
                latest_case.status = VerificationCaseStatus.limited
            else:
                latest_case.status = VerificationCaseStatus.pending

    async def create_portal_session(
        self,
        session: AsyncSession,
        *,
        customer_user: CustomerUser,
        user_agent: str | None,
        ip_address: str | None,
        trusted_device: bool,
    ) -> tuple[str, PortalSession]:
        raw_token = generate_session_secret()
        portal_session = PortalSession(
            customer_user_id=customer_user.id,
            session_token_hash=stable_token_hash(raw_token, self.settings),
            csrf_token=generate_csrf_token(),
            user_agent=user_agent,
            ip_address=ip_address,
            trusted_device=trusted_device,
            expires_at=utc_now() + timedelta(seconds=self.settings.customer_portal.session_max_age_seconds),
            last_seen_at=utc_now(),
        )
        session.add(portal_session)
        await session.flush()
        return raw_token, portal_session

    async def resolve_portal_session(
        self,
        session: AsyncSession,
        *,
        raw_token: str,
    ) -> AuthenticatedPortalUser | None:
        token_hash = stable_token_hash(raw_token, self.settings)
        portal_session = await session.scalar(
            select(PortalSession).where(PortalSession.session_token_hash == token_hash)
        )
        if portal_session is None or portal_session.revoked_at is not None or portal_session.expires_at <= utc_now():
            return None
        customer_user = await session.get(CustomerUser, portal_session.customer_user_id)
        if customer_user is None or not customer_user.is_active:
            return None
        portal_session.last_seen_at = utc_now()
        await session.flush()
        return AuthenticatedPortalUser(
            customer_user=customer_user,
            portal_session=portal_session,
            csrf_token=portal_session.csrf_token,
        )

    async def revoke_portal_session(
        self,
        session: AsyncSession,
        *,
        raw_token: str,
    ) -> None:
        token_hash = stable_token_hash(raw_token, self.settings)
        portal_session = await session.scalar(
            select(PortalSession).where(PortalSession.session_token_hash == token_hash)
        )
        if portal_session is None:
            return
        portal_session.revoked_at = utc_now()
        await session.flush()

    async def revoke_session_by_id(
        self,
        session: AsyncSession,
        *,
        customer_user: CustomerUser,
        portal_session_id: str,
    ) -> bool:
        stmt = select(PortalSession).where(
            PortalSession.id == portal_session_id,
            PortalSession.customer_user_id == customer_user.id,
        )
        existing = (await session.execute(stmt)).scalar_one_or_none()
        if existing is None:
            return False
        existing.revoked_at = utc_now()
        await session.flush()
        return True

    async def complete_onboarding(
        self,
        session: AsyncSession,
        *,
        customer_user: CustomerUser,
        mode: str,
        relationship: str,
        household_name: str,
        child_name: str,
        timezone: str,
        child_phone_number: str | None,
    ) -> tuple[Household, ChildProfile]:
        if mode not in {"for_myself", "for_someone_else"}:
            raise ValueError("Invalid onboarding mode")
        if not household_name.strip():
            raise ValueError("Household name is required")
        if not child_name.strip():
            raise ValueError("Child/profile name is required")

        household = await session.scalar(select(Household).where(Household.account_id == customer_user.account_id))
        if household is None:
            household = Household(
                account_id=customer_user.account_id,
                name=household_name.strip(),
                timezone=timezone.strip() or "America/New_York",
                is_self_managed=mode == "for_myself",
            )
            session.add(household)
            await session.flush()

        role = HouseholdRole.owner if mode == "for_myself" else HouseholdRole.guardian
        assignment = await session.scalar(
            select(RoleAssignment).where(
                RoleAssignment.customer_user_id == customer_user.id,
                RoleAssignment.household_id == household.id,
            )
        )
        if assignment is None:
            session.add(
                RoleAssignment(
                    account_id=customer_user.account_id,
                    household_id=household.id,
                    customer_user_id=customer_user.id,
                    role=role,
                )
            )

        companion_user = None
        normalized_phone = (child_phone_number or customer_user.phone_number or "").strip()
        if normalized_phone:
            companion_user = await session.scalar(select(User).where(User.phone_number == normalized_phone))
            if companion_user is None:
                companion_user = User(
                    display_name=child_name.strip(),
                    phone_number=normalized_phone,
                    timezone=timezone.strip() or "America/New_York",
                )
                session.add(companion_user)
                await session.flush()

        child_profile = await session.scalar(
            select(ChildProfile)
            .where(ChildProfile.account_id == customer_user.account_id)
            .order_by(desc(ChildProfile.created_at))
        )
        if child_profile is None:
            child_profile = ChildProfile(
                account_id=customer_user.account_id,
                household_id=household.id,
                companion_user_id=companion_user.id if companion_user else None,
                first_name=child_name.strip(),
                display_name=child_name.strip(),
                preferences_json={"onboarding_mode": mode},
            )
            session.add(child_profile)
        else:
            child_profile.household_id = household.id
            child_profile.first_name = child_name.strip()
            child_profile.display_name = child_name.strip()
            child_profile.companion_user_id = companion_user.id if companion_user else child_profile.companion_user_id

        customer_user.relationship_label = relationship
        await session.flush()
        return household, child_profile

    async def current_verification_case(self, session: AsyncSession, *, customer_user: CustomerUser) -> VerificationCase | None:
        return await session.scalar(
            select(VerificationCase)
            .where(VerificationCase.customer_user_id == customer_user.id)
            .order_by(desc(VerificationCase.created_at))
        )

    async def active_sessions(self, session: AsyncSession, *, customer_user: CustomerUser) -> list[PortalSession]:
        stmt = (
            select(PortalSession)
            .where(
                PortalSession.customer_user_id == customer_user.id,
                PortalSession.revoked_at.is_(None),
                PortalSession.expires_at > utc_now(),
            )
            .order_by(desc(PortalSession.updated_at))
            .limit(20)
        )
        return list((await session.execute(stmt)).scalars().all())

    async def verification_queue_counts(self, session: AsyncSession) -> dict[str, int]:
        values = {}
        for status in VerificationCaseStatus:
            values[status.value] = int(
                await session.scalar(select(func.count()).select_from(VerificationCase).where(VerificationCase.status == status)) or 0
            )
        return values
