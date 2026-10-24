"""Shared bean-reference selection for assembly imports, audits and credentials.

An import of an API constant or nested request DTO does not require a client
bean or grant a service capability. This is a conservative source check;
Spring context refresh remains the authoritative wiring check.
"""
import json
from pathlib import Path
import re
import sys


def uses_api(source, qualified):
    if qualified not in source:
        return False
    source = re.sub(r'/\*.*?\*/|//[^\n]*', '', source, flags=re.S)
    spellings = [qualified]
    simple = qualified.rsplit('.', 1)[1]
    if re.search(r'\bimport\s+' + re.escape(qualified) + r'\s*;', source):
        spellings.append(simple)
    for name in spellings:
        # Runtime method-policy annotations require the same owner/client bean.
        if re.search(r'@' + re.escape(name) + r'\s*(?:\(|$)', source, flags=re.M):
            return True
        if re.search(r'(?<![\w.])' + re.escape(name) + r'\s*\.\s*class\b', source):
            return True
        pattern = (r'(?<![\w.])' + re.escape(name)
                   + r'(?:\s*<[^;{}()]*>)?\s*(?:>\s*)?\s+\w+\s*(?=[,);=])')
        if re.search(pattern, source):
            return True
    return False


def references(binding):
    return [f"com.raxim.myscoutee.domain.{binding['domain']}.api.{api}"
            for api in [binding['api'], *binding.get('providedApis', [])]] + binding.get('consumerApis', [])


def consumer_domains(binding, sources):
    # Library implementations no longer appear in the application's source scan.
    # Declare their unchanged bean requirements in the existing transport manifest.
    explicit = binding.get('consumerDomains', [])
    if not isinstance(explicit, list) or not all(isinstance(domain, str) for domain in explicit) or not set(explicit).issubset(sources):
        raise ValueError(f"Invalid consumer domains for {binding['configuration']}")
    detected = {domain for domain, source in sources.items()
                if any(uses_api(source, ref) for ref in references(binding))}
    return sorted(detected.union(explicit))


def usage(shell, backend):
    root = Path(backend) / 'server/projects/profile/src/main/java/com/raxim/myscoutee/domain'
    sources = {domain.name: '\n'.join(file.read_text() for file in domain.rglob('*.java')
               if file.relative_to(domain).parts[0] not in ('api', 'dto', 'transport'))
               for domain in root.iterdir() if domain.is_dir()}
    manifest = json.loads((Path(shell) / 'transport/bindings.json').read_text())
    domains = {domain for service in json.loads((Path(shell) / 'assemblies.json').read_text())['services'].values()
               for domain in service['domains']}
    sources.update({domain: '' for domain in domains if domain not in sources})
    return {binding['configuration']: consumer_domains(binding, sources)
            for binding in [*manifest['bindings'], *manifest.get('localBindings', [])]}


if __name__ == '__main__':
    print(json.dumps(usage(sys.argv[1], sys.argv[2])))
