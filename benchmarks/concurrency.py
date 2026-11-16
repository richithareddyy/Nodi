"""Local ledger concurrency: shared budgets, approval races and duplicate requests.

No model calls or external effects. Connection setup is included in operation time;
mission/setup/teardown are excluded. Reports contain no DSN or bearer credentials.
"""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import math
import os
from pathlib import Path
import platform
import subprocess
import tempfile
import threading
import time
from urllib.parse import urlsplit,urlunsplit,parse_qsl,urlencode
from uuid import uuid4
from nodi.gateway import Gateway
from nodi.models import Contract,Action
from nodi.queries import ReadModel
from benchmarks.metrics import metadata


def distribution(values):
    values=sorted(values)
    def percentile(q): return values[math.ceil(len(values)*q)-1] if values else None
    return {'n':len(values),'p50_ms':percentile(.5),'p95_ms':percentile(.95),'p99_ms':percentile(.99),
            'mean_ms':sum(values)/len(values) if values else None}


def scoped(dsn,schema):
    parsed=urlsplit(dsn)
    return urlunsplit(parsed._replace(query=urlencode([(k,v) for k,v in parse_qsl(parsed.query) if k!='nodi_schema']+[('nodi_schema',schema)])))


async def trial(backend,workers,workload,dsn=None):
    if backend not in ('sqlite','postgres') or workers not in (10,50,100) or workload not in ('shared_budget','approval_race','duplicates'):
        raise ValueError('invalid benchmark dimensions')
    with tempfile.TemporaryDirectory(prefix='nodi-load-') as directory:
        schema='nodi_b'+uuid4().hex
        database=scoped(dsn,schema) if backend=='postgres' else directory+'/ledger.db'
        gateway=Gateway(database)
        try:
            now=int(time.time())
            contract=Contract('root','Buy monitors within the reviewed budget.',spending_limit=90000,
                              maximum_transaction=30000,approval_threshold=25000,max_actions=200,delegation_depth=1,expires_at=now+3600)
            root=gateway.issue(contract)['visa_id']
            work=[]
            if workload=='shared_budget':
                for i in range(workers):
                    child=gateway.delegate('root',root,replace(contract,agent_id=f'agent-{i}',delegation_depth=0))['visa_id']
                    work.append((f'agent-{i}',Action(f'buy-{i}',child,'purchase','vendor:approved',{'product_id':'monitor-basic','quantity':1})))
            else:
                action=Action('same-request',root,'purchase','vendor:approved',{'product_id':'monitor-pro' if workload=='approval_race' else 'monitor-basic','quantity':1})
                work=[('root',action)]*workers
                if workload=='approval_race':
                    assert gateway.execute('root',action)['decision']=='ASK_HUMAN'
                    fingerprint=ReadModel(gateway).request_status('root','same-request')['fingerprint']
            barrier=threading.Barrier(workers)
            def execute(item):
                barrier.wait(timeout=30)
                start=time.perf_counter()
                try:
                    approved=False
                    if workload=='approval_race':
                        try:
                            gateway.approve('root','same-request',True,fingerprint)
                            approved=True
                        except ValueError: pass  # A competing exact approval already won.
                    result=gateway.execute(*item)
                    return {'latency_ms':(time.perf_counter()-start)*1000,'decision':result['decision'],
                            'replayed':result.get('replayed',False),'approval_won':approved,'error':None}
                except Exception as error:
                    return {'latency_ms':(time.perf_counter()-start)*1000,'error':type(error).__name__}
            loop=asyncio.get_running_loop()
            with ThreadPoolExecutor(max_workers=workers) as pool:
                start=time.perf_counter()
                rows=await asyncio.gather(*(loop.run_in_executor(pool,execute,item) for item in work))
                seconds=time.perf_counter()-start
            snapshot=gateway.snapshot()
            root_state=next(v for v in snapshot['visas'] if v['id']==root)
            expected=5 if workload=='shared_budget' else 1
            expected_spend=90000 if workload=='shared_budget' else 27500 if workload=='approval_race' else 18000
            valid=(len(snapshot['effects'])==expected and root_state['spent']==expected_spend and gateway.verify_audit())
            errors=sum(r['error'] is not None for r in rows)
            return {'backend':backend,'workers':workers,'workload':workload,'elapsed_seconds':seconds,
                    'completed_operations':len(rows)-errors,'errors':errors,'operations_per_second':len(rows)/seconds,
                    'successful_operations_per_second':(len(rows)-errors)/seconds,'latency':distribution([r['latency_ms'] for r in rows]),
                    'decisions':{d:sum(r.get('decision')==d for r in rows) for d in ('ALLOW','DENY','ASK_HUMAN')},
                    'approval_winners':sum(r.get('approval_won',False) for r in rows),
                    'committed_effects':len(snapshot['effects']),'root_spent_cents':root_state['spent'],
                    'invariants_passed':valid,'samples':rows,'status':'complete' if valid and not errors else 'failed'}
        finally:
            if backend=='postgres':
                import psycopg
                with psycopg.connect(dsn) as db:
                    db.execute(psycopg.sql.SQL('DROP SCHEMA {} CASCADE').format(psycopg.sql.Identifier(schema)))


