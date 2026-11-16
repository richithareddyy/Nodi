"""Reproducible local lost-response recovery using two independent SQLite stores."""
import json
import tempfile
from .domains import get_domain
from .gateway import Gateway
from .models import Action
from .mock_payments import MockPaymentProvider
from .outbox import Outbox


def run():
    now = [2_000_000_000]
    with tempfile.TemporaryDirectory() as directory:
        provider = MockPaymentProvider(directory + '/provider.db')
        gateway = Gateway(directory + '/ledger.db', payment_provider=provider, clock=lambda: now[0])
        visa = gateway.issue(get_domain('payments').default_contract('buyer', now[0]))['visa_id']
        action = Action('monitor', visa, 'purchase', 'vendor:approved', {'product_id': 'monitor-basic', 'quantity': 1})
        states = [gateway.execute('buyer', action)['execution_state']]
        provider.set_mode('lose_response')
        Outbox(gateway).run_once()
        states.append(gateway.execute('buyer', action)['execution_state'])
        now[0] += 31
        Outbox(gateway).run_once()
        states.append(gateway.execute('buyer', action)['execution_state'])
        payments = provider.snapshot()
        snapshot = gateway.snapshot()
        assert states == ['PENDING', 'UNKNOWN', 'COMMITTED']
        assert len(payments['payments']) == 1 and not snapshot['reservations']
        return {'scope': 'one local mock payment with a lost response; no real money', 'states': states,
                'provider_charge_calls': sum(c['method'] == 'charge' for c in payments['calls']),
                'provider_lookup_calls': sum(c['method'] == 'lookup' for c in payments['calls']),
                'committed_mock_charges': sum(p['status'] == 'COMMITTED' for p in payments['payments']),
                'remaining_reservations': len(snapshot['reservations']), 'audit_valid': gateway.verify_audit()}


if __name__ == '__main__':
    print(json.dumps(run(), indent=2))
