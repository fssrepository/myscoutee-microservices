#!/usr/bin/env bash
set -euo pipefail
support_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
backend_root="$(realpath -- "${MYSCOUTEE_BACKEND_SOURCE:-$PWD}")"
export MYSCOUTEE_BACKEND_SOURCE="${backend_root}"
state_root="$(python3 "${support_root}/tools/runtime_config.py" state)"
shell_root="${backend_root}/config/microservices"
build_root="${backend_root}/server/build/microservices"
# Public convenience credential for this loopback-only developer lab.
config() { python3 "${support_root}/tools/runtime_config.py" "$1"; }
grafana_lab_password="$(config lab.grafanaPassword)"
image_prefix="$(config images.prefix)"
image_tag="$(config images.buildTag)"
immutable_version="$(config images.immutableVersion)"
frontend_base="$(config images.frontendBase)"
monitoring_project="$(config lab.monitoringProject)"
export LAB_PROMETHEUS_PORT="$(config lab.prometheusPort)"
export LAB_GRAFANA_PORT="$(config lab.grafanaPort)"
export LAB_MONITORING_PROJECT="${monitoring_project}"
export LAB_ISSUE="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["issue"])' "${shell_root}/assemblies.json")"

preflight() {
  python3 - <<'PY'
import fcntl, os, shutil
from pathlib import Path
mem = dict(line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())
print('Available memory: %.1f GiB' % (int(mem['MemAvailable'].split()[0]) / 1024 ** 2))
print('Free workspace disk: %.1f GiB' % (shutil.disk_usage('.').free / 1024 ** 3))
try:
    fd = os.open('/dev/kvm', os.O_RDWR)
    try: print('KVM API:', fcntl.ioctl(fd, 0xAE00, 0))
    finally: os.close(fd)
except OSError as error: print('KVM unavailable:', error.strerror)
for name in ('docker', 'java', 'qemu-system-x86_64', 'virsh', 'terraform', 'kubectl'):
    print(name + ':', shutil.which(name) or 'not installed')
PY
  docker ps --format '{{.Names}}  {{.Image}}'
}

build_jars() {
  require_idle_build_owner
  local revision
  revision="$(git -C "${backend_root}" rev-parse HEAD)"
  "${support_root}/gradlew" --no-daemon --parallel \
    -PbackendRevision="${revision}" "${@:-verifyAssemblies}"
}

require_idle_build_owner() {
  # Never compete with an existing hot-deploy owner. Preflight output remains reviewable.
  preflight
  if ps -eo args= | grep -E '[b]ootRun|[f]rontend_auto_watch|[s]tart_profilemanagement_dev|[n]g serve' >/dev/null; then
    echo 'An existing dev build/server owns compilation. Use its pipeline; separate assembly build stopped.' >&2
    exit 2
  fi
  if docker ps --format '{{.Names}} {{.Image}}' | grep -Ei 'sync|watch|(^|[-_:])dev($|[-_: ])' >/dev/null; then
    echo 'A dev/watch container is running. Stop its owner before this serial packaging build.' >&2
    exit 2
  fi
}

build_frontend_image() {
  require_idle_build_owner
  install -d -m 0700 "${state_root}"
  touch "${state_root}/images.tsv"
  chmod 0600 "${state_root}/images.tsv"
  # Reuse the canonical optimized build, stamping and production Dockerfile.
  # This explicit release action is never called by VM/workload startup.
  (cd "${backend_root}/frontend" && \
    node_modules/.bin/ng build --configuration production && \
    FRONTEND_BUILD_OUTPUT_DIR=prod node scripts/stamp-prod-build.js)
  package_frontend_image
}

package_frontend_image() {
  # Repackage an already built canonical bundle; no Angular compilation.
  test -f "${backend_root}/frontend/prod/index.html"
  docker build --network=none --pull=false --file "${backend_root}/server/docker/images/nginx/prod/Dockerfile" \
    --build-context "frontend-prod=${backend_root}/frontend/prod" \
    --build-context "nginx-config=${backend_root}/config/nginx" \
    --build-arg "NGINX_BASE_IMAGE=${NGINX_BASE_IMAGE:-$(config images.nginxBase)}" \
    --label "com.myscoutee.task=${LAB_ISSUE}" --tag "${frontend_base}" \
    "${backend_root}/server/docker"
  record_image "${frontend_base}"
  docker tag "${frontend_base}" "${image_prefix}frontend:${image_tag}"
  record_image "${image_prefix}frontend:${image_tag}"
}

