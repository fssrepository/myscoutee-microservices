#!/usr/bin/env python3
"""Root-only Ubuntu host lifecycle. Never owns user Terraform state or guest disks.

The existing VPS installer enables boot services and does not track removals;
it cannot implement this developer-host lifecycle. No passwords are stored here.
"""
import argparse
import fcntl
import grp
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import subprocess
import sys
import tempfile

from runtime_config import LAB

# Shared host package inventory, deliberately independent of project VM state.
ROOT = Path('/var/lib/myscoutee-msc121-host')
PACKAGES = ['qemu-system-x86', 'qemu-utils', 'libvirt-daemon-system',
            'libvirt-clients', 'genisoimage']
UNITS = ['libvirtd.service', 'libvirt-guests.service', 'virtlogd.service',
         'virtlockd.service', 'virtlogd.socket', 'virtlogd-admin.socket',
         'virtlockd.socket', 'virtlockd-admin.socket', 'libvirtd.socket',
         'libvirtd-ro.socket', 'libvirtd-admin.socket', 'libvirtd-tcp.socket',
         'libvirtd-tls.socket']
START_COMMAND = ['/usr/bin/systemctl', 'start', 'virtlogd.socket', 'virtlockd.socket', 'libvirtd.socket']
START_POLICY = Path('/etc/sudoers.d/myscoutee-lab-start')


def run(*args, check=True):
    return subprocess.run(args, text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, check=check, env={**os.environ, 'LC_ALL': 'C'})


def execute(*args):
    subprocess.run(args, check=True, env={**os.environ, 'LC_ALL': 'C',
                                        'DEBIAN_FRONTEND': 'noninteractive'})


def save(state):
    temporary = ROOT / 'inventory.new'
    temporary.write_text(json.dumps(state, indent=2) + '\n')
    temporary.replace(ROOT / 'inventory.json')


def packages():
    rows = run('dpkg-query', '-W', '-f=${Package}\t${db:Status-Status}\t${Version}\n').stdout
    return {p: v for p, status, v in (r.split('\t') for r in rows.splitlines()) if status == 'installed'}


def apt_actions(output, verbs):
    return {line.split()[1] for line in output.splitlines() if line.split() and line.split()[0] in verbs}


def check_purge(plan, owned):
    removed = apt_actions(plan, {'Remv', 'Purg'})
    if removed - set(owned) or apt_actions(plan, {'Inst'}):
        raise RuntimeError('Purge would change unowned packages; preserved everything. Review shared dependencies.')


def unit_value(unit, prop):
    return run('systemctl', 'show', unit, '--property=' + prop, '--value', check=False).stdout.strip()


def no_foreign_resources(require_empty=False, allow_default=False):
    # Purging a shared libvirt installation would break even stopped guests.
    if not Path('/usr/bin/virsh').exists():
        return
    for command in (('list', '--all', '--name'), ('net-list', '--all', '--name'),
                    ('pool-list', '--all', '--name')):
        result = run('virsh', '-c', 'qemu:///system', *command)
        names = set(result.stdout.split())
        allowed = set() if require_empty else {LAB['vmName']}
        if allow_default and command[0] == 'net-list':
            allowed.add('default')
        if names - allowed:
            raise RuntimeError('Other libvirt resources exist; host services/packages preserved: ' + ', '.join(sorted(names)))


def disable_owned(state):
    for unit in state['owned_units']:
        path = Path('/etc/systemd/system') / unit
        if path.is_symlink() and os.readlink(path) == '/dev/null':
            execute('systemctl', 'unmask', unit)
        if unit_value(unit, 'LoadState') != 'not-found':
            execute('systemctl', 'disable', '--now', unit)


