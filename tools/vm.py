#!/usr/bin/env python3
"""Explicit, private-state Terraform/KVM lab lifecycle; never enables host boot.

Run host.py install/start first. No host sudo, package installation or user-wide
SSH/kube configuration is hidden in these operations.
"""
import argparse
import base64
import fcntl
import grp
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import select
import shlex
import shutil
import subprocess
import sys
import termios
import time
import urllib.request
import xml.etree.ElementTree as ET

from runtime_config import SHELL, SUPPORT, BUILD, BACKEND, CONFIG, LAB, STATE
import terraform_state
VM = STATE / 'vm'
NAME = LAB['vmName']
CIDR = LAB['networkCidr']
IP = str(ipaddress.ip_network(CIDR)[10])
MEMORY_MIB = LAB['memoryMiB']
OBSERVE_INTERVAL_SECONDS = 1
ADDRESSES = {'libvirt_pool.lab', 'libvirt_network.lab', 'libvirt_volume.base',
             'libvirt_volume.system', 'libvirt_volume.bootstrap',
             'libvirt_cloudinit_disk.bootstrap', 'libvirt_domain.lab'}
PRIVATE_FILES = {'ubuntu-noble.qcow2', 'ubuntu-noble.download', 'id_ed25519',
                 'id_ed25519.pub', 'known_hosts', 'known_hosts.old', 'kubeconfig',
                 'inputs.tfvars.json', 'terraform.tfstate', 'terraform.tfstate.backup',
                 'start.tfplan', 'destroy.tfplan', 'workloads.json', 'workload-secrets.json',
                 'forward.env', 'mongo-replicaset.json', 'mongo-statefulset.json'}


def run(*args, check=True, timeout=60):
    return subprocess.run([str(a) for a in args], text=True, capture_output=True,
                          check=check, timeout=timeout, env={**os.environ, 'LC_ALL': 'C'})


def private_directory(path):
    # Reject symlinks in ancestors as well as at the leaf; no cleanup can escape.
    if path != path.resolve():
        raise RuntimeError('Refusing symlinked private state: ' + str(path))
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    if path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise RuntimeError('Private lab state must be owned by this user with mode 0700: ' + str(path))


def virsh(*args, check=True):
    return run('virsh', '-c', 'qemu:///system', *args, check=check)


def terraform(*args, capture=False):
    command = [str(SUPPORT / 'tools/terraform.sh'), 'run', *map(str, args)]
    if capture:
        return run(*command, timeout=180).stdout
    subprocess.run(command, check=True)


def init():
    arguments=[] if terraform_state.remote() else ['-backend-config=path=' + str(VM / 'terraform.tfstate')]
    terraform('init', '-input=false', '-reconfigure', '-lockfile=readonly', *arguments)


def state_resources():
    state = VM / 'terraform.tfstate'
    raw = terraform_state.read_state(state)
    resources = {}
    for resource in raw.get('resources', []):
        address = resource['type'] + '.' + resource['name']
        if resource.get('module') or resource.get('mode') != 'managed' or address not in ADDRESSES:
            raise RuntimeError('Unexpected resource in lab state: ' + address)
        instances = resource.get('instances', [])
        if len(instances) != 1:
            raise RuntimeError('Unexpected/deposed state instances; review interrupted operation: ' + address)
        resources[address] = instances[0]['attributes']
    return resources


def ownership():
    virsh('uri')  # Connection failure is not evidence that resources are absent.
    resources = state_resources()
    for kind, prefix in (('domain', 'dom'), ('network', 'net-'), ('pool', 'pool-')):
        command = prefix + 'uuid'
        current = virsh(command, NAME, check=False)
        if current.returncode == 0:
            recorded = resources.get('libvirt_' + kind + '.lab', {})
            if not recorded or current.stdout.strip() != recorded.get('uuid', recorded.get('id')):
                raise RuntimeError('Name/UUID collision or lost state; refusing to adopt/delete: ' + NAME + ' ' + kind)
    return resources


def check_plan(plan, destroy=False):
    for change in plan.get('resource_changes', []):
        if change['address'] not in ADDRESSES:
            raise RuntimeError('Plan contains an unowned resource: ' + change['address'])
        actions = change['change']['actions']
        if 'delete' in actions and not destroy:
            raise RuntimeError('Start would replace/delete persistent lab state. Review the plan; use destroy explicitly to reset.')
        if destroy and actions not in (['delete'], ['no-op']):
            raise RuntimeError('Destroy plan contains unexpected mutations.')


