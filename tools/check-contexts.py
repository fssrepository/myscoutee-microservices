#!/usr/bin/env python3
"""Check real assembly Spring contexts serially, without rebuilding images.

Reuses the Helm-rendered environment and existing lab support services through
one temporary SSH tunnel. Requires backend replicas at zero. Starts each built
JAR in its existing production runtime base, records its context refresh,
retains started owners for later startup dependencies, then removes its own containers/tunnel. This is wiring evidence, not a
readiness or distributed-flow test. Native startup/index initializers run
against the lab databases; no fixtures are reset or imported.
"""
import base64
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import tempfile
import time
import uuid

import yaml
import workloads as w


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def rendered_environments():
    values = w.chart_values(backends_enabled=False)
    rendered = w.helm('template', w.NAMESPACE, w.chart_directory(),
                      '-n', w.NAMESPACE, '-f', '-', values=values)
    objects = list(yaml.safe_load_all(rendered.stdout))
    sources = {}
    deployments = {}
    for obj in objects:
        if not obj:
            continue
        kind, name = obj['kind'], obj['metadata']['name']
        if kind == 'ConfigMap':
            sources[(kind, name)] = obj.get('data', {})
        elif kind == 'Secret':
            sources[(kind, name)] = {
                **{key: base64.b64decode(value).decode() for key, value in obj.get('data', {}).items()},
                **obj.get('stringData', {})}
        elif kind == 'Deployment' and name in w.SERVICES:
            deployments[name] = obj
    environments = {}
    for name, obj in deployments.items():
        env = {}
        for entry in obj['spec']['template']['spec']['containers'][0]['envFrom']:
            kind = 'Secret' if 'secretRef' in entry else 'ConfigMap'
            ref = entry.get('secretRef', entry.get('configMapRef'))
            env.update(sources[(kind, ref['name'])])
        environments[name] = env
    return environments


