#!/usr/bin/env python3
"""Versioned image cache and Kubernetes workloads inside the owned project guest.

Startup never invokes a build. lock-images explicitly records already-built
artifacts; import-images transfers only missing immutable versions to containerd.
"""
import argparse
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import subprocess
import sys
import tempfile
import time
import uuid

import vm
from transport_usage import consumer_domains

SHELL = vm.SHELL
BUILD = vm.BUILD
BACKEND = vm.BACKEND
CONFIG = json.loads((SHELL / 'deploy/runtime.json').read_text())
SERVICES = json.loads((SHELL / 'assemblies.json').read_text())['services']
TRANSPORTS = json.loads((SHELL / 'transport/bindings.json').read_text())['bindings']
NAMESPACE = CONFIG['namespace']
LOCK = SHELL / 'deploy/images.lock.json'
LABEL = {'app.kubernetes.io/part-of': NAMESPACE}
IMAGES = CONFIG['images']
PACKAGING = BACKEND / 'config/packaging/node_modules/@fssrepository/myscoutee-packaging'
INVENTORY = vm.VM / 'workloads.json'
SECRET_FILE = vm.VM / 'workload-secrets.json'


def execute(command, data=None, check=True, timeout=120):
    return subprocess.run([str(a) for a in command], input=data, text=True,
                          capture_output=True, check=check, timeout=timeout)


def kube(*args, data=None, check=True, timeout=120):
    command = shlex.join(['sudo', 'k3s', 'kubectl', '--request-timeout=30s', *args])
    return execute(vm.ssh_command(command), data, check, timeout)


def kube_json(*args):
    return json.loads(kube(*args, '-o', 'json').stdout)


def image_lock():
    manifest = json.loads(LOCK.read_text())
    if manifest.get('schemaVersion') != 1 or set(manifest['images']) != set(SERVICES) | set(CONFIG['dependencies']):
        raise RuntimeError('Image lock does not match current assembly/dependency inventory.')
    for name, entry in manifest['images'].items():
        expected = 'docker.io/library/' + IMAGES['prefix'] + name + ':' + IMAGES['immutableVersion'] + '-' + entry['id'].removeprefix('sha256:')[:12]
        if not re.fullmatch(r'sha256:[a-f0-9]{64}', entry['id']) or entry['reference'] != expected:
            raise RuntimeError('Invalid immutable image identity: ' + name)
    return manifest['images']


def lock_images():
    images = {}
    sources = {name: IMAGES['prefix'] + name + ':' + IMAGES['buildTag'] for name in SERVICES}
    sources.update(CONFIG['dependencies'])
    for name, source in sources.items():
        info = json.loads(vm.run('docker', 'image', 'inspect', source).stdout)[0]
        fingerprint = vm.run(PACKAGING / 'release/lib/image-content-fingerprint.sh', 'docker', source).stdout.strip()
        images[name] = {'source': source, 'id': info['Id'], 'contentFingerprint': fingerprint,
                        'sourceDigests': info.get('RepoDigests', []),
                        'reference': 'docker.io/library/' + IMAGES['prefix'] + name + ':' + IMAGES['immutableVersion'] + '-' + info['Id'].split(':')[1][:12]}
    candidate = json.dumps({'schemaVersion': 1, 'images': images}, indent=2) + '\n'
    if not LOCK.exists() or LOCK.read_text() != candidate:
        LOCK.write_text(candidate)
    print('Locked existing image identities; no build or pull was performed: ' + str(LOCK))


def guest_images():
    data = json.loads(vm.ssh('sudo k3s crictl images -o json').stdout)
    return {tag: image['id'] for image in data['images'] for tag in image.get('repoTags', [])}


