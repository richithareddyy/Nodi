"""Measure transaction acquisition delay with slow/failing in-process evaluators."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
from pathlib import Path
import tempfile
import threading
import time
from nodi.gateway import Gateway
from nodi.intent import DomainEvaluator
from nodi.domains import get_domain
from nodi.models import Action
from benchmarks.metrics import metadata, timing


class MeasuredGateway(Gateway):
    def __init__(self,*args,**kwargs):
        self.waits=[]
        self.capture=False
        super().__init__(*args,**kwargs)

    @contextmanager
    def transaction(self):
        start=time.perf_counter()
        with super().transaction() as db:
            if self.capture: self.waits.append((time.perf_counter()-start)*1000)
            yield db


class DelayedEvaluator:
    def __init__(self,delay,fail): self.delay,self.fail=delay,fail
    def evaluate(self,*args):
        time.sleep(self.delay)
        if self.fail: raise TimeoutError('controlled evaluator failure')
        return DomainEvaluator().evaluate(*args)


def measure(workers=8,delay=.05):
    if not 2<=workers<=32 or not 0<=delay<=.5: raise ValueError('bounded workers and delay required')
    report={**metadata(),'workers':workers,'configured_delay_ms':delay*1000,
            'method':'Time from transaction entry through BEGIN IMMEDIATE; includes file/connect overhead. One barrier-synchronized batch per condition.',
            'conditions':{}}
    with tempfile.TemporaryDirectory(prefix='nodi-lock-') as directory:
        for mode,seconds,fail in [('fast',0,False),('slow',delay,False),('failing',delay,True)]:
            gateway=MeasuredGateway(f'{directory}/{mode}.db',evaluator=DelayedEvaluator(seconds,fail))
            visa=gateway.issue(get_domain('procurement').default_contract('agent',int(time.time())))['visa_id']
            barrier=threading.Barrier(workers)
            def work(i):
                barrier.wait(timeout=10)
                start=time.perf_counter()
                result=gateway.execute('agent',Action(f'call-{i}',visa,'search_products','catalog:monitors',{'query':'monitor'}))
                return result['decision'],(time.perf_counter()-start)*1000
            gateway.capture=True
            start=time.perf_counter()
            with ThreadPoolExecutor(max_workers=workers) as pool: rows=list(pool.map(work,range(workers)))
            elapsed=(time.perf_counter()-start)*1000
            gateway.capture=False
            state=gateway.snapshot()
            assert state['visas'][0]['spent']==0 and state['visas'][0]['actions']<=workers
            report['conditions'][mode]={'writer_acquisition':timing(gateway.waits),'end_to_end':timing([r[1] for r in rows]),
                                         'batch_ms':elapsed,'decisions':{d:sum(r[0]==d for r in rows) for d in ('ALLOW','DENY','ASK_HUMAN')},
                                         'audit_valid':gateway.verify_audit()}
    return report

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('--output',type=Path,required=True)
    a=p.parse_args()
    with a.output.open('x') as stream: json.dump(measure(),stream,indent=2); stream.write('\n')
