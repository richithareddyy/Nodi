"""Shared domain result types; preparing an action never executes it."""
from dataclasses import asdict, dataclass
import math
from typing import Protocol
from .models import Action, Contract, Decision

@dataclass(frozen=True)
class Prepared:
    resources: frozenset[str]
    cost: int
    category: str
    impact: str
    result: dict


@dataclass(frozen=True)
class Intent:
    mission_alignment_score: float
    risk_score: float
    reason: str
    evidence: list[str]
    recommended_decision: Decision

    def validate(self, *, reason_limit=4096, evidence_limit=40) -> None:
        if type(self.recommended_decision) is not str or self.recommended_decision not in ("ALLOW", "DENY", "ASK_HUMAN"):
            raise ValueError("invalid intent decision")
        for score in (self.mission_alignment_score, self.risk_score):
            if type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 1:
                raise ValueError("invalid intent score")
        if type(self.reason) is not str or not self.reason.strip() or len(self.reason) > reason_limit:
            raise ValueError("invalid intent reason")
        if type(self.evidence) is not list or len(self.evidence) > evidence_limit or any(
            type(item) is not str or not item.strip() or len(item) > 500 for item in self.evidence
        ):
            raise ValueError("invalid intent evidence")

    def dictionary(self) -> dict:
        return asdict(self)


class Evaluator(Protocol):
    def evaluate(self, contract: Contract, action: Action, prepared: Prepared) -> Intent: ...
