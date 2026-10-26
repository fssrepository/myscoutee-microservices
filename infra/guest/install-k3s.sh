#!/usr/bin/env bash
# Runs inside the headless guest only. Reconcile this lab's pinned k3s service.
set -euo pipefail
[[ "${EUID}" == 0 ]] || { echo 'Guest provisioning requires root.' >&2; exit 2; }
python3 - <<'PY'
import hashlib, json, os, time, urllib.request
from pathlib import Path
versions = json.loads(Path('/etc/myscoutee-lab/versions.json').read_text())
artifact = versions['k3s']
target = Path('/usr/local/bin/k3s')
if os.environ.get('MYSCOUTEE_K3S_HOST_STAGED') == 'true':
    # The existing authenticated SSH transport supplies files from GitLab.
    # No credential, proxy or additional download service is installed here.
    archive = Path('/var/lib/rancher/k3s/agent/images/k3s-airgap-images-amd64.tar.zst')
    expected = [(target, artifact['sha256']), (archive, versions['k3sAirgap']['sha256'])]
    deadline = time.monotonic() + 300
    while not all(p.is_file() and hashlib.sha256(p.read_bytes()).hexdigest() == checksum for p, checksum in expected):
        if time.monotonic() >= deadline:
            raise SystemExit('Pinned k3s inputs were not supplied by the infrastructure job; no external download attempted.')
        time.sleep(2)
if not target.exists() or hashlib.sha256(target.read_bytes()).hexdigest() != artifact['sha256']:
    temporary = target.with_suffix('.download')
    with urllib.request.urlopen(artifact['url'], timeout=120) as source, temporary.open('wb') as output:
        while block := source.read(1024 * 1024): output.write(block)
    if hashlib.sha256(temporary.read_bytes()).hexdigest() != artifact['sha256']:
        temporary.unlink()
        raise SystemExit('k3s checksum mismatch')
    os.chmod(temporary, 0o755)
    temporary.replace(target)
PY
install -d -m 0750 /var/lib/myscoutee-lab
fingerprint=/var/lib/myscoutee-lab/k3s-inputs.sha256
candidate="$(mktemp /var/lib/myscoutee-lab/k3s-inputs.XXXXXX)"
trap 'rm -f -- "${candidate}"' EXIT
sha256sum /usr/local/bin/k3s /etc/rancher/k3s/config.yaml /etc/systemd/system/k3s.service | sha256sum > "${candidate}"
if ! cmp -s "${candidate}" "${fingerprint}"; then
  systemctl daemon-reload
  systemctl enable k3s.service
  systemctl restart k3s.service
  install -m 0600 "${candidate}" "${fingerprint}"
else
  systemctl start k3s.service
fi
systemctl is-active --quiet k3s.service