record_image() {
  local image_ref="$1" image_id
  install -d -m 0700 "${state_root}"
  touch "${state_root}/images.tsv"
  chmod 0600 "${state_root}/images.tsv"
  image_id="$(docker image inspect --format '{{.Id}}' "${image_ref}")"
  printf '%s\t%s\n' "${image_ref}" "${image_id}" >> "${state_root}/images.tsv"
}

build_minio_image() {
  require_idle_build_owner
  install -d -m 0700 "${state_root}"
  touch "${state_root}/images.tsv"
  chmod 0600 "${state_root}/images.tsv"
  # Reuse the production image and seed startup; startup never calls this build.
  docker build --file "${backend_root}/server/docker/images/minio/Dockerfile" \
    --label "com.myscoutee.task=${LAB_ISSUE}" --tag "${image_prefix}minio:${image_tag}" \
    "${backend_root}/server"
  record_image "${image_prefix}minio:${image_tag}"
}

build_release_simulator_image() {
  require_idle_build_owner
  install -d -m 0700 "${state_root}"
  touch "${state_root}/images.tsv"
  chmod 0600 "${state_root}/images.tsv"
  mkdir -p "${build_root}/runtime-context"
  docker build --file "${backend_root}/server/docker/images/release-simulator/Dockerfile" \
    --label "com.myscoutee.task=${LAB_ISSUE}" --tag "${image_prefix}release-simulator-base:${image_tag}" \
    "${build_root}/runtime-context"
  record_image "${image_prefix}release-simulator-base:${image_tag}"
  docker build --network=none --pull=false --file "${support_root}/deploy/release-simulator.Dockerfile" \
    --build-arg "RELEASE_SIMULATOR_BASE_IMAGE=${image_prefix}release-simulator-base:${image_tag}" \
    --build-context "backend-source=${backend_root}" --label "com.myscoutee.task=${LAB_ISSUE}" \
    --tag "${image_prefix}release-simulator:${image_tag}" "${build_root}/runtime-context"
  record_image "${image_prefix}release-simulator:${image_tag}"
}

build_support_images() {
  require_idle_build_owner
  install -d -m 0700 "${state_root}"
  touch "${state_root}/images.tsv"
  chmod 0600 "${state_root}/images.tsv"
  local service source image_ref
  # The native Kubernetes image volume mounts these files directly. No process,
  # sidecar or init container runs from this data-only artifact.
  image_ref="${image_prefix}mongo-seed:${image_tag}"
  docker build --network=none --pull=false --file "${support_root}/deploy/mongo-seed.Dockerfile" \
    --label "com.myscoutee.task=${LAB_ISSUE}" --tag "${image_ref}" \
    "${backend_root}/server/docker/conf/mongodb"
  record_image "${image_ref}"
  # Production Dockerfiles own these optimized Go binaries. Build serially;
  # never overwrite the monolith's tags or start a second dev/watch process.
  while IFS=$'\t' read -r service source; do
    source="$(realpath -- "${backend_root}/${source}")"
    local dockerfile="${source}/Dockerfile"
    if [[ "${service}" == mcp ]]; then
      dockerfile="${backend_root}/server/docker/images/mcp/Dockerfile"
    fi
    [[ -f "${dockerfile}" ]] || { echo "Missing canonical Dockerfile: ${dockerfile}" >&2; return 2; }
    image_ref="${image_prefix}${service}:${image_tag}"
    docker build --file "${dockerfile}" --label "com.myscoutee.task=${LAB_ISSUE}" \
      --tag "${image_ref}" "${source}"
    record_image "${image_ref}"
  done < <(python3 - "${shell_root}/deploy/runtime.json" <<'CONFIG'
import json, sys
config = json.load(open(sys.argv[1]))
for name, source in config['images']['supportSources'].items():
    if name in config['dependencies']:
        print(name + '\t' + source)
CONFIG
  )
}