def overlap(routes, owned_bridge=False):
    desired = ipaddress.ip_network(CIDR)
    for route in routes:
        destination = route.get('dst', 'default')
        if destination == 'default':
            continue
        network = ipaddress.ip_network(destination, strict=False)
        if owned_bridge and route.get('dev') == LAB['bridge'] and network.subnet_of(desired):
            continue
        if desired.overlaps(network):
            raise RuntimeError('Lab subnet overlaps an existing route/network: ' + destination)


def require_kvm():
    # libvirt's QEMU process opens /dev/kvm. The CI management account only
    # needs its existing authenticated libvirt connection, not raw device access.
    capabilities = ET.fromstring(virsh('domcapabilities', '--virttype', 'kvm', '--arch', 'x86_64').stdout)
    if capabilities.findtext('domain') != 'kvm' or capabilities.findtext('arch') != 'x86_64':
        raise RuntimeError('The system libvirt connection does not support the required x86_64 KVM guest.')


def preflight(resources):
    require_kvm()
    overlap(json.loads(run('ip', '-json', '-4', 'route', 'show', 'table', 'all').stdout),
            owned_bridge='libvirt_network.lab' in resources)
    for name in virsh('net-list', '--all', '--name').stdout.split():
        if name == NAME:
            continue
        xml = ET.fromstring(virsh('net-dumpxml', name).stdout)
        for address in xml.findall('ip'):
            value = address.get('address', '')
            if ':' not in value and value:
                overlap([{'dst': str(ipaddress.ip_network(value + '/' + (address.get('prefix') or address.get('netmask')), strict=False))}])
    if shutil.which('docker'):
        networks = run('docker', 'network', 'ls', '-q').stdout.split()
        if networks:
            for network in json.loads(run('docker', 'network', 'inspect', *networks).stdout):
                for config in network.get('IPAM', {}).get('Config', []) or []:
                    if config.get('Subnet') and ':' not in config['Subnet']:
                        overlap([{'dst': config['Subnet']}])
        containers = run('docker', 'ps', '--format', '{{.Names}} {{.Image}}').stdout.lower()
        if any(word in containers for word in ('-dev', ':dev', 'sync', 'watch')):
            raise RuntimeError(f'Stop the existing dev/watch owner before starting the {MEMORY_MIB // 1024}-GiB lab.')
    processes = run('ps', '-eo', 'args=').stdout
    if any(word in processes for word in ('org.gradle.launcher.daemon', 'bootRun', 'frontend_auto_watch', 'ng serve')):
        raise RuntimeError('A dev/build owner is still running. Run builds and VM startup serially.')
    memory = dict(line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())
    available = int(memory['MemAvailable'].split()[0]) // 1024
    existing_disks = all('libvirt_volume.' + name in resources for name in ('base', 'system', 'bootstrap'))
    if not existing_disks and shutil.disk_usage('/var/lib/libvirt/images').free < 50 * 1024 ** 3:
        raise RuntimeError('Need 50 GiB free for the sparse 40-GiB guest and image staging.')
    print(f'Preflight passed: {available} MiB available; no subnet collision; KVM ready.')


