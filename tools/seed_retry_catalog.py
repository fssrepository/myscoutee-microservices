"""Configured additive retry-catalog migration using the canonical importer."""
import hashlib
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
from seed_mongo import DATABASES, IMPORT_FILES, STATE_DB, SEED_CONFIG, seed_bundle, mongo, import_files

# Explicit additive migration for the outbound retry model. Existing job state,
# pending deliveries and application fixtures are never imported/replaced.
RETRY_POLICY = SEED_CONFIG['retryCatalogMigration']
RETRY_COLLECTIONS = set(RETRY_POLICY['newCollections'])


def retry_catalog_delta(files, baseline):
    selected = {}
    for name in set(files) | set(baseline):
        if name in IMPORT_FILES:
            continue
        if name not in files:
            raise RuntimeError('Retry migration cannot remove seed files: ' + name)
        if name in baseline and files[name].read_bytes() == baseline[name].read_bytes():
            continue
        collection = Path(name).stem
        rows = json.loads(files[name].read_text())
        if collection in RETRY_COLLECTIONS and name not in baseline:
            selected[name] = rows
            continue
        if collection not in {'notificationRuleConfigs', 'i18n_messages', 'i18n_bundles'} or name not in baseline:
            raise RuntimeError('Unrelated seed change in retry migration: ' + name)
        old = {row['_id']: row for row in json.loads(baseline[name].read_text())}
        new = {row['_id']: row for row in rows}
        if len(new) != len(rows):
            raise RuntimeError('Duplicate canonical seed identity: ' + name)
        if collection == 'i18n_bundles':
            if old.keys() != new.keys() or any({k:v for k,v in old[key].items() if k != 'version'} !=
                    {k:v for k,v in new[key].items() if k != 'version'} for key in old):
                raise RuntimeError('Retry migration only changes bundle version')
            selected[name] = rows
            continue
        if any(new.get(key) != row for key, row in old.items()):
            raise RuntimeError('Retry migration cannot change existing catalog rows: ' + name)
        additions = [row for row in rows if row['_id'] not in old]
        rule_keys = set(RETRY_POLICY['ruleKeys'])
        message_keys = set(RETRY_POLICY['messageKeys'])
        if any(row.get('ruleKey') not in rule_keys if collection == 'notificationRuleConfigs'
               else row.get('key') not in message_keys for row in additions):
            raise RuntimeError('Unrelated catalog addition: ' + name)
        selected[name] = additions
    return selected


def add_retry_catalog(backend, files, allowlist, fingerprint, namespace_uid, revision):
    # Validate the source delta against an exact Git revision. The retained lab
    # may have an older bootstrap: record this additive migration separately and
    # never claim that unrelated catalogs were upgraded to the current bundle.
    resolved = subprocess.check_output(['git', '-C', str(backend), 'rev-parse', '--verify', revision + '^{commit}'], text=True).strip()
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        archive = subprocess.check_output(['git', '-C', str(backend), 'archive', resolved,
                                          'server/.env', 'server/docker/conf/mongodb'])
        import io
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(root, filter='data')
        baseline, _, _ = seed_bundle(root)
        delta = retry_catalog_delta(files, baseline)
        migration = hashlib.sha256(json.dumps(delta, sort_keys=True).encode()).hexdigest()
        guard = """
const seed=db.getSiblingDB(%s).seed, state=seed.findOne({_id:'canonical'});
if(!state || state.status!=='complete' || state.namespaceUid!==%s)
  throw new Error('Retry catalog migration requires an owned completed bootstrap');
const saved=seed.findOne({_id:'outbound-retries'});
if(saved && (saved.fingerprint!==%s || saved.namespaceUid!==%s))
  throw new Error('Retry catalog migration identity changed');
print(JSON.stringify({complete:saved?.status==='complete',bootstrap:state.fingerprint}));
""" % tuple(map(json.dumps, (STATE_DB, namespace_uid, migration, namespace_uid)))
        state = mongo(guard)
        if state['complete']:
            print('Retry catalog migration already complete; no data changed.')
            return
        # Select only missing rows. This retains runtime revisions and admin job
        # overrides even if an interrupted migration is resumed.
        expected = {db: {Path(name).stem: rows for name, rows in delta.items()
                        if name.startswith(source + '/')} for db, source in DATABASES.items()}
        missing = mongo("""
const expected=%s, missing={};
for(const [name, collections] of Object.entries(expected)) {
  missing[name]={};
  for(const [collection, rows] of Object.entries(collections)) {
    const target=db.getSiblingDB(name).getCollection(collection);
    missing[name][collection]=rows.filter(row=>collection==='i18n_bundles' || !target.findOne({_id:row._id}));
  }
}
print(JSON.stringify(missing));
""" % json.dumps(expected))
        selected = {name: files[name] for name in IMPORT_FILES}
        for db, collections in missing.items():
            for collection, rows in collections.items():
                name = DATABASES[db] + '/' + collection + '.json'
                path = root / 'delta' / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(rows))
                selected[name] = path
        narrow = ';'.join(db + ':' + ','.join(sorted(collections)) for db, collections in missing.items())
        import_files(selected, narrow, fingerprint)
        mongo("""
const seed=db.getSiblingDB(%s).seed;
if(!seed.findOne({_id:'canonical',namespaceUid:%s,fingerprint:%s,status:'complete'}))
  throw new Error('Bootstrap marker changed during retry migration');
const expected=%s;
for(const [name, collections] of Object.entries(expected))
  for(const [collection, rows] of Object.entries(collections))
    for(const row of rows) if(!db.getSiblingDB(name).getCollection(collection).findOne({_id:row._id}))
      throw new Error('Retry catalog import incomplete');
seed.updateOne({_id:'outbound-retries'},{$set:{namespaceUid:%s,fingerprint:%s,status:'complete',
 sourceBaseline:%s,bootstrap:%s,completedAt:new Date()}},{upsert:true});
print(JSON.stringify({updated:true}));
""" % tuple(map(json.dumps, (STATE_DB, namespace_uid, state['bootstrap'], expected,
                              namespace_uid, migration, resolved, state['bootstrap']))))
        print('PASS: additive retry catalogs imported; existing jobs, runtime state and fixtures retained.')


