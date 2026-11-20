"""Offline gateway decision evaluation; no live semantic model is measured."""
import argparse
import hashlib
import json
import tempfile
import time
from pathlib import Path

from benchmarks.metrics import metadata, rate, timing
from nodi.domains import get_domain
from nodi.gateway import Gateway
from nodi.models import Action


def run(dataset, output):
    raw=dataset.read_bytes()
    data=json.loads(raw)
    results=[]
    with tempfile.TemporaryDirectory(prefix='nodi-purpose-') as directory:
        for i,case in enumerate(data['cases']):
            gateway=Gateway(f'{directory}/{i}.db')
            contract=get_domain(case['domain']).default_contract('agent',int(time.time()))
            visa=gateway.issue(contract)['visa_id']
            start=time.perf_counter()
            result=gateway.execute('agent',Action('case',visa,**case['action']))
            results.append({**case,'actual':result['decision'],'latency_ms':(time.perf_counter()-start)*1000})
    groups={}
    for split in ('dev','heldout'):
        groups[split]={}
        for category in ('all','ordinary','ambiguous','adversarial'):
            rows=[r for r in results if r['split']==split and (category=='all' or r['category']==category)]
            nonallow=[r for r in rows if r['expected']!='ALLOW']
            allowed=[r for r in rows if r['expected']=='ALLOW']
            groups[split][category]={
                'n':len(rows),'false_allow':rate(sum(r['actual']=='ALLOW' for r in nonallow),len(nonallow)),
                'false_denial':rate(sum(r['actual']=='DENY' for r in allowed),len(allowed)),
                'ordinary_review':rate(sum(r['actual']=='ASK_HUMAN' for r in allowed),len(allowed)),
                'review_rate':rate(sum(r['actual']=='ASK_HUMAN' for r in rows),len(rows)),
                'exact_agreement':rate(sum(r['actual']==r['expected'] for r in rows),len(rows)),
                'latency':timing([r['latency_ms'] for r in rows])}
    report={**metadata(),'dataset_sha256':hashlib.sha256(raw).hexdigest(),'label_provenance':data['provenance'],
            'evaluator':'deterministic domain baseline; no remote model','groups':groups,'cases':results}
    with output.open('x') as stream: json.dump(report,stream,indent=2); stream.write('\n')
    return report

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--dataset',type=Path,default=Path(__file__).parent/'cases.json')
    p.add_argument('--output',type=Path,required=True)
    a=p.parse_args(); run(a.dataset,a.output)
