#!/usr/bin/env python3
"""Owned two-member Mongo lab conversion, backup and idempotent initialization.

Both members vote. This demonstrates replication, not one-node-loss write
availability. Default application reads stay on the primary for consistency.
"""
import argparse
import datetime
import fcntl
import gzip
import hashlib
import json
import os
import shlex
import subprocess
import time
import uuid
import workloads as w

STATE = w.vm.VM / 'mongo-replicaset.json'
STATEFUL_STATE = w.vm.VM / 'mongo-statefulset.json'
DATABASES = w.CONFIG['databases']
MEMBERS = [name + '.' + w.NAMESPACE + '.svc.cluster.local:27017' for name in ('mongodb', 'mongodb-secondary')]


def workload():
    current = w.kube('-n', w.NAMESPACE, 'get', 'statefulset/mongodb', '--ignore-not-found', '-o', 'json')
    return 'statefulset/mongodb' if current.stdout.strip() else 'deployment/mongodb'


def member_target(member='mongodb'):
    if workload().startswith('statefulset/'):
        return 'pod/mongodb-' + ('1' if member == 'mongodb-secondary' else '0')
    return 'deployment/' + member


def direct(script, deployment='mongodb'):
    prefix = "db.getSiblingDB('admin').auth(process.env.MONGO_INITDB_ROOT_USERNAME,process.env.MONGO_INITDB_ROOT_PASSWORD);\n"
    result = w.kube('-n', w.NAMESPACE, 'exec', member_target(deployment), '--', 'mongosh',
        'mongodb://localhost:27017/?directConnection=true', '--quiet', '--eval', prefix + script)
    return json.loads(result.stdout.strip().splitlines()[-1])


def configured():
    current = w.kube('-n', w.NAMESPACE, 'get', workload(), '--ignore-not-found', '-o', 'json')
    if not current.stdout.strip():
        return False
    if json.loads(current.stdout).get('status', {}).get('readyReplicas', 0) < 1:
        return False
    result = direct("try {print(JSON.stringify({config:db.adminCommand({replSetGetConfig:1}).config}));} catch(e) {if(![76,94].includes(e.code))throw e;print(JSON.stringify({config:null}));}")
    config = result['config']
    if config is None:
        return False
    if config['_id'] != DATABASES['replicaSet'] or sorted(m['host'] for m in config['members']) != sorted(MEMBERS):
        raise RuntimeError('Existing replica configuration differs; refusing reconfiguration')
    return True


def save_state(state, path=None):
    path = path or STATE
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(state, indent=2) + '\n')
    temporary.chmod(0o600)
    os.replace(temporary, path)


def require_conversion_receipt():
    uid = w.kube_json('get', 'namespace', w.NAMESPACE)['metadata']['uid']
    if STATE.exists():
        state = json.loads(STATE.read_text())
        if state['namespaceUid'] != uid:
            raise RuntimeError('Mongo conversion receipt belongs to another namespace')
        if state.get('backup'):
            backup = w.vm.VM / state['backup']
            if not backup.is_file() or hashlib.sha256(backup.read_bytes()).hexdigest() != state['sha256']:
                raise RuntimeError('Mongo conversion backup is missing or changed')
        return
    existing = w.kube('-n', w.NAMESPACE, 'get', 'pvc/mongodb', '--ignore-not-found', '-o', 'json')
    if existing.stdout.strip():
        raise RuntimeError('Existing Mongo data requires: python3 ../myscoutee-microservices/tools/mongo_replica.py configure')
    save_state({'namespaceUid': uid, 'phase': 'fresh', 'createdAt': datetime.datetime.now(datetime.timezone.utc).isoformat()})


def initialize():
    if not configured():
        config = {'_id': DATABASES['replicaSet'], 'members': [{'_id': i, 'host': host, 'priority': 2 if i == 0 else 1}
                                            for i, host in enumerate(MEMBERS)]}
        direct('print(JSON.stringify(rs.initiate(' + json.dumps(config) + ')));')
    deadline = time.monotonic() + 150
    while True:
        state = direct("const s=rs.status();print(JSON.stringify({members:s.members.map(m=>({name:m.name,state:m.stateStr,health:m.health}))}));")
        if sorted(row['state'] for row in state['members']) == ['PRIMARY', 'SECONDARY'] and all(row['health'] == 1 for row in state['members']):
            return state
        if time.monotonic() > deadline:
            raise RuntimeError('Replica initial synchronization did not complete; retain data and rerun configure')
        time.sleep(2)


