"""Count-based intervals and descriptive timing summaries for local experiments."""
import hashlib
import math
import platform
import subprocess
from datetime import UTC, datetime
from pathlib import Path

PROVENANCE = {'label_source': 'machine-authored, specification-derived cases',
              'independently_validated': False,
              'limitation': 'Labels are test expectations, not independently validated ground truth.'}


def rate(successes, total):
    if not 0 <= successes <= total:
        raise ValueError('invalid counts')
    if not total:
        return {'numerator': successes, 'denominator': total, 'rate': None, 'wilson95': None}
    z = 1.959963984540054
    p = successes / total
    center = (p + z*z/(2*total)) / (1+z*z/total)
    half = z * math.sqrt(p*(1-p)/total + z*z/(4*total*total)) / (1+z*z/total)
    return {'numerator': successes, 'denominator': total, 'rate': p,
            'wilson95': [max(0, center-half), min(1, center+half)]}


def timing(values):
    values = sorted(values)
    return {'n': len(values), 'mean_ms': sum(values)/len(values) if values else None,
            'p50_ms': values[math.ceil(len(values)*.5)-1] if values else None,
            'p95_ms': values[math.ceil(len(values)*.95)-1] if values else None}


def metadata():
    def git(*args):
        return subprocess.check_output(['git', *args], text=True).strip()
    try:
        revision, dirty = git('rev-parse', 'HEAD'), bool(git('status', '--porcelain'))
    except (OSError, subprocess.CalledProcessError):
        revision, dirty = None, None
    return {'date_utc': datetime.now(UTC).isoformat(), 'git_commit': revision,
            'git_dirty': dirty, 'python': platform.python_version(), 'platform': platform.platform(),
            'label_provenance': PROVENANCE,
            'source_sha256': {str(p.relative_to(Path(__file__).resolve().parents[1])): hashlib.sha256(p.read_bytes()).hexdigest()
                              for directory in ('benchmarks','nodi')
                              for p in sorted((Path(__file__).resolve().parents[1]/directory).rglob('*.py'))}}