def prepare():
    versions = json.loads((SUPPORT / 'infra/versions.json').read_text())
    artifact = versions['ubuntu']
    image = VM / 'ubuntu-noble.qcow2'
    def digest(path):
        with path.open('rb') as stream:
            return hashlib.file_digest(stream, 'sha256').hexdigest() if hasattr(hashlib, 'file_digest') else stream_hash(stream)
    if not image.exists() or digest(image) != artifact['sha256']:
        if os.environ.get('MYSCOUTEE_INFRA_OFFLINE') == 'true':
            raise RuntimeError('Pinned Ubuntu image is missing. Run the optional manual infrastructure preparation job.')
        temporary = image.with_suffix('.download')
        with urllib.request.urlopen(artifact['url'], timeout=60) as source, temporary.open('wb') as out:
            shutil.copyfileobj(source, out, 1024 * 1024)
        if digest(temporary) != artifact['sha256']:
            temporary.unlink()
            raise RuntimeError('Ubuntu image checksum mismatch.')
        temporary.replace(image)
    # The pinned cloud image already supplies curl, CA certificates and Python.
    # Only its missing guest-agent packages are added; cloud-init never runs apt.
    packages = STATE / 'tools/guest-packages'
    packages.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name, artifact in versions['guestPackages'].items():
        package = packages / name
        if package.is_file() and digest(package) == artifact['sha256']:
            continue
        if os.environ.get('MYSCOUTEE_INFRA_OFFLINE') == 'true':
            raise RuntimeError('Pinned guest package is missing or corrupt: ' + name + '. Run optional infrastructure preparation.')
        temporary = package.with_suffix('.download')
        try:
            with urllib.request.urlopen(artifact['url'], timeout=60) as source, temporary.open('wb') as out:
                shutil.copyfileobj(source, out)
            if digest(temporary) != artifact['sha256']:
                raise RuntimeError('Guest package checksum mismatch: ' + name)
            temporary.replace(package)
        finally:
            temporary.unlink(missing_ok=True)
    key = VM / 'id_ed25519'
    if not key.exists():
        if state_resources() or (VM / 'known_hosts').exists():
            raise RuntimeError('Existing VM lost its key; do not silently replace its access identity.')
        run('ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', NAME, '-f', key)
    # Rebuild public material from the private key, never rotate on an ordinary start.
    key.with_suffix('.pub').write_text(run('ssh-keygen', '-y', '-f', key).stdout)
    variables = dict(verified_cloud_image=str(image), guest_package_directory=str(packages), ssh_public_key_file=str(key.with_suffix('.pub')),
                     network_cidr=CIDR, memory_mib=MEMORY_MIB, running=True,
                     lab_name=NAME, bridge_name=LAB['bridge'], mac_address=LAB['macAddress'])
    if os.environ.get('MYSCOUTEE_INFRA_OFFLINE') == 'true':
        variables['k3s_host_staged'] = True
    (VM / 'inputs.tfvars.json').write_text(json.dumps(variables, indent=2) + '\n')


def stream_hash(stream):
    digest = hashlib.sha256()
    for block in iter(lambda: stream.read(1024 * 1024), b''):
        digest.update(block)
    return digest.hexdigest()


def no_autostart(resources):
    for kind, command in (('domain', 'autostart'), ('network', 'net-autostart'), ('pool', 'pool-autostart')):
        if 'libvirt_' + kind + '.lab' in resources:
            listing = {'domain': 'list', 'network': 'net-list', 'pool': 'pool-list'}[kind]
            if NAME in virsh(listing, '--all', '--name').stdout.split():
                virsh(command, NAME, '--disable')
    # libvirt-guests can resume guests independently of their autostart flag.
    result = run('systemctl', 'is-enabled', 'libvirt-guests.service', check=False)
    if result.stdout.strip() in {'enabled', 'enabled-runtime'}:
        raise RuntimeError('Host guest save/resume was enabled. Stop and reconcile before using this lab.')


def ssh_command(*command, interactive=False):
    return ['ssh', '-F', '/dev/null', '-i', str(VM / 'id_ed25519'), '-o', 'BatchMode=yes',
            '-o', 'IdentitiesOnly=yes', '-o', 'ConnectTimeout=5',
            '-o', 'StrictHostKeyChecking=yes', '-o', 'UserKnownHostsFile=' + str(VM / 'known_hosts'),
            *(['-t'] if interactive else []), 'myscoutee@' + IP, *command]


def ssh(*command, timeout=30, check=True):
    return run(*ssh_command(*command), timeout=timeout, check=check)