def collection_counts():
    return direct("const r={};for(const n of " + json.dumps([DATABASES["real"], DATABASES["demo"]]) + ") {const d=db.getSiblingDB(n);r[n]={};for(const c of d.getCollectionNames().sort())r[n][c]=d.getCollection(c).countDocuments({});}print(JSON.stringify(r));")


def backup(path=None, purpose="replicaset", extra=None):
    path = path or STATE
    uid = w.kube_json('get', 'namespace', w.NAMESPACE)['metadata']['uid']
    if path.exists():
        state = json.loads(path.read_text())
        if path == STATE:
            require_conversion_receipt()
        else:
            verify_receipt(state)
        return state
    folder = w.vm.VM / 'backups'
    w.vm.private_directory(folder)
    archive = folder / ('mongo-before-' + purpose + '-' + datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '.archive.gz')
    counts = collection_counts()
    command = shlex.join(['sudo', 'k3s', 'kubectl', '-n', w.NAMESPACE, 'exec', member_target(), '--',
        'sh', '-ec', 'exec mongodump --host 127.0.0.1 --username "$MONGO_INITDB_ROOT_USERNAME" --password "$MONGO_INITDB_ROOT_PASSWORD" --authenticationDatabase admin --archive --gzip'])
    with archive.open('xb') as output:
        result = subprocess.run(w.vm.ssh_command(command), stdout=output, stderr=subprocess.PIPE, timeout=180)
    archive.chmod(0o600)
    if result.returncode:
        raise RuntimeError('Mongo backup failed; partial archive retained, conversion not started')
    with gzip.open(archive, 'rb') as stream:
        while stream.read(1024 * 1024):
            pass  # Verify the complete compressed stream before conversion.
    if archive.stat().st_size < 1024:
        raise RuntimeError('Unexpectedly small Mongo archive')
    state = {'namespaceUid': uid, 'phase': 'backed-up', 'backup': str(archive.relative_to(w.vm.VM)),
             'sha256': hashlib.sha256(archive.read_bytes()).hexdigest(), 'counts': counts, **(extra or {})}
    save_state(state, path)
    return state


def replication_probe():
    marker = 'replica-check-' + uuid.uuid4().hex
    host = 'mongodb://' + ','.join(MEMBERS) + '/?replicaSet=' + DATABASES['replicaSet']
    # Discover the current primary; never assume the first member is still primary.
    script = "const c=new Mongo(" + json.dumps(host) + ");c.getDB('admin').auth(process.env.MONGO_INITDB_ROOT_USERNAME,process.env.MONGO_INITDB_ROOT_PASSWORD);const d=c.getDB(" + json.dumps(DATABASES["bootstrap"]) + ");"
    direct(script + 'd.replicaChecks.insertOne({_id:' + json.dumps(marker) + '},{writeConcern:{w:"majority",wtimeout:15000}});print(JSON.stringify({written:true}));')
    try:
        secondary = next(row['name'].split('.')[0] for row in initialize()['members'] if row['state'] == 'SECONDARY')
        observed = direct('db.getMongo().setReadPref("secondary");print(JSON.stringify({found:db.getSiblingDB(' + json.dumps(DATABASES["bootstrap"]) + ').replicaChecks.countDocuments({_id:' + json.dumps(marker) + '})}));', secondary)
        if observed['found'] != 1:
            raise RuntimeError('Replica write was not present on the second member')
    finally:
        direct(script + 'd.replicaChecks.deleteOne({_id:' + json.dumps(marker) + '},{writeConcern:{w:"majority",wtimeout:15000}});print(JSON.stringify({deleted:true}));')


def verify_receipt(state):
    if state['namespaceUid'] != w.kube_json('get', 'namespace', w.NAMESPACE)['metadata']['uid']:
        raise RuntimeError('Mongo migration receipt belongs to another namespace')
    archive = w.vm.VM / state['backup']
    if not archive.is_file() or hashlib.sha256(archive.read_bytes()).hexdigest() != state['sha256']:
        raise RuntimeError('Mongo migration backup is missing or changed')


def existing(kind, name):
    result = w.kube('-n', w.NAMESPACE, 'get', kind, name, '--ignore-not-found', '-o', 'json')
    return json.loads(result.stdout) if result.stdout.strip() else None


