#!/usr/bin/env bash
# Pinned user-local Helm; no host package or default kubeconfig changes.
set -euo pipefail
export MYSCOUTEE_BACKEND_SOURCE="$(realpath -- "${MYSCOUTEE_BACKEND_SOURCE:-$PWD}")"
umask 077
support_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
state_root="$(python3 "${support_root}/tools/runtime_config.py" state)"
python3 - "${support_root}" "${state_root}" "${support_root}" <<'PY'
import hashlib, json, os, sys, tarfile, urllib.request
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]) / 'tools'))
import vm
root = Path(sys.argv[2])
for directory in (root, root / 'tools', root / 'helm'):
    vm.private_directory(directory)
artifact = json.loads((Path(sys.argv[3]) / 'infra/helm-version.json').read_text())
archive = root / 'tools' / ('helm-' + artifact['version'] + '.tar.gz')
if not archive.exists() or hashlib.sha256(archive.read_bytes()).hexdigest() != artifact['sha256']:
    with urllib.request.urlopen(artifact['url'], timeout=60) as source:
        content = source.read()
    if hashlib.sha256(content).hexdigest() != artifact['sha256']:
        raise SystemExit('Helm archive checksum mismatch')
    archive.write_bytes(content)
with tarfile.open(archive) as stream:
    content = stream.extractfile('linux-amd64/helm').read()
target = root / 'tools/helm'
if not target.exists() or target.read_bytes() != content:
    target.write_bytes(content)
    target.chmod(0o700)
PY
export HELM_CACHE_HOME="${state_root}/helm/cache"
export HELM_CONFIG_HOME="${state_root}/helm/config"
export HELM_DATA_HOME="${state_root}/helm/data"
export KUBECONFIG="${state_root}/vm/kubeconfig"
exec "${state_root}/tools/helm" "$@"
