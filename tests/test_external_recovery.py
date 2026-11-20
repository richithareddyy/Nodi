"""Actual subprocess death across an HTTP boundary with an independent durable store."""
import json
import os
import secrets
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from test_outbox import NOW, assert_conserved

from benchmarks.recovery.client import TestServiceProvider
from nodi.domains import get_domain
from nodi.gateway import Gateway
from nodi.mock_payments import MockPaymentProvider
from nodi.models import Action
from nodi.outbox import Outbox


@pytest.fixture
def external(tmp_path):
    children = []
    token = secrets.token_urlsafe(32)
    database = tmp_path/'external.db'
    def start(port=0, crash=False):
        ready = tmp_path/f'ready-{len(children)}.json'
        command = [sys.executable,'-m','benchmarks.recovery.service','--database',str(database),'--ready',str(ready),'--port',str(port)]
        if crash: command.append('--crash-after-charge')
        child = subprocess.Popen(command,env={**os.environ,'NODI_TEST_SERVICE_TOKEN':token},stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        children.append(child)
        deadline = time.monotonic()+10
        while not ready.exists() and child.poll() is None and time.monotonic()<deadline: time.sleep(.01)
        assert ready.exists(), 'test service failed to start'
        bound = json.loads(ready.read_text())['port']
        return TestServiceProvider(bound,token), child, bound
    yield start, database, token
    for child in children:
        if child.poll() is None: child.terminate()
        child.communicate(timeout=5)


def system(tmp_path, provider):
    now = [NOW]
    gateway = Gateway(str(tmp_path/'gateway.db'), payment_provider=provider,clock=lambda:now[0])
    contract = get_domain('payments').default_contract('buyer',NOW)
    visa = gateway.issue(contract)['visa_id']
    return gateway, now, Action('buy',visa,'purchase','vendor:approved',{'product_id':'monitor-basic','quantity':1})


@pytest.mark.parametrize('stage',['after_reserve','after_claim','after_provider','before_commit'])
def test_gateway_process_crash_over_http(tmp_path, external, stage):
    start, database, token = external
    provider, _, port = start()
    gateway, now, action = system(tmp_path,provider)
    code = '''
import json,os,sys
from nodi.gateway import Gateway
from nodi.models import Action
from nodi.outbox import Outbox
from benchmarks.recovery.client import TestServiceProvider
path,port,action,stage=sys.argv[1:]
g=Gateway(path,payment_provider=TestServiceProvider(int(port),os.environ['NODI_TEST_SERVICE_TOKEN']),clock=lambda:2000000000)
g.execute('buyer',Action(**json.loads(action)))
if stage=='after_reserve': os._exit(71)
def fault(current):
    if current==stage: os._exit(71)
Outbox(g,fault=fault).run_once()
'''
    child = subprocess.run([sys.executable,'-c',code,gateway.path,str(port),json.dumps(action.dictionary()),stage],
                           env={**os.environ,'NODI_TEST_SERVICE_TOKEN':token},capture_output=True,timeout=15)
    assert child.returncode==71,child.stderr.decode()
    observer = MockPaymentProvider(database)
    assert_conserved(gateway,observer)
    now[0]+=31
    Outbox(gateway).run_once()
    assert gateway.execute('buyer',action)['execution_state']=='COMMITTED'
    assert len(observer.snapshot()['payments'])==1
    assert sum(c['method']=='charge' for c in observer.snapshot()['calls'])==1
    assert_conserved(gateway,observer)


def test_service_crash_after_execution_before_acknowledgement(tmp_path, external):
    start, database, _ = external
    provider, child, port = start(crash=True)
    gateway, now, action = system(tmp_path,provider)
    gateway.execute('buyer',action)
    Outbox(gateway).run_once()
    assert child.wait(timeout=5)==73
    observer = MockPaymentProvider(database)
    assert len(observer.snapshot()['payments'])==1
    assert gateway.snapshot()['outbox'][0]['state']=='UNKNOWN'
    assert_conserved(gateway,observer)
    replacement, _, _ = start(port=port)
    assert replacement.identity==provider.identity
    now[0]+=31
    Outbox(gateway).run_once()
    assert gateway.snapshot()['outbox'][0]['state']=='COMMITTED'
    assert [c['method'] for c in observer.snapshot()['calls']]==['charge','lookup']
    assert_conserved(gateway,observer)


@pytest.mark.parametrize('lifecycle',['expired','revoked'])
def test_absent_retry_fenced_after_authority_ends(tmp_path, external, lifecycle):
    start, database, _ = external
    provider, _, _ = start()
    gateway, now, action = system(tmp_path,provider)
    gateway.execute('buyer',action)
    observer = MockPaymentProvider(database)
    observer.set_mode('timeout')
    Outbox(gateway).run_once()
    if lifecycle=='revoked': gateway.revoke(action.visa_id)
    else: now[0]+=86400
    observer.set_mode('success')
    now[0]+=31
    Outbox(gateway).run_once()
    assert gateway.snapshot()['outbox'][0]['state']=='FAILED'
    payment=observer.snapshot()['payments'][0]
    payload={'amount_cents':18000,'merchant':'approved.example','product_id':'monitor-basic','quantity':1}
    assert provider.charge(payment['key'],payload)['status']=='FAILED'
    assert_conserved(gateway,observer)


def test_concurrent_http_workers_and_budget_reservations(tmp_path, external):
    start,database,_=external
    provider,_,_=start()
    gateway,now,action=system(tmp_path,provider)
    with ThreadPoolExecutor(max_workers=12) as pool:
        results=list(pool.map(lambda i:gateway.execute('buyer',replace(action,request_id=f'buy-{i}')),range(24)))
    assert sum(r['decision']=='ALLOW' for r in results)==1
    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(lambda _:Outbox(gateway).run_once(),range(24)))
    observer=MockPaymentProvider(database)
    assert len(observer.snapshot()['payments'])==1
    for _ in range(10):
        now[0]+=31
        assert Outbox(gateway).run_once() is None
    assert_conserved(gateway,observer)


def test_expired_lease_duplicate_workers_use_one_external_effect(tmp_path, external):
    import threading
    start,database,_=external
    provider,_,_=start()
    gateway,now,action=system(tmp_path,provider)
    gateway.execute('buyer',action)
    entered,release=threading.Event(),threading.Event()
    original=provider.charge
    def delayed(key,payload):
        if threading.current_thread().name.startswith('stale'):
            entered.set()
            assert release.wait(5)
        return original(key,payload)
    provider.charge=delayed
    with ThreadPoolExecutor(max_workers=1,thread_name_prefix='stale') as pool:
        old=pool.submit(Outbox(gateway).run_once)
        assert entered.wait(5)
        now[0]+=31
        Outbox(gateway).run_once()
        release.set()
        old.result(timeout=5)
    observer=MockPaymentProvider(database)
    assert len(observer.snapshot()['payments'])==1
    assert sum(c['method']=='charge' for c in observer.snapshot()['calls'])==2
    assert_conserved(gateway,observer)


def test_executed_unknown_reconciles_after_revocation(tmp_path, external):
    start,database,_=external
    provider,_,_=start()
    gateway,now,action=system(tmp_path,provider)
    gateway.execute('buyer',action)
    observer=MockPaymentProvider(database)
    observer.set_mode('lose_response')
    Outbox(gateway).run_once()
    gateway.revoke(action.visa_id)
    assert gateway.snapshot()['reservations']
    now[0]+=31
    Outbox(gateway).run_once()
    assert gateway.snapshot()['outbox'][0]['state']=='COMMITTED'
    assert_conserved(gateway,observer)
