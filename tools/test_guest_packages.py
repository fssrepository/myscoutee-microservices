"""Offline bootstrap rejects incomplete bytes before any package installation."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

source = Path(__file__).resolve().parents[1] / 'infra/guest/install-guest-packages.py'
spec = importlib.util.spec_from_file_location('guest_packages', source)
guest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guest)


class GuestPackagesTest(unittest.TestCase):
    def fixture(self, root):
        path = root / 'agent.deb'
        path.write_bytes(b'verified package')
        versions = root / 'versions.json'
        versions.write_text(json.dumps({'guestPackages': {'agent.deb': {
            'package': 'qemu-guest-agent', 'version': '1:8.2.2',
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}}}))
        return path, versions

    def test_missing_corrupt_and_symlinked_packages_never_execute_dpkg(self):
        for mode in ('missing', 'corrupt', 'symlink'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                path, versions = self.fixture(root)
                if mode == 'corrupt':
                    path.write_bytes(b'wrong bytes')
                else:
                    path.unlink()
                    if mode == 'symlink':
                        path.symlink_to(versions)
                with patch.object(guest.subprocess, 'run') as run:
                    with self.assertRaises(ValueError):
                        guest.install(root, versions)
                    run.assert_not_called()

    def test_missing_package_is_installed_only_from_local_verified_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, versions = self.fixture(root)
            with patch.object(guest.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '')) as run:
                guest.install(root, versions)
                self.assertEqual(run.call_args.args[0], ['dpkg', '--force-confold', '--install', str(path)])
                self.assertTrue(run.call_args.kwargs['check'])

    def test_newer_security_update_is_not_downgraded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, versions = self.fixture(root)
            with patch.object(guest.subprocess, 'run', side_effect=[
                    subprocess.CompletedProcess([], 0, 'install ok installed\n1:9.0'),
                    subprocess.CompletedProcess([], 0)]) as run:
                guest.install(root, versions)
                self.assertEqual(run.call_count, 2)
                self.assertEqual(run.call_args.args[0], ['dpkg', '--compare-versions', '1:9.0', 'ge', '1:8.2.2'])


if __name__ == '__main__':
    unittest.main()
