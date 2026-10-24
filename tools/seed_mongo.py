#!/usr/bin/env python3
"""Bootstrap empty owned lab databases with the canonical prod/DEMO importer.

Never run on each deployment. Repeated identical bootstrap is a no-op; changed
seeds and existing application data require an explicit migration/reset workflow.
An interrupted identical bootstrap can resume while all backend replicas are zero.
"""

import importlib
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tarfile
import tempfile

import workloads as w
import mongo_replica

REAL_DB = w.CONFIG['databases']['real']
DEMO_DB = w.CONFIG['databases']['demo']
DATABASES = {REAL_DB: 'myscoutee_db', DEMO_DB: 'demo_db'}
STATE_DB = w.CONFIG['databases']['bootstrap']
IMPORT_FILES = ('mongo_import.sh', 'mongo-seed-policy.inc')
SEED_CONFIG = json.loads((w.SHELL / 'seed.json').read_text())


def seed_bundle(backend):
    source = backend / 'server/docker/conf/mongodb'
    # The production Compose env is the allowlist owner. No copy of the list here.
    lines = (backend / 'server/.env').read_text().splitlines()
    rule = next(line.split('=', 1)[1] for line in lines
                if line.startswith('MONGO_IMPORT_ALLOWED_COLLECTIONS_BY_DB='))
    rules = dict(item.split(':', 1) for item in rule.split(';'))
    if not rules.get('myscoutee_db') or rules['myscoutee_db'] == '*' or rules.get('demo_db') != '*':
        raise RuntimeError('Review changed canonical production/DEMO seed policy before lab bootstrap.')
    allowlist = ';'.join(database + ':' + rules[directory] for database, directory in DATABASES.items())
    files = {name: source / name for name in IMPORT_FILES}
    for directory in DATABASES.values():
        for path in sorted((source / directory).glob('*.json')):
            if not re.fullmatch(r'[A-Za-z0-9_]+\.json', path.name):
                raise RuntimeError('Unexpected seed filename.')
            # Canonical links are read once and archived as files, never as links.
            if not path.resolve().is_relative_to(backend.resolve()):
                raise RuntimeError('Seed link escapes the canonical backend checkout.')
            files[directory + '/' + path.name] = path
    if not any(name.startswith('demo_db/') for name in files):
        raise RuntimeError('Canonical DEMO seed is absent.')
    return files, allowlist, bundle_fingerprint(files, allowlist)


def bundle_fingerprint(files, allowlist, policy=None):
    digest = hashlib.sha256(json.dumps({'databases': DATABASES, 'allowlist': allowlist}, sort_keys=True).encode())
    for name, path in files.items():
        digest.update(name.encode() + b'\0')
        digest.update(policy if name == 'mongo-seed-policy.inc' and policy is not None else path.read_bytes())
    return digest.hexdigest()


def catalog_baseline_fingerprint(files, allowlist, catalog):
    """Reconstruct only the known allowlist omission, retaining every seed byte.

    A caller-supplied old fingerprint cannot bless unrelated source changes.
    This migration adds eligibility for an already-present canonical catalog.
    """
    rules = dict(item.split(':', 1) for item in allowlist.split(';'))
    previous_rules = ';'.join(db + ':' + (rule if rule == '*' else
        ','.join(item for item in rule.split(',') if item != catalog)) for db, rule in rules.items())
    policy = files['mongo-seed-policy.inc'].read_text()
    lines = policy.splitlines(keepends=True)
    indices = [index for index, line in enumerate(lines) if line.startswith('allowed_collections_by_db=')]
    if len(indices) != 1 or lines[indices[0]].count(',' + catalog + ',') != 2:
        raise RuntimeError('Canonical importer allowlist shape changed; review a separate migration.')
    lines[indices[0]] = lines[indices[0]].replace(',' + catalog + ',', ',')
    return bundle_fingerprint(files, previous_rules, ''.join(lines).encode())


def mongo(script):
    prefix = "db.getSiblingDB('admin').auth(process.env.MONGO_INITDB_ROOT_USERNAME,process.env.MONGO_INITDB_ROOT_PASSWORD);\n"
    host = ['--host', w.CONFIG['databases']['replicaSet'] + '/mongodb:27017,mongodb-secondary:27017'] if w.CONFIG.get('mongoReplicaSet') else []
    result = w.kube('-n', w.NAMESPACE, 'exec', mongo_replica.member_target(), '--',
                    'mongosh', *host, '--quiet', '--eval', prefix + script, timeout=120)
    return json.loads(result.stdout.strip().splitlines()[-1])


