#!/usr/bin/env bash
set -euo pipefail

repository_root="$(cd "${MYSCOUTEE_BACKEND_SOURCE:-$PWD}" && pwd)"
test_level="${1:-local}"
if [[ $# -gt 0 ]]; then shift; fi
case "${test_level}" in
  local) junit_task=test ;;
  standard|embedded|integration) junit_task="${test_level}Test" ;;
  *) echo 'Usage: ../myscoutee-microservices/tools/run-backend-junit.sh [local|standard|embedded|integration] [Gradle test options]' >&2; exit 2 ;;
esac

cd "${repository_root}/server"
exec ./gradlew ":projects:profile:${junit_task}" \
  -PisolatedJUnit=true \
  --project-cache-dir "${repository_root}/server/build/junit-gradle" \
  --no-daemon --parallel \
  "$@"
