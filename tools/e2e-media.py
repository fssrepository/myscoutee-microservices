#!/usr/bin/env python3
"""Remove only a UUID-owned profile-test media prefix from the owned KVM lab."""

import argparse
import json
import re
import workloads as w


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--owner', required=True)
    args = parser.parse_args()
    config = json.loads((w.SHELL / 'checks/media-fixture.json').read_text())
    if not re.fullmatch(config['ownerPattern'], args.owner):
        parser.error('Expected the profile fixture UUID owner.')
    if not w.namespace_owned():
        raise RuntimeError('Owned lab namespace absent; no media was removed.')
    w.kube('-n', w.NAMESPACE, 'exec', 'deployment/minio', '--', 'sh', '-c',
           'mc rm --recursive --force "${MINIO_SEED_ALIAS:-myscoutee}/${MEDIA_BUCKET}/public/demo/images/$1/profile/" >/dev/null',
           'cleanup-owned-profile', args.owner)


if __name__ == '__main__':
    main()