def install(state, user):
    identity = pwd.getpwnam(user)
    if identity.pw_uid == 0:
        raise RuntimeError('Name the unprivileged lab owner with --user.')
    if not state:
        baseline = packages()
        # Preserving an existing resume-at-boot service would violate the lab contract.
        if unit_value('libvirt-guests.service', 'UnitFileState') in {'enabled', 'enabled-runtime'}:
            raise RuntimeError('Existing libvirt-guests boot resume conflicts with this lab. No host changes made.')
        owned_units = [u for u in UNITS if unit_value(u, 'LoadState') == 'not-found']
        state.update(user=user, baseline=baseline, manual=run('apt-mark', 'showmanual').stdout.split(),
                     owned_units=owned_units, added={}, group_added=False, phase='planned',
                     default_network_preexisting=Path('/etc/libvirt/qemu/networks/default.xml').exists())
        save(state)
    if state['user'] != user:
        raise RuntimeError('Host inventory belongs to another lab user.')
    if state['phase'] == 'installed':
        if set(PACKAGES) - set(packages()):
            raise RuntimeError('Recorded host installation changed; missing packages must be reviewed.')
        for unit in state['owned_units']:
            if unit_value(unit, 'UnitFileState') in {'enabled', 'enabled-runtime'}:
                raise RuntimeError('An owned unit was enabled externally: ' + unit)
        print('Host packages already configured. Services remain explicitly controlled by start/stop.')
        return
    # Persistent masks prevent package postinst and sockets from starting NEW daemons,
    # including after an interrupted install/reboot. Never mask pre-existing units.
    for unit in state['owned_units']:
        execute('systemctl', 'mask', unit)
    execute('apt-get', 'update')
    install_args = ['apt-get', 'install', '--no-install-recommends', '--no-upgrade', *PACKAGES]
    plan = run(*install_args, '--simulate').stdout
    if apt_actions(plan, {'Remv', 'Purg'}):
        raise RuntimeError('Installation would remove existing packages.')
    planned = apt_actions(plan, {'Inst'})
    if planned & set(state['baseline']):
        raise RuntimeError('Installation would upgrade existing packages; no automatic host upgrade is allowed.')
    state['planned_packages'] = sorted(set(state.get('planned_packages', [])) | planned)
    save(state)
    try:
        execute(*install_args, '-y')
    finally:
        current = packages()
        state['added'] = {p: current[p] for p in state['planned_packages'] if p in current and p not in state['baseline']}
        save(state)
    # libvirt installs a default network that this lab never uses. Suppress its
    # autostart before any explicit daemon startup; leave existing definitions alone.
    if not state['default_network_preexisting']:
        definition = Path('/etc/libvirt/qemu/networks/default.xml')
        if definition.exists():
            state['default_network_sha256'] = hashlib.sha256(definition.read_bytes()).hexdigest()
            save(state)
        link = Path('/etc/libvirt/qemu/networks/autostart/default.xml')
        if link.is_symlink():
            if link.resolve() != Path('/etc/libvirt/qemu/networks/default.xml'):
                raise RuntimeError('Changed default-network autostart link; refusing to alter it.')
            link.unlink()
    disable_owned(state)
    # Preserve apt's original manual/auto marks on pre-existing requested packages.
    restore_auto = [p for p in PACKAGES if p in state['baseline'] and p not in state['manual']]
    if restore_auto:
        execute('apt-mark', 'auto', *restore_auto)
    if user not in grp.getgrnam('libvirt').gr_mem and identity.pw_gid != grp.getgrnam('libvirt').gr_gid:
        state['group_added'] = True
        save(state)
        execute('usermod', '-a', '-G', 'libvirt', user)
    state['phase'] = 'installed'
    save(state)
    print('Host configured. No lab daemons or guests enabled at boot. Run host.py start explicitly.')


def start(state):
    if state.get('phase') != 'installed':
        raise RuntimeError('Run host.py install first.')
    for unit in state['owned_units']:
        if unit_value(unit, 'UnitFileState') in {'enabled', 'enabled-runtime'}:
            raise RuntimeError('An owned unit was enabled externally; reconcile before startup: ' + unit)
    execute('systemctl', 'start', 'virtlogd.socket', 'virtlockd.socket', 'libvirtd.socket')
    execute('virsh', '-c', 'qemu:///system', 'uri')


def start_unprivileged():
    if not all(unit_value(unit, 'ActiveState') == 'active' for unit in START_COMMAND[2:]):
        result=run('sudo','-n',*START_COMMAND,check=False)
        if result.returncode:
            raise RuntimeError('Host startup is not authorized. Run host.py authorize-start once with sudo; subsequent starts never prompt for a password.')
    run('virsh','-c','qemu:///system','uri')
    print('Host sockets ready; no interactive sudo request and no boot enablement.')


def authorize_start(state, user):
    if state.get('phase') != 'installed':
        raise RuntimeError('Run host.py install before authorizing startup.')
    if not re.fullmatch(r'[a-z_][a-z0-9_-]*',user) or pwd.getpwnam(user).pw_uid == 0:
        raise RuntimeError('Invalid unprivileged lab owner.')
    content=user+' ALL=(root) NOPASSWD: '+' '.join(START_COMMAND)+'\n'
    if START_POLICY.is_symlink(): raise RuntimeError('Refusing symlinked startup policy.')
    if START_POLICY.exists() and START_POLICY.read_text()!=content:
        if hashlib.sha256(START_POLICY.read_bytes()).hexdigest()!=state.get('startup_policy_sha256'):
            raise RuntimeError('Existing startup policy differs; preserved for review.')
    temporary=ROOT/'startup-policy.new'
    temporary.write_text(content);temporary.chmod(0o440)
    try:
        run('/usr/sbin/visudo','-cf',temporary)
        if not START_POLICY.exists() or START_POLICY.read_text()!=content:
            with tempfile.NamedTemporaryFile(mode='w',dir=START_POLICY.parent,prefix='.myscoutee-start-',delete=False) as output:
                pending=Path(output.name)
                output.write(content)
            try:
                pending.chmod(0o440)
                pending.replace(START_POLICY)
            finally: pending.unlink(missing_ok=True)
        state['startup_policy_sha256']=hashlib.sha256(content.encode()).hexdigest()
        state['startup_user']=user
        save(state)
    finally: temporary.unlink(missing_ok=True)
    print('Authorized only the fixed three-socket startup command for the selected execution user.')