async def run(args):
    dsn=os.environ.get('NODI_BENCH_POSTGRES_DSN')
    if 'postgres' in args.backends and not dsn: raise ValueError('set NODI_BENCH_POSTGRES_DSN for a dedicated test database')
    report={**metadata(),'postgres_pool_max_size':64,'cpu_count':os.cpu_count(),'machine':platform.machine(),'repeats':args.repeats,
            'server_environment':os.environ.get('NODI_BENCH_SERVER_NOTE','Not recorded; Docker info describes the client host only.'),
            'method':'Barrier-synchronized bursts using asyncio and bounded thread pools; connection/approval costs included; no retries, models or external effects.',
            'limitations':'Small local bursts, not sustained production load. Global audit lock serializes writes. Percentiles include errors; samples and errors retained. No latency confidence interval claimed.',
            'trials':[]}
    report.pop('label_provenance', None)  # This experiment measures operations, not labeled semantic cases.
    if dsn and 'postgres' in args.backends:
        import psycopg
        with psycopg.connect(dsn) as db: report['postgres_version']=db.execute('SHOW server_version').fetchone()[0]
        report['driver_version']=psycopg.__version__
    try:
        result=subprocess.run(['docker','info','--format','{{json .}}'],capture_output=True,text=True,timeout=10)
        info=json.loads(result.stdout)
        report['docker_resources']={k:info.get(k) for k in ('NCPU','MemTotal','Architecture','ServerVersion')}
    except (OSError,subprocess.TimeoutExpired,ValueError): report['docker_resources']=None
    for repeat in range(args.repeats):
        # Reverse backend order on alternate repetitions to reduce order bias.
        for backend in args.backends[::(-1 if repeat%2 else 1)]:
            for workers in args.workers:
                for workload in ('shared_budget','approval_race','duplicates'):
                    row=await trial(backend,workers,workload,dsn)
                    report['trials'].append({**row,'repeat':repeat+1})
    report['status']='complete' if all(r['status']=='complete' for r in report['trials']) else 'failed'
    with args.output.open('x') as stream: json.dump(report,stream,indent=2);stream.write('\n')
    print(json.dumps({'status':report['status'],'trials':len(report['trials']),'output':str(args.output)}))
    if report['status']!='complete': raise SystemExit(1)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backends',nargs='+',choices=['sqlite','postgres'],default=['sqlite','postgres'])
    parser.add_argument('--workers',nargs='+',type=int,choices=[10,50,100],default=[10,50,100])
    parser.add_argument('--repeats',type=int,choices=range(1,11),default=3)
    parser.add_argument('--output',type=Path,required=True)
    asyncio.run(run(parser.parse_args()))

if __name__=='__main__': main()
