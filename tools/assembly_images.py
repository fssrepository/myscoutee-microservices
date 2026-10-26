#!/usr/bin/env python3
"""Build only domain-group images whose actual Gradle/image inputs changed."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess

INPUT_LABEL = 'com.myscoutee.assembly-inputs'


def run(*args, capture=False, check=True):
    return subprocess.run([str(value) for value in args], check=check, text=True,
                          capture_output=capture)


def fingerprint(manifest, runtime_id, hashes=None):
    """Paths/mtimes/checkout revisions are not content; dependency bytes are."""
    hashes = {} if hashes is None else hashes
    files = {}
    for name, location in sorted(manifest['files'].items()):
        path = Path(location)
        if path not in hashes:
            digest = hashlib.sha256()
            with path.open('rb') as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    digest.update(chunk)
            hashes[path] = digest.hexdigest()
        files[name] = hashes[path]
    content = {'schemaVersion': 1, 'service': manifest['service'],
               'properties': manifest['properties'], 'files': files, 'runtime': runtime_id}
    return hashlib.sha256(json.dumps(content, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def inspect(reference):
    result = run('docker', 'image', 'inspect', reference, capture=True, check=False)
    if result.returncode:
        if 'No such image' in result.stderr or 'No such object' in result.stderr:
            return None
        raise RuntimeError('Cannot inspect image: ' + reference)
    return json.loads(result.stdout)[0]


def reuse(reference, registry_reference, service, key):
    info = inspect(reference)
    if info is None and registry_reference:
        result = run('docker', 'pull', registry_reference, capture=True, check=False)
        if result.returncode:
            # An absent cache is buildable; outages/auth failures are not a cache miss.
            if not any(message in result.stderr.lower() for message in ('manifest unknown', 'manifest not found')):
                raise RuntimeError('Cannot read assembly cache from registry: ' + registry_reference)
            return None
        info = inspect(registry_reference)
    if info is None:
        return None
    labels = info['Config'].get('Labels') or {}
    if labels.get(INPUT_LABEL) != key or labels.get('com.myscoutee.assembly') != service:
        raise RuntimeError('Assembly cache identity mismatch: ' + service)
    run('docker', 'tag', info['Id'], reference)
    return info['Id']


def main():
    from runtime_config import BACKEND, BUILD, SHELL, SUPPORT, STATE, CONFIG
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--services', help='Optional comma-separated domain-group names')
    args = parser.parse_args()
    os.umask(0o077)
    assemblies = json.loads((SHELL / 'assemblies.json').read_text())['services']
    if args.services:
        selected = set(args.services.split(','))
        if not selected <= set(assemblies):
            parser.error('Unknown assembly selection')
        assemblies = {name: item for name, item in assemblies.items() if name in selected}
    images = CONFIG['images']
    prefix, tag = images['prefix'], images['buildTag']
    registry = os.environ.get('MYSCOUTEE_ASSEMBLY_CACHE_REGISTRY', '').rstrip('/')
    run(SUPPORT / 'gradlew', '--no-daemon', *[':' + name + ':writeAssemblyInputs' for name in assemblies])
    run('python3', SUPPORT / 'tools/render-deployment.py', BACKEND)
    context = BUILD / 'runtime-context'
    context.mkdir(parents=True, exist_ok=True)
    STATE.mkdir(parents=True, exist_ok=True)
    runtimes = {}
    for stage in sorted({item['runtimeStage'] for item in assemblies.values()}):
        reference = prefix + stage + ':' + tag
        run('docker', 'build', '--target', stage,
            '--file', BACKEND / 'server/docker/images/profilemanagement/prod/Dockerfile',
            '--tag', reference, context)
        runtimes[stage] = inspect(reference)['Id']
        with (STATE / 'images.tsv').open('a') as inventory:
            inventory.write(reference + '\t' + runtimes[stage] + '\n')
    hashes, planned = {}, {}
    for name, assembly in assemblies.items():
        manifest = json.loads((BUILD / name / 'assembly-inputs.json').read_text())
        key = fingerprint(manifest, runtimes[assembly['runtimeStage']], hashes)
        reference = prefix + name + ':inputs-' + key
        remote = registry + '/kvm/' + name + ':inputs-' + key if registry else ''
        image_id = reuse(reference, remote, name, key)
        planned[name] = {'inputFingerprint': key, 'cacheReference': reference,
                         'imageId': image_id, 'action': 'reuse' if image_id else 'build'}
        print(name + ': ' + planned[name]['action'] + ' (inputs ' + key[:12] + ')', flush=True)
    changed = [name for name, item in planned.items() if item['action'] == 'build']
    if changed:
        run(SUPPORT / 'gradlew', '--no-daemon', '--parallel',
            *['-PassemblyInputFingerprint.' + name + '=' + planned[name]['inputFingerprint'] for name in changed],
            *[':' + name + ':bootJar' for name in changed])
        run('python3', SUPPORT / 'tools/audit-assemblies.py', '--artifacts', '--services', ','.join(changed))
    revision = run('git', '-C', BACKEND, 'rev-parse', 'HEAD', capture=True).stdout.strip()
    dirty = bool(run('git', '-C', BACKEND, 'status', '--porcelain', '--untracked-files=normal', capture=True).stdout)
    for name, item in planned.items():
        if item['action'] == 'build':
            run('docker', 'build', '--file', BUILD / 'deployment' / (name + '.Dockerfile'),
                '--build-arg', 'BACKEND_REVISION=' + revision,
                '--build-arg', 'BACKEND_DIRTY=' + str(dirty).lower(),
                '--label', INPUT_LABEL + '=' + item['inputFingerprint'],
                '--tag', item['cacheReference'], BUILD / name / 'libs')
            item['imageId'] = inspect(item['cacheReference'])['Id']
        reference = prefix + name + ':' + tag
        run('docker', 'tag', item['imageId'], reference)
        with (STATE / 'images.tsv').open('a') as inventory:
            inventory.write(reference + '\t' + item['imageId'] + '\n')
            inventory.write(item['cacheReference'] + '\t' + item['imageId'] + '\n')
    (BUILD / 'assembly-image-plan.json').write_text(json.dumps(planned, indent=2) + '\n')
    print(f'Assembly images: {len(changed)} built, {len(planned) - len(changed)} reused.', flush=True)


if __name__ == '__main__':
    main()
