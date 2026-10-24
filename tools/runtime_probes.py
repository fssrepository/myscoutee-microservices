"""Existing owned-service transport capability and STUN probes."""
import base64
import json
import os
from pathlib import Path
import secrets
import socket
import struct
import subprocess
import time
import workloads as w


def transport_scopes():
    """Read deployed Secret references and keys without exposing their values."""
    expected = w.transport_credentials_by_service(w.SERVICES,
            Path(os.environ.get('MYSCOUTEE_BACKEND_SOURCE', w.BACKEND)))
    credentials = json.loads(w.SECRET_FILE.read_text())
    secrets_by_name = {}
    for name, service in w.SERVICES.items():
        deployment = w.kube_json('-n', w.NAMESPACE, 'get', 'deployment', name)
        references = [item['secretRef']['name']
                      for container in deployment['spec']['template']['spec']['containers']
                      for item in container.get('envFrom', []) if 'secretRef' in item]
        received = {}
        for reference in references:
            if reference not in secrets_by_name:
                secrets_by_name[reference] = w.kube_json('-n', w.NAMESPACE, 'get', 'secret', reference).get('data', {})
            for key, value in secrets_by_name[reference].items():
                if key.startswith('MYSCOUTEE_REMOTE_') or key == 'MYSCOUTEE_DELEGATION_PRIVATE_KEY':
                    received[key] = value
        private_key = received.pop('MYSCOUTEE_DELEGATION_PRIVATE_KEY', None)
        if (private_key is not None) != ('accounts' in service['domains']):
            raise RuntimeError('Incorrect actor-signing key scope: ' + name)
        if set(received) != set(expected[name]):
            raise RuntimeError('Incorrect transport capability scope: ' + name)
        for environment, credential in expected[name].items():
            if base64.b64decode(received[environment]).decode() != credentials[credential]:
                raise RuntimeError('Transport capability differs from owned state: ' + name)
        print('PASS: ' + name + ' receives only its owned/consumed transport capabilities')


def stun_probe(kind, host=None, port=None):
    # Same Binding Request and response checks as ChatCallSignalingService.probeStun.
    transaction = secrets.token_bytes(12)
    packet = struct.pack('!HHI', 1, 0, 0x2112a442) + transaction
    with socket.socket(socket.AF_INET, kind) as channel:
        channel.settimeout(3)
        channel.connect((host or w.vm.IP, port or w.CONFIG['stunNodePort']))
        channel.sendall(packet)
        if kind == socket.SOCK_DGRAM:
            result = channel.recv(2048)
        else:
            result = b''
            while len(result) < 20:
                part = channel.recv(20 - len(result))
                if not part:
                    raise RuntimeError('STUN closed before its response header.')
                result += part
            length = struct.unpack('!H', result[2:4])[0]
            if length > 2028:
                raise RuntimeError('Oversized STUN probe response.')
            while len(result) < 20 + length:
                part = channel.recv(20 + length - len(result))
                if not part:
                    raise RuntimeError('STUN closed before its response body.')
                result += part
        if len(result) < 20:
            raise RuntimeError('Truncated STUN response.')
        message, length, cookie = struct.unpack('!HHI', result[:8])
        if message != 0x0101 or cookie != 0x2112a442 or result[8:20] != transaction or length > len(result) - 20:
            raise RuntimeError('Invalid STUN Binding Response.')
    print('PASS: STUN Binding Response over ' + ('UDP' if kind == socket.SOCK_DGRAM else 'TCP'))


def check_stun_image(image):
    """Exercise the existing Compose STUN command with the lab's empty capability set."""
    def docker(*arguments):
        return subprocess.run(['docker', *arguments], capture_output=True, text=True,
                              check=True, timeout=30).stdout.strip()
    compose = w.BACKEND / 'server/docker-compose-stun.yml'
    config = json.loads(docker('compose', '-f', str(compose), 'config', '--format', 'json'))['services']['stun']
    container = docker('run', '--detach', '--user', '65532:65532', '--cap-drop', 'ALL',
                       '--security-opt', 'no-new-privileges:true', '--read-only',
                       '--tmpfs', '/tmp:rw,nosuid,nodev,noexec,size=16m,mode=0700,uid=65532,gid=65532',
                       '--tmpfs', '/var/lib/coturn:rw,nosuid,nodev,noexec,size=16m,mode=0700,uid=65532,gid=65532',
                       '--memory', '64m', '--cpus', '0.5',
                       '--publish', '127.0.0.1::3478/tcp', '--publish', '127.0.0.1::3478/udp',
                       '--entrypoint', config['entrypoint'][0], image, *config['command'])
    try:
        info = json.loads(docker('inspect', container))[0]
        ports = info['NetworkSettings']['Ports']
        deadline = time.monotonic() + 15
        while True:
            try:
                docker('exec', container, *config['healthcheck']['test'][1:])
                break
            except subprocess.CalledProcessError:
                if time.monotonic() >= deadline:
                    raise RuntimeError('Unprivileged STUN image did not become ready.') from None
                time.sleep(0.2)
        scratch_paths = sorted(info['HostConfig']['Tmpfs'])
        if len(scratch_paths) != 2:
            raise RuntimeError('STUN scratch mount inventory differs from this probe.')
        scratch = docker('exec', container, 'stat', '-c', '%a:%u:%g', *scratch_paths).splitlines()
        if scratch != ['700:65532:65532', '700:65532:65532']:
            raise RuntimeError('STUN scratch filesystems are not private to the runtime user.')
        status = dict(line.split(':', 1) for line in docker('exec', container, 'cat', '/proc/1/status').splitlines() if ':' in line)
        for capability in ('CapInh', 'CapPrm', 'CapEff', 'CapBnd', 'CapAmb'):
            if int(status[capability].strip(), 16) != 0:
                raise RuntimeError('STUN retained a runtime capability: ' + capability)
        if status['NoNewPrivs'].strip() != '1' or set(status['Uid'].split()) != {'65532'}:
            raise RuntimeError('STUN privilege boundary differs from the lab configuration.')
        for kind, protocol in ((socket.SOCK_DGRAM, 'udp'), (socket.SOCK_STREAM, 'tcp')):
            stun_probe(kind, '127.0.0.1', int(ports['3478/' + protocol][0]['HostPort']))
        print('PASS: non-root STUN, private 0700 scratch, read-only filesystem, no-new-privileges, all capability sets empty')
    finally:
        docker('rm', '--force', container)


def owned_deployment(name):
    deployment = w.kube_json('-n', w.NAMESPACE, 'get', 'deployment', name)
    if any(deployment['metadata'].get('labels', {}).get(key) != value for key, value in w.LABEL.items()):
        raise RuntimeError('Refusing to scale an unowned deployment')
    if deployment['spec']['template']['spec']['containers'][0]['image'] != w.image_lock()[name]['reference']:
        raise RuntimeError(name + ' image differs from the versioned lock')
    return deployment


def memory():
    raw = w.vm.ssh('cat /proc/meminfo').stdout
    values = dict((line.split(':', 1)[0], int(line.split()[1])) for line in raw.splitlines())
    return {key: values[key] for key in ('MemTotal', 'MemAvailable', 'SwapTotal', 'SwapFree')}
