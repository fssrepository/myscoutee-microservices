#!/usr/bin/env python3
"""Read-only container CPU/quota snapshot; run as root inside the owned guest."""
import argparse
import json
from pathlib import Path
import subprocess
import time


def cri(*args):
    return json.loads(subprocess.run(['k3s', 'crictl', *args], check=True,
        capture_output=True, text=True, timeout=10).stdout)


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--namespace', required=True)
args = parser.parse_args()

rows = []
for container in cri('ps', '-o', 'json')['containers']:
    labels = container.get('labels', {})
    if labels.get('io.kubernetes.pod.namespace') != args.namespace:
        continue
    info = cri('inspect', container['id'])['info']
    pid = info['pid']
    group = next(line.split(':', 2)[2] for line in Path(f'/proc/{pid}/cgroup').read_text().splitlines()
                 if line.startswith('0::'))
    root = Path('/sys/fs/cgroup') / group.lstrip('/')
    stat = dict(line.split() for line in (root / 'cpu.stat').read_text().splitlines())
    rows.append({'pod': labels.get('io.kubernetes.pod.name'), 'container': container['metadata']['name'],
        'id': container['id'], 'at': time.time(), 'cpu': {k: int(v) for k, v in stat.items()},
        'cpuMax': (root / 'cpu.max').read_text().strip(),
        'memoryBytes': int((root / 'memory.current').read_text())})
print(json.dumps({'at': time.time(), 'containers': rows}))
