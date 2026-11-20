"""Render measured load results with counts and descriptive trial-bootstrap intervals."""
import argparse
import json
import random
from pathlib import Path

from .concurrency import distribution


def summarize(report):
    result=[]
    for backend in ('sqlite','postgres'):
        for workers in (10,50,100):
            for workload in ('shared_budget','approval_race','duplicates'):
                trials=[r for r in report['trials'] if (r['backend'],r['workers'],r['workload'])==(backend,workers,workload)]
                if not trials: continue
                values=[r['successful_operations_per_second'] for r in trials]
                rng=random.Random(17)
                samples=sorted(sum(rng.choices(values,k=len(values)))/len(values) for _ in range(2000))
                result.append({'backend':backend,'workers':workers,'workload':workload,'trials':len(trials),
                               'n':sum(len(r['samples']) for r in trials),'errors':sum(r['errors'] for r in trials),
                               'ops_per_second':sum(values)/len(values),'bootstrap95':[samples[49],samples[1949]],
                               'latency':distribution([s['latency_ms'] for r in trials for s in r['samples']])})
    return result


def render(report):
    lines=['# Local concurrency measurements','',
           'Measured ledger operations; no real purchases or model calls. Source/version/hardware details and raw observations are in the accompanying JSON.', '',
           '| Backend | Workers | Workload | Trials / operations | Errors | Mean ops/s [descriptive 95% interval] | p50 / p95 / p99 ms |',
           '| --- | ---: | --- | ---: | ---: | --- | --- |']
    for row in summarize(report):
        lo,hi=row['bootstrap95'];lat=row['latency']
        lines.append(f"| {row['backend']} | {row['workers']} | {row['workload']} | {row['trials']} / {row['n']} | {row['errors']} | {row['ops_per_second']:.1f} [{lo:.1f}, {hi:.1f}] | {lat['p50_ms']:.1f} / {lat['p95_ms']:.1f} / {lat['p99_ms']:.1f} |")
    lines += ['', 'Intervals resample entire trials (2,000 deterministic bootstrap draws). With only a few local trials they are descriptive, not reliable population or production bounds. Latency uses all recorded operations, including errors. No percentile confidence intervals are claimed.', '',
              'SQLite runs on the client filesystem; PostgreSQL may run in Docker or on another host. Consult server_environment in the JSON: these are deployment-path comparisons, not isolated database-engine rankings. PostgreSQL uses a bounded pool and retains a global write/audit-order lock. Setup and teardown are excluded; connection checkout, approvals and database work are included. Throughput counts completed gateway operations including denials and replays, not new purchases. HTTP/MCP transport is excluded. These synchronized bursts do not establish sustained throughput, horizontal scalability or third-party effect reliability.']
    return '\n'.join(lines)+'\n'


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report',type=Path);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    with args.output.open('x') as stream: stream.write(render(json.loads(args.report.read_text())))
