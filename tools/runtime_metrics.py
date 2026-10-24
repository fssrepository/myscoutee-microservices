"""Existing host/guest resource counters and comparable interval calculations."""
import json
import os
from pathlib import Path
import re
import shlex
import time
import workloads as w


def snapshot():
    code = (w.vm.SUPPORT / 'infra/guest/resource-snapshot.py').read_text()
    result = json.loads(w.vm.ssh('sudo python3 -c ' + shlex.quote(code) + ' --namespace ' + shlex.quote(w.NAMESPACE)).stdout)
    counters = [int(value) for value in Path('/proc/stat').read_text().splitlines()[0].split()[1:9]]
    qemu = w.vm.virsh('domstats', w.vm.NAME, '--cpu-total').stdout
    result['host'] = {'at': time.monotonic(), 'cpuTotal': sum(counters),
                      'cpuIdle': counters[3] + counters[4], 'logicalCpus': os.cpu_count(),
                      'qemuCpuNs': int(re.search(r'cpu.time=(\d+)', qemu).group(1))}
    return result


def resource_deltas(before, after):
    old = {row['id']: row for row in before['containers']}
    deltas = []
    for row in after['containers']:
        previous = old.pop(row['id'], None)
        if previous is None:
            deltas.append({'pod': row['pod'], 'replacedDuringRun': True})
            continue
        seconds = row['at'] - previous['at']
        if seconds <= 0 or row['cpu']['usage_usec'] < previous['cpu']['usage_usec']:
            raise ValueError('Invalid CPU counter interval')
        deltas.append({'pod': row['pod'], 'container': row['container'], 'seconds': seconds,
            'cpuCores': (row['cpu']['usage_usec'] - previous['cpu']['usage_usec']) / 1e6 / seconds,
            'throttledSeconds': (row['cpu'].get('throttled_usec', 0) - previous['cpu'].get('throttled_usec', 0)) / 1e6,
            'cpuMax': row['cpuMax'], 'memoryMiB': row['memoryBytes'] / 1024 ** 2})
    deltas.extend({'pod': row['pod'], 'replacedDuringRun': True} for row in old.values())
    return deltas


def host_delta(before, after):
    old, new = before['host'], after['host']
    seconds = new['at'] - old['at']
    total = new['cpuTotal'] - old['cpuTotal']
    if seconds <= 0 or total <= 0:
        raise ValueError('Invalid host CPU interval')
    return {'seconds': seconds, 'logicalCpus': new['logicalCpus'],
            'busyPercent': 100 * (1 - (new['cpuIdle'] - old['cpuIdle']) / total),
            'qemuCpuCores': (new['qemuCpuNs'] - old['qemuCpuNs']) / 1e9 / seconds}
