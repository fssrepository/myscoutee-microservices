#!/usr/bin/env bash
# Static validation is available before installing any host virtualization packages.
set -euo pipefail
export MYSCOUTEE_BACKEND_SOURCE="$(realpath -- "${MYSCOUTEE_BACKEND_SOURCE:-$PWD}")"
umask 077
support_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
state_root="$(python3 "${support_root}/tools/runtime_config.py" state)"
for directory in "${state_root}" "${state_root}/tools" "${state_root}/terraform" "${state_root}/tmp"; do
  [[ ! -L "${directory}" ]] || { echo 'Refusing symlinked lab state.' >&2; exit 2; }
done
if [[ "${1:-validate}" == clean-tools ]]; then
  PYTHONPATH="${support_root}/tools${PYTHONPATH:+:$PYTHONPATH}" python3 - "${state_root}/vm/terraform.tfstate" <<'PY'
import sys
from pathlib import Path
from terraform_state import read_state
if read_state(Path(sys.argv[1])).get('resources'):
    raise SystemExit('Destroy the Terraform-owned VM resources before removing their tool/cache state.')
PY
  for directory in tools terraform/providers tmp helm; do
    [[ ! -L "${state_root}/${directory}" ]] || { echo 'Refusing symlinked tool state.' >&2; exit 2; }
  done
  rm -rf -- "${state_root}/tools" "${state_root}/terraform/providers" "${state_root}/tmp" "${state_root}/helm"
  rmdir "${state_root}/terraform" "${state_root}" 2>/dev/null || true
  exit 0
fi
install -d -m 0700 "${state_root}/tools" "${state_root}/terraform" "${state_root}/tmp"
export TF_DATA_DIR="${state_root}/terraform/providers"
export TMPDIR="${state_root}/tmp"
export TF_IN_AUTOMATION=1
TF_CLI_CONFIG_FILE="$(python3 "${support_root}/tools/terraform_cache.py" "${support_root}" "${state_root}")"
export TF_CLI_CONFIG_FILE
python3 - "${support_root}/infra/versions.json" "${state_root}/tools" <<'PY'
import hashlib, io, json, os, sys, urllib.request, zipfile
from pathlib import Path
artifact = json.loads(Path(sys.argv[1]).read_text())['terraform']
root = Path(sys.argv[2])
archive = root / ('terraform-' + artifact['version'] + '.zip')
if not archive.exists() or hashlib.sha256(archive.read_bytes()).hexdigest() != artifact['sha256']:
    if os.environ.get('MYSCOUTEE_INFRA_OFFLINE') == 'true':
        raise SystemExit('Pinned Terraform installer is missing. Run the optional manual infrastructure preparation job.')
    with urllib.request.urlopen(artifact['url'], timeout=60) as source:
        data = source.read()
    if hashlib.sha256(data).hexdigest() != artifact['sha256']:
        raise SystemExit('Terraform checksum mismatch')
    archive.write_bytes(data)
with zipfile.ZipFile(archive) as files:
    content = files.read('terraform')
target = root / 'terraform'
if not target.exists() or target.read_bytes() != content:
    target.write_bytes(content)
    os.chmod(target, 0o700)
PY
terraform_bin="${state_root}/tools/terraform"
module="$(python3 "${support_root}/tools/terraform_state.py" module)"
case "${1:-validate}" in
  validate)
    "${terraform_bin}" -chdir="${module}" init -backend=false -input=false
    "${terraform_bin}" -chdir="${module}" fmt -check
    "${terraform_bin}" -chdir="${module}" validate
    ;;
  fmt) "${terraform_bin}" -chdir="${module}" fmt ;;
  run) shift; exec "${terraform_bin}" -chdir="${module}" "$@" ;;
  *) echo 'Usage: terraform.sh [validate|fmt|clean-tools|run <terraform arguments>]' >&2; exit 2 ;;
esac
