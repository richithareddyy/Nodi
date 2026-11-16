"""Explicit mock-payment domain; existing procurement visas remain simulations."""
from ..models import Contract
from .procurement.metadata import CATALOG, SCHEMAS
from .procurement.tools import prepare
from .procurement.intent import ProcurementEvaluator


class PaymentDomain:
    name = 'payments'
    purpose = 'mock_payment'
    tools = frozenset({'purchase'})
    resources = frozenset({'vendor:approved'})
    tool_schemas = {'purchase': SCHEMAS['purchase']}
    trusted_metadata = {'catalog': CATALOG, 'merchant': 'approved.example'}
    aliases = {}
    evaluator = ProcurementEvaluator()
    dataset = None

    def default_contract(self, agent_id, now):
        return Contract(agent_id, 'Buy a monitor under $300.', purpose=self.purpose,
                        allowed_tools=['purchase'], allowed_resources=['vendor:approved'],
                        forbidden_operations=[], forbidden_resources=[], expires_at=now + 3600)

    def prepare(self, action):
        if action.tool != 'purchase':
            raise ValueError('mock payments support only purchase')
        return prepare(action)

    def setup_action(self, visa_id):
        raise ValueError('mock payments use outbox fault tests, not simulated evaluation setup')