class GuestObserver:
    """One bounded, read-only SSH stream for the lifetime of this terminal view."""
    def __init__(self):
        self.process = None
        self.watch = True

    def close(self):
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=3)
            self.process.stdout.close()
            self.process = None

    def read(self):
        if self.process is None or self.process.poll() is not None:
            self.close()
            command = 'sudo python3 -u -' + (' --watch' if self.watch else '')
            self.process = subprocess.Popen(ssh_command(command), stdin=subprocess.PIPE,
                                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            self.process.stdin.write((SUPPORT / 'infra/guest/observe.py').read_text())
            self.process.stdin.close()
        if not select.select([self.process.stdout], [], [], 25)[0]:
            self.close()
            raise RuntimeError('Guest observation timed out; next snapshot will reconnect.')
        line = self.process.stdout.readline()
        if not line:
            self.close()
            raise RuntimeError('Guest observation disconnected; next snapshot will reconnect.')
        try:
            frame = json.loads(line)
        except ValueError as error:
            self.close()
            raise RuntimeError('Invalid guest observation; next snapshot will reconnect.') from error
        return frame.get('snapshot', frame.get('error', 'Guest observation unavailable.'))


_guest_observer = GuestObserver()
_vm_observation = None
_monitoring_observation = None
_monitoring_cpu = {}


def monitoring_snapshot():
    """Read cgroup counters; Docker discovery is separate from the one-second rates."""
    global _monitoring_observation
    now = time.monotonic()
    if _monitoring_observation is None or now - _monitoring_observation[0] >= 5:
        ids = run('docker', 'ps', '-q', '--filter', 'label=com.docker.compose.project=' + LAB['monitoringProject']).stdout.split()
        entries = []
        if ids:
            for entry in json.loads(run('docker', 'inspect', *ids).stdout):
                if not entry['State']['Running']:
                    continue
                pid = entry['State']['Pid']
                group = next(line.split(':', 2)[2] for line in Path(f'/proc/{pid}/cgroup').read_text().splitlines()
                             if line.startswith('0::'))
                entries.append(((entry['Id'], entry['State']['StartedAt']), entry['Name'].removeprefix('/' + LAB['monitoringProject'] + '-'),
                                Path('/sys/fs/cgroup') / group.lstrip('/')))
        _monitoring_observation = (now, entries)
    entries = _monitoring_observation[1]
    if not entries:
        _monitoring_cpu.clear()
        return ['\nHost monitoring: stopped.']
    lines = ['\nHOST DOCKER / MONITORING', '%-24s %8s   %s' % ('CONTAINER', 'CPU %', 'RAM / LIMIT')]
    active = {entry[0] for entry in entries}
    for key in list(_monitoring_cpu):
        if key not in active:
            del _monitoring_cpu[key]
    for key, name, root in entries:
        try:
            cpu = int(dict(line.split() for line in (root / 'cpu.stat').read_text().splitlines())['usage_usec'])
            memory = int((root / 'memory.current').read_text())
            inactive = int(dict(line.split() for line in (root / 'memory.stat').read_text().splitlines()).get('inactive_file', 0))
            limit = (root / 'memory.max').read_text().strip()
        except FileNotFoundError:
            _monitoring_observation = None
            lines.append(name + ': stopped/replaced; refreshing inventory.')
            continue
        at = time.monotonic()
        previous = _monitoring_cpu.get(key)
        _monitoring_cpu[key] = (at, cpu)
        rate = '%.2f%%' % (max(0, cpu - previous[1]) / max(0.000001, at - previous[0]) / 10000) if previous else '-'
        maximum = 'unlimited' if limit == 'max' else '%.0f MiB' % (int(limit) / 1024 ** 2)
        lines.append('%-24s %8s   %.1f MiB / %s' % (name, rate, max(0, memory - inactive) / 1024 ** 2, maximum))
    return lines


def observe_snapshot():
    """Collect first, then repaint once; slow SSH must not blank the screen."""
    global _vm_observation
    lines = [LAB['displayName'] + ' | ' + time.strftime('%Y-%m-%d %H:%M:%S %Z'),
             f"App :{CONFIG['localPort']}  |  Grafana :{LAB['grafanaPort']}  |  Prometheus :{LAB['prometheusPort']}  (localhost)",
             '']
    memory = dict(line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())
    lines.append('HOST / KVM   Available RAM: %.1f GiB' % (int(memory['MemAvailable'].split()[0]) / 1024 ** 2))
    try:
        with (VM / 'lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if _vm_observation is None or time.monotonic() - _vm_observation[0] >= 30:
                resources = ownership()
                info = virsh('dominfo', NAME).stdout if 'libvirt_domain.lab' in resources else ''
                _vm_observation = (time.monotonic(), resources, info)
            _, resources, info = _vm_observation
            if 'libvirt_domain.lab' not in resources:
                _guest_observer.close()
                lines.append('VM: absent. Explicit startup: python3 ../myscoutee-microservices/tools/vm.py start')
            else:
                facts = dict(line.split(':', 1) for line in info.splitlines() if ':' in line)
                lines.append('VM           %s | %s vCPU | %.1f GiB assigned | autostart: %s' % (
                    facts.get('State', '?').strip(), facts.get('CPU(s)', '?').strip(),
                    int(facts['Max memory'].split()[0]) / 1024 ** 2, facts.get('Autostart', '?').strip()))
        # Waiting for the next streamed sample must not hold the lifecycle lock.
        if 'libvirt_domain.lab' in resources and facts.get('State', '').strip() == 'running':
            lines.append('\n' + _guest_observer.read())
        else:
            _guest_observer.close()
    except BlockingIOError:
        # No guest frames are consumed while a lifecycle operation holds this
        # lock. Close the producer too, or its pipe replays old startup samples
        # at one frame/second after the deployment has already become ready.
        _guest_observer.close()
        _vm_observation = None
        lines.append('VM lifecycle operation in progress; next snapshot will retry.')
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        lines.append('VM observation unavailable: ' + str(error))
    try:
        lines.extend(monitoring_snapshot())
    except (OSError, subprocess.SubprocessError) as error:
        lines.append('Monitoring observation unavailable: ' + str(error))
    return '\n'.join(lines)


def terminal_frame(snapshot, columns, rows):
    # Leave the bottom row and final column unused: neither wrapping nor a final
    # newline may scroll the terminal. --once remains an untruncated text report.
    clean = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', snapshot)
    lines = clean.splitlines()
    width = max(1, columns - 1)
    height = max(1, rows - 2)
    if len(lines) > height:
        lines = lines[:max(0, height - 1)] + ['[Resize for more rows; vm.py observe --once shows the full report.]']
    footer = f'Refresh: {OBSERVE_INTERVAL_SECONDS}s; inventory: 5s | Ctrl+C closes only this view.'
    return '\n'.join(line[:width] for line in [*lines, footer])


def observe(once=False):
    """Read-only view in one alternate screen; never hold a lock between reads."""
    interactive = sys.stdout.isatty() and not once
    input_mode = None
    _guest_observer.watch = not once
    try:
        if once:
            try:
                monitoring_snapshot()  # Prime CPU counters before the bounded guest sample.
            except (OSError, subprocess.SubprocessError):
                pass  # The normal frame reports monitoring availability.
        if interactive:
            if sys.stdin.isatty():
                input_mode = termios.tcgetattr(sys.stdin.fileno())
                quiet = input_mode.copy()
                quiet[3] &= ~(termios.ECHO | termios.ECHONL)
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSAFLUSH, quiet)
            # Save/disable alternate-screen wheel-to-arrow translation. Suppress
            # input echo too, so terminals without that mode cannot print ^[[A/B.
            sys.stdout.write('\033[?1007s\033[?1007l\033[?1049h\033[?25l')
            sys.stdout.flush()
        while True:
            started = time.monotonic()
            snapshot = observe_snapshot()
            if interactive:
                size = shutil.get_terminal_size()
                frame = terminal_frame(snapshot, size.columns, size.lines)
                # Erase every line's tail as well as the unused bottom area.
                # Moving home alone leaves old text when a new line is shorter.
                sys.stdout.write('\033[H' + '\033[K\n'.join(frame.splitlines()) + '\033[K\033[J')
                sys.stdout.flush()
            else:
                print(snapshot, flush=True)
            if once:
                return
            time.sleep(max(0, OBSERVE_INTERVAL_SECONDS - (time.monotonic() - started)))
    finally:
        _guest_observer.close()
        if interactive:
            sys.stdout.write('\033[?25h\033[?1049l\033[?1007r')
            sys.stdout.flush()
            if input_mode is not None:
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSAFLUSH, input_mode)