def require_owned(obj):
    if any(obj['metadata'].get('labels', {}).get(k) != v for k, v in w.LABEL.items()):
        raise RuntimeError('Refusing unowned Mongo resource: ' + obj['metadata']['name'])
    annotations = obj['metadata'].get('annotations', {})
    if any(annotations.get(key, w.NAMESPACE) != w.NAMESPACE for key in
           ('meta.helm.sh/release-name', 'meta.helm.sh/release-namespace')):
        raise RuntimeError('Mongo resource belongs to another Helm release')


def content_snapshot():
    return direct("const r={};for(const n of " + json.dumps([DATABASES["real"], DATABASES["demo"]]) + ") {const d=db.getSiblingDB(n);r[n]={hashes:d.runCommand({dbHash:1}).collections,indexes:{}};for(const c of d.getCollectionNames().sort())r[n].indexes[c]=d.getCollection(c).getIndexes();}print(JSON.stringify(r));")


def require_stateful_volumes():
    legacy = existing('deployment', 'mongodb') or existing('pvc', 'mongodb')
    if not STATEFUL_STATE.exists():
        if legacy:
            raise RuntimeError('Existing Mongo volumes require: mongo_replica.py statefulset')
        return  # Fresh namespace: StatefulSet provisions its own per-pod claims.
    state = json.loads(STATEFUL_STATE.read_text())
    verify_receipt(state)
    if state.get('phase') not in ('volumes-ready', 'data-verified', 'verified'):
        raise RuntimeError('Mongo StatefulSet migration incomplete; rerun mongo_replica.py statefulset')
    for row in state['volumes']:
        claim = existing('pvc', row['target'])
        if not claim or claim['metadata']['uid'] != row['targetUid'] or claim['spec'].get('volumeName') != row['pv']['metadata']['name'] or claim.get('status', {}).get('phase') != 'Bound':
            raise RuntimeError('Mongo StatefulSet claim identity or binding changed')
        pv = w.kube_json('get', 'pv', row['pv']['metadata']['name'])
        if pv['metadata']['uid'] != row['pv']['metadata']['uid'] or pv['spec']['claimRef']['uid'] != row['targetUid']:
            raise RuntimeError('Mongo StatefulSet volume identity changed')


def validate_volume(row, pv):
    old = row['pv']
    if pv['metadata']['uid'] != old['metadata']['uid'] or pv['spec'].get('local') != old['spec'].get('local') or pv['spec'].get('nodeAffinity') != old['spec'].get('nodeAffinity'):
        raise RuntimeError('Mongo volume identity or location changed')
    ref = pv['spec'].get('claimRef', {})
    if ref.get('namespace') != w.NAMESPACE or (ref.get('name'), ref.get('uid')) not in (
            (row['claim']['metadata']['name'], row['claim']['metadata']['uid']),
            (row['target'], row.get('targetUid'))):
        raise RuntimeError('Mongo volume bound to an unexpected claim')