def stop(state):
    if not state:
        return
    if unit_value('libvirtd.socket', 'ActiveState') == 'active' or unit_value('libvirtd.service', 'ActiveState') == 'active':
        no_foreign_resources(allow_default=not state['default_network_preexisting'])
        running = run('virsh', '-c', 'qemu:///system', 'list', '--name').stdout.strip()
        if running:
            raise RuntimeError('Stop the lab VM before stopping host services.')
    for unit in state['owned_units']:
        if unit_value(unit, 'LoadState') != 'not-found':
            execute('systemctl', 'stop', unit)


def purge(state):
    if not state:
        print('No owned host installation remains.')
        return
    if state.get('startup_policy_sha256') and START_POLICY.exists():
        if START_POLICY.is_symlink() or hashlib.sha256(START_POLICY.read_bytes()).hexdigest()!=state['startup_policy_sha256']:
            raise RuntimeError('Startup policy changed externally; preserved for review.')
    if state['phase'] == 'installed':
        start(state)
        # Delete only the package-created UNUSED default network, never an existing one.
        if not state['default_network_preexisting']:
            info = run('virsh', '-c', 'qemu:///system', 'net-info', 'default', check=False)
            if info.returncode == 0:
                if re.search(r'^Active:\s+yes$', info.stdout, re.M):
                    raise RuntimeError('The default network has been used; preserve it and review before purge.')
                definition = Path('/etc/libvirt/qemu/networks/default.xml')
                if not definition.exists() or hashlib.sha256(definition.read_bytes()).hexdigest() != state.get('default_network_sha256'):
                    raise RuntimeError('Default network definition changed; preserved before purge.')
                execute('virsh', '-c', 'qemu:///system', 'net-undefine', 'default')
        no_foreign_resources(require_empty=True)
        stop(state)
        state['phase'] = 'purging'
        save(state)
    current = packages()
    owned = {p: v for p, v in state['added'].items() if p in current}
    changed = [p for p, v in owned.items() if current[p] != v]
    if changed:
        raise RuntimeError('Owned packages changed after installation; preserved: ' + ', '.join(changed))
    if owned:
        plan = run('apt-get', '--simulate', 'purge', *sorted(owned)).stdout
        check_purge(plan, owned)
        execute('apt-get', 'purge', '-y', *sorted(owned))
    if state['group_added']:
        try:
            if state['user'] in grp.getgrnam('libvirt').gr_mem:
                execute('gpasswd', '-d', state['user'], 'libvirt')
        except KeyError:
            pass  # Package purge may already have removed its group.
    for unit in state['owned_units']:
        path = Path('/etc/systemd/system') / unit
        if path.is_symlink() and os.readlink(path) == '/dev/null':
            execute('systemctl', 'unmask', unit)
    if state.get('startup_policy_sha256') and START_POLICY.exists():
        START_POLICY.unlink()
    (ROOT / 'inventory.json').unlink()
    print('Removed tracked host packages and membership; no autoremove or global configuration cleanup.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['install', 'authorize-start', 'start', 'stop', 'purge', 'status'])
    parser.add_argument('--user', default=os.environ.get('SUDO_USER', ''))
    args = parser.parse_args()
    if os.geteuid() != 0 and args.action == 'start':
        start_unprivileged()
        return
    if os.geteuid() != 0:
        parser.error('Root is needed for host packages/services. Run with sudo in a terminal; never put a password in this script.')
    release = Path('/etc/os-release').read_text()
    if 'ID=ubuntu\n' not in release or 'VERSION_ID="22.04"' not in release:
        parser.error('The host lifecycle currently targets the inspected Ubuntu 22.04 host only.')
    os.umask(0o077)
    if ROOT.is_symlink():
        raise RuntimeError('Refusing symlinked root inventory.')
    ROOT.mkdir(mode=0o700, exist_ok=True)
    if ROOT.stat().st_uid != 0 or ROOT.stat().st_mode & 0o022:
        raise RuntimeError('Host inventory must be root-owned and not writable by others.')
    with (ROOT / 'lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        file = ROOT / 'inventory.json'
        state = json.loads(file.read_text()) if file.exists() else {}
        if args.action == 'install':
            install(state, args.user)
        elif args.action == 'authorize-start':
            authorize_start(state, args.user)
        elif args.action == 'start':
            start(state)
        elif args.action == 'stop':
            stop(state)
        elif args.action == 'purge':
            purge(state)
        else:
            print(json.dumps(state, indent=2))
    if not (ROOT / 'inventory.json').exists():
        # Retain the tiny root-owned lock directory to avoid concurrent lock replacement.
        print('Host inventory is empty; only the lifecycle lock remains at ' + str(ROOT))


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, subprocess.CalledProcessError, KeyError) as error:
        sys.exit(str(error))