def pin_guest_key():
    # Obtain public identity over the already authenticated hypervisor connection,
    # not from an unauthenticated ssh-keyscan on the network.
    request = {'execute': 'guest-exec', 'arguments': {'path': '/usr/bin/cat',
               'arg': ['/etc/ssh/ssh_host_ed25519_key.pub'], 'capture-output': True}}
    result = virsh('qemu-agent-command', NAME, json.dumps(request), check=False)
    if result.returncode != 0:
        return False  # Agent is installed during cloud-init.
    pid = json.loads(result.stdout).get('return', {}).get('pid')
    if pid is None:
        return False
    for attempt in range(20):
        result = virsh('qemu-agent-command', NAME, json.dumps({'execute': 'guest-exec-status', 'arguments': {'pid': pid}}))
        status = json.loads(result.stdout).get('return', {})
        if status.get('exited'):
            if status.get('exitcode') != 0:
                return False
            key = base64.b64decode(status['out-data'], validate=True).decode().split()
            if len(key) < 2 or key[0] != 'ssh-ed25519' or not re.fullmatch(r'[A-Za-z0-9+/=]+', key[1]):
                raise RuntimeError('Guest agent returned an invalid SSH public key.')
            line = IP + ' ' + ' '.join(key[:2])
            known = VM / 'known_hosts'
            if known.exists() and line not in known.read_text().splitlines():
                raise RuntimeError('Recorded guest host key changed; refusing to replace its identity.')
            known.write_text(line + '\n')
            return True
        time.sleep(0.25)
    return False


