#!/usr/bin/env python3
"""Run existing E2E Mongo fixtures only in the owned KVM lab's DEMO database."""

import argparse
import importlib
import json
import subprocess
import sys

import workloads as w


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', required=True)
    args = parser.parse_args()
    if args.database != w.CONFIG['databases']['demo']:
        parser.error('KVM fixtures may target only the configured DEMO database.')
    script = sys.stdin.read(1024 * 1024 + 1)
    if not script.strip() or len(script) > 1024 * 1024:
        raise ValueError('Expected a bounded fixture script on stdin.')
    if not w.namespace_owned():
        raise RuntimeError('Owned lab namespace absent; no fixture was executed.')
    seed = importlib.import_module('seed_mongo')
    result = seed.mongo('db = db.getSiblingDB(' + json.dumps(args.database) + ');\n' + script)
    print(json.dumps(result))


if __name__ == '__main__':
    try:
        main()
    except subprocess.CalledProcessError as error:
        if not json.loads((w.SHELL / 'seed.json').read_text())['fixtureErrorStderr']:
            raise
        # Report the Mongo/transport failure, not the full fixture command and data.
        raise SystemExit(error.stderr.strip() or f'KVM fixture failed (exit {error.returncode}).')
