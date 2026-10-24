"""Verify project binding and isolated Gradle arguments without starting Gradle."""
import json
from pathlib import Path
import os
import subprocess
import tempfile
import unittest


SUPPORT = Path(__file__).resolve().parents[1]


class JunitLauncherTest(unittest.TestCase):
    def test_direct_runner_binds_the_selected_project_and_preserves_isolation(self):
        for name in ('myscoutee-backend', 'myscoutee-template'):
            with self.subTest(project=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / 'application with spaces'
                (root / 'server').mkdir(parents=True)
                wrapper = root / 'server/gradlew'
                wrapper.write_text('#!/usr/bin/env python3\nimport json,os,sys\n'
                                   'print(json.dumps({"cwd":os.getcwd(),"args":sys.argv[1:]}))\n')
                wrapper.chmod(0o755)
                env = {**os.environ, 'MYSCOUTEE_BACKEND_SOURCE': str(root)}
                for level, task in (('', 'test'), ('local', 'test'), ('standard', 'standardTest'),
                                    ('embedded', 'embeddedTest'), ('integration', 'integrationTest')):
                    command = ['bash', str(SUPPORT / 'tools/run-backend-junit.sh')]
                    if level:
                        command.extend([level, '--tests', '*Example Test', '-Pinput=a b'])
                    result = subprocess.run(command, cwd=directory, env=env, text=True,
                                            capture_output=True, check=True)
                    invocation = json.loads(result.stdout)
                    self.assertEqual(str(root / 'server'), invocation['cwd'])
                    expected = [':projects:profile:' + task, '-PisolatedJUnit=true',
                                '--project-cache-dir', str(root / 'server/build/junit-gradle'),
                                '--no-daemon', '--parallel']
                    if level:
                        expected.extend(['--tests', '*Example Test', '-Pinput=a b'])
                    self.assertEqual(expected, invocation['args'])
                rejected = subprocess.run(command[:2] + ['invalid'], cwd=directory, env=env,
                                          text=True, capture_output=True)
                self.assertEqual(2, rejected.returncode)
                self.assertEqual('', rejected.stdout)


if __name__ == '__main__':
    unittest.main()
