"""Explicit opt-in domain for Docker reads; never silently simulated."""
from ..domain_types import Intent, Prepared
from ..models import Contract, Action
from ..execution.validation import SCHEMAS, RESOURCES, URLS, COMMANDS, validate


class ReadOnlyEvaluator:
    def evaluate(self, contract, action, prepared):
        # Reading arbitrary workspace content needs exact-action human consent.
        if action.tool in ('filesystem.read', 'sqlite.query'):
            return Intent(.5, .5, 'Review the exact real data read', ['read-only does not mean nonsensitive'], 'ASK_HUMAN')
        return Intent(.95, .1, 'Bounded documentation GET or fixed diagnostic', [], 'ALLOW')


class ReadOnlyDomain:
    name = 'sandbox'
    purpose = 'sandbox_readonly'
    tools = frozenset(SCHEMAS)
    resources = RESOURCES
    tool_schemas = SCHEMAS
    trusted_metadata = {'urls': URLS, 'commands': COMMANDS, 'workspace': 'operator-mounted research directory'}
    aliases = {}
    evaluator = ReadOnlyEvaluator()
    dataset = None  # Integration tests use real Docker; no fabricated fixture results.

    def default_contract(self, agent_id, now):
        return Contract(agent_id, 'Read approved research data and public documentation.', purpose=self.purpose,
                        allowed_tools=sorted(self.tools), allowed_resources=sorted(self.resources),
                        forbidden_operations=[], forbidden_resources=[], spending_limit=0,
                        maximum_transaction=0, approval_threshold=0, expires_at=now + 3600)

    def prepare(self, action):
        resource = validate(action.tool, action.arguments)
        if action.resource != resource:
            raise ValueError('claimed resource does not match sandbox destination')
        return Prepared(frozenset({resource}), 0, 'sandbox_readonly', 'read', {})

    def setup_action(self, visa_id):
        return Action('setup', visa_id, 'shell.read', 'runtime:info', {'command': 'system'})
