"""Verify private seed staging without accessing a VM or database."""

import importlib
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import workloads
seed_mongo = importlib.import_module('seed_mongo')

class SeedStagingTest(unittest.TestCase):
    def test_import_uses_atomic_private_staging_and_cleans_only_its_created_directory(self):
        prefix = workloads.NAMESPACE + '-seed-' + 'a' * 16 + '-'
        remote = '/tmp/' + prefix + 'Abc1234567'
        operations = []
        def kube(*args, **kwargs):
            operations.append(args)
            return subprocess.CompletedProcess(args, 0, remote + '\n', '')
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(seed_mongo.mongo_replica, 'member_target', return_value='pod/mongodb-0'), \
             patch.object(seed_mongo.w, 'kube', side_effect=kube), \
             patch.object(seed_mongo.w.vm, 'ssh_command', return_value=['ssh', 'owned']), \
             patch.object(seed_mongo.subprocess, 'run') as transfer:
            source = Path(directory) / 'mongo_import.sh'
            source.write_text('# canonical importer\n')
            seed_mongo.import_files({'mongo_import.sh': source}, 'owned:*', 'a' * 64,
                                    databases={'owned': 'demo_db'})
        self.assertIn(('mktemp', '-d', '-t', prefix + 'XXXXXXXXXX'),
                      [command[-4:] for command in operations])
        transfer.assert_called_once()
        imported = next(command for command in operations if 'env' in command)
        self.assertIn('MONGO_SEED_ROOT=' + remote, imported)
        self.assertIn('MONGO_IMPORT_REPLACE_COLLECTIONS=false', imported)
        self.assertEqual(operations[-1][-3:], ('rm', '-rf', remote))

    def test_import_cleanup_runs_after_archive_transfer_failure(self):
        remote = '/tmp/' + workloads.NAMESPACE + '-seed-' + 'a' * 16 + '-Abc1234567'
        with patch.object(seed_mongo.mongo_replica, 'member_target', return_value='pod/mongodb-0'), \
             patch.object(seed_mongo.w, 'kube', return_value=subprocess.CompletedProcess([], 0, remote + '\n', '')) as kube, \
             patch.object(seed_mongo.w.vm, 'ssh_command', return_value=['ssh', 'owned']), \
             patch.object(seed_mongo.subprocess, 'run', side_effect=RuntimeError('transfer failed')):
            with self.assertRaisesRegex(RuntimeError, 'transfer failed'):
                seed_mongo.import_files({}, 'owned:*', 'a' * 64)
        self.assertEqual(kube.call_args.args[-3:], ('rm', '-rf', remote))

    def test_invalid_remote_staging_path_cannot_import_or_delete_anything(self):
        for remote in ('/', '/tmp/foreign', 'relative', '/tmp/../foreign'):
            with self.subTest(remote=remote), \
                 patch.object(seed_mongo.mongo_replica, 'member_target', return_value='pod/mongodb-0'), \
                 patch.object(seed_mongo.w, 'kube', return_value=subprocess.CompletedProcess([], 0, remote, '')) as kube, \
                 patch.object(seed_mongo.subprocess, 'run') as transfer:
                with self.assertRaisesRegex(RuntimeError, 'Unexpected seed staging'):
                    seed_mongo.import_files({}, 'owned:*', 'a' * 64)
                kube.assert_called_once()
                transfer.assert_not_called()