def stage_k3s():
    """Copy pinned GitLab inputs over the existing authenticated SSH transport."""
    versions=json.loads((SUPPORT/'infra/versions.json').read_text())
    files=[('k3s','/usr/local/bin/k3s','0755'),
           ('k3sAirgap','/var/lib/rancher/k3s/agent/images/k3s-airgap-images-amd64.tar.zst','0644')]
    for key,destination,mode in files:
        local=STATE/'tools'/Path(destination).name
        expected=versions[key]['sha256']
        if not local.is_file(): raise RuntimeError('Missing staged '+key+'; run optional infrastructure preparation.')
        with local.open('rb') as stream:
            if stream_hash(stream)!=expected: raise RuntimeError('Staged '+key+' checksum mismatch.')
        current=ssh('sudo sha256sum '+shlex.quote(destination),check=False)
        if current.returncode==0 and current.stdout.split()[0]==expected: continue
        directory=str(Path(destination).parent)
        temporary=destination+'.incoming'
        ssh('sudo install -d -m 0755 '+shlex.quote(directory))
        with local.open('rb') as stream:
            subprocess.run(ssh_command('sudo sh -c '+shlex.quote('umask 077; cat > '+shlex.quote(temporary))),
                           stdin=stream,check=True,timeout=300)
        if ssh('sudo sha256sum '+shlex.quote(temporary)).stdout.split()[0]!=expected:
            raise RuntimeError('Guest '+key+' checksum mismatch; refusing installation.')
        ssh('sudo chmod '+mode+' '+shlex.quote(temporary)+' && sudo mv '+shlex.quote(temporary)+' '+shlex.quote(destination))
    print('Pinned k3s binary and offline images delivered from the local registry cache.')


def ready():
    deadline = time.monotonic() + 600
    staged=False
    progress=0
    phase='QEMU guest agent (guest bootstrap and OS package installation)'
    while time.monotonic() < deadline:
        if time.monotonic() >= progress:
            print('Waiting for '+phase+'; no interactive password input is used.',flush=True)
            progress=time.monotonic()+30
        if not pin_guest_key():
            time.sleep(5)
            continue
        phase='cloud-init completion and Kubernetes API readiness'
        if os.environ.get('MYSCOUTEE_INFRA_OFFLINE') == 'true' and not staged:
            print('Delivering pinned k3s files over authenticated SSH.',flush=True)
            stage_k3s()
            staged=True
        result = ssh('sudo cloud-init status --format json', check=False)
        if result.returncode in (0, 1, 2):
            status = json.loads(result.stdout)
            if status.get('status') == 'error' or status.get('errors'):
                raise RuntimeError('Guest cloud-init failed; inspect with vm.py ssh. VM retained for diagnosis.')
            # cloud-init uses exit 2 for completed runs with recoverable
            # warnings. Still require successful Kubernetes readiness.
            if status.get('status') == 'done' and result.returncode in (0, 2):
                if result.returncode == 2:
                    print('Guest cloud-init completed with warnings; inspect cloud-init status for details.',flush=True)
                health = ssh('sudo k3s kubectl get --raw=/readyz', check=False)
                if health.returncode == 0 and health.stdout.strip() == 'ok':
                    print('Guest cloud-init completed and k3s API is ready.')
                    return
        time.sleep(5)
    raise RuntimeError('Guest not ready in 10 minutes; waiting for '+phase+'. VM retained for diagnosis; no blind recreation.')


def kubeconfig():
    content = ssh('sudo cat /etc/rancher/k3s/k3s.yaml').stdout
    if 'server: https://127.0.0.1:6443' not in content:
        raise RuntimeError('Unexpected guest kubeconfig server; no user config changed.')
    (VM / 'kubeconfig').write_text(content.replace('server: https://127.0.0.1:6443', 'server: https://' + IP + ':6443'))
    print('Private kubeconfig saved at ' + str(VM / 'kubeconfig'))