def import_images():
    images = image_lock()
    cached = guest_images()
    missing = []
    for name, image in images.items():
        reference = image['reference']
        if reference in cached:
            if cached[reference] != image['id']:
                raise RuntimeError('Guest immutable tag collision: ' + reference)
            print('Guest cache hit: ' + name, flush=True)
            continue
        # Verify the config identity before tagging/saving. Missing artifacts are
        # a release/distribution issue, never permission to rebuild implicitly.
        info = json.loads(vm.run('docker', 'image', 'inspect', image['id']).stdout)[0]
        if info['Id'] != image['id']:
            raise RuntimeError('Local image identity mismatch: ' + name)
        existing = vm.run('docker', 'image', 'inspect', reference, check=False)
        if existing.returncode == 0 and json.loads(existing.stdout)[0]['Id'] != image['id']:
            raise RuntimeError('Host immutable tag collision: ' + reference)
        vm.run('docker', 'image', 'tag', image['id'], reference)
        with (vm.STATE / 'images.tsv').open('a') as inventory:
            inventory.write(reference.removeprefix('docker.io/library/') + '\t' + image['id'] + '\n')
        missing.append(reference)
    if missing:
        print(f'Transferring {len(missing)} existing images to guest containerd; no build.', flush=True)
        # Stream one deduplicated archive over pinned SSH; no large staging tar.
        with subprocess.Popen(['docker', 'image', 'save', *missing], stdout=subprocess.PIPE) as source:
            try:
                result = subprocess.run(vm.ssh_command('sudo k3s ctr -n k8s.io images import -'),
                                        stdin=source.stdout, capture_output=True, text=True, timeout=600)
                source.stdout.close()
                status = source.wait(timeout=10)
                if status or result.returncode:
                    raise RuntimeError('Image transfer failed: ' + result.stderr[-1500:])
            finally:
                if source.poll() is None:
                    source.terminate()
                    source.wait(timeout=10)
    cached = guest_images()
    for name, image in images.items():
        if cached.get(image['reference']) != image['id']:
            raise RuntimeError('Guest image verification failed: ' + name)
    print('All guest image identities match the versioned lock.', flush=True)


def namespace_owned(create=False):
    namespaces = kube_json('get', 'namespaces')['items']
    current = next((item for item in namespaces if item['metadata']['name'] == NAMESPACE), None)
    inventory = json.loads(INVENTORY.read_text()) if INVENTORY.exists() else {}
    if current:
        if current['metadata']['uid'] != inventory.get('namespaceUid'):
            raise RuntimeError('Namespace identity missing or changed; refusing to adopt: ' + NAMESPACE)
        if any(current['metadata'].get('labels', {}).get(k) != v for k, v in LABEL.items()):
            raise RuntimeError('Namespace ownership label changed.')
    elif inventory:
        raise RuntimeError('Recorded namespace disappeared; review state before recreating workloads.')
    elif create:
        result = kube('create', '-f', '-', '-o', 'json', data=json.dumps({
            'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': NAMESPACE, 'labels': LABEL}}))
        current = json.loads(result.stdout)
        INVENTORY.write_text(json.dumps({'namespaceUid': current['metadata']['uid']}) + '\n')
    return current


