"""Check the static seed mount's inputs and the canonical shell import routing."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import workloads as w


class MongoSeedVolumeTest(unittest.TestCase):
    def test_seed_policy_preserves_real_demo_boundaries(self):
        from seed_mongo import seed_bundle
        files, policy, _ = seed_bundle(w.BACKEND)
        self.assertTrue(any(name.startswith('myscoutee_db/') for name in files))
        self.assertTrue(any(name.startswith('demo_db/') for name in files))
        self.assertFalse(any(name.startswith('e2e_db/') for name in files))
        rules = dict(item.split(':', 1) for item in policy.split(';'))
        self.assertNotIn('users', rules[w.CONFIG['databases']['real']].split(','))
        self.assertEqual(rules[w.CONFIG['databases']['demo']], '*')

    def test_volume_artifact_is_locked_but_never_becomes_a_workload(self):
        reference = w.CONFIG['volumeImages']['mongo-seed']
        self.assertTrue(reference.startswith(w.IMAGES['prefix']))
        self.assertFalse(any(name == 'mongo-seed' for _, name in w.chart_objects()))
        with tempfile.TemporaryDirectory() as folder:
            lock = Path(folder) / 'images.json'
            names = set(w.SERVICES) | set(w.CONFIG['dependencies']) | {'mongo-seed'}
            images = {name: {'id': 'sha256:' + 'a' * 64,
                'reference': 'docker.io/library/' + w.IMAGES['prefix'] + name + ':' +
                w.IMAGES['immutableVersion'] + '-' + 'a' * 12} for name in names}
            lock.write_text(json.dumps({'schemaVersion': 1, 'images': images}))
            with patch.object(w, 'LOCK', lock):
                self.assertIn('mongo-seed', w.image_lock())
                del images['mongo-seed']
                lock.write_text(json.dumps({'schemaVersion': 1, 'images': images}))
                with self.assertRaisesRegex(RuntimeError, 'inventory'):
                    w.image_lock()

    def test_shell_importer_uses_configured_real_and_demo_databases(self):
        importer = w.BACKEND / 'server/docker/conf/mongodb/mongo_import.sh'
        for configured in (False, True):
            with self.subTest(configured=configured), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                commands = root / 'bin'
                commands.mkdir()
                log = root / 'imports'
                for directory, name in [('myscoutee_db', 'catalog'), ('demo_db', 'users'), ('e2e_db', 'users')]:
                    seed = root / directory
                    seed.mkdir()
                    (seed / (name + '.json')).write_text('[{"_id":"fixture"}]')
                (commands / 'mongosh').write_text('#!/bin/sh\nexit 0\n')
                (commands / 'mongoimport').write_text(
                    '#!/bin/sh\nwhile [ "$#" -gt 0 ]; do\n'
                    'case "$1" in -d) shift; database=$1;; --file) shift; file=$1;; esac\n'
                    'shift\ndone\nprintf "%s %s\\n" "$database" "$file" >> "$IMPORT_LOG"\n')
                for command in commands.iterdir():
                    command.chmod(0o755)
                real = 'isolated_real' if configured else 'myscoutee_db'
                demo = 'isolated_demo' if configured else 'demo_db'
                environment = {'PATH': str(commands) + os.pathsep + os.defpath,
                    'IMPORT_LOG': str(log), 'MONGO_SEED_ROOT': str(root),
                    'MONGO_APP_DATABASE': real, 'MONGO_SEED_DIRECTORY': 'myscoutee_db',
                    'MONGO_IMPORT_ALL_SEED_DATABASES': 'true',
                    'MONGO_IMPORT_ALLOWED_COLLECTIONS_BY_DB': real + ':catalog;' + demo + ':*'}
                if configured:
                    environment['MONGO_DEMO_DATABASE'] = demo
                result = subprocess.run(['bash', str(importer)], env=environment, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(log.read_text().splitlines(), [
                    demo + ' ' + str(root / 'demo_db/users.json'),
                    real + ' ' + str(root / 'myscoutee_db/catalog.json')])