build_images() {
  python3 "${support_root}/tools/audit-assemblies.py" --artifacts
  python3 "${support_root}/tools/render-deployment.py" "${backend_root}"
  install -d -m 0700 "${state_root}"
  touch "${state_root}/images.tsv"
  chmod 0600 "${state_root}/images.tsv"
  local stage service revision dirty image_ref
  revision="$(git -C "${backend_root}" rev-parse HEAD)"
  dirty=false
  [[ -z "$(git -C "${backend_root}" status --porcelain --untracked-files=normal)" ]] || dirty=true
  mkdir -p "${build_root}/runtime-context"
  for stage in jre-runtime media-runtime; do
    image_ref="${image_prefix}${stage}:${image_tag}"
    docker build --target "${stage}" --label "com.myscoutee.task=${LAB_ISSUE}" \
      --file "${backend_root}/server/docker/images/profilemanagement/prod/Dockerfile" \
      --tag "${image_ref}" "${build_root}/runtime-context"
    record_image "${image_ref}"
  done
  while IFS= read -r service; do
    image_ref="${image_prefix}${service}:${image_tag}"
    docker build --file "${build_root}/deployment/${service}.Dockerfile" \
      --build-arg BACKEND_REVISION="${revision}" --build-arg BACKEND_DIRTY="${dirty}" \
      --tag "${image_ref}" "${build_root}/${service}/libs"
    record_image "${image_ref}"
  done < <(python3 -c 'import json,sys; print("\n".join(json.load(open(sys.argv[1]))["services"]))' "${shell_root}/assemblies.json")
}

clean_images() {
  [[ -f "${state_root}/images.tsv" ]] || return 0
  local image_ref expected actual failed=0
  # Only task-recorded tags still pointing to the recorded image are removable.
  # No force, container removal, global prune, or build-cache removal.
  while IFS=$'\t' read -r image_ref expected; do
    if ! { [[ "${expected}" =~ ^sha256:[a-f0-9]{64}$ ]] && python3 - "${image_ref}" "${shell_root}" <<'IDENTITY'
import re, sys
sys.path.insert(0, sys.argv[2] + '/tools')
from runtime_config import CONFIG
images = CONFIG['images']
pattern = re.escape(images['prefix']) + r'[a-z0-9-]+:(?:' + re.escape(images['buildTag']) + '|' + re.escape(images['immutableVersion']) + r'-[a-f0-9]{12}|inputs-[a-f0-9]{64})'
if sys.argv[1] != images['frontendBase'] and not re.fullmatch(pattern, sys.argv[1]):
    raise SystemExit(1)
IDENTITY
    }; then
      echo 'Invalid image inventory; stopped.' >&2; return 2;
    fi
    actual="$(docker image inspect --format '{{.Id}}' "${image_ref}" 2>/dev/null || true)"
    [[ -n "${actual}" ]] || continue
    if [[ "${actual}" != "${expected}" ]]; then
      echo "Preserved changed image: ${image_ref}" >&2
      failed=1
      continue
    fi
    docker image rm "${image_ref}" || failed=1
  done < <(tac "${state_root}/images.tsv" | awk -F '\t' '!seen[$1]++')
  if [[ "${failed}" == 0 ]]; then rm "${state_root}/images.tsv"; rmdir "${state_root}" 2>/dev/null || true; fi
  return "${failed}"
}

monitoring() {
  local action="$1"
  export LAB_MONITORING_BUILD_DIRECTORY="${build_root}/observability"
  local environment_file="${state_root}/monitoring.env"
  if [[ "${action}" == up ]]; then
    python3 "${support_root}/tools/render-deployment.py" "${backend_root}"
    install -d -m 0700 "${state_root}"
    if [[ ! -f "${environment_file}" ]]; then
      (umask 077; printf 'GRAFANA_ADMIN_PASSWORD=%s\n' "${grafana_lab_password}" > "${environment_file}")
    fi
  fi
  [[ -f "${environment_file}" ]] || { echo 'Monitoring has no local state.'; return 0; }
  local -a compose=(docker compose --project-name "${monitoring_project}" --env-file "${environment_file}" -f "${support_root}/observability/compose.yaml")
  case "${action}" in
    up)
      "${compose[@]}" up -d
      # A newly created Prometheus has no signal handler yet: early SIGHUP
      # terminates it (exit 129). Reload only after its HTTP readiness gate.
      python3 - <<'PY'
