"""Leased mock-payment worker and reconciler. Provider calls occur outside SQLite locks."""
import argparse
import json
import re
from pathlib import Path
from uuid import uuid4

from .domain_types import Intent
from .domains import get_domain
from .mock_payments import encoded, payload_hash
from .models import Action
from .outbox_store import payment_payload, reserved, settle, update_response
from .policy import checks


class Outbox:
    def __init__(self, gateway, *, lease_seconds=30, retry_delay=5, fault=None):
        if gateway.payment_provider is None:
            raise ValueError('mock payment provider is not enabled')
        if type(lease_seconds) is not int or lease_seconds < 1 or type(retry_delay) is not int or retry_delay < 1:
            raise ValueError('positive integer lease and retry delay required')
        self.gateway, self.provider = gateway, gateway.payment_provider
        self.lease_seconds, self.retry_delay = lease_seconds, retry_delay
        self.fault = fault or (lambda stage: None)

    def _authorize(self, db, row):
        from .capability_store import request_binding
        request_binding(self.gateway, db, row['principal'], row['request_id'])
        action = Action(**json.loads(row['action']))
        chain = self.gateway._chain(db, row['visa_id'])
        now = int(self.gateway.clock())
        if chain[0][1].agent_id != row['principal'] or chain[0][1].purpose != 'mock_payment':
            raise ValueError('payment identity or domain changed')
        prepared = get_domain('payments').prepare(action)
        if encoded(payment_payload(action, prepared)) != row['payload'] or prepared.cost != row['amount']:
            raise ValueError('trusted payment metadata changed')
        for depth, (node, contract) in enumerate(chain):
            money, actions = reserved(db, node['id'], excluding=row['id'])
            if depth > contract.delegation_depth or any(not item['passed'] for item in
                    checks(contract, node['status'], node['spent'] + money, node['actions'] + actions, action, prepared, now)):
                raise ValueError('current ancestry policy denies payment')
        if now >= row['deadline']:
            raise ValueError('dispatch authority expired')
        # Revalidate intent as well as deterministic policy at dispatch/retry.
        intent = self.gateway.evaluator.evaluate(chain[0][1], action, prepared)
        if not isinstance(intent, Intent):
            raise ValueError('invalid dispatch intent')
        intent.validate()
        encoded(intent.dictionary())
        if intent.recommended_decision == 'DENY':
            raise ValueError('intent denies payment')
        review = (intent.recommended_decision == 'ASK_HUMAN' or intent.mission_alignment_score < .9
                  or intent.risk_score >= .7 or any(prepared.cost > c.approval_threshold for _, c in chain))
        if review and not row['approved']:
            raise ValueError('new exact-action review required; submit a new request')
        if int(self.gateway.clock()) >= min(row['deadline'], *(c.expires_at for _, c in chain)):
            raise ValueError('authority expired during dispatch review')
        return action, chain

    def _dispatch_audit(self, db, row):
        action, chain = self._authorize(db, row)
        self.gateway._audit(db, {'event': 'outbox_dispatch', 'outbox_id': row['id'],
                                'agent': row['principal'], 'action': action.dictionary(), 'decision': 'ALLOW',
                                'executed': False, 'delegation_chain': [n['id'] for n, _ in chain],
                                'authority_expires_at': row['deadline']})

    def _claim(self, outbox_id):
        with self.gateway.transaction() as db:
            row = db.execute('''SELECT * FROM outbox WHERE state IN ('PENDING','UNKNOWN') AND lease_until<=?
                AND (CAST(? AS TEXT) IS NULL OR id=?) ORDER BY updated_at,id LIMIT 1''',
                (int(self.gateway.clock()), outbox_id, outbox_id)).fetchone()
            if row is None:
                return None
            if row['provider_id'] != self.provider.identity:
                raise ValueError('restore the original provider store; identity mismatch')
            if row['state'] == 'PENDING':
                try:
                    self._dispatch_audit(db, row)
                except Exception:
                    settle(self.gateway, db, row, 'FAILED', reason='Current dispatch authorization failed')
                    return {'id': row['id'], 'terminal': True}
            token = uuid4().hex
            db.execute("UPDATE outbox SET state='UNKNOWN',lease=?,lease_until=?,attempts=attempts+1,updated_at=? WHERE id=?",
                       (token, int(self.gateway.clock()) + self.lease_seconds, int(self.gateway.clock()), row['id']))
            update_response(db, row, 'UNKNOWN', reason='Payment dispatched or being reconciled; outcome not yet confirmed')
            self.gateway._audit(db, {'event': 'outbox_claimed', 'outbox_id': row['id'], 'previous_state': row['state']})
            return {**dict(row), 'token': token}

    def _owns(self, db, work):
        row = db.execute('SELECT * FROM outbox WHERE id=?', (work['id'],)).fetchone()
        return row if row and row['state'] == 'UNKNOWN' and row['lease'] == work['token'] else None

    def _validate_result(self, work, result):
        if (type(result) is not dict or result.get('key') != work['id']
                or result.get('payload_hash') != payload_hash(json.loads(work['payload']))
                or result.get('status') not in ('ABSENT', 'COMMITTED', 'FAILED')):
            raise ValueError('unbound or invalid provider result')
        fields = {'status', 'key', 'payload_hash'}
        if result['status'] != 'ABSENT':
            fields |= {'charged_cents', 'receipt', 'mock'}
            if (result.get('mock') is not True or type(result.get('receipt')) is not str
                    or not re.fullmatch(r'[a-f0-9]{32}', result['receipt'])):
                raise ValueError('invalid mock provider receipt')
            expected = work['amount'] if result['status'] == 'COMMITTED' else 0
            if type(result.get('charged_cents')) is not int or result['charged_cents'] != expected:
                raise ValueError('invalid provider amount')
        if set(result) != fields:
            raise ValueError('unexpected provider response fields')
        encoded(result)
        return result['status']

    def run_once(self, outbox_id=None):
        work = self._claim(outbox_id)
        if work is None:
            return None
        if work.get('terminal'):
            return work['id']
        # BaseException fault hooks model abrupt process death, not a recoverable timeout.
        self.fault('after_claim')
        payload = json.loads(work['payload'])
        try:
            if work['state'] == 'UNKNOWN':
                result = self.provider.lookup(work['id'], payload)
                state = self._validate_result(work, result)
                if state == 'ABSENT':
                    with self.gateway.transaction() as db:
                        row = self._owns(db, work)
                        if row is None:
                            return work['id']
                        try:
                            self._dispatch_audit(db, row)
                            retry = True
                        except Exception:
                            retry = False
                    # A fenced cancellation, not ABSENT alone, permits releasing funds.
                    result = (self.provider.charge if retry else self.provider.cancel)(work['id'], payload)
            else:
                result = self.provider.charge(work['id'], payload)
            state = self._validate_result(work, result)
            if state == 'ABSENT':
                raise ValueError('charge/cancel has no terminal result')
            self.fault('after_provider')
            with self.gateway.transaction() as db:
                row = self._owns(db, work)
                if row is not None:
                    settle(self.gateway, db, row, state, result=result)
                    self.fault('before_commit')
        except Exception:
            # Never infer failure from a timeout, malformed response, or commit error.
            with self.gateway.transaction() as db:
                row = self._owns(db, work)
                if row is not None:
                    db.execute('UPDATE outbox SET lease=NULL,lease_until=?,updated_at=? WHERE id=?',
                               (int(self.gateway.clock()) + self.retry_delay, int(self.gateway.clock()), row['id']))
                    self.gateway._audit(db, {'event': 'outbox_unknown', 'outbox_id': row['id'],
                                            'reason': 'Provider or settlement outcome requires reconciliation'})
        return work['id']


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='.nodi.local.json')
    parser.add_argument('--limit', type=int, default=1, help='At most 1..100 ready deliveries/reconciliations')
    args = parser.parse_args(argv)
    if not 1 <= args.limit <= 100:
        parser.error('limit must be 1..100')
    from .gateway import Gateway
    from .mock_payments import from_config
    from .semantic import SemanticReviewer
    config = json.loads(Path(args.config).read_text())
    provider = from_config(config.get('mock_payments'))
    evaluator = SemanticReviewer(**config['semantic_service']) if config.get('semantic_service') else None
    worker = Outbox(Gateway(config['database'], evaluator=evaluator, payment_provider=provider))
    attempted = []
    for _ in range(args.limit):
        key = worker.run_once()
        if key is None:
            break
        attempted.append(key)
    print(json.dumps({'processed': attempted, 'scope': 'mock payments only; UNKNOWN reservations remain held'}))


if __name__ == '__main__':
    main()
