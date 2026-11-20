"""Reservation and settlement operations inside the gateway's existing transaction."""
import json
from uuid import uuid4

from .mock_payments import encoded, payload_hash

SCHEMA = '''
CREATE TABLE IF NOT EXISTS outbox (
    id TEXT PRIMARY KEY, principal TEXT NOT NULL, request_id TEXT NOT NULL,
    visa_id TEXT NOT NULL REFERENCES visas(id), provider_id TEXT NOT NULL,
    action TEXT NOT NULL, payload TEXT NOT NULL, amount INTEGER NOT NULL CHECK(amount>0),
    state TEXT NOT NULL CHECK(state IN ('PENDING','UNKNOWN','COMMITTED','FAILED')),
    approved INTEGER NOT NULL, deadline INTEGER NOT NULL, lease TEXT,
    lease_until INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL, UNIQUE(principal, request_id));
CREATE TABLE IF NOT EXISTS reservations (
    outbox_id TEXT NOT NULL REFERENCES outbox(id), visa_id TEXT NOT NULL REFERENCES visas(id),
    amount INTEGER NOT NULL CHECK(amount>0), PRIMARY KEY(outbox_id,visa_id));
CREATE INDEX IF NOT EXISTS reservations_visa ON reservations(visa_id);
CREATE INDEX IF NOT EXISTS outbox_work ON outbox(state,lease_until,updated_at);
'''


def reserved(db, visa_id, excluding=None):
    row = db.execute('SELECT COALESCE(SUM(amount),0),COUNT(*) FROM reservations WHERE visa_id=? AND outbox_id != ?',
                     (visa_id, excluding or '')).fetchone()
    return row[0], row[1]


def payment_payload(action, prepared):
    payload = {'amount_cents': prepared.cost, 'merchant': 'approved.example', **action.arguments}
    payload_hash(payload)
    return payload


def enqueue(gateway, db, principal, action, prepared, chain, approved, deadline, response):
    key = uuid4().hex
    payload = payment_payload(action, prepared)
    db.execute('''INSERT INTO outbox(id,principal,request_id,visa_id,provider_id,action,payload,amount,
                  state,approved,deadline,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
               (key, principal, action.request_id, action.visa_id, gateway.payment_provider.identity,
                encoded(action.dictionary()), encoded(payload), prepared.cost, 'PENDING', int(approved),
                deadline if approved else min(c.expires_at for _, c in chain), int(gateway.clock())))
    db.executemany('INSERT INTO reservations VALUES(?,?,?)', [(key, row['id'], prepared.cost) for row, _ in chain])
    response.update(executed=False, execution_state='PENDING', outbox_id=key,
                    reason='Authorized and reserved; payment has not been confirmed')


def update_response(db, row, state, **changes):
    request = db.execute('SELECT response FROM requests WHERE principal=? AND id=?',
                         (row['principal'], row['request_id'])).fetchone()
    response = json.loads(request[0])
    response.update(execution_state=state, executed=state == 'COMMITTED', **changes)
    db.execute('UPDATE requests SET response=? WHERE principal=? AND id=?',
               (encoded(response), row['principal'], row['request_id']))
    return response


def settle(gateway, db, row, state, result=None, reason=''):
    if row['state'] in ('COMMITTED', 'FAILED'):
        return
    allocations = list(db.execute('SELECT * FROM reservations WHERE outbox_id=?', (row['id'],)))
    if not allocations:
        raise ValueError('missing outbox reservations')
    changes = {'reason': reason or ('Mock payment confirmed' if state == 'COMMITTED' else 'Payment failed or cancelled')}
    if state == 'COMMITTED':
        effect_id = uuid4().hex
        action = json.loads(row['action'])
        effect = {'simulation': False, 'mock': True, 'backend': 'mock-payment-outbox', **result}
        db.execute('INSERT INTO effects VALUES(?,?,?,?,?)', (effect_id, row['principal'], row['request_id'], action['tool'], encoded(effect)))
        for reservation in allocations:
            db.execute('UPDATE visas SET spent=spent+?, actions=actions+1 WHERE id=?', (reservation['amount'], reservation['visa_id']))
        changes.update(effect_id=effect_id, result=effect)
    db.execute('DELETE FROM reservations WHERE outbox_id=?', (row['id'],))
    db.execute('UPDATE outbox SET state=?,lease=NULL,lease_until=0,updated_at=? WHERE id=?', (state, int(gateway.clock()), row['id']))
    response = update_response(db, row, state, **changes)
    chain = gateway._chain(db, row['visa_id'])
    remaining = min(c.spending_limit - node['spent'] - reserved(db, node['id'])[0] for node, c in chain)
    response['remaining_budget_cents'] = max(0, remaining)
    db.execute('UPDATE requests SET response=? WHERE principal=? AND id=?', (encoded(response), row['principal'], row['request_id']))
    gateway._audit(db, {'event': 'outbox_settled', 'outbox_id': row['id'], 'agent': row['principal'],
                       'action': json.loads(row['action']), 'delegation_chain': [a['visa_id'] for a in allocations], **response})


def cancel_pending(gateway, db, visa_id):
    rows = list(db.execute('''WITH RECURSIVE subtree(id) AS (
        SELECT ? UNION SELECT v.id FROM visas v JOIN subtree s ON v.parent=s.id)
        SELECT * FROM outbox WHERE state='PENDING' AND visa_id IN (SELECT id FROM subtree)''', (visa_id,)))
    for row in rows:
        settle(gateway, db, row, 'FAILED', reason='Queued payment cancelled by visa edit or revocation')
    return len(rows)