def main():
    w.namespace_owned()
    deployments = w.kube_json('-n', w.NAMESPACE, 'get', 'deployments')['items']
    if any(item['metadata']['name'] in w.SERVICES and (
            item['spec'].get('replicas', 0) or item['status'].get('replicas', 0)) for item in deployments):
        raise RuntimeError('Stop the owned backend replicas before serial context verification.')
    processes = subprocess.check_output(['ps', '-eo', 'args='], text=True)
    if re.search(r'bootRun|frontend_auto_watch|start_profilemanagement_dev|ng serve|GradleDaemon|GradleWorkerMain', processes):
        raise RuntimeError('A dev/build owner is running; context verification must be serial.')
    available = int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines()
                         if line.startswith('MemAvailable:')))
    if available < 4096 * 1024:
        raise RuntimeError('At least 4 GiB available host memory is required.')
    environments = rendered_environments()
    services = w.kube_json('-n', w.NAMESPACE, 'get', 'services')['items']
    forwards, replacements = [], []
    application_ports = {name: free_port() for name in w.SERVICES}
    management_ports = {name: free_port() for name in w.SERVICES}
    for service in services:
        name, address = service['metadata']['name'], service['spec'].get('clusterIP')
        if not address or address == 'None':
            continue
        for port in service['spec']['ports']:
            if port.get('protocol', 'TCP') != 'TCP':
                continue
            if name in application_ports:
                local = application_ports[name] if port['port'] == 8080 else management_ports[name]
                replacements.append((f'{name}:{port["port"]}', f'127.0.0.1:{local}'))
                continue
            local = free_port()
            forwards.extend(['-L', f'127.0.0.1:{local}:{address}:{port["port"]}'])
            replacements.append((f'{name}:{port["port"]}', f'127.0.0.1:{local}'))
    report_dir = w.BUILD / 'reports/contexts'
    report_dir.mkdir(parents=True, exist_ok=True)
    report = {}
    containers = []
    ssh = w.vm.ssh_command('true')
    # vm.ssh_command owns the pinned key, host and identity options.
    command = [*ssh[:-2], *forwards, '-o', 'ExitOnForwardFailure=yes', '-N', ssh[-2]]
    tunnel = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        time.sleep(0.5)
        if tunnel.poll() is not None:
            raise RuntimeError('Context tunnel could not start.')
        for name, assembly in w.SERVICES.items():
            jar = w.BUILD / f'{name}/libs/myscoutee-{name}.jar'
            if not jar.is_file():
                raise RuntimeError('Build the assembly JAR first: ' + name)
            env = dict(environments[name])
            for key, value in list(env.items()):
                if key.endswith('_HOST'):
                    # Host/port pairs are represented separately in Spring config.
                    suffix = '6379' if 'REDIS' in key else '27017' if 'MONGODB' in key else None
                    replacement = dict(replacements).get(value + ':' + str(suffix))
                    if replacement:
                        env[key] = '127.0.0.1'
                        env[key.removesuffix('_HOST') + '_PORT'] = replacement.rsplit(':', 1)[1]
                elif key.endswith(('_URL', '_URI', '_ENDPOINT')):
                    for source, target in replacements:
                        env[key] = env[key].replace('//' + source + '/', '//' + target + '/').replace(
                            '//' + source + '?', '//' + target + '?').replace('@' + source + '/', '@' + target + '/')
                        if env[key].endswith('//' + source):
                            env[key] = env[key][:-len(source)] + target
            env.update(SERVER_PORT=str(application_ports[name]), MANAGEMENT_SERVER_PORT=str(management_ports[name]),
                       SERVER_ADDRESS='127.0.0.1', MANAGEMENT_SERVER_ADDRESS='127.0.0.1')
            container = 'msc121-context-' + name + '-' + uuid.uuid4().hex[:8]
            containers.append(container)
            log_path = report_dir / (name + '.log')
            started = time.monotonic()
            status = 'timeout'
            with tempfile.NamedTemporaryFile(mode='w', prefix='msc121-context-', delete=True) as env_file:
                os.chmod(env_file.name, 0o600)
                for key, value in env.items():
                    if '\n' in str(value):
                        raise RuntimeError('Multiline environment value cannot be passed in Docker env-file: ' + key)
                    env_file.write(key + '=' + str(value) + '\n')
                env_file.flush()
                try:
                    w.execute(['docker', 'run', '-d', '--name', container,
                        '--label', 'com.myscoutee.task=MSC-121', '--network', 'host',
                        '--memory', str(assembly['memoryMiB']) + 'm', '--cpus', '2',
                        '--user', '10001:10001', '--read-only', '--cap-drop', 'ALL',
                        '--security-opt', 'no-new-privileges', '--env-file', env_file.name,
                        '--tmpfs', '/tmp:rw,size=128m',
                        '--tmpfs', '/var/lib/myscoutee:rw,uid=10001,gid=10001,size=16m',
                        '--mount', f'type=bind,source={jar},target=/app/application.jar,readonly',
                        'myscoutee-service-' + assembly['runtimeStage'] + ':msc121',
                        'java', '-Xmx' + str(assembly['heapMiB']) + 'm', '-jar', '/app/application.jar'])
                    while time.monotonic() - started < 90:
                        logs = w.execute(['docker', 'logs', container], check=False)
                        output = logs.stdout + logs.stderr
                        log_path.write_text(output)
                        log_path.chmod(0o600)
                        if re.search(r'Started CoreApplication in ', output):
                            status = 'context-refreshed'
                            break
                        state = w.execute(['docker', 'inspect', '--format', '{{.State.Running}}', container])
                        if state.stdout.strip() != 'true':
                            status = 'failed'
                            break
                        time.sleep(0.5)
                finally:
                    if status != 'context-refreshed':
                        w.execute(['docker', 'rm', '-f', container], check=False)
                        containers.remove(container)
            report[name] = {'status': status, 'seconds': round(time.monotonic() - started, 1),
                            'log': str(log_path.relative_to(w.BUILD))}
            (report_dir / 'results.json').write_text(json.dumps(report, indent=2) + '\n')
            print(name + ': ' + status + ' (' + str(report[name]['seconds']) + 's)', flush=True)
    finally:
        for container in reversed(containers):
            w.execute(['docker', 'rm', '-f', container], check=False)
        tunnel.terminate()
        try:
            tunnel.wait(timeout=5)
        except subprocess.TimeoutExpired:
            tunnel.kill()
            tunnel.wait()
    return 0 if len(report) == len(w.SERVICES) and all(
        item['status'] == 'context-refreshed' for item in report.values()) else 1


if __name__ == '__main__':
    raise SystemExit(main())
