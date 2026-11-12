"""Trusted plugins declare closed schemas, metadata and an intent baseline."""
from typing import Protocol
from ..domain_types import Evaluator, Prepared
from ..models import Action, Contract


class Domain(Protocol):
    name: str
    purpose: str
    tools: frozenset[str]
    resources: frozenset[str]
    tool_schemas: dict[str, tuple[set[str], set[str]]]
    trusted_metadata: dict
    aliases: dict[str, str]
    evaluator: Evaluator
    dataset: str

    def prepare(self, action: Action) -> Prepared: ...
    def default_contract(self, agent_id: str, now: int) -> Contract: ...
    def setup_action(self, visa_id: str) -> Action: ...
