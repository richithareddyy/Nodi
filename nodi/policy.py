"""Pure deterministic checks, evaluated for the entire delegation chain."""
from .models import Action, Contract
from .domain_types import Prepared


def checks(contract: Contract, status: str, spent: int, actions: int,
           action: Action, prepared: Prepared, now: int) -> list[dict]:
    conditions = {
        "active": status == "active",
        "unexpired": now < contract.expires_at,
        "tool_allowed": action.tool in contract.allowed_tools,
        "operation_allowed": action.tool not in contract.forbidden_operations,
        "resources_allowed": prepared.resources <= set(contract.allowed_resources),
        "resources_not_forbidden": not prepared.resources.intersection(contract.forbidden_resources),
        "transaction_limit": prepared.cost <= contract.maximum_transaction,
        "cumulative_budget": spent + prepared.cost <= contract.spending_limit,
        "action_limit": actions < contract.max_actions,
    }
    return [{"check": name, "passed": passed} for name, passed in conditions.items()]


def attenuation(parent: Contract, child: Contract) -> None:
    if parent.delegation_depth < 1 or child.delegation_depth >= parent.delegation_depth:
        raise ValueError("delegation depth must decrease")
    if child.mission != parent.mission or child.purpose != parent.purpose:
        raise ValueError("delegation must preserve the authorized mission and purpose")
    for name in ("allowed_tools", "allowed_resources"):
        if not set(getattr(child, name)) <= set(getattr(parent, name)):
            raise ValueError(f"delegation widens {name}")
    for name in ("forbidden_resources", "forbidden_operations"):
        if not set(getattr(parent, name)) <= set(getattr(child, name)):
            raise ValueError(f"delegation weakens {name}")
    for name in ("spending_limit", "maximum_transaction", "approval_threshold", "expires_at", "max_actions"):
        if getattr(child, name) > getattr(parent, name):
            raise ValueError(f"delegation widens {name}")
