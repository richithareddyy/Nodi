"""Offline delegation with shared ledger limits; all effects are simulated."""
import json
import secrets
import tempfile
from dataclasses import replace

from .capabilities import CapabilityError, Keys, attenuate, describe
from .domains import get_domain
from .gateway import Gateway
from .models import Action


def run():
    now=2000000000
    with tempfile.TemporaryDirectory(prefix='nodi-capabilities-') as directory:
        gateway=Gateway(directory+'/ledger.db',clock=lambda:now,capability_keys=Keys({'demo':secrets.token_bytes(32)}))
        contract=get_domain('procurement').default_contract('owner',now)
        visa=gateway.issue(contract)['visa_id']
        root=gateway.issue_capability(visa,'demo')['token']
        first=attenuate(root,replace(contract,agent_id='helper-one',delegation_depth=0))
        second=attenuate(root,replace(contract,agent_id='helper-two',delegation_depth=0))
        offline_nodes=len(gateway.snapshot()['visas'])
        def purchase(token):
            return gateway.execute_capability(token,Action('buy',describe(token)['visa_id'],'purchase','vendor:approved',{'product_id':'monitor-basic','quantity':1}))
        accepted=purchase(first)
        denied=purchase(second)
        gateway.revoke(visa)
        try:
            purchase(first)
            revoked=False
        except CapabilityError:
            revoked=True
        state=gateway.snapshot()
        return {'scope':'local simulation; no real purchases','nodes_before_first_use':offline_nodes,
                'first_decision':accepted['decision'],'sibling_decision':denied['decision'],
                'committed_effects':len(state['effects']),
                'root_spent_cents':next(v['spent'] for v in state['visas'] if v['id']==visa),
                'revoked_token_rejected':revoked,'audit_valid':gateway.verify_audit()}


if __name__=='__main__': print(json.dumps(run(),indent=2))