def claim_bootstrap(fingerprint, namespace_uid):
    # Only bootstrap metadata is inserted here; inspect BOTH databases first.
    # A durable fingerprint permits recovery of this exact interrupted import.
    return mongo("""
const databases = %s;
if(process.env.MONGO_REAL_DATABASE!==%s||process.env.MONGO_DEMO_DATABASE!==%s)
  throw new Error('Unexpected lab database configuration');
const state=db.getSiblingDB(%s).getCollection('seed');
const saved=state.findOne({_id:'canonical'});
const fingerprint=%s;
if(saved && (saved.fingerprint!==fingerprint || saved.namespaceUid!==%s))
  throw new Error('Seed ownership/content changed; explicit migration required');
if(!saved) {
  for(const name of Object.keys(databases)) {
    const target=db.getSiblingDB(name);
    if(target.getCollectionNames().filter(n=>!n.startsWith('system.')).some(n=>target.getCollection(n).findOne({})!==null))
      throw new Error('Existing data; refusing bootstrap');
  }
  state.insertOne({_id:'canonical',fingerprint,namespaceUid:%s,status:'started',startedAt:new Date()});
}
print(JSON.stringify({status:saved?saved.status:'started'}));
""" % (json.dumps(DATABASES), json.dumps(REAL_DB), json.dumps(DEMO_DB), json.dumps(STATE_DB), json.dumps(fingerprint),
         json.dumps(namespace_uid), json.dumps(namespace_uid)))


def snapshot(fingerprint, namespace_uid):
    # Hash and index metadata stay in memory. No document or credential is printed.
    return mongo("""
const seed=db.getSiblingDB(%s).seed.findOne({_id:'canonical'});
if(!seed || seed.status!=='complete' || seed.fingerprint!==%s || seed.namespaceUid!==%s)
  throw new Error('Matching completed bootstrap required');
const result={seed,databases:{}};
for(const name of %s) {
  const target=db.getSiblingDB(name);
  const digest=target.runCommand({dbHash:1});
  if(digest.ok!==1)throw new Error('Database hash unavailable');
  const collections={};
  for(const collection of target.getCollectionNames().filter(n=>!n.startsWith('system.')).sort()) {
    const source=target.getCollection(collection);
    collections[collection]={count:source.countDocuments({}),hash:digest.collections[collection],
      indexes:source.getIndexes().sort((a,b)=>a.name.localeCompare(b.name))};
  }
  result.databases[name]=collections;
}
print(JSON.stringify(result));
""" % (json.dumps(STATE_DB), json.dumps(fingerprint), json.dumps(namespace_uid), json.dumps(list(DATABASES))))


def verify_persistence(fingerprint, namespace_uid, restart):
    before=snapshot(fingerprint, namespace_uid)
    if restart:
        controller = mongo_replica.workload()
        w.kube('-n', w.NAMESPACE, 'rollout', 'restart', controller)
        w.kube('-n', w.NAMESPACE, 'rollout', 'status', controller, '--timeout=90s', timeout=100)
        if snapshot(fingerprint, namespace_uid) != before:
            raise RuntimeError('Mongo content, indexes or bootstrap marker changed across pod replacement.')
    counts={name:{'collections':len(collections),'documents':sum(row['count'] for row in collections.values())}
            for name,collections in before['databases'].items()}
    print('PASS: Mongo canonical bootstrap verified' + (' across pod replacement' if restart else '') + ': ' + json.dumps(counts,sort_keys=True))


