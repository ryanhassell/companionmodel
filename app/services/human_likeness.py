from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.communication import Conversation, Message
from app.models.enums import Direction
from app.models.persona import Persona
from app.models.user import User
from app.services.candidate_reply import CandidateReplyService
from app.services.reply_ranker import ReplyRankerService
from app.services.turn_classifier import TurnClassifierService
from app.utils.text import normalize_text, similarity_score, truncate_text
from app.utils.time import utc_now


@dataclass(slots=True)
class ReplayTurn:
    inbound: str
    baseline: str
    candidate: str
    actual: str


class HumanLikenessService:
    def __init__(
        self,
        turn_classifier_service: TurnClassifierService,
        candidate_reply_service: CandidateReplyService,
        reply_ranker_service: ReplyRankerService,
    ) -> None:
        self.turn_classifier_service = turn_classifier_service
        self.candidate_reply_service = candidate_reply_service
        self.reply_ranker_service = reply_ranker_service

    async def scoreboard_metrics(
        self,
        session: AsyncSession,
        *,
        user: User,
        lookback: int = 120,
    ) -> dict[str, Any]:
        rows = list(
            (
                await session.execute(
                    select(Message)
                    .where(Message.user_id == user.id)
                    .order_by(Message.created_at.desc())
                    .limit(lookback)
                )
            ).scalars().all()
        )
        return self._metrics_from_rows(list(reversed(rows)))

    async def daily_score_series(
        self,
        session: AsyncSession,
        *,
        user: User,
        days: int = 30,
    ) -> list[dict[str, Any]]:
        days = max(1, min(days, 120))
        start = utc_now() - timedelta(days=days)
        rows = list(
            (
                await session.execute(
                    select(Message)
                    .where(Message.user_id == user.id, Message.created_at >= start)
                    .order_by(Message.created_at.asc())
                )
            ).scalars().all()
        )
        by_day: dict[str, list[Message]] = {}
        for row in rows:
            if row.created_at is None:
                continue
            key = row.created_at.date().isoformat()
            by_day.setdefault(key, []).append(row)
        series: list[dict[str, Any]] = []
        for offset in range(days):
            day = (start.date() + timedelta(days=offset + 1)).isoformat()
            metrics = self._metrics_from_rows(by_day.get(day, []))
            series.append(
                {
                    "date": day,
                    "score": metrics["score"],
                    "repetition_rate": metrics["repetition_rate"],
                    "lexical_variety": metrics["lexical_variety"],
                    "outbound_count": metrics["outbound_count"],
                }
            )
        return series

    def _metrics_from_rows(self, rows: list[Message]) -> dict[str, Any]:
        outbound = [item.body or "" for item in rows if item.direction == Direction.outbound and item.body]
        inbound = [item.body or "" for item in rows if item.direction == Direction.inbound and item.body]
        if not outbound:
            return {
                "outbound_count": 0,
                "repetition_rate": 0.0,
                "lexical_variety": 0.0,
                "avg_length": 0.0,
                "unanswered_question_rate": 0.0,
                "safety_rewrite_rate": 0.0,
                "score": 0.0,
            }

        repeated = 0
        for idx in range(1, len(outbound)):
            if similarity_score(outbound[idx - 1], outbound[idx]) > 0.9:
                repeated += 1

        tokens = [token for text in outbound for token in normalize_text(text).split() if token]
        unique_tokens = len(set(tokens))
        lexical_variety = unique_tokens / max(len(tokens), 1)
        avg_length = sum(len(text) for text in outbound) / max(len(outbound), 1)

        unanswered = 0
        answered_pool = iter(outbound)
        for text in inbound:
            if "?" not in text:
                continue
            reply = next(answered_pool, "")
            if similarity_score(text, reply) < 0.12:
                unanswered += 1
        question_count = sum(1 for text in inbound if "?" in text)

        safety_rewrite_count = sum(
            1
            for item in rows
            if isinstance(item.metadata_json, dict)
            and isinstance(item.metadata_json.get("reply_pipeline"), dict)
            and isinstance(item.metadata_json["reply_pipeline"].get("safety_rewrite"), dict)
            and bool(item.metadata_json["reply_pipeline"]["safety_rewrite"].get("applied"))
        )

        repetition_rate = repeated / max(len(outbound), 1)
        unanswered_rate = unanswered / max(question_count, 1) if question_count else 0.0
        safety_rewrite_rate = safety_rewrite_count / max(len(outbound), 1)
        score = max(
            0.0,
            min(
                1.0,
                0.45
                + (lexical_variety * 0.8)
                - (repetition_rate * 0.55)
                - (unanswered_rate * 0.35)
                - (safety_rewrite_rate * 0.1),
            ),
        )
        return {
            "outbound_count": len(outbound),
            "repetition_rate": round(repetition_rate, 4),
            "lexical_variety": round(lexical_variety, 4),
            "avg_length": round(avg_length, 2),
            "unanswered_question_rate": round(unanswered_rate, 4),
            "safety_rewrite_rate": round(safety_rewrite_rate, 4),
            "score": round(score, 4),
        }

    async def run_ab_replay(
        self,
        session: AsyncSession,
        *,
        user: User,
        persona: Persona | None,
        config: dict[str, Any],
        max_turns: int = 20,
    ) -> dict[str, Any]:
        conversation_stmt = (
            select(Conversation)
            .where(Conversation.user_id == user.id)
            .order_by(Conversation.updated_at.desc())
            .limit(1)
        )
        if persona is not None:
            conversation_stmt = (
                select(Conversation)
                .where(Conversation.user_id == user.id, Conversation.persona_id == persona.id)
                .order_by(Conversation.updated_at.desc())
                .limit(1)
            )
        conversation = (await session.execute(conversation_stmt)).scalar_one_or_none()
        if conversation is None:
            return {"turns": 0, "replay": [], "summary": {"ab_win_rate": 0.0, "notes": "No conversation found"}}

        messages = list(
            (
                await session.execute(
                    select(Message)
                    .where(Message.conversation_id == conversation.id)
                    .order_by(Message.created_at.asc())
                    .limit(400)
                )
            ).scalars().all()
        )
        inbound_messages = [item for item in messages if item.direction == Direction.inbound and item.body][-max_turns:]
        if not inbound_messages:
            return {"turns": 0, "replay": [], "summary": {"ab_win_rate": 0.0, "notes": "No inbound turns"}}

        replay_rows: list[ReplayTurn] = []
        ab_wins = 0
        for inbound in inbound_messages:
            recent = [item for item in messages if item.created_at <= inbound.created_at][-18:]
            classification = await self.turn_classifier_service.classify(
                session,
                user=user,
                persona=persona,
                conversation=conversation,
                inbound_message=inbound,
                recent_messages=recent,
                config=config,
                conversation_state=None,
            )
            baseline = self._legacy_baseline_reply(inbound.body or "")
            candidates = await self.candidate_reply_service.generate_candidates(
                session,
                user=user,
                persona=persona,
                conversation=conversation,
                inbound_message=inbound,
                recent_messages=recent,
                memory_hits=[],
                config=config,
                conversation_state=None,
                classification=classification,
            )
            ranked = self.reply_ranker_service.rank(
                candidates=candidates or [baseline],
                inbound_text=inbound.body or "",
                recent_messages=recent,
                classification=classification,
            )
            candidate = ranked[0].text if ranked else baseline
            actual = self._actual_reply_for_inbound(messages, inbound)
            replay_rows.append(
                ReplayTurn(
                    inbound=truncate_text(inbound.body or "", 220),
                    baseline=truncate_text(baseline, 220),
                    candidate=truncate_text(candidate, 220),
                    actual=truncate_text(actual, 220),
                )
            )
            if self._is_candidate_better(inbound.body or "", baseline, candidate, recent):
                ab_wins += 1

        win_rate = ab_wins / max(len(replay_rows), 1)
        return {
            "turns": len(replay_rows),
            "replay": [
                {
                    "inbound": row.inbound,
                    "baseline": row.baseline,
                    "candidate": row.candidate,
                    "actual": row.actual,
                }
                for row in replay_rows
            ],
            "summary": {
                "ab_win_rate": round(win_rate, 4),
                "candidate_wins": ab_wins,
                "total": len(replay_rows),
            },
        }

    def _legacy_baseline_reply(self, inbound_text: str) -> str:
        text = (inbound_text or "").strip()
        if not text:
            return "I'm here with you."
        if "?" in text:
            return "I hear you. Let me answer that simply and then we can keep going."
        lowered = normalize_text(text)
        if any(token in lowered for token in ["sad", "anxious", "bad", "upset"]):
            return "That sounds really heavy. I'm here with you right now."
        return "Got you. Tell me a little more so I can respond better."

    def _actual_reply_for_inbound(self, messages: list[Message], inbound: Message) -> str:
        for item in messages:
            if item.created_at <= inbound.created_at:
                continue
            if item.direction == Direction.outbound and item.body:
                return item.body
        return ""

    def _is_candidate_better(
        self,
        inbound_text: str,
        baseline: str,
        candidate: str,
        recent_messages: list[Message],
    ) -> bool:
        baseline_rank = self.reply_ranker_service.rank(
            candidates=[baseline],
            inbound_text=inbound_text,
            recent_messages=recent_messages,
            classification={"direct_question": "?" in inbound_text},
        )[0].score
        candidate_rank = self.reply_ranker_service.rank(
            candidates=[candidate],
            inbound_text=inbound_text,
            recent_messages=recent_messages,
            classification={"direct_question": "?" in inbound_text},
        )[0].score
        return candidate_rank > baseline_rank