def secrets_data():
    added = not SECRET_FILE.exists()
    values = json.loads(SECRET_FILE.read_text()) if not added else {
        'mongoPassword': secrets.token_hex(24), 'mongoRootPassword': secrets.token_hex(24),
        'localizationToken': secrets.token_urlsafe(32)}
    generated = {
            'configurationMasterKey': secrets.token_hex(32),
            'mongoReplicaKey': base64.b64encode(secrets.token_bytes(512)).decode('ascii')}
    if 'payment-simulator' in CONFIG['dependencies']:
        generated.update({
            'paymentSimulatorApiKey': 'sk_test_' + secrets.token_urlsafe(32),
            'paymentSimulatorWebhookSecret': 'whsec_' + secrets.token_urlsafe(32),
            'paymentSimulatorPosKey': str(uuid.uuid4()),
            'paymentSimulatorAuditToken': secrets.token_urlsafe(32)})
    for key, value in generated.items():
        if key not in values:
            values[key] = value
            added = True
    if not re.fullmatch(r'[0-9a-f]{64}', values['configurationMasterKey']):
        raise RuntimeError('Invalid configuration master key; refusing automatic replacement.')
    if ('delegationPrivateKey' in values) != ('delegationPublicKey' in values):
        raise RuntimeError('Incomplete actor delegation keypair; refusing automatic replacement.')
    if 'delegationPrivateKey' not in values:
        # Keys remain in the existing private state file. Only the accounts owner
        # receives the private key; other assemblies receive the public verifier.
        private = subprocess.run(['openssl','genpkey','-algorithm','RSA','-pkeyopt','rsa_keygen_bits:2048'],
                                 capture_output=True,check=True).stdout
        private_der = subprocess.run(['openssl','pkcs8','-topk8','-nocrypt','-outform','DER'],
                                     input=private,capture_output=True,check=True).stdout
        public_der = subprocess.run(['openssl','pkey','-pubout','-outform','DER'],
                                    input=private,capture_output=True,check=True).stdout
        values['delegationPrivateKey'] = base64.b64encode(private_der).decode('ascii')
        values['delegationPublicKey'] = base64.b64encode(public_der).decode('ascii')
        added = True
    try:
        derived_public = subprocess.run(['openssl','pkey','-inform','DER','-pubout','-outform','DER'],
                                        input=base64.b64decode(values['delegationPrivateKey'],validate=True),
                                        capture_output=True,check=True).stdout
        if derived_public != base64.b64decode(values['delegationPublicKey'],validate=True):
            raise ValueError('Mismatched keypair')
    except (ValueError, subprocess.CalledProcessError):
        raise RuntimeError('Invalid actor delegation keypair; refusing automatic replacement.') from None
    for key in ['minioRootPassword', *(binding['credential'] for binding in TRANSPORTS),
                *('delegationClient-' + name for name, service in SERVICES.items() if 'accounts' not in service['domains'])]:
        if key not in values:
            values[key] = secrets.token_urlsafe(32)
            added = True
    if added:
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', dir=SECRET_FILE.parent,
                                             prefix='.workload-secrets-', delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(json.dumps(values) + '\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, SECRET_FILE)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return values


def transport_settings(services):
    runtime, credential_names = {}, {}
    for binding in TRANSPORTS:
        owners = [name for name, service in services.items() if binding['domain'] in service['domains']]
        if len(owners) != 1:
            raise RuntimeError('Transport domain must have exactly one owner: ' + binding['domain'])
        prefix = 'MYSCOUTEE_REMOTE_' + binding['propertyPrefix'].replace('-', '_').upper()
        runtime[prefix + '_URL'] = 'http://' + owners[0] + ':8080'  # NOSONAR - python:S5332: explicit insecure HTTP opt-in on the private, single-VM lab network.
        runtime[prefix + '_ALLOW_INSECURE_HTTP'] = 'true'
        credential_names[prefix + '_' + binding.get('credentialProperty', 'read-token').replace('-', '_').upper()] = binding['credential']
    return runtime, credential_names


def transport_credentials_by_service(services, backend):
    """Mirror the explicit build imports: give a capability only to its owner and consumers."""
    root = Path(backend) / 'server/projects/profile/src/main/java/com/raxim/myscoutee/domain'
    sources = {domain: '\n'.join(path.read_text() for path in (root / domain).rglob('*.java')
               if path.relative_to(root / domain).parts[0] not in ('api', 'dto', 'transport'))
               for service in services.values() for domain in service['domains']}
    result = {name: {} for name in services}
    for binding in TRANSPORTS:
        owners = [name for name, service in services.items() if binding['domain'] in service['domains']]
        if len(owners) != 1:
            raise RuntimeError('Transport domain must have exactly one owner: ' + binding['domain'])
        environment = ('MYSCOUTEE_REMOTE_' + binding['propertyPrefix'].replace('-', '_').upper()
                       + '_' + binding.get('credentialProperty', 'read-token').replace('-', '_').upper())
        required = set(consumer_domains(binding, sources))
        for name, service in services.items():
            consumers = [domain for domain in service['domains'] if domain in required]
            if binding.get('producerDomains') and any(domain not in binding['producerDomains']
                    and domain != binding['domain'] for domain in consumers):
                raise RuntimeError('Unauthorized producer domain for ' + binding['api'])
            if name == owners[0] or consumers:
                result[name][environment] = binding['credential']
    return result


def actor_delegation_settings(services, credentials, capabilities):
    """Generate exact forwarding permissions from the same owner/consumer manifest as the transports."""
    owners = [name for name, service in services.items() if 'accounts' in service['domains']]
    if len(owners) != 1:
        raise RuntimeError('Actor delegation requires exactly one accounts owner')
    clients = {}
    for name, service in services.items():
        if name == owners[0]:
            continue
        audiences = {'assembly:public:' + name}
        operations = []
        for binding in TRANSPORTS:
            configured = binding.get('actorOperations', [])
            if binding['domain'] in service['domains']:
                audiences.update(operation['audience'] for operation in configured)
            elif binding['credential'] in capabilities[name].values():
                operations.extend(configured)
        clients[name] = {'token': credentials['delegationClient-' + name],
                         'audiences': sorted(audiences), 'operations': operations}
    return {'owner': owners[0], 'clients': clients}


def release_simulator_ready():
    result = kube('-n', NAMESPACE, 'get', 'deployment', 'release-simulator', '--ignore-not-found', '-o', 'json')
    if not result.stdout.strip():
        return False
    deployment = json.loads(result.stdout)
    if any(deployment['metadata'].get('labels', {}).get(k) != v for k, v in LABEL.items()):
        raise RuntimeError('Unowned release simulator deployment')
    return deployment.get('status', {}).get('availableReplicas', 0) > 0


def release_simulator_certificate():
    certificate = kube('-n', NAMESPACE, 'exec', 'deployment/release-simulator', '--',
                       'cat', '/data/tls/certificate.pem').stdout.strip()
    if (len(certificate) > 8192 or not certificate.startswith('-----BEGIN CERTIFICATE-----\n')
            or not certificate.endswith('\n-----END CERTIFICATE-----')
            or certificate.count('-----BEGIN ') != 1):
        raise RuntimeError('Release simulator did not provide one public TLS certificate')
    return certificate + '\n'


def runtime_environment():
    replacements = {'@PUBLIC_BASE_URL@': 'http://localhost:' + str(CONFIG['localPort']),
                    '@LOOPBACK_BASE_URL@': 'http://127.0.0.1:' + str(CONFIG['localPort']),
                    '@VM_IP@': vm.IP, '@STUN_NODE_PORT@': str(CONFIG['stunNodePort'])}
    result = {}
    for key, value in CONFIG['environment'].items():
        for placeholder, replacement in replacements.items():
            value = value.replace(placeholder, replacement)
        result[key] = value
    return result


def chart_values(backends_enabled=True, *, credentials=None, cluster_dns=None, release_certificate=None):
    """Compose chart values; explicit runtime inputs also permit offline analysis."""
    backend = Path(os.environ.get('MYSCOUTEE_BACKEND_SOURCE', BACKEND))
    transport_runtime, _ = transport_settings(SERVICES)
    credentials = secrets_data() if credentials is None else credentials
    capabilities = transport_credentials_by_service(SERVICES, backend)
    delegation = actor_delegation_settings(SERVICES, credentials, capabilities)
    return {
        'images': image_lock(),
        'releaseSimulatorCertificate': ((release_simulator_certificate() if backends_enabled else '')
                                        if release_certificate is None else release_certificate),
        'mongodb': {'replicaSet': CONFIG.get('mongoReplicaSet', False), 'cpuLimit': str(CONFIG.get('mongoCpuLimit', 2))},
        'services': {name: {**config, 'replicas': 1 if backends_enabled else 0} for name, config in SERVICES.items()},
        'frontend': {'replicas': CONFIG['frontendReplicas'], 'nodePort': CONFIG['frontendNodePort']},
        'metricsNodePort': CONFIG['metricsNodePort'],
        'clusterDns': (kube_json('-n', 'kube-system', 'get', 'service', 'kube-dns')['spec']['clusterIP']
                       if cluster_dns is None else cluster_dns),
        'credentials': credentials,
        'transportCredentialsByService': capabilities,
        'actorDelegation': delegation,
        'publicBaseUrl': 'http://localhost:' + str(CONFIG['localPort']),
        'stunNodePort': CONFIG['stunNodePort'],
        'databases': CONFIG['databases'],
        'configurationKeyPath': CONFIG['configurationKeyPath'],
        'runtime': {
            **runtime_environment(),
            'MONGODB_DATABASE': CONFIG['databases']['real'],
            'MONGODB_DEMO_DATABASE': CONFIG['databases']['demo'],
            'MYSCOUTEE_DELEGATION_URL': 'http://' + delegation['owner'] + ':8080',  # NOSONAR - python:S5332: explicit insecure HTTP opt-in on the private, single-VM lab network.
            'MYSCOUTEE_DELEGATION_ALLOW_INSECURE_HTTP': 'true',
            **transport_runtime},
        'shared': {
            'mediaService': next(name for name, service in SERVICES.items() if 'media' in service['domains']),
            'publicUpstreamRoutes': '\n        '.join(route['pattern'] + ' ' + route['service'] + '.' + NAMESPACE + '.svc.cluster.local:8080;' for route in CONFIG['publicRoutes']),
            'metricsUpstreamRoutes': '\n'.join('/metrics/' + name + ' ' + name + '.' + NAMESPACE + '.svc.cluster.local:8081;' for name in SERVICES),
            'runtimeStatusLocation': (backend / 'config/nginx/http/runtime-status.conf').read_text(),
            'mongoInitScript': (backend / 'server/docker/conf/mongodb/db_init.js').read_text(),
            'responsePolicy': (backend / 'config/nginx/http/https-response-policy.prod.conf').read_text()}}


def chart_objects():
    mongo = ([('Secret', 'mongodb-replica-key'), ('StatefulSet', 'mongodb'),
              ('Service', 'mongodb'), ('Service', 'mongodb-secondary'), ('Service', 'mongodb-members')]
             if CONFIG.get('mongoReplicaSet') else
             [('PersistentVolumeClaim', 'mongodb'), ('Deployment', 'mongodb'), ('Service', 'mongodb')])
    return [('Secret', 'runtime'), ('Secret', 'mongodb-bootstrap'), ('Secret', 'minio-bootstrap'),
            *([('Secret', 'payment-simulator')] if 'payment-simulator' in CONFIG['dependencies'] else []),
            *mongo, ('Secret', 'actor-delegation-issuer'), ('Secret', 'deployment-configuration'),
            *[('Secret', 'transport-' + name) for name in SERVICES],
            ('ServiceAccount', 'runtime-status'), ('Role', 'runtime-status'), ('RoleBinding', 'runtime-status'),
            ('ConfigMap', 'runtime'), ('ConfigMap', 'mongodb-init'), ('ConfigMap', 'frontend'), ('ConfigMap', 'release-simulator-ca'),
            ('Service', 'metrics-gateway'), ('PersistentVolumeClaim', 'frontend-nginx-state'), ('PersistentVolumeClaim', 'minio'),
            ('PersistentVolumeClaim', 'release-simulator'),
            *([('PersistentVolumeClaim', 'registry'), ('PersistentVolumeClaim', 'registry-client')] if 'registry' in CONFIG['dependencies'] else []),
            *([('PersistentVolumeClaim', 'payment-simulator')] if 'payment-simulator' in CONFIG['dependencies'] else []),
            *[(kind, name) for name in [*SERVICES, *CONFIG['dependencies']] if name != 'mongodb' for kind in ['Deployment', 'Service']]]


def helm(*args, values=None, timeout=180, check=True):
    return execute([vm.SUPPORT / 'tools/helm.sh', *args],
                   data=json.dumps(values) if values is not None else None, timeout=timeout, check=check)


def stale_transport_key_patch(secret, allowed):
    # Kubernetes persists stringData as data. Removing a stringData entry from
    # an older Helm manifest therefore leaves its live data entry behind.
    stale = sorted(key for key in secret.get('data', {})
                   if (key.startswith('MYSCOUTEE_REMOTE_') or key == 'MYSCOUTEE_DELEGATION_CLIENT_TOKEN') and key not in allowed)
    if not stale:
        return []
    return [{'op': 'test', 'path': '/metadata/resourceVersion',
             'value': secret['metadata']['resourceVersion']},
            *[{'op': 'remove', 'path': '/data/' + key.replace('~', '~0').replace('/', '~1')}
              for key in stale]]


def backend_deployments():
    namespace_owned()
    objects = kube_json('-n', NAMESPACE, 'get', 'deployments')['items']
    backends = [item for item in objects if item['metadata']['name'] in SERVICES]
    for item in backends:
        if any(item['metadata'].get('labels', {}).get(k) != v for k, v in LABEL.items()):
            raise RuntimeError('Unowned backend deployment: ' + item['metadata']['name'])
    return backends


def require_backend_upgrade_quiescence(images):
    """This lab uses coordinated upgrades while owner document schemas evolve."""
    deployments = backend_deployments()
    changed = any(any(c['image'] != images[item['metadata']['name']]['reference']
                      for c in item['spec']['template']['spec']['containers']) for item in deployments)
    if not changed:
        return
    if any(item['spec'].get('replicas', 0) or item['status'].get('replicas', 0) for item in deployments):
        raise RuntimeError('Backend image changes require workloads.py backends-stop before up; support services remain running.')
    pods = kube_json('-n', NAMESPACE, 'get', 'pods')['items']
    if any(p['metadata'].get('labels', {}).get('app.kubernetes.io/name') in SERVICES for p in pods):
        raise RuntimeError('Wait for all old backend pods to terminate before the coordinated upgrade.')


def stop_backends():
    deployments = backend_deployments()
    for item in deployments:
        kube('-n', NAMESPACE, 'scale', 'deployment/' + item['metadata']['name'], '--replicas=0')
    for item in deployments:
        name = item['metadata']['name']
        selector = ','.join(k + '=' + v for k, v in item['spec']['selector']['matchLabels'].items())
        kube('-n', NAMESPACE, 'wait', '--for=delete', 'pod', '-l', selector, '--timeout=60s', timeout=70)
    print('Owned backends stopped; support services and volumes retained.')


def migrate_mongo_member_services(services):
    """Replace only owned member Services; Kubernetes clusterIP is immutable.

    Existing member DNS names and PVCs remain unchanged. The following Helm
    upgrade creates headless Services; a rerun resumes if interrupted here.
    """
    if not services:
        return
    if any(item['spec'].get('replicas', 0) or item['status'].get('replicas', 0)
           for item in backend_deployments()):
        raise RuntimeError('Mongo member DNS migration requires workloads.py backends-stop first.')
    for service in services:
        if service['metadata']['name'] not in ('mongodb', 'mongodb-secondary') or any(
                service['metadata'].get('labels', {}).get(k) != v for k, v in LABEL.items()):
            raise RuntimeError('Refusing to replace an unowned Mongo member Service')
    for service in services:
        name = service['metadata']['name']
        current = kube_json('-n', NAMESPACE, 'get', 'service', name)
        if current['metadata']['uid'] != service['metadata']['uid']:
            raise RuntimeError('Mongo member Service changed during migration')
        kube('-n', NAMESPACE, 'delete', 'service', name, '--wait=true')


def chart_directory():
    execute([sys.executable, vm.SUPPORT / 'tools/render-deployment.py', BACKEND])
    return BUILD / 'chart'


def deploy_chart(check_only=False, backends_enabled=True):
    # Start the canonical simulator first on a fresh lab. Transfer only its public
    # CA into a ConfigMap before starting the platform owner; never share its keys.
    if backends_enabled and not check_only and not release_simulator_ready():
        deploy_chart(backends_enabled=False)
    if CONFIG.get('mongoReplicaSet') and not check_only:
        from mongo_replica import configured, require_conversion_receipt, initialize, require_stateful_volumes
        require_conversion_receipt()
        require_stateful_volumes()
        if backends_enabled and not configured():
            deploy_chart(backends_enabled=False)
            initialize()
    # Lint the bootstrap form without requiring a running simulator. Real backend
    # deployment must read its CA successfully; never replace it with an empty value.
    values = chart_values(backends_enabled=backends_enabled and not check_only)
    chart = chart_directory()
    lint = helm('lint', chart, '-f', '-', values=values)
    print(lint.stdout, end='', flush=True)
    if check_only:
        return
    if backends_enabled:
        require_backend_upgrade_quiescence(image_lock())
    scoped = {'runtime': {}}
    scoped.update({'transport-' + name: set(keys) | (
        {'MYSCOUTEE_DELEGATION_CLIENT_TOKEN'} if 'accounts' not in values['services'][name]['domains'] else set())
        for name, keys in values['transportCredentialsByService'].items()})
    secret_migrations = []
    mongo_service_migrations = []
    # A one-time migration of this task's earlier manifests. Never use Helm's
    # broad --take-ownership option or overwrite another release's resources.
    for kind, name in chart_objects():
        current = kube('-n', NAMESPACE, 'get', kind, name, '--ignore-not-found', '-o', 'json')
        if not current.stdout.strip():
            continue
        obj = json.loads(current.stdout)
        metadata = obj['metadata']
        if any(metadata.get('labels', {}).get(k) != v for k, v in LABEL.items()):
            raise RuntimeError('Refusing Helm adoption of an unowned object: ' + name)
        annotations = metadata.get('annotations', {})
        if annotations.get('meta.helm.sh/release-name', NAMESPACE) != NAMESPACE or annotations.get('meta.helm.sh/release-namespace', NAMESPACE) != NAMESPACE:
            raise RuntimeError('Kubernetes object belongs to another Helm release: ' + name)
        if annotations.get('meta.helm.sh/release-name') != NAMESPACE:
            patch = {'metadata': {'labels': {'app.kubernetes.io/managed-by': 'Helm'}, 'annotations': {
                'meta.helm.sh/release-name': NAMESPACE, 'meta.helm.sh/release-namespace': NAMESPACE}}}
            kube('-n', NAMESPACE, 'patch', kind, name, '--type=merge', '-p', json.dumps(patch))
            obj = kube_json('-n', NAMESPACE, 'get', kind, name)
        if CONFIG.get('mongoReplicaSet') and kind == 'Service' and name in ('mongodb', 'mongodb-secondary') and obj['spec'].get('clusterIP') != 'None':
            mongo_service_migrations.append(obj)
        if kind == 'Secret' and name in scoped:
            migration = stale_transport_key_patch(obj, scoped[name])
            if migration:
                secret_migrations.append((name, migration))
    migrate_mongo_member_services(mongo_service_migrations)
    for name, migration in secret_migrations:
        kube('-n', NAMESPACE, 'patch', 'secret', name, '--type=json', '-p', json.dumps(migration))
    result = helm('upgrade', '--install', NAMESPACE, chart, '-n', NAMESPACE, '-f', '-',
                  '--server-side=false', '--history-max=3', '--wait=watcher', '--timeout=90s', values=values, check=False)
    if result.returncode:
        status()
        raise RuntimeError('Helm rollout failed or timed out; resources retained for diagnosis. Inspect workload status/logs. No application-ready claim.')
    print(result.stdout, end='')


def status():
    if namespace_owned():
        print(kube('-n', NAMESPACE, 'get', 'deployments,statefulsets,pods,services,pvc', '-o', 'wide').stdout)
    else:
        print('No application namespace has been created.')


def tunnel(action):
    config = vm.VM / 'forward.env'
    if action == 'start':
        namespace_owned()
        if not (vm.VM / 'known_hosts').exists():
            raise RuntimeError('Guest SSH identity is not pinned.')
        # The existing packaging transport consumes a trusted, local shell env.
        # SSH options in that helper are word-split: reject ambiguous paths.
        if re.search(r'\s', str(vm.VM)):
            raise RuntimeError('The packaging forwarding helper requires a state path without whitespace.')
        values = {'REMOTE_HOST': vm.IP, 'REMOTE_USER': 'myscoutee',
                  'REMOTE_KEY_FILE': str(vm.VM / 'id_ed25519'),
                  'REMOTE_HOST_KEY_CHECKING': 'yes',
                  'REMOTE_SSH_OPTIONS': '-F /dev/null -o BatchMode=yes -o IdentitiesOnly=yes -o UserKnownHostsFile=' + str(vm.VM / 'known_hosts'),
                  'LOCAL_FORWARD_HOST': '127.0.0.1',
                  'LOCAL_FORWARD_PORTS': str(CONFIG['localPort']) + ':' + str(CONFIG['frontendNodePort'])}
        config.write_text('\n'.join(key + '=' + shlex.quote(value) for key, value in values.items()) + '\n')
    if config.exists():
        result = subprocess.run([str(PACKAGING / 'client-tools/forward-localhost.sh'), action],
                                env={**os.environ, 'CONFIG_FILE': str(config)}, check=True, capture_output=True, text=True)
        print(result.stdout, end='')
        if action == 'stop':
            config.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['lock-images', 'import-images', 'up', 'frontend-up', 'status', 'backends-stop', 'stop', 'forward', 'tunnel-stop', 'chart-check'])
    args = parser.parse_args()
    if os.geteuid() == 0:
        parser.error('Run as the lab user, not root.')
    os.umask(0o077)
    if args.action == 'lock-images':
        lock_images()
        return
    if args.action == 'tunnel-stop':
        tunnel('stop')
        return
    vm.require_libvirt_access()
    vm.private_directory(vm.STATE)
    vm.private_directory(vm.VM)
    with (vm.VM / 'lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if 'libvirt_domain.lab' not in vm.ownership() or vm.virsh('domstate', vm.NAME).stdout.strip() != 'running':
            raise RuntimeError('Start the owned VM first with vm.py start.')
        if args.action == 'import-images':
            import_images()
        elif args.action in ('up', 'frontend-up'):
            import_images()
            namespace_owned(create=True)
            deploy_chart(backends_enabled=args.action == 'up')
            status()
        elif args.action == 'chart-check':
            deploy_chart(check_only=True)
        elif args.action == 'forward':
            tunnel('start')
        elif args.action == 'backends-stop':
            stop_backends()
        elif args.action == 'stop':
            if namespace_owned():
                for name in [*SERVICES, *CONFIG['dependencies'], *(['mongodb-secondary'] if CONFIG.get('mongoReplicaSet') else [])]:
                    result = kube('-n', NAMESPACE, 'get', 'deployment', name, '--ignore-not-found', '-o', 'json')
                    if result.stdout.strip():
                        obj = json.loads(result.stdout)
                        if any(obj['metadata'].get('labels', {}).get(k) != v for k, v in LABEL.items()):
                            raise RuntimeError('Refusing to scale an unowned deployment: ' + name)
                        kube('-n', NAMESPACE, 'scale', 'deployment/' + name, '--replicas=0')
                mongo = kube('-n', NAMESPACE, 'get', 'statefulset/mongodb', '--ignore-not-found', '-o', 'json')
                if mongo.stdout.strip():
                    obj = json.loads(mongo.stdout)
                    if any(obj['metadata'].get('labels', {}).get(k) != v for k, v in LABEL.items()):
                        raise RuntimeError('Refusing to scale an unowned Mongo StatefulSet')
                    kube('-n', NAMESPACE, 'scale', 'statefulset/mongodb', '--replicas=0')
                print('Owned workloads scaled to zero; volumes retained.')
        else:
            status()


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        if isinstance(error, subprocess.CalledProcessError):
            # kubectl can echo submitted secret YAML on a validation failure.
            print('Command failed; private input/output withheld. Exit: ' + str(error.returncode), file=sys.stderr)
        else:
            print(str(error), file=sys.stderr)
        sys.exit(1)