import os, time, urllib.error, urllib.request
deadline = time.monotonic() + 60
while time.monotonic() < deadline:
    try:
        with urllib.request.urlopen('http://127.0.0.1:' + os.environ['LAB_PROMETHEUS_PORT'] + '/-/ready', timeout=2) as response:
            if response.status == 200:
                break
    except (OSError, urllib.error.URLError):
        pass
    time.sleep(1)
else:
    raise SystemExit('Prometheus did not become ready; inspect the owned monitoring container logs.')
PY
      "${compose[@]}" kill --signal SIGHUP prometheus
      ;;
    stop) "${compose[@]}" stop ;;
    reset-password)
      # Explicitly reconcile an existing lab volume to the documented login.
      python3 - "${environment_file}" "${grafana_lab_password}" <<'PY'
import base64, json, os, sys, urllib.request
from pathlib import Path
path = Path(sys.argv[1])
previous = path.read_text().strip().split('=', 1)[1]
password = sys.argv[2]
authorization = base64.b64encode(('lab:' + previous).encode()).decode()
request = urllib.request.Request('http://127.0.0.1:' + os.environ['LAB_GRAFANA_PORT'] + '/api/user/password',
    method='PUT', headers={'Authorization': 'Basic ' + authorization, 'Content-Type': 'application/json'},
    data=json.dumps({'oldPassword': previous, 'newPassword': password, 'confirmNew': password}).encode())
if previous != password:
    with urllib.request.urlopen(request, timeout=10) as response:
        if response.status != 200:
            raise SystemExit('Grafana password update failed; existing state preserved.')
    path.write_text('GRAFANA_ADMIN_PASSWORD=' + password + '\n')
print('The loopback-only Grafana login is now lab / ' + password)
PY
      ;;
    destroy)
      "${compose[@]}" down --volumes
      rm -- "${environment_file}"
      rmdir "${state_root}" 2>/dev/null || true
      ;;
  esac
}

case "${1:-preflight}" in
  preflight) preflight ;;
  build) build_jars; build_images ;;
  jars) build_jars ;;
  jars-build) build_jars assembleServices; python3 "${support_root}/tools/audit-assemblies.py" --artifacts ;;
  images) build_images ;;
  assembly-images) require_idle_build_owner; python3 "${support_root}/tools/assembly_images.py" "${@:2}" ;;
  frontend-image) build_frontend_image ;;
  frontend-package) package_frontend_image ;;
  minio-image) build_minio_image ;;
  support-images) build_support_images ;;
  release-simulator-image) build_release_simulator_image ;;
  render) python3 "${support_root}/tools/render-deployment.py" "${backend_root}" ;;
  monitoring-up) monitoring up ;;
  monitoring-stop) monitoring stop ;;
  monitoring-destroy) monitoring destroy ;;
  monitoring-reset-password) monitoring reset-password ;;
  clean-images) clean_images ;;
  clean-build)
    if [[ -n "$(docker ps -q --filter "label=com.docker.compose.project=${monitoring_project}")" ]]; then
      echo 'Stop the task monitoring stack before removing its generated configuration.' >&2
      exit 2
    fi
    [[ ! -L "${build_root}" && ! -L "${build_root}/.gradle" ]] || { echo 'Refusing symlinked build paths.' >&2; exit 2; }
    rm -rf -- "${build_root}" "${build_root}/.gradle"
    ;;
  *) echo 'Usage: lab.sh [preflight|build|jars|jars-build|images|frontend-image|frontend-package|minio-image|support-images|release-simulator-image|render|monitoring-up|monitoring-stop|monitoring-destroy|monitoring-reset-password|clean-images|clean-build]' >&2; exit 2 ;;
esac
