#!/usr/bin/env python3
"""Generate a Dockerfile per configured assembly from the common image recipe."""
import importlib.util
from pathlib import Path
import json
import os
import shutil
import sys

# The caller selects its existing project context; common mechanisms stay here.
backend = Path(sys.argv[1] if len(sys.argv) == 2 else
               os.environ.get('MYSCOUTEE_BACKEND_SOURCE', Path.cwd())).resolve()
os.environ['MYSCOUTEE_BACKEND_SOURCE'] = str(backend)
from runtime_config import CONFIG, LAB, SHELL, SUPPORT, BUILD

spec = importlib.util.spec_from_file_location('assembly_audit', SUPPORT / 'tools/audit-assemblies.py')
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)
services, _ = audit.configuration()
template = (SUPPORT / 'deploy/images/service.Dockerfile.template').read_text()
output = BUILD / 'deployment'
output.mkdir(parents=True, exist_ok=True)
for name, service in services.items():
    stage = 'media-runtime' if 'media' in service['domains'] else 'jre-runtime'
    if service['runtimeStage'] != stage:
        raise ValueError(f'{name}: runtimeStage must be {stage} for its domains')
    text = (template.replace('@SERVICE@', name).replace('@RUNTIME_IMAGE@', CONFIG['images']['prefix'] + stage + ':' + CONFIG['images']['buildTag'])
            .replace('@HEAP_MIB@', str(service['heapMiB'])).replace('@ISSUE@', json.loads((audit.SHELL / 'assemblies.json').read_text())['issue']))
    (output / f'{name}.Dockerfile').write_text(text)
    print(f'{name}: {stage}, heap {service["heapMiB"]} MiB / container {service["memoryMiB"]} MiB')

# JSON is valid YAML; the target list follows the same deployment inventory.
import json
import vm
runtime = json.loads((audit.SHELL / 'deploy/runtime.json').read_text())
monitoring = BUILD / 'observability'
monitoring.mkdir(parents=True, exist_ok=True)
(monitoring / 'prometheus.yml').write_text(json.dumps({
    'global': {'scrape_interval': '30s', 'scrape_timeout': '5s'},
    'scrape_configs': [
        {'job_name': 'prometheus', 'static_configs': [{'targets': ['127.0.0.1:' + str(LAB['prometheusPort'])]}]},
        {'job_name': 'myscoutee',
         'static_configs': [{'targets': [vm.IP + ':' + str(runtime['metricsNodePort'])], 'labels': {'service': name}} for name in services],
         'relabel_configs': [{'source_labels': ['service'], 'target_label': '__metrics_path__', 'replacement': '/metrics/$1'}]}
    ]
}, indent=2) + '\n')

# Reuse the versioned dashboard and provisioning; only project labels/ports vary.
for source in (SUPPORT / 'observability/grafana').rglob('*'):
    if not source.is_file():
        continue
    target = monitoring / 'grafana' / source.relative_to(SUPPORT / 'observability/grafana')
    target.parent.mkdir(parents=True, exist_ok=True)
    text = source.read_text()
    for key, value in {'DISPLAY_NAME': LAB['displayName'],
                       'ISSUE': json.loads((audit.SHELL / 'assemblies.json').read_text())['issue'],
                       'PROMETHEUS_PORT': str(LAB['prometheusPort'])}.items():
        text = text.replace('@' + key + '@', json.dumps(value)[1:-1])
    target.write_text(text)

# Helm reads one complete chart. Assemble source owners into project build output,
# retaining the exact existing templates/defaults rather than another source tree.
chart = BUILD / 'chart'
inputs = {}
for root in (SUPPORT / 'deploy/chart', backend / 'config/microservices/chart'):
    for source in root.rglob('*'):
        if source.is_file():
            relative = source.relative_to(root)
            if relative in inputs:
                raise ValueError('Duplicate chart source ownership: ' + str(relative))
            inputs[relative] = source
for target in chart.rglob('*'):
    if target.is_file() and target.relative_to(chart) not in inputs:
        target.unlink()
for relative, source in inputs.items():
    target = chart / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
