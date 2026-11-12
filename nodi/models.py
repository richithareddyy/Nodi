"""Strict contracts. Money is always integer USD cents; resources are exact IDs."""
from dataclasses import asdict, dataclass, field
import re
import time
from typing import Any, Literal

from .domains.procurement.metadata import TOOLS, RESOURCES

Decision = Literal["ALLOW", "DENY", "ASK_HUMAN"]


def integer(value: Any, name: str, minimum: int = 0) -> None:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")


def text(value: Any, name: str, maximum: int = 2000) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"invalid {name}")


@dataclass(frozen=True)
class Contract:
    agent_id: str
    mission: str
    purpose: str = "monitor_procurement"
    allowed_tools: list[str] = field(default_factory=lambda: ["search_products", "compare_products", "purchase", "send_email"])
    allowed_resources: list[str] = field(default_factory=lambda: ["catalog:monitors", "vendor:approved", "file:quote.pdf"])
    forbidden_resources: list[str] = field(default_factory=lambda: ["file:employees.csv", "storage:external"])
    forbidden_operations: list[str] = field(default_factory=lambda: ["upload_file"])
    spending_limit: int = 30000
    maximum_transaction: int = 30000
    approval_threshold: int = 25000
    max_actions: int = 30
    delegation_depth: int = 1
    expires_at: int = field(default_factory=lambda: int(time.time()) + 3600)

    def __post_init__(self) -> None:
        text(self.agent_id, "agent_id", 100)
        text(self.mission, "mission")
        from .domains import get_domain
        domain = get_domain(self.purpose)
        if self.purpose != domain.purpose:
            raise ValueError("contract purpose must be the domain's canonical purpose")
        for name in ("spending_limit", "maximum_transaction", "approval_threshold", "max_actions", "delegation_depth", "expires_at"):
            integer(getattr(self, name), name)
        if self.delegation_depth > 8 or self.max_actions < 1:
            raise ValueError("invalid delegation depth or action count")
        for name, vocabulary in (("allowed_tools", domain.tools), ("forbidden_operations", domain.tools), ("allowed_resources", domain.resources), ("forbidden_resources", domain.resources)):
            values = getattr(self, name)
            if type(values) is not list or any(type(v) is not str for v in values) or not set(values) <= vocabulary or len(values) != len(set(values)):
                raise ValueError(f"invalid {name}")
        if self.maximum_transaction > self.spending_limit or self.approval_threshold > self.maximum_transaction:
            raise ValueError("threshold <= maximum transaction <= spending limit required")

    def dictionary(self) -> dict:
        return asdict(self)


def draft(mission: str, agent_id: str, now: int | None = None) -> Contract:
    """Compatibility entry point for the original monitor drafting grammar."""
    from .domains.procurement.contracts import draft as procurement_draft
    return procurement_draft(mission, agent_id, now)


@dataclass(frozen=True)
class Action:
    request_id: str
    visa_id: str
    tool: str
    resource: str
    arguments: dict
    context: str = ""

    def __post_init__(self) -> None:
        for name in ("request_id", "visa_id", "tool", "resource"):
            text(getattr(self, name), name, 150)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", self.request_id):
            raise ValueError("invalid request_id")
        if type(self.arguments) is not dict or not isinstance(self.context, str) or len(self.context) > 4000:
            raise ValueError("invalid arguments/context")

    def dictionary(self) -> dict:
        return asdict(self)