def rebind_volume(row, state):
    old = row['claim']
    name = row['pv']['metadata']['name']
    pv = w.kube_json('get', 'pv', name)
    validate_volume(row, pv)
    # Retain is set and observed before releasing the old claim. Never delete PVs.
    if pv['spec']['persistentVolumeReclaimPolicy'] != 'Retain':
        w.kube('patch', 'pv', name, '--type=json', '-p', json.dumps([
            {'op': 'test', 'path': '/metadata/uid', 'value': pv['metadata']['uid']},
            {'op': 'replace', 'path': '/spec/persistentVolumeReclaimPolicy', 'value': 'Retain'}]))
    target = existing('pvc', row['target'])
    if not target:
        spec = {k: old['spec'][k] for k in ('accessModes', 'resources', 'storageClassName', 'volumeMode', 'volumeName')}
        w.kube('create', '-f', '-', data=json.dumps({'apiVersion': 'v1', 'kind': 'PersistentVolumeClaim',
            'metadata': {'name': row['target'], 'namespace': w.NAMESPACE,
                         'labels': {**w.LABEL, 'app.kubernetes.io/name': 'mongodb'},
                         'annotations': {'myscoutee.io/migrated-from-pvc-uid': old['metadata']['uid']}}, 'spec': spec}))
        target = existing('pvc', row['target'])
    require_owned(target)
    if target['metadata'].get('annotations', {}).get('myscoutee.io/migrated-from-pvc-uid') != old['metadata']['uid'] or target['spec'].get('volumeName') != name:
        raise RuntimeError('Refusing to adopt an unrelated target Mongo claim')
    if row.get('targetUid', target['metadata']['uid']) != target['metadata']['uid']:
        raise RuntimeError('Target Mongo claim was replaced')
    row['targetUid'] = target['metadata']['uid']
    save_state(state, STATEFUL_STATE)
    previous = existing('pvc', old['metadata']['name'])
    if previous:
        if previous['metadata']['uid'] != old['metadata']['uid']:
            raise RuntimeError('Original Mongo claim was replaced')
        require_owned(previous)
        pv = w.kube_json('get', 'pv', name)
        validate_volume(row, pv)
        if pv['spec']['persistentVolumeReclaimPolicy'] != 'Retain':
            raise RuntimeError('Mongo volume retention was not applied')
        w.kube('-n', w.NAMESPACE, 'delete', 'pvc', old['metadata']['name'], '--wait=true', '--timeout=60s')
    pv = w.kube_json('get', 'pv', name)
    validate_volume(row, pv)
    ref = {'apiVersion': 'v1', 'kind': 'PersistentVolumeClaim', 'name': row['target'],
           'namespace': w.NAMESPACE, 'uid': row['targetUid']}
    w.kube('patch', 'pv', name, '--type=json', '-p', json.dumps([
        {'op': 'test', 'path': '/metadata/resourceVersion', 'value': pv['metadata']['resourceVersion']},
        {'op': 'replace', 'path': '/spec/claimRef', 'value': ref}]))
    w.kube('-n', w.NAMESPACE, 'wait', 'pvc/' + row['target'], '--for=jsonpath={.status.phase}=Bound', '--timeout=60s')


