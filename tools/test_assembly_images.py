"""Domain input changes and verified image reuse, without a running VM."""
import copy
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import assembly_images as images


class AssemblyImagesTest(unittest.TestCase):
    def test_domain_shared_dependency_and_runtime_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ('help', 'communication', 'contract', 'dependency'):
                (root / name).write_text(name)
            manifests = {name: {'service': name, 'properties': {'domains': [name]},
                'files': {key: str(root / key) for key in (name, 'contract', 'dependency')}}
                for name in ('help', 'communication')}
            def keys():
                return {name: images.fingerprint(value, 'runtime-v1') for name, value in manifests.items()}
            before = keys()
            (root / 'help').write_text('help fix')
            changed = keys()
            self.assertNotEqual(before['help'], changed['help'])
            self.assertEqual(before['communication'], changed['communication'])
            for shared in ('contract', 'dependency'):
                before = keys()
                (root / shared).write_text('changed ' + shared)
                self.assertTrue(all(before[name] != key for name, key in keys().items()))
            self.assertNotEqual(images.fingerprint(manifests['help'], 'runtime-v1'),
                                images.fingerprint(manifests['help'], 'runtime-v2'))

    def test_checkout_path_and_mtime_are_not_build_inputs(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'first').write_text('same source')
            (root / 'second').write_text('same source')
            manifest = {'service': 'help', 'properties': {}, 'files': {'source': str(root / 'first')}}
            relocated = copy.deepcopy(manifest)
            relocated['files']['source'] = str(root / 'second')
            self.assertEqual(images.fingerprint(manifest, 'runtime'), images.fingerprint(relocated, 'runtime'))
            relocated['properties']['heapMiB'] = 512
            self.assertNotEqual(images.fingerprint(manifest, 'runtime'), images.fingerprint(relocated, 'runtime'))

    def test_local_image_reuse_does_not_pull_or_build(self):
        info = {'Id': 'sha256:original', 'Config': {'Labels': {
            images.INPUT_LABEL: 'key', 'com.myscoutee.assembly': 'help'}}}
        with patch.object(images, 'inspect', return_value=info), patch.object(images, 'run') as run:
            self.assertEqual(images.reuse('local', 'remote', 'help', 'key'), 'sha256:original')
            run.assert_called_once_with('docker', 'tag', 'sha256:original', 'local')

    def test_fresh_runner_recovers_registry_image_and_checks_identity(self):
        info = {'Id': 'sha256:original', 'Config': {'Labels': {
            images.INPUT_LABEL: 'key', 'com.myscoutee.assembly': 'help'}}}
        with patch.object(images, 'inspect', side_effect=[None, info]), \
             patch.object(images, 'run', return_value=subprocess.CompletedProcess([], 0)) as run:
            self.assertEqual(images.reuse('local', 'remote', 'help', 'key'), 'sha256:original')
            self.assertEqual(run.call_args_list[0].args, ('docker', 'pull', 'remote'))
        with patch.object(images, 'inspect', return_value=info):
            with self.assertRaisesRegex(RuntimeError, 'identity mismatch'):
                images.reuse('local', 'remote', 'communication', 'key')

    def test_registry_absence_builds_but_outage_or_auth_failure_does_not(self):
        for message, missing in [('manifest unknown', True), ('unauthorized', False), ('connection refused', False)]:
            with self.subTest(message=message), patch.object(images, 'inspect', return_value=None), \
                 patch.object(images, 'run', return_value=subprocess.CompletedProcess([], 1, '', message)):
                if missing:
                    self.assertIsNone(images.reuse('local', 'remote', 'help', 'key'))
                else:
                    with self.assertRaisesRegex(RuntimeError, 'Cannot read'):
                        images.reuse('local', 'remote', 'help', 'key')


if __name__ == '__main__':
    unittest.main()
