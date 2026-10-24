#!/usr/bin/env bash
set -euo pipefail
shell_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
backend_root="$(realpath -- "${MYSCOUTEE_BACKEND_SOURCE:-$PWD}")"
if [[ ! -x "${backend_root}/server/gradlew" ]]; then
  echo 'Set MYSCOUTEE_BACKEND_SOURCE to a backend checkout containing server/gradlew.' >&2
  exit 2
fi
exec "${backend_root}/server/gradlew" -p "${shell_root}" \
  --project-cache-dir "${backend_root}/server/build/microservices/.gradle" \
  -PbackendSourceDir="${backend_root}" "$@"