def import_files(files, allowlist, fingerprint, databases=DATABASES, catalog=None):
    # Atomic creation (0700) prevents adopting a pre-created directory or symlink.
    prefix = w.NAMESPACE + '-seed-' + fingerprint[:16] + '-'
    directory = w.kube('-n', w.NAMESPACE, 'exec', mongo_replica.member_target(), '--',
                       'mktemp', '-d', '-t', prefix + 'XXXXXXXXXX').stdout.strip()
    path = Path(directory)
    if not path.is_absolute() or '..' in path.parts or not re.fullmatch(re.escape(prefix) + r'[A-Za-z0-9]{10}', path.name):
        raise RuntimeError('Unexpected seed staging directory; refusing import or cleanup.')
    try:
        with tempfile.TemporaryFile() as archive:
            with tarfile.open(fileobj=archive, mode='w:gz', dereference=True) as tar:
                for name, path in files.items():
                    tar.add(path, arcname=name, recursive=False)
            archive.seek(0)
            command = shlex.join(['sudo', 'k3s', 'kubectl', '-n', w.NAMESPACE,
                                 'exec', '-i', mongo_replica.member_target(), '--', 'tar', '-xzf', '-', '-C', directory])
            subprocess.run(w.vm.ssh_command(command), stdin=archive, capture_output=True, check=True, timeout=120)
        for database, source in databases.items():
            print('Importing canonical ' + source + ' into owned ' + database + ' using the prod allowlist.', flush=True)
            single_file = directory + '/' + source + '/' + catalog + '.json' if catalog else ''
            w.kube('-n', w.NAMESPACE, 'exec', mongo_replica.member_target(), '--', 'env',
                   'MONGO_IMPORT_HOST=' + (w.CONFIG['databases']['replicaSet'] + '/mongodb:27017,mongodb-secondary:27017' if w.CONFIG.get('mongoReplicaSet') else '127.0.0.1'),
                   'MONGO_SEED_ROOT=' + directory, 'MONGO_APP_DATABASE=' + database,
                   'MONGO_SEED_DIRECTORY=' + source, 'MONGO_IMPORT_ALL_SEED_DATABASES=false',
                   'MONGO_IMPORT_REPLACE_COLLECTIONS=false',
                   'MONGO_IMPORT_SINGLE_FILE=' + single_file,
                   'MONGO_IMPORT_ALLOWED_COLLECTIONS_BY_DB=' + allowlist,
                   'bash', directory + '/mongo_import.sh', timeout=1200)
    finally:
        w.kube('-n', w.NAMESPACE, 'exec', mongo_replica.member_target(), '--', 'rm', '-rf', directory, check=False)


def add_catalog(files, allowlist, fingerprint, namespace_uid, catalog, previous):
    """Explicit catalog-only migration from a reviewed bootstrap fingerprint.

    Before invoking, verify that the supplied fingerprint matches the source
    before changing only this catalog/import allowlist. Existing differing rows
    are rejected. The canonical importer is reused; no whole-database reset.
    """
    if catalog_baseline_fingerprint(files, allowlist, catalog) != previous:
        raise RuntimeError('Source changes exceed the catalog allowlist migration; refusing bootstrap marker update.')
    expected = {db: json.loads(files[source + '/' + catalog + '.json'].read_text())
                for db, source in DATABASES.items()}
    guard = """
const expected=%s, catalog=%s;
const state=db.getSiblingDB(%s).seed.findOne({_id:'canonical'});
if(!state || state.status!=='complete' || state.namespaceUid!==%s ||
   ![%s,%s].includes(state.fingerprint)) throw new Error('Catalog migration baseline mismatch');
function normalize(v) { if(Array.isArray(v))return v.map(normalize);
  if(v && typeof v==='object')return Object.fromEntries(Object.keys(v).sort().map(k=>[k,normalize(v[k])]));return v; }
const missing=[];
for(const [name, rows] of Object.entries(expected)) {
  const byId=new Map(rows.map(r=>[r._id,JSON.stringify(normalize(r))]));
  if(!rows.length || byId.size!==rows.length)throw new Error('Invalid canonical catalog');
  const current=db.getSiblingDB(name).getCollection(catalog).find({}).toArray();
  for(const row of current)if(byId.get(row._id)!==JSON.stringify(normalize(row)))
    throw new Error('Existing catalog differs; refusing overwrite');
  if(current.length!==rows.length)missing.push(name);
}
print(JSON.stringify({fingerprint:state.fingerprint,missing}));
""" % (json.dumps(expected), json.dumps(catalog), json.dumps(STATE_DB), json.dumps(namespace_uid),
       json.dumps(previous), json.dumps(fingerprint))
    before = mongo(guard)
    if before['missing']:
        if before['fingerprint'] == fingerprint:
            raise RuntimeError('Completed catalog data changed; refusing automatic repair.')
        databases = {name: DATABASES[name] for name in before['missing']}
        selected = {name: files[name] for name in IMPORT_FILES}
        selected.update({source + '/' + catalog + '.json': files[source + '/' + catalog + '.json']
                         for source in databases.values()})
        narrow_allowlist = ';'.join(name + ':' + catalog for name in databases)
        # This capability is limited to an explicitly production-managed catalog.
        rules = dict(rule.split(':', 1) for rule in allowlist.split(';'))
        for name in databases:
            if rules[name] != '*' and catalog not in rules[name].split(','):
                raise RuntimeError('Catalog is not in the canonical seed allowlist: ' + name)
        import_files(selected, narrow_allowlist, fingerprint, databases, catalog=catalog)
    if mongo(guard)['missing']:
        raise RuntimeError('Catalog import remains incomplete.')
    if before['fingerprint'] != fingerprint:
        mongo("""
const result=db.getSiblingDB(%s).seed.updateOne({_id:'canonical',namespaceUid:%s,fingerprint:%s},
  {$set:{fingerprint:%s},$push:{migrations:{catalog:%s,from:%s,to:%s,completedAt:new Date()}}});
if(result.matchedCount!==1)throw new Error('Bootstrap marker changed during catalog migration');
print(JSON.stringify({updated:true}));
""" % (json.dumps(STATE_DB), json.dumps(namespace_uid), json.dumps(previous), json.dumps(fingerprint),
       json.dumps(catalog), json.dumps(previous), json.dumps(fingerprint)))
    print('PASS: canonical ' + catalog + ' catalog verified in REAL/DEMO; existing business data preserved.')