def migrate_statefulset():
    if not w.CONFIG.get('mongoReplicaSet'):
        raise RuntimeError('StatefulSet migration requires the two-member replica profile')
    if STATEFUL_STATE.exists():
        state = json.loads(STATEFUL_STATE.read_text())
        verify_receipt(state)
        if state['phase'] == 'verified':
            require_stateful_volumes()
            initialize()
            replication_probe()
            print('PASS: existing StatefulSet, retained volumes and replication verified; no restart repeated')
            return
    else:
        if existing('statefulset', 'mongodb'):
            require_owned(existing('statefulset', 'mongodb'))
            initialize()
            replication_probe()
            print('PASS: fresh StatefulSet replica verified; no legacy migration required')
            return
        rows = []
        for i, name in enumerate(('mongodb', 'mongodb-secondary')):
            deployment = existing('deployment', name)
            claim = existing('pvc', name)
            if not deployment or not claim:
                raise RuntimeError('Both original Mongo deployments and claims must exist')
            require_owned(deployment); require_owned(claim)
            pv = w.kube_json('get', 'pv', claim['spec']['volumeName'])
            row = {'claim': claim, 'pv': pv, 'deployment': deployment, 'target': 'data-mongodb-' + str(i)}
            validate_volume(row, pv)
            if existing('pvc', row['target']):
                raise RuntimeError('Target Mongo claim already exists without a migration receipt')
            rows.append(row)
        w.stop_backends()
        initialize()
        state = backup(STATEFUL_STATE, 'statefulset', {'volumes': rows, 'content': content_snapshot()})
    if 'volumes' not in state:
        raise RuntimeError('Backup exists but migration inventory was not saved; data has not been moved')
    w.stop_backends()
    if state['phase'] not in ('volumes-ready', 'data-verified'):
        for row in state['volumes']:
            name = row['deployment']['metadata']['name']
            deployment = existing('deployment', name)
            if not deployment or deployment['metadata']['uid'] != row['deployment']['metadata']['uid']:
                raise RuntimeError('Original Mongo controller changed during migration')
            require_owned(deployment)
            w.kube('-n', w.NAMESPACE, 'scale', 'deployment/' + name, '--replicas=0')
        for row in state['volumes']:
            selector = ','.join(k + '=' + v for k, v in row['deployment']['spec']['selector']['matchLabels'].items())
            w.kube('-n', w.NAMESPACE, 'wait', 'pod', '-l', selector, '--for=delete', '--timeout=90s')
        for row in state['volumes']:
            rebind_volume(row, state)
        state['phase'] = 'volumes-ready'
        save_state(state, STATEFUL_STATE)
    require_stateful_volumes()
    w.deploy_chart(backends_enabled=False)
    result = initialize()
    if state['phase'] != 'data-verified' and (collection_counts() != state['counts'] or content_snapshot() != state['content']):
        raise RuntimeError('Mongo content/counts/indexes differ; backends remain stopped and backup retained')
    replication_probe()
    state['phase'] = 'data-verified'
    save_state(state, STATEFUL_STATE)
    w.deploy_chart(backends_enabled=True)
    state.update(phase='verified', members=result['members'])
    save_state(state, STATEFUL_STATE)
    print('PASS: one StatefulSet, two pods, original volumes/content/indexes retained; majority write and secondary read verified')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['configure', 'status', 'tune-cpu', 'statefulset'])
    args = parser.parse_args()
    os.umask(0o077)
    if not w.namespace_owned():
        raise RuntimeError('Start the owned lab namespace first')
    if args.action == 'statefulset':
        with (w.vm.VM / 'lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            migrate_statefulset()
        return
    if args.action == 'tune-cpu':
        # Kubernetes in-place resize preserves the warmed database/process. The
        # next Helm apply persists the same versioned limit on its Deployment.
        cores = str(w.CONFIG.get('mongoCpuLimit', 2))
        if cores not in ('2', '3'):
            raise RuntimeError('This 4-vCPU lab permits a Mongo ceiling of 2 or 3 cores')
        with (w.vm.VM / 'lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            pods = w.kube_json('-n', w.NAMESPACE, 'get', 'pods', '-l', ('statefulset.kubernetes.io/pod-name=mongodb-0' if workload().startswith('statefulset/') else 'app.kubernetes.io/name=mongodb'))['items']
            if len(pods) != 1:
                raise RuntimeError('Expected one original Mongo member pod')
            pod = pods[0]
            if pod['spec']['containers'][0]['image'] != w.image_lock()['mongodb']['reference']:
                raise RuntimeError('Unexpected Mongo image')
            original = pod['status']['containerStatuses'][0]
            patch = [{'op': 'test', 'path': '/metadata/uid', 'value': pod['metadata']['uid']},
                     {'op': 'replace', 'path': '/spec/containers/0/resources/limits/cpu', 'value': cores}]
            w.kube('-n', w.NAMESPACE, 'patch', 'pod/' + pod['metadata']['name'], '--subresource=resize', '--type=json', '-p', json.dumps(patch))
            deadline = time.monotonic() + 45
            while True:
                current = w.kube_json('-n', w.NAMESPACE, 'get', 'pod/' + pod['metadata']['name'])['status']['containerStatuses'][0]
                if current['containerID'] != original['containerID'] or current['restartCount'] != original['restartCount']:
                    raise RuntimeError('Mongo restarted during CPU resize; do not treat the measurement as warmed')
                if current.get('resources', {}).get('limits', {}).get('cpu') == cores:
                    print('PASS: Mongo CPU limit is ' + cores + ' cores; process and warm cache retained')
                    return
                if time.monotonic() >= deadline:
                    raise RuntimeError('CPU resize did not become effective')
                time.sleep(1)
    if args.action == 'status':
        print(json.dumps(initialize() if configured() else {'configured': False}, indent=2))
        return
    if not w.CONFIG.get('mongoReplicaSet'):
        raise RuntimeError('Set mongoReplicaSet=true in deploy/runtime.json before conversion')
    with (w.vm.VM / 'lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if STATE.exists() and json.loads(STATE.read_text()).get('phase') == 'verified' and configured():
            require_conversion_receipt()
            replication_probe()
            w.deploy_chart(backends_enabled=True)
            print('PASS: existing replica set verified; no backup, initialization or coordinated stop repeated')
            return
        # Reject legacy unconverted volumes before stopping healthy backends.
        # The current chart only provisions replica members as a StatefulSet.
        require_stateful_volumes()
        w.stop_backends()
        state = backup()
        w.deploy_chart(backends_enabled=False)
        result = initialize()
        if state.get('phase') == 'backed-up' and collection_counts() != state['counts']:
            raise RuntimeError('Collection counts changed during conversion; backends remain stopped')
        replication_probe()
        state['phase'] = 'verified'
        state['members'] = result['members']
        save_state(state)
        w.deploy_chart(backends_enabled=True)
        print('PASS: two Mongo members synchronized, original data retained, majority-write/secondary-read probe cleaned')
        print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
