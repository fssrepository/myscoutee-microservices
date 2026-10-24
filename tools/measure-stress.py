#!/usr/bin/env python3
"""Run an unchanged repository stress scenario with guest cgroup evidence."""

import argparse
import datetime
import json
import os
from pathlib import Path
import subprocess
import workloads as w
from runtime_metrics import snapshot, resource_deltas, host_delta








def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('scenario', choices=json.loads((w.SHELL / 'checks/stress.json').read_text())['scenarios'])
    args = parser.parse_args()
    if not w.namespace_owned():
        raise RuntimeError('Owned running lab required')
    root = w.BUILD / 'reports/comparison'
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    before = snapshot()
    result = subprocess.run(['node', str(w.BACKEND / 'tests/e2e/stress/backend/run.mjs'),
        '--scenario', args.scenario, '--record', '--artifacts-dir', str(root)],
        env={**os.environ, 'E2E_STRESS_BASE_URL': 'http://localhost:' + str(w.CONFIG['localPort']),
             'E2E_NATIVE_DEMO_SESSIONS': 'true', 'E2E_SKIP_E2E_RESET': 'true'})
    after = snapshot()
    deltas = resource_deltas(before, after)
    report = {'scenario': args.scenario, 'exitCode': result.returncode, 'before': before, 'after': after,
              'deltas': deltas, 'host': host_delta(before, after)}
    output = root / (args.scenario + '-resources-' + stamp + '.json')
    output.write_text(json.dumps(report, indent=2) + '\n')
    print('Guest CPU/quota evidence: ' + str(output))
    print(json.dumps(deltas, indent=2))
    return result.returncode


if __name__ == '__main__':
    raise SystemExit(main())