def reset_lab(fingerprint, namespace_uid, previous):
    """Explicit disposable-lab reset using the existing verified Mongo backup.

    The caller already holds the VM lock and verifies stopped backend writers,
    namespace ownership and Mongo image identity. Ordinary startup never calls it.
    Keep a reset marker before dropping anything so the same command can resume.
    """
    guard = """
if(process.env.MONGO_REAL_DATABASE!==%s || process.env.MONGO_DEMO_DATABASE!==%s)
  throw new Error('Unexpected lab database configuration');
const state=db.getSiblingDB(%s).seed.findOne({_id:'canonical'});
if(!state || state.namespaceUid!==%s)throw new Error('Lab seed ownership mismatch');
const complete=state.fingerprint===%s && state.status==='complete';
const original=state.fingerprint===%s && state.status==='complete';
const resuming=state.fingerprint===%s && state.resetFrom===%s && ['resetting','started'].includes(state.status);
if(!complete && !original && !resuming)throw new Error('Lab reset baseline mismatch');
""" % (json.dumps(REAL_DB), json.dumps(DEMO_DB), json.dumps(STATE_DB),
       json.dumps(namespace_uid), json.dumps(fingerprint), json.dumps(previous),
       json.dumps(fingerprint), json.dumps(previous))
    current = mongo(guard + "print(JSON.stringify({complete,original,resuming}));")
    if current['complete']:
        return
    receipt = w.vm.VM / ('seed-reset-' + previous + '-' + fingerprint + '.json')
    if current['resuming'] and not receipt.exists():
        raise RuntimeError('Interrupted lab reset lost its backup receipt; refusing another reset.')
    saved = mongo_replica.backup(receipt, 'seed-reset', {'seedFrom': previous, 'seedTo': fingerprint})
    if saved.get('seedFrom') != previous or saved.get('seedTo') != fingerprint:
        raise RuntimeError('Lab reset backup belongs to a different seed transition.')
    print('Verified pre-reset Mongo backup: ' + str(w.vm.VM / saved['backup']), flush=True)
    mongo(guard + """
if(complete)throw new Error('Seed changed after reset preparation');
const marker=db.getSiblingDB(%s).seed;
marker.updateOne({_id:'canonical'},{$set:{fingerprint:%s,resetFrom:%s,status:'resetting'}});
for(const name of %s) {
  if(db.getSiblingDB(name).dropDatabase().ok!==1)throw new Error('Failed lab reset: '+name);
}
marker.updateOne({_id:'canonical'},{$set:{status:'started'}});
print(JSON.stringify({reset:true}));
""" % (json.dumps(STATE_DB), json.dumps(fingerprint), json.dumps(previous), json.dumps(list(DATABASES))))