def is_ready(resources):
    """Inspect the owned guest without starting or provisioning anything."""
    if 'libvirt_domain.lab' not in resources:
        return False
    if virsh('domstate', NAME).stdout.strip() != 'running' or not (VM / 'known_hosts').is_file():
        return False
    health = ssh('sudo k3s kubectl get --raw=/readyz', check=False)
    return health.returncode == 0 and health.stdout.strip() == 'ok'


def require_ready():
    if not is_ready(ownership()):
        raise RuntimeError('KVM infrastructure is not ready. Check or run the optional manual infra:start job, then retry deployment.')
    print('KVM guest and Kubernetes API are ready; no startup or provisioning needed.')


def start():
    resources = ownership()
    preflight(resources)
    no_autostart(resources)
    prepare()
    init()
    # Runtime activation is explicitly requested here, never at host boot.
    for kind, prefix in (('pool', 'pool-'), ('network', 'net-')):
        if 'libvirt_' + kind + '.lab' in resources:
            info = virsh(prefix + 'info', NAME).stdout
            if (kind == 'pool' and 'inactive' in info) or (kind == 'network' and re.search(r'^Active:\s+no$', info, re.M)):
                virsh(prefix + 'start', NAME)
    plan = VM / 'start.tfplan'
    terraform('plan', '-input=false', '-var-file=' + str(VM / 'inputs.tfvars.json'), '-out=' + str(plan))
    changes = json.loads(terraform('show', '-json', plan, capture=True))
    check_plan(changes)
    try:
        if any(change['address'] == 'libvirt_domain.lab' and change['change']['actions'] == ['update']
               for change in changes.get('resource_changes', [])) and virsh('domstate', NAME).stdout.strip() == 'running':
            # The provider otherwise force-stops after its short shutdown wait.
            # Reuse the owner's graceful stop; a timeout must retain the guest.
            stop(keep_infrastructure=True)
        terraform('apply', '-input=false', plan)
    finally:
        no_autostart(ownership())
        plan.unlink(missing_ok=True)
    ready()
    kubeconfig()


def stop(keep_infrastructure=False):
    resources = ownership()
    # Reuse the workload transport's exact endpoint/process ownership checks.
    # No lifecycle lock is reacquired by this action, including after VM failure.
    run(sys.executable, SUPPORT / 'tools/workloads.py', 'tunnel-stop')
    no_autostart(resources)
    if 'libvirt_domain.lab' in resources:
        result = virsh('domstate', NAME, check=False)
        if result.returncode == 0 and result.stdout.strip() != 'shut off':
            virsh('shutdown', NAME)
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                if virsh('domstate', NAME).stdout.strip() == 'shut off':
                    break
                time.sleep(2)
            else:
                raise RuntimeError('Guest did not shut down; retained it instead of forcing power loss.')
    if keep_infrastructure:
        return
    for kind, prefix in (('network', 'net-'), ('pool', 'pool-')):
        if 'libvirt_' + kind + '.lab' in resources:
            info = virsh(prefix + 'info', NAME, check=False)
            if info.returncode == 0:
                active = 'running' in info.stdout if kind == 'pool' else 'yes' in next((l for l in info.stdout.splitlines() if l.startswith('Active:')), '')
                if active:
                    virsh(prefix + 'destroy', NAME)  # Deactivates only; definitions/disks survive.
    print('Lab stopped; data retained; no autostart.')


def mongo_backup_cleanup():
    folder = VM / 'backups'
    if not folder.exists():
        return []
    if folder.is_symlink() or not folder.is_dir():
        raise RuntimeError('Unexpected Mongo backup directory; preserved')
    expected = []
    for purpose in ('replicaset', 'statefulset'):
        receipt = VM / ('mongo-' + purpose + '.json')
        if not receipt.exists():
            continue
        if not receipt.is_file() or receipt.is_symlink():
            raise RuntimeError('Mongo backup ownership receipt missing; preserved')
        state = json.loads(receipt.read_text())
        relative = state.get('backup', '')
        if not relative:
            continue  # Fresh replica initialization has no conversion archive.
        if not re.fullmatch(r'backups/mongo-before-' + purpose + r'-\d{8}T\d{6}Z\.archive\.gz', relative):
            raise RuntimeError('Unexpected Mongo backup path; preserved')
        archive = VM / relative
        if archive.exists():
            if archive.is_symlink() or not archive.is_file():
                raise RuntimeError('Unexpected Mongo backup entries; preserved')
            if hashlib.sha256(archive.read_bytes()).hexdigest() != state.get('sha256'):
                raise RuntimeError('Mongo backup changed; preserved')
            expected.append(archive)
    if set(folder.iterdir()) != set(expected):
        raise RuntimeError('Unexpected Mongo backup entries; preserved')
    return expected


