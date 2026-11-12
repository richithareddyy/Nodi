"""Strict JSON policy compilation to ordinary server-stored contracts."""
import argparse
from dataclasses import replace
from decimal import Decimal, localcontext
from fnmatch import fnmatchcase
import json
import re
import sys
import time

from .domains import get_domain
from .json_input import validate_json
from .models import integer, text

MAX_BYTES = 65536


def fields(value, required, optional=frozenset()):
    if type(value) is not dict or not required <= value.keys() or not value.keys() <= required | optional:
        raise ValueError("missing or unknown policy fields")
    return value


def names(value, name):
    if type(value) is not list or len(value) > 100 or any(type(v) is not str or not v or len(v) > 200 for v in value):
        raise ValueError(f"{name} must be a bounded list of names")
    if len(set(value)) != len(value):
        raise ValueError(f"duplicate entries in {name}")
    return value


def cents(value):
    if type(value) not in (int, Decimal):
        raise ValueError("money must be a JSON number, not a string or boolean")
    amount = Decimal(value)
    if not amount.is_finite() or not 0 <= amount <= 1_000_000_000:
        raise ValueError("money must be between zero and one billion USD")
    with localcontext() as context:
        context.prec = 32
        rounded = amount.quantize(Decimal("0.01"))
        if rounded != amount:
            raise ValueError("money must be an exact whole number of cents")
        return int(rounded * 100)


def duration(value):
    if type(value) is not str or not re.fullmatch(r"[1-9][0-9]{0,6}[smhd]", value):
        raise ValueError("expires_in must be a positive integer followed by s, m, h or d")
    seconds = int(value[:-1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[value[-1]]
    if seconds > 7 * 86400:
        raise ValueError("policy expiry cannot exceed seven days")
    return seconds


def compile_policy(value, *, now=None, agent_id=None):
    fields(value, {"mission", "domain", "allow", "deny", "budget", "delegation", "approval", "expires_in"},
           {"agent_id", "resources", "max_actions"})
    if type(value["domain"]) is not str:
        raise ValueError("domain must be a registered domain name")
    domain = get_domain(value["domain"])
    if value["domain"] != domain.name:
        raise ValueError("use the domain name, not its internal purpose")
    now = int(time.time()) if now is None else now
    integer(now, "now")
    identity = agent_id if agent_id is not None else value.get("agent_id", domain.name)
    text(identity, "agent_id", 100)
    text(value["mission"], "mission")
    allowed = names(value["allow"], "allow")
    accepted = domain.tools | domain.aliases.keys()
    if not set(allowed) <= accepted:
        raise ValueError("allow contains unknown tools; use exact domain tool IDs or documented aliases")
    denied = names(value["deny"], "deny")
    if any(not re.fullmatch(r"[A-Za-z0-9_.?*\[\]!\-]+", pattern) for pattern in denied):
        raise ValueError("deny patterns must contain only tool-name characters and glob syntax")
    resolved = lambda name: domain.aliases.get(name, name)
    blocked = {resolved(name) for name in accepted if any(fnmatchcase(name, pattern) for pattern in denied)}
    baseline = domain.default_contract(identity, now)
    blocked.update(baseline.forbidden_operations)
    effective = sorted({resolved(name) for name in allowed} - blocked)
    resources = value.get("resources")
    if "resources" not in value:
        resource_allow, resource_deny = baseline.allowed_resources, baseline.forbidden_resources
    else:
        fields(resources, {"allow", "deny"})
        resource_allow = names(resources["allow"], "resources.allow")
        resource_deny = names(resources["deny"], "resources.deny")
        if not set(resource_allow + resource_deny) <= domain.resources:
            raise ValueError("resources must use exact registered domain resource IDs")
        resource_deny = sorted(set(resource_deny) | set(baseline.forbidden_resources))
    budget = fields(value["budget"], {"max_usd", "max_transaction_usd"})
    approval = fields(value["approval"], {"purchase_above_usd"})
    delegation = fields(value["delegation"], {"max_depth"})
    max_actions = value.get("max_actions", baseline.max_actions)
    contract = replace(baseline, mission=value["mission"], allowed_tools=effective,
                       forbidden_operations=sorted(blocked),
                       allowed_resources=sorted(set(resource_allow) - set(resource_deny)),
                       forbidden_resources=sorted(resource_deny),
                       spending_limit=cents(budget["max_usd"]),
                       maximum_transaction=cents(budget["max_transaction_usd"]),
                       approval_threshold=cents(approval["purchase_above_usd"]),
                       delegation_depth=delegation["max_depth"], max_actions=max_actions,
                       expires_at=now + duration(value["expires_in"]))
    return {"domain": domain.name, "contract": contract.dictionary(), "mode": "simulation",
            "effective_tools": effective, "blocked_tools": sorted(blocked),
            "unmatched_deny_patterns": [pattern for pattern in denied
                                        if not any(fnmatchcase(name, pattern) for name in accepted)],
            "resources_defaulted": "resources" not in value,
            "aliases_used": {name: resolved(name) for name in allowed if name in domain.aliases},
            "tool_arguments": {name: {"required": sorted(domain.tool_schemas[name][0]),
                                      "optional": sorted(domain.tool_schemas[name][1])} for name in effective},
            "scope": "compilation only; an administrator must review and issue this contract"}


def load_policy(path, **options):
    with open(path, "rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("policy exceeds 64 KiB")
    validate_json(raw)
    return compile_policy(json.loads(raw.decode("utf-8"), parse_float=Decimal), **options)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m nodi policy", description=__doc__)
    parser.add_argument("operation", choices=["validate", "explain"])
    parser.add_argument("file")
    parser.add_argument("--agent", help="Identity for the compiled contract; defaults to policy agent_id or domain")
    args = parser.parse_args(argv)
    try:
        result = load_policy(args.file, agent_id=args.agent)
    except (ValueError, TypeError, OSError, RecursionError) as exc:
        print(json.dumps({"valid": False, "error": str(exc)}, ensure_ascii=True), file=sys.stderr)
        raise SystemExit(2) from None
    print(json.dumps({"valid": True, **(result if args.operation == "explain" else {"domain": result["domain"]})}, indent=2))
