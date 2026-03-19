from __future__ import annotations

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.communication import Conversation, Message
from app.models.enums import MessageStatus
from app.models.persona import Persona
from app.models.user import User
from app.utils.time import utc_now


class ConversationService:
    async def get_or_create_user_by_phone(self, session: AsyncSession, phone_number: str) -> User:
        stmt = select(User).where(User.phone_number == phone_number)
        user = (await session.execute(stmt)).scalar_one_or_none()
        if user:
            return user
        user = User(phone_number=phone_number)
        session.add(user)
        await session.flush()
        return user

    async def get_active_persona(self, session: AsyncSession, user: User | None = None) -> Persona | None:
        if user and user.preferred_persona_id:
            stmt = select(Persona).where(Persona.id == user.preferred_persona_id)
            persona = (await session.execute(stmt)).scalar_one_or_none()
            if persona:
                return persona
        stmt = select(Persona).where(Persona.is_active.is_(True)).order_by(desc(Persona.updated_at))
        return (await session.execute(stmt)).scalars().first()

    async def get_or_create_conversation(
        self,
        session: AsyncSession,
        *,
        user: User,
        persona: Persona | None,
    ) -> Conversation:
        stmt = (
            select(Conversation)
            .where(Conversation.user_id == user.id)
            .order_by(desc(Conversation.updated_at))
        )
        conversation = (await session.execute(stmt)).scalars().first()
        if conversation:
            if persona and conversation.persona_id != persona.id:
                conversation.persona_id = persona.id
            return conversation
        conversation = Conversation(user_id=user.id, persona_id=persona.id if persona else None)
        session.add(conversation)
        await session.flush()
        return conversation

    async def recent_messages(
        self,
        session: AsyncSession,
        *,
        conversation_id,
        limit: int,
    ) -> list[Message]:
        stmt = (
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(desc(Message.created_at))
            .limit(limit)
        )
        return list(reversed((await session.execute(stmt)).scalars().all()))

    def mark_inbound(self, user: User, conversation: Conversation) -> None:
        current = utc_now()
        user.last_inbound_at = current
        conversation.last_inbound_at = current

    def mark_outbound(self, user: User, conversation: Conversation) -> None:
        current = utc_now()
        user.last_outbound_at = current
        conversation.last_outbound_at = current
