#!/usr/bin/env python3
"""Sample existing container cgroups over one owned SSH connection; no metrics agent."""
import argparse
import base64
import contextlib
import io
import json
from pathlib import Path
import ssl
import subprocess
import tempfile
import time
import urllib.request


@contextlib.contextmanager
def api_reader():
    # Reuse the guest's existing admin kubeconfig, never send credentials to the host.
    config = json.loads(subprocess.check_output(
        ['k3s', 'kubectl', 'config', 'view', '--raw', '-o', 'json'], text=True, timeout=10))
    current = next(c['context'] for c in config['contexts'] if c['name'] == config['current-context'])
    cluster = next(c['cluster'] for c in config['clusters'] if c['name'] == current['cluster'])
    user = next(u['user'] for u in config['users'] if u['name'] == current['user'])
    context = ssl.create_default_context(cadata=base64.b64decode(cluster['certificate-authority-data']).decode())
    with tempfile.TemporaryDirectory(prefix='myscoutee-observe-') as private:
        cert, key = Path(private) / 'client.crt', Path(private) / 'client.key'
        for path, value in [(cert, 'client-certificate-data'), (key, 'client-key-data')]:
            path.touch(mode=0o600)
            path.write_bytes(base64.b64decode(user[value]))
        context.load_cert_chain(cert, key)
        # SSLContext has loaded both files; leave no credential copy while watching.
        cert.unlink(); key.unlink()
        def read(path):
            with urllib.request.urlopen(cluster['server'].rstrip('/') + path,
                                        context=context, timeout=5) as response:
                return json.load(response)
        yield read


def render(items, summaries):
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        render_rows(items, summaries)
    return output.getvalue().rstrip()


def render_rows(items, summaries):
    memory = dict(line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())
    print('GUEST RAM    %.0f MiB available / %.0f MiB total' %
          (int(memory['MemAvailable'].split()[0]) / 1024, int(memory['MemTotal'].split()[0]) / 1024))
    for node in (i for i in items if i['kind'] == 'Node'):
        ready = next((c['status'] for c in node['status']['conditions'] if c['type'] == 'Ready'), 'Unknown')
        print('KUBERNETES   %s | Ready=%s | %s' % (node['metadata']['name'], ready, node['status']['nodeInfo']['kubeletVersion']))
    stats = {(pod['podRef']['namespace'], pod['podRef']['name'], container['name']): container
             for summary in summaries for pod in summary.get('pods', []) for container in pod.get('containers', [])}
    pods = [i for i in items if i['kind'] == 'Pod']
    print('\n%-12s  %-24s  %-6s  %-22s  %6s  %8s' % ('NAMESPACE', 'WORKLOAD', 'READY', 'POD STATE', 'CPU %', 'RAM MiB'))
    previous_namespace = None
    for deploy in sorted((i for i in items if i['kind'] in ('Deployment', 'StatefulSet')), key=lambda i: (i['metadata']['namespace'], i['metadata']['name'])):
        namespace = deploy['metadata']['namespace']
        if previous_namespace and namespace != previous_namespace:
            print()
        previous_namespace = namespace
        selector = deploy['spec']['selector']['matchLabels']
        owned = [p for p in pods if p['metadata']['namespace'] == namespace and
                 all(p['metadata'].get('labels', {}).get(k) == v for k, v in selector.items())]
        states = set()
        cpu = used = 0
        samples = 0
        for pod in owned:
            statuses = pod.get('status', {}).get('containerStatuses', [])
            states.add(next((s['state']['waiting']['reason'] for s in statuses if 'waiting' in s.get('state', {})),
                            pod.get('status', {}).get('phase', 'Unknown')))
            for status in statuses:
                entry = stats.get((namespace, pod['metadata']['name'], status['name']))
                if entry:
                    samples += 1
                    cpu += int(entry.get('cpu', {}).get('usageNanoCores', 0)) / 10 ** 7
                    used += int(entry.get('memory', {}).get('workingSetBytes', 0)) / 1024 ** 2
        ready = str(deploy.get('status', {}).get('readyReplicas', 0)) + '/' + str(deploy['spec'].get('replicas', 1))
        print('%-12s  %-24s  %-6s  %-22s  %6s  %8s' % (
            namespace.removeprefix('myscoutee-'), deploy['metadata']['name'], ready,
            ','.join(sorted(states)) or 'no pods', '%.1f' % cpu if samples else '-', '%.1f' % used if samples else '-'))


class ContainerSamples:
    def __init__(self):
        self.containers = []
        self.previous = {}

    def refresh(self, items):
        paths = {path.name.removeprefix('cri-containerd-').removesuffix('.scope'): path
                 for path in Path('/sys/fs/cgroup/kubepods.slice').rglob('cri-containerd-*.scope')}
        self.containers = [(pod['metadata'], status, paths.get(status.get('containerID', '').removeprefix('containerd://')))
                           for pod in items if pod['kind'] == 'Pod'
                           for status in pod.get('status', {}).get('containerStatuses', [])]
        active = {status.get('containerID') for _, status, _ in self.containers}
        self.previous = {key: value for key, value in self.previous.items() if key in active}

    def sample(self):
        pods = {}
        for metadata, status, root in self.containers:
            if root is None:
                continue
            try:
                cpu = int(dict(line.split() for line in (root / 'cpu.stat').read_text().splitlines())['usage_usec'])
                memory = int((root / 'memory.current').read_text())
                inactive = int(dict(line.split() for line in (root / 'memory.stat').read_text().splitlines()).get('inactive_file', 0))
            except FileNotFoundError:
                continue  # Terminated/replaced container; never reuse its last sample.
            now = time.monotonic()
            key = status['containerID']
            previous = self.previous.get(key)
            self.previous[key] = (now, cpu)
            if previous is None:
                continue  # A rate requires two samples of this exact container.
            nanos = max(0, cpu - previous[1]) * 1000 / max(0.000001, now - previous[0])
            pod = pods.setdefault((metadata['namespace'], metadata['name']),
                                  {'podRef': {'namespace': metadata['namespace'], 'name': metadata['name']}, 'containers': []})
            pod['containers'].append({'name': status['name'], 'cpu': {'usageNanoCores': nanos},
                                      'memory': {'workingSetBytes': max(0, memory - inactive)}})
        return [{'pods': list(pods.values())}]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--watch', action='store_true')
    args = parser.parse_args()
    samples = ContainerSamples()
    with api_reader() as read:
        items, inventory_at, primed = [], float('-inf'), False
        while True:
            started = time.monotonic()
            try:
                if started - inventory_at >= 5:
                    items = []
                    for endpoint, kind in [('/api/v1/nodes', 'Node'), ('/apis/apps/v1/deployments', 'Deployment'), ('/apis/apps/v1/statefulsets', 'StatefulSet'), ('/api/v1/pods', 'Pod')]:
                        items.extend({**item, 'kind': kind} for item in read(endpoint)['items'])
                    samples.refresh(items)
                    inventory_at = started
                if not primed:
                    samples.sample()
                    time.sleep(1)
                    primed = True
                print(json.dumps({'snapshot': render(items, samples.sample())}), flush=True)
            except (OSError, ValueError, KeyError) as error:
                print(json.dumps({'error': 'Guest observation unavailable: ' + type(error).__name__}), flush=True)
            if not args.watch:
                return
            time.sleep(1)


if __name__ == '__main__':
    try:
        main()
    except (BrokenPipeError, KeyboardInterrupt):
        pass
