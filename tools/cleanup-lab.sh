#!/usr/bin/env bash
# Explicit full removal of this lab. Run in a terminal for the host sudo step.
set -euo pipefail
export MYSCOUTEE_BACKEND_SOURCE="$(realpath -- "${MYSCOUTEE_BACKEND_SOURCE:-$PWD}")"
umask 077
tools_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
state_root="$(python3 "${tools_root}/runtime_config.py" state)"
support_root="$(python3 "${tools_root}/runtime_config.py" support-root)"
[[ "${1:-}" == --purge-host || -z "${1:-}" ]] || {
  echo 'Usage: cleanup-lab.sh [--purge-host]. Deletes this lab and its data; no global prune.' >&2; exit 2;
}
if command -v virsh >/dev/null; then
  python3 "${tools_root}/vm.py" destroy
else
  # A missing libvirt tool is not permission to erase a resource inventory.
  PYTHONPATH="${support_root}/tools${PYTHONPATH:+:$PYTHONPATH}" python3 - "${state_root}/vm/terraform.tfstate" <<'PY'
import sys
from pathlib import Path
from terraform_state import read_state
if read_state(Path(sys.argv[1])).get('resources'):
    raise SystemExit('Restore host tooling before destroying recorded VM resources; state preserved.')
PY
fi
"${tools_root}/lab.sh" monitoring-destroy
"${tools_root}/lab.sh" clean-images
"${tools_root}/lab.sh" clean-build
"${tools_root}/terraform.sh" clean-tools
if [[ "${1:-}" == --purge-host ]]; then
  sudo python3 "${support_root}/tools/host.py" purge
  rm -f -- "${state_root}/host-setup.complete"
fi
echo 'Lab cleanup completed. Lifecycle lock files are retained; unrelated resources are preserved.'
