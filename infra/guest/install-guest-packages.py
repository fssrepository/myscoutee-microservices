#!/usr/bin/env python3
"""Install the pinned cloud image's missing packages without an APT connection."""
import hashlib
import json
from pathlib import Path
import subprocess


def install(directory=Path('/var/lib/myscoutee-bootstrap'),
            versions=Path('/etc/myscoutee-lab/versions.json')):
    packages = json.loads(versions.read_text())['guestPackages']
    pending = []
    for name, spec in packages.items():
        if Path(name).name != name or not name.endswith('.deb'):
            raise ValueError('Invalid guest package filename')
        path = directory / name
        if not path.is_file() or path.is_symlink():
            raise ValueError('Required staged guest package is missing: ' + name)
        if hashlib.sha256(path.read_bytes()).hexdigest() != spec['sha256']:
            raise ValueError('Guest package checksum mismatch: ' + name)
        current = subprocess.run(['dpkg-query', '-W', '-f=${Status}\n${Version}', spec['package']],
                                 text=True, capture_output=True)
        lines = current.stdout.splitlines()
        if (current.returncode == 0 and len(lines) == 2 and lines[0] == 'install ok installed'
                and subprocess.run(['dpkg', '--compare-versions', lines[1], 'ge', spec['version']]).returncode == 0):
            continue  # Do not downgrade a guest that has since received security updates.
        pending.append(str(path))
    if pending:
        subprocess.run(['dpkg', '--force-confold', '--install', *pending], check=True)
    print('Guest bootstrap packages verified; no upstream package download used.', flush=True)


if __name__ == '__main__':
    install()