def destroy():
    backups = mongo_backup_cleanup()  # Verify ownership before touching the VM.
    resources = ownership()
    if not resources:
        run(sys.executable, SUPPORT / 'tools/workloads.py', 'tunnel-stop')
    if resources:
        stop()
        init()
        # Volume deletion needs its owned pool active even while the VM is off.
        if 'libvirt_pool.lab' in resources:
            virsh('pool-start', NAME, check=False)
        plan = VM / 'destroy.tfplan'
        terraform('plan', '-destroy', '-input=false', '-var-file=' + str(VM / 'inputs.tfvars.json'), '-out=' + str(plan))
        check_plan(json.loads(terraform('show', '-json', plan, capture=True)), destroy=True)
        terraform('apply', '-input=false', plan)
        if ownership():
            raise RuntimeError('Terraform still records resources; state retained for recovery.')
    # Never remove state before Terraform has confirmed that resources are gone.
    for archive in backups:
        archive.unlink()
    if (VM / 'backups').exists():
        (VM / 'backups').rmdir()
    for child in VM.iterdir():
        if child.name == 'lock':
            continue
        if child.name not in PRIVATE_FILES or child.is_symlink() or child.is_dir():
            raise RuntimeError('Unexpected entry in VM private state; preserved: ' + str(child))
        child.unlink()
    print('VM, network, pool, owned disks and private guest material removed.')


def require_libvirt_access():
    try:
        group = grp.getgrnam('libvirt')
        if group.gr_gid not in {*os.getgroups(), os.getgid()}:
            import pwd
            if pwd.getpwuid(os.getuid()).pw_name not in group.gr_mem:
                raise RuntimeError('Run setup-host.sh to configure libvirt access first.')
            # Apply recorded supplementary membership without a desktop logout.
            os.execvp('sg', ['sg', 'libvirt', '-c', shlex.join([sys.executable, str(Path(sys.argv[0]).resolve()), *sys.argv[1:]])])
    except KeyError:
        raise RuntimeError('libvirt is not installed; run setup-host.sh in a terminal first.') from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['preflight', 'prepare', 'start', 'ready', 'stop', 'destroy', 'status', 'kubeconfig', 'ssh', 'observe', 'console'])
    parser.add_argument('--once', action='store_true', help='Print one observe snapshot and exit.')
    args = parser.parse_args()
    if args.once and args.action != 'observe':
        parser.error('--once applies only to observe.')
    if os.geteuid() == 0:
        parser.error('Run as the lab user, not root. Host operations use host.py separately.')
    if args.action != 'prepare':
        require_libvirt_access()
    os.umask(0o077)
    private_directory(STATE)
    private_directory(VM)
    if args.action == 'observe':
        observe(once=args.once)
        return
    with (VM / 'lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.action == 'preflight':
            preflight(ownership())
        elif args.action == 'prepare':
            prepare()
        elif args.action == 'start':
            start()
        elif args.action == 'ready':
            require_ready()
        elif args.action == 'stop':
            stop()
        elif args.action == 'destroy':
            destroy()
        elif args.action == 'status':
            resources = ownership()
            print('Recorded resources:', ', '.join(resources) or 'none')
            if 'libvirt_domain.lab' in resources:
                print(virsh('dominfo', NAME).stdout)
        elif args.action == 'kubeconfig':
            ownership()
            kubeconfig()
        elif args.action == 'ssh':
            ownership()
            print(ssh('sudo cloud-init status --long; sudo systemctl status k3s --no-pager', check=False).stdout)
        else:
            if 'libvirt_domain.lab' not in ownership() or virsh('domstate', NAME).stdout.strip() != 'running':
                raise RuntimeError('The owned guest must be running before opening its console.')
    if args.action == 'console':
        # Release the lifecycle lock before handing control to the user.
        os.execvp('ssh', ssh_command('bash -l', interactive=True))


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        sys.exit(str(error))
    except KeyboardInterrupt:
        print('\nCommand interrupted; existing lab state is retained.')
