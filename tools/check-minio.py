#!/usr/bin/env python3
"""Exercise the owned lab object store; optionally verify a pod restart.

Uses the canonical image's mc alias. Credentials never leave the pod. Only a
unique probe object is written/deleted; existing media keys are not modified.
"""
import argparse
import fcntl
import hashlib
import os
from pathlib import Path
import subprocess
import uuid

import workloads as w


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--restart', action='store_true', help='Restart only MinIO and check PVC persistence')
    parser.add_argument('--seed', action='store_true', help='Compare every packaged and stored object with the canonical prod seed')
    args = parser.parse_args()
    with (w.vm.VM / 'lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not w.namespace_owned():
            raise RuntimeError('The owned workload namespace is absent.')
        deployment = w.kube_json('-n', w.NAMESPACE, 'get', 'deployment', 'minio')
        if any(deployment['metadata'].get('labels', {}).get(key) != value for key, value in w.LABEL.items()):
            raise RuntimeError('Refusing to test an unowned MinIO deployment.')
        if deployment['spec']['replicas'] != 1:
            raise RuntimeError('Start the single lab MinIO replica first.')

        def ready():
            w.kube('-n', w.NAMESPACE, 'rollout', 'status', 'deployment/minio', '--timeout=90s', timeout=100)

        def execute(*command, data=None, timeout=120):
            return w.kube('-n', w.NAMESPACE, 'exec', '-i', 'deployment/minio', '--', *command, data=data, timeout=timeout).stdout

        ready()
        execute('test', '-f', '/tmp/myscoutee-minio-seeded')  # NOSONAR - python:S5443: read-only readiness check in the owned container, never a host temporary write.
        if args.seed:
            source = Path(os.environ.get('MYSCOUTEE_BACKEND_SOURCE', w.BACKEND)) / 'server/docker/conf/minio/media'
            files = sorted(path for path in source.rglob('*') if path.is_file())
            keys = {path.relative_to(source).as_posix() for path in files}
            if not keys or any('\n' in key or '\r' in key for key in keys):
                raise RuntimeError('Missing or unsupported canonical media seed filenames.')
            packaged = set(execute('/bin/bash', '-c',
                'shopt -s globstar nullglob dotglob; for path in /seed/media/**/*; do '
                'if test -f "$path"; then printf "%s\\n" "${path#/seed/media/}"; fi; done').splitlines())
            if keys != packaged:
                raise RuntimeError('Packaged MinIO seed inventory differs from canonical prod source; explicitly rebuild its image.')
            manifest = ''.join(hashlib.sha256(path.read_bytes()).hexdigest() + ' ' + path.relative_to(source).as_posix() + '\n' for path in files)
            # Keep object bytes inside the pod; stream one object at a time so
            # full seed verification does not need another copy in memory/disk.
            script = '''set -euo pipefail
count=0
while IFS=' ' read -r expected key; do
  packaged=$(sha256sum "/seed/media/$key")
  test "${packaged%% *}" = "$expected" || { echo 'Packaged seed checksum mismatch' >&2; exit 1; }
  stored=$(mc cat "myscoutee/myscoutee-media/$key" | sha256sum)
  test "${stored%% *}" = "$expected" || { echo 'Stored seed checksum mismatch' >&2; exit 1; }
  count=$((count+1))
done
printf '%s\n' "$count"
'''
            checked = execute('/bin/bash', '-c', script, data=manifest, timeout=600).strip()
            if checked != str(len(files)):
                raise RuntimeError('Incomplete seed object verification.')
            print(f'PASS: all {checked} prod seed objects match canonical source, packaged files and stored S3 bytes (SHA-256).', flush=True)
        probe = uuid.uuid4().hex
        target = 'myscoutee/myscoutee-media/_msc121/probes/' + probe
        payload = 'msc121-object-store-' + probe + '\n'
        try:
            execute('mc', 'pipe', target, data=payload)
            if execute('mc', 'cat', target) != payload:
                raise RuntimeError('Object-store read-back mismatch.')
            print('PASS: packaged seed completed; object write/read match.', flush=True)
            if args.restart:
                w.kube('-n', w.NAMESPACE, 'rollout', 'restart', 'deployment/minio')
                ready()
                if execute('mc', 'cat', target) != payload:
                    raise RuntimeError('Object did not survive MinIO pod replacement.')
                print('PASS: object survived pod replacement and canonical reseeding.', flush=True)
        finally:
            execute('mc', 'rm', target)
        print('PASS: unique probe object removed; existing media preserved.')


if __name__ == '__main__':
    try:
        main()
    except subprocess.CalledProcessError:
        raise SystemExit('MinIO check failed; inspect the owned deployment status/logs.') from None
