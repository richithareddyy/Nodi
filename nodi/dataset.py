"""Validate the entire evaluation dataset before creating any simulation state."""
import json
import hashlib
import re
from dataclasses import dataclass, replace
from .json_input import validate_json
from .models import Action
from .domains import get_domain

MAX_DATASET_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class Dataset:
    cases: list[dict]
    sha256: str


def load_dataset(source, domain="procurement"):
    with source.open('rb') as stream:
        raw = stream.read(MAX_DATASET_BYTES + 1)
    if len(raw) > MAX_DATASET_BYTES:
        raise ValueError('dataset exceeds 4 MiB')
    try:
        validate_json(raw)
        cases = json.loads(raw)
    except (ValueError, RecursionError):
        raise ValueError('dataset must contain unambiguous finite UTF-8 JSON') from None
    if type(cases) is not list or not 1 <= len(cases) <= 1000:
        raise ValueError('dataset must contain 1..1000 cases')
    seen = set()
    required = {'id', 'category', 'expected', 'dangerous'}
    plugin = get_domain(domain)
    contract = plugin.default_contract(plugin.name, 2000000000)
    for position, case in enumerate(cases, 1):
        try:
            if type(case) is not dict or not required <= case.keys():
                raise ValueError
            key = case['id']
            if type(key) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', key) or key in seen:
                raise ValueError
            seen.add(key)
            if type(case['category']) is not str or not case['category'].strip() or len(case['category']) > 100:
                raise ValueError
            if type(case['dangerous']) is not bool or case['expected'] not in ('ALLOW', 'DENY', 'ASK_HUMAN'):
                raise ValueError
            if case['dangerous'] and case['expected'] == 'ALLOW':
                raise ValueError
            if 'precondition' in case and case['precondition'] not in ('expired', 'revoked', 'budget_spent', 'action_limit'):
                raise ValueError
            if plugin.name != 'procurement' and case.get('precondition') == 'budget_spent':
                raise ValueError
            if key == 'setup' and case.get('precondition') in ('budget_spent', 'action_limit'):
                raise ValueError
            kind = case.get('kind', 'action')
            common = required | {'kind', 'precondition'}
            if kind == 'delegation':
                if not set(case) <= common | {'changes'} or type(case.get('changes')) is not dict:
                    raise ValueError
                replace(contract, **{'agent_id': 'child', 'delegation_depth': 0, **case['changes']})
            elif kind == 'action':
                if not {'tool', 'resource', 'arguments'} <= case.keys() or not set(case) <= common | {'tool', 'resource', 'arguments', 'context', 'principal'}:
                    raise ValueError
                principal = case.get('principal', 'procurement')
                if type(principal) is not str or not principal.strip() or len(principal) > 100:
                    raise ValueError
                Action(key, 'fixture-visa', case['tool'], case['resource'], case['arguments'], case.get('context', ''))
            else:
                raise ValueError
        except (ValueError, TypeError, KeyError):
            raise ValueError(f'invalid dataset case at position {position}; check fields, labels, ID and precondition') from None
    return Dataset(cases, hashlib.sha256(raw).hexdigest())
