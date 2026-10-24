#!/usr/bin/env bash
# Reproducible desktop entry points. Repeated calls do not duplicate each view.
set -euo pipefail
export MYSCOUTEE_BACKEND_SOURCE="$(realpath -- "${MYSCOUTEE_BACKEND_SOURCE:-$PWD}")"
umask 077
tools_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
state_root="$(python3 "${tools_root}/runtime_config.py" state)"
display_name="$(python3 "${tools_root}/runtime_config.py" lab.displayName)"
install -d -m 0700 "${state_root}"
case "${1:-runtime}" in
  runtime|observe)
    flock -n -E 0 "${state_root}/terminal-observe.lock" \
      gnome-terminal --wait --window --geometry=116x38 --title="${display_name} — VM / Kubernetes / containers" \
      -- python3 "${tools_root}/vm.py" observe &
    if [[ "${1:-runtime}" == "runtime" ]]; then
    flock -n -E 0 "${state_root}/terminal-console.lock" \
      gnome-terminal --wait --window --maximize --title="${display_name} — guest shell" \
      -- python3 "${tools_root}/vm.py" console &
    fi
    wait
    ;;
  setup)
    flock -n -E 0 "${state_root}/terminal-setup.lock" \
      gnome-terminal --wait --window --title="${display_name} — host setup" \
      -- bash -c 'bash "$1/setup-host.sh"; result=$?; printf "\nExit status: %s\nPress Enter to close.\n" "$result"; read -r; exit "$result"' _ "${tools_root}"
    ;;
  *) echo 'Usage: open-terminals.sh [runtime|observe|setup]' >&2; exit 2 ;;
esac
