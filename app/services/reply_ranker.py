from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.models.communication import Message
from app.models.enums import Direction
from app.utils.text import normalize_text, similarity_score


@dataclass(slots=True)
class RankedReply:
    text: str
    score: float
    reasons: list[str]


class ReplyRankerService:
    def rank(
        self,
        *,
        candidates: list[str],
        inbound_text: str,
        recent_messages: list[Message],
        classification: dict[str, Any],
    ) -> list[RankedReply]:
        ranked: list[RankedReply] = []
        prior_outbound = [msg.body or "" for msg in recent_messages if msg.direction == Direction.outbound and msg.body]
        direct_question = bool(classification.get("direct_question"))
        inbound_norm = normalize_text(inbound_text)
        for text in candidates:
            score = 0.0
            reasons: list[str] = []
            candidate_norm = normalize_text(text)
            if direct_question and "?" in inbound_text:
                if _has_any_overlap(inbound_norm, candidate_norm):
                    score += 0.5
                    reasons.append("direct_answer_overlap")
                if len(text.split()) >= 4:
                    score += 0.15
                    reasons.append("non_trivial_answer")
            if any(sig in candidate_norm for sig in ["i can", "i think", "sounds", "yeah", "totally", "got you"]):
                score += 0.15
                reasons.append("casual_natural_phrase")

            if len(text) <= 480:
                score += 0.1
                reasons.append("within_length")

            if prior_outbound:
                worst_similarity = max(similarity_score(prior, text) for prior in prior_outbound)
                if worst_similarity > 0.92:
                    score -= 0.6
                    reasons.append("high_repetition_penalty")
                elif worst_similarity < 0.65:
                    score += 0.2
                    reasons.append("novelty_bonus")

            if text.count("?") > 1:
                score -= 0.1
                reasons.append("too_many_questions_penalty")

            ranked.append(RankedReply(text=text, score=score, reasons=reasons))

        ranked.sort(key=lambda item: item.score, reverse=True)
        return ranked


def _has_any_overlap(inbound: str, outbound: str) -> bool:
    inbound_tokens = [token for token in inbound.split() if len(token) >= 4]
    if not inbound_tokens:
        return False
    return any(token in outbound for token in inbound_tokens[:8])
