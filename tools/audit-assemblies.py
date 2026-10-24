#!/usr/bin/env python3
"""Check physical artifact ownership and list candidate remote dependencies.

The report is an inventory, not a declaration that arbitrary Java contracts
are serializable or that the services have working network bindings.
"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import zipfile
from transport_usage import uses_api, usage

from runtime_config import SHELL, BUILD, BACKEND
JAVA = BACKEND / 'server/projects/profile/src/main/java'
PREFIX = 'com/raxim/myscoutee/domain/'


def configuration():
    config = json.loads((SHELL / 'assemblies.json').read_text())
    if config['schemaVersion'] != 1:
        raise ValueError('Unsupported assembly schema')
    services = config['services']
    owners = {}
    for name, service in services.items():
        if not re.fullmatch('[a-z][a-z0-9-]*', name):
            raise ValueError(f'Unsafe service name: {name}')
        if not service['domains']:
            raise ValueError(f'{name} owns no domains')
        if service['heapMiB'] >= service['memoryMiB']:
            raise ValueError(f'{name} needs native memory outside its heap')
        for domain in service['domains']:
            if domain in owners:
                raise ValueError(f'Duplicate domain owner: {domain}')
            owners[domain] = name
    actual = {p.name for p in (JAVA / PREFIX).iterdir() if p.is_dir()}
    actual.update(json.loads((BACKEND / 'server/gradle/base-domains.json').read_text()))
    if actual != set(owners):
        raise ValueError(f'Domain coverage differs: missing={actual-set(owners)}, unknown={set(owners)-actual}')
    return services, owners


def class_owner(path):
    pos = path.find(PREFIX)
    if pos == -1 or not path.endswith('.class'):
        return None
    domain, area, *_ = path[pos + len(PREFIX):].split('/')
    return domain, area


def audit_jar(name, assembly, transport_usage):
    path = BUILD / name / 'libs' / f'myscoutee-{name}.jar'
    owned_count = 0
    libraries = []
    classes = set()
    with zipfile.ZipFile(path) as jar:
        assert 'BOOT-INF/classes/com/raxim/myscoutee/Main.class' not in jar.namelist(), name
        assert 'BOOT-INF/classes/application-dev.yaml' not in jar.namelist(), name
        assert 'SPRING_PROFILES_ACTIVE:prod' in jar.read('BOOT-INF/classes/application.yaml').decode(), name
        assert jar.read('BOOT-INF/classes/application-prod.yaml') == (BACKEND / 'server/projects/profile/src/main/resources/application-prod.yaml').read_bytes(), name
        properties = jar.read('BOOT-INF/classes/application.properties').decode()
        assert 'management.server.port=8081' in properties, name
        assert f'spring.application.name={name}\n' in properties, name
        metadata = jar.read('META-INF/myscoutee-assembly.properties').decode()
        assert f'assembly={name}\n' in metadata
        assert f"domains={','.join(assembly['domains'])}\n" in metadata
        # Verify imports in the executable configuration, not only packaged adapter classes.
        configuration_bytes = jar.read('BOOT-INF/classes/com/raxim/myscoutee/microservices/bootstrap/AssemblyConfiguration.class')
        websocket_config = 'com/raxim/myscoutee/common/config/ChatWebSocketConfig'
        owns_chat = 'chat' in assembly['domains']
        socket_transport = 'com/raxim/myscoutee/microservices/bootstrap/ChatWebSocketTransportConfiguration'
        needs_socket_transport = owns_chat and 'accounts' not in assembly['domains']
        assert (socket_transport.encode() in configuration_bytes) == needs_socket_transport, f'{name}: incorrect socket authorization binding'
        assert (f'BOOT-INF/classes/{socket_transport}.class' in jar.namelist()) == needs_socket_transport, f'{name}: incorrect socket transport ownership'
        assert (websocket_config.encode() in configuration_bytes) == owns_chat, f'{name}: incorrect chat WebSocket configuration import'
        assert (f'BOOT-INF/classes/{websocket_config}.class' in jar.namelist()) == owns_chat, f'{name}: incorrect chat WebSocket configuration ownership'
        for shared_import in json.loads((SHELL / 'assemblies.json').read_text())['sharedConfigurationImports']:
            assert shared_import.replace('.', '/').encode() in configuration_bytes, f'{name}: missing configured shared import {shared_import}'
        for binding in json.loads((SHELL / 'transport/bindings.json').read_text())['bindings']:
            domain, contract = binding['domain'], binding['api']
            transport, prefix = binding['package'], binding['configuration']
            references = [f'com.raxim.myscoutee.domain.{domain}.api.{api}'
                          for api in [contract, *binding.get('providedApis', [])]] + binding.get('consumerApis', [])
            consumes = any(domain in assembly['domains'] for domain in transport_usage[binding['configuration']])
            owner = domain in assembly['domains']
            client = f'{transport}/{prefix}ClientConfiguration'.encode()
            server = f'{transport}/{prefix}ServerConfiguration'.encode()
            assert (server in configuration_bytes) == owner, f'{name}: incorrect {contract} owner binding'
            assert (client in configuration_bytes) == (consumes and not owner), f'{name}: missing or unexpected {contract} client binding'
        for binding in json.loads((SHELL / 'transport/bindings.json').read_text()).get('localBindings', []):
            references = [f"com.raxim.myscoutee.domain.{binding['domain']}.api.{api}"
                          for api in [binding['api'], *binding.get('providedApis', [])]]
            consumes = any(domain in assembly['domains'] for domain in transport_usage[binding['configuration']])
            expected = consumes and (binding.get('includeOwner', False) or binding['domain'] not in assembly['domains'])
            local_configuration = ('bootstrap/' + binding['configuration']).encode()
            assert (local_configuration in configuration_bytes) == expected, f"{name}: incorrect local {binding['api']} binding"
        for item in jar.namelist():
            containers = [(item.removeprefix('BOOT-INF/classes/'), None)]
            if item.startswith('BOOT-INF/lib/') and item.endswith('.jar'):
                libraries.append(item.rsplit('/', 1)[-1])
                assert 'devtools' not in item, item
                with zipfile.ZipFile(io.BytesIO(jar.read(item))) as nested:
                    containers = [(entry, item) for entry in nested.namelist()]
            for entry, nested_name in containers:
                if entry.startswith('com/raxim/myscoutee/') and entry.endswith('.class'):
                    assert entry not in classes, f'{name}: duplicate class {entry}'
                    classes.add(entry)
                ownership = class_owner(entry)
                if ownership:
                    domain, area = ownership
                    assert area in ('api', 'dto') or domain in assembly['domains'], f'{name}: foreign implementation {entry} in {nested_name}'
                    if area not in ('api', 'dto', 'transport'):
                        owned_count += 1
        assert owned_count > 0, name
        assert any(lib.startswith('micrometer-registry-prometheus-') for lib in libraries), name
    return {'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'jarMiB': round(path.stat().st_size / 1024 ** 2, 1),
            'implementationClasses': owned_count, 'libraries': sorted(libraries)}


def inventory(assembly, owners):
    references = {}
    for domain in assembly['domains']:
        for file in (JAVA / PREFIX / domain).rglob('*.java'):
            if file.relative_to(JAVA / PREFIX / domain).parts[0] == 'transport':
                continue
            source = re.sub(r'/\*.*?\*/|//[^\n]*', '', file.read_text(), flags=re.S)
            for target, area, symbol in re.findall(r'com\.raxim\.myscoutee\.domain\.(\w+)\.(api|dto)\.([\w.$]+)', source):
                if target not in assembly['domains'] and area == 'api':
                    key = f'com.raxim.myscoutee.domain.{target}.api.{symbol}'
                    entry = references.setdefault(key, {'owner': owners[target], 'callers': []})
                    entry['callers'].append(str(file.relative_to(JAVA)))
    for entry in references.values():
        entry['callers'] = sorted(set(entry['callers']))
    return dict(sorted(references.items()))


def injected_inventory(assembly, owners):
    """Source candidates, not a substitute for Spring context verification.

    Include qualified declarations and constructor parameters as well as
    imported field types. Keep the exact source locations so a reviewer can
    classify configuration, data views and real remote bean dependencies.
    """
    manifest = json.loads((SHELL / 'transport/bindings.json').read_text())
    bound = {f"com.raxim.myscoutee.domain.{b['domain']}.api.{api}"
             for b in [*manifest['bindings'], *manifest.get('localBindings', [])]
             for api in [b['api'], *b.get('providedApis', [])]}
    candidates = {}
    for domain in assembly['domains']:
        for file in (JAVA / PREFIX / domain).rglob('*.java'):
            if file.relative_to(JAVA / PREFIX / domain).parts[0] in ('api', 'dto', 'transport'):
                continue
            source = re.sub(r'/\*.*?\*/|//[^\n]*', lambda m: '\n' * m[0].count('\n'),
                            file.read_text(), flags=re.S)
            imports = dict((symbol, f'com.raxim.myscoutee.domain.{target}.api.{symbol}')
                           for target, symbol in re.findall(
                               r'import\s+com\.raxim\.myscoutee\.domain\.(\w+)\.api\.(\w+)\s*;', source))
            types = dict(imports)
            for qualified in re.findall(r'com\.raxim\.myscoutee\.domain\.\w+\.api\.\w+', source):
                types[qualified] = qualified
            constructors = [(match.start(1), match.end(1)) for match in re.finditer(
                r'\b' + re.escape(file.stem) + r'\s*\(([^{};]*)\)\s*(?:throws\s+[^{}]+)?\{', source)]
            for spelling, qualified in types.items():
                target = qualified.split('.')[4]
                if target in assembly['domains'] or qualified in bound:
                    continue
                # A declaration must be a field or parameter, not merely a
                # method call, return type, import, cast or class literal.
                # Lazy/provider and collection injections still require an owner bean.
                # Start the match at the wrapper so the field/constructor checks below
                # examine the declaration, not the nested API token in its generic.
                wrapper = r'(?:(?:[\w]+\.)*(?:ObjectProvider|ObjectFactory|Provider|Optional|List|Set|Collection)\s*<\s*)'
                pattern = (r'(?<![\w.])(?:' + wrapper + re.escape(spelling) + r'\s*>|'
                           + re.escape(spelling) + r'(?:\s*<[^;{}()]*>)?)\s+(\w+)\s*(?=[,);=])')
                locations = []
                for match in re.finditer(pattern, source):
                    line_start = source.rfind('\n', 0, match.start()) + 1
                    line_end = source.find('\n', match.end())
                    prefix = source[line_start:match.start()]
                    field = re.search(r'\b(?:private|protected|public)\s+(?:final\s+)?$', prefix)
                    parameter = any(start <= match.start() < end for start, end in constructors)
                    if not (field or parameter):
                        continue
                    locations.append({'file': str(file.relative_to(JAVA)),
                                      'line': source.count('\n', 0, match.start()) + 1,
                                      'declaration': source[line_start:line_end if line_end >= 0 else None].strip()})
                if locations:
                    entry = candidates.setdefault(qualified, {'owner': owners[target], 'declarations': []})
                    entry['declarations'].extend(locations)
    return dict(sorted(candidates.items()))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--artifacts', action='store_true')
    args = parser.parse_args()
    services, owners = configuration()
    report = {'issue': json.loads((SHELL / 'assemblies.json').read_text())['issue'], 'runtimeVerified': False, 'services': {}}
    transport_usage = usage(SHELL, BACKEND) if args.artifacts else {}
    for name, assembly in services.items():
        details = {'domains': assembly['domains'], 'candidateRemoteContracts': inventory(assembly, owners),
                   'unboundInjectedApiCandidates': injected_inventory(assembly, owners)}
        if args.artifacts:
            details['artifact'] = audit_jar(name, assembly, transport_usage)
        report['services'][name] = details
        artifact = details.get('artifact', {})
        print(f"{name}: {len(assembly['domains'])} domains, {len(details['candidateRemoteContracts'])} remote API references"
              + (f", {artifact['jarMiB']} MiB JAR, ownership PASS" if artifact else ''))
    destination = BUILD / 'reports/assemblies.json'
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, indent=2) + '\n')
    print(f'Report: {destination}')


if __name__ == '__main__':
    main()