def main(argv=None):
    retry_migration = (importlib.import_module('seed_retry_catalog')
                       if SEED_CONFIG['retryCatalogMigration'] else None)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--verify',action='store_true',help='Read completed bootstrap, content hashes and indexes without importing')
    parser.add_argument('--restart',action='store_true',help='Verify identical data/indexes after replacing only the Mongo pod; implies --verify')
    if retry_migration:
        parser.add_argument('--add-retry-catalog', action='store_true', help='Add only outbound retry state and hourly job catalog rows')
        parser.add_argument('--from-revision', help='Exact Git baseline for the additive retry catalog migration')
    parser.add_argument('--add-catalog', choices=['landingSlides'], help='Explicitly add the omitted production landing catalog')
    parser.add_argument('--reset-lab', action='store_true', help='Back up and reset both owned lab databases to the current canonical seed; never automatic')
    parser.add_argument('--from-fingerprint', help='Reviewed complete-bootstrap fingerprint before the explicit catalog change or lab reset')
    args=parser.parse_args(argv)
    if args.add_catalog and (args.verify or args.restart or not args.from_fingerprint or not re.fullmatch(r'[a-f0-9]{64}', args.from_fingerprint)):
        parser.error('Catalog migration requires --from-fingerprint and cannot combine with verification/restart.')
    if args.reset_lab and (args.verify or args.restart or args.add_catalog or not args.from_fingerprint or not re.fullmatch(r'[a-f0-9]{64}', args.from_fingerprint)):
        parser.error('Lab reset requires --from-fingerprint and cannot combine with other seed actions.')
    if args.from_fingerprint and not (args.add_catalog or args.reset_lab):
        parser.error('--from-fingerprint requires --add-catalog or --reset-lab')

    if retry_migration and args.add_retry_catalog:
        if not args.from_revision or args.add_catalog or args.reset_lab or args.verify or args.restart or args.from_fingerprint:
            parser.error('Retry catalog migration requires --from-revision and no other action.')
    elif retry_migration and args.from_revision:
        parser.error('--from-revision requires --add-retry-catalog')

    backend = Path(os.environ.get('MYSCOUTEE_BACKEND_SOURCE', w.BACKEND))
    files, allowlist, fingerprint = seed_bundle(backend)
    with (w.vm.VM / 'lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        namespace = w.namespace_owned()
        if not namespace:
            raise RuntimeError('Owned workload namespace is absent.')
        for name in w.SERVICES:
            deployment = w.kube_json('-n', w.NAMESPACE, 'get', 'deployment', name)
            if deployment['spec'].get('replicas', 1) != 0 or deployment.get('status', {}).get('replicas', 0):
                raise RuntimeError('Stop all owned backends with workloads.py frontend-up before seed bootstrap.')
        deployment = w.kube_json('-n', w.NAMESPACE, 'get', mongo_replica.workload())
        if any(deployment['metadata'].get('labels', {}).get(key) != value for key, value in w.LABEL.items()):
            raise RuntimeError('Refusing unowned MongoDB deployment.')
        if deployment['spec']['template']['spec']['containers'][0]['image'] != w.image_lock()['mongodb']['reference']:
            raise RuntimeError('MongoDB image differs from the versioned lock.')
        if retry_migration and args.add_retry_catalog:
            retry_migration.add_retry_catalog(backend, files, allowlist, fingerprint, namespace['metadata']['uid'], args.from_revision)
            return
        if args.add_catalog:
            add_catalog(files, allowlist, fingerprint, namespace['metadata']['uid'], args.add_catalog, args.from_fingerprint)
            return
        if args.verify or args.restart:
            verify_persistence(fingerprint, namespace['metadata']['uid'], args.restart)
            return
        if args.reset_lab:
            reset_lab(fingerprint, namespace['metadata']['uid'], args.from_fingerprint)
        if w.CONFIG.get('mongoReplicaSet'):
            # A fresh frontend-only deployment has no primary yet. Reuse the
            # owned replica initializer before the canonical bootstrap write.
            mongo_replica.initialize()
        state = claim_bootstrap(fingerprint, namespace['metadata']['uid'])
        if state['status'] == 'complete':
            print('Canonical seed already bootstrapped; no data or indexes changed.')
            return
        if state['status'] != 'started':
            raise RuntimeError('Unknown bootstrap state.')
        import_files(files, allowlist, fingerprint)
        result = mongo("""
const stats=%s;
for(const name of Object.keys(stats)) {
  const target=db.getSiblingDB(name);
  stats[name]={users:target.users.countDocuments({}),articles:target.ideaPosts.countDocuments({}),
policies:target.accessPolicyDefinitions.countDocuments({}),i18n:target.i18n_messages.countDocuments({})};
  if(!stats[name].policies || !stats[name].i18n)throw new Error('Required catalog seed is empty');
}
if(!stats[%s].users)throw new Error('DEMO users absent');
db.getSiblingDB(%s).seed.updateOne({_id:'canonical',fingerprint:%s},{$set:{status:'complete',completedAt:new Date()}});
print(JSON.stringify(stats));
""" % (json.dumps(DATABASES), json.dumps(DEMO_DB), json.dumps(STATE_DB), json.dumps(fingerprint)))
        print('Canonical bootstrap verified:', json.dumps(result, sort_keys=True))



