#!/usr/bin/env python3
"""Install and open the optional Kiali view for the isolated Ambient experiment."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from control_plane import helm, load_receipt as mesh_receipt, release_inventory as mesh_inventory
from smoke import NAMESPACE, existing_namespace, kube, project_context, require_cluster


DIRECTORY = Path(__file__).resolve().parent
REPOSITORIES = {
    "prometheus-community": "https://prometheus-community.github.io/helm-charts",
    "kiali": "https://kiali.org/helm-charts",
}
CHARTS = (
    ("mesh-prometheus", "prometheus-community/prometheus", "29.40.0", "prometheus-values.yaml"),
    ("kiali-server", "kiali/kiali-server", "2.34.0", "kiali-values.yaml"),
)
PORT = 20001


def receipt_path(vm):
    return vm.STATE / "service-mesh-dashboard.json"


def load_receipt(vm):
    path = receipt_path(vm)
    if not path.is_file():
        return None
    receipt = json.loads(path.read_text())
    installed = receipt.get("releases")
    prefixes = [[row[0] for row in CHARTS[:index]] for index in range(len(CHARTS) + 1)]
    next_name = CHARTS[len(installed)][0] if isinstance(installed, list) and \
        installed in prefixes and len(installed) < len(CHARTS) else None
    if receipt.get("schemaVersion") != 1 or receipt.get("guestIP") != vm.IP or \
            installed not in prefixes or receipt.get("installing") not in (None, next_name):
        raise RuntimeError("Unrecognized dashboard installation receipt")
    return receipt


def save_receipt(vm, receipt):
    vm.private_directory(vm.STATE)
    path = receipt_path(vm)
    temporary = path.with_suffix(".new")
    if temporary.exists():
        raise RuntimeError("Interrupted dashboard receipt write; inspect private state")
    try:
        temporary.write_text(json.dumps(receipt, indent=2) + "\n")
        temporary.chmod(0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def inventory(vm):
    result = helm(vm, "list", "-n", "istio-system", "-o", "json")
    return {row["name"]: row for row in json.loads(result.stdout)}


def check_release(row, name, chart, version, *, statuses=("deployed",)):
    chart_name = chart.split("/")[1]
    if row.get("chart") != f"{chart_name}-{version}" or row.get("status") not in statuses:
        raise RuntimeError(f"Unexpected dashboard release {name}: {row.get('chart')} / {row.get('status')}")


def require_mesh(vm):
    receipt = mesh_receipt(vm)
    if receipt is None or receipt["releases"] != ["istio-base", "istiod", "istio-cni", "ztunnel"]:
        raise RuntimeError("Install this experiment's Istio control plane first")
    for name in receipt["releases"]:
        if mesh_inventory(vm).get(name, {}).get("status") != "deployed":
            raise RuntimeError(f"Istio release {name} is not deployed")


def repositories(vm):
    current = {row["name"]: row["url"] for row in json.loads(helm(vm, "repo", "list", "-o", "json").stdout or "[]")}
    for name, url in REPOSITORIES.items():
        if name in current and current[name] != url:
            raise RuntimeError(f"Helm repository {name} has a different URL")
        if name not in current:
            helm(vm, "repo", "add", name, url)
    helm(vm, "repo", "update", "prometheus-community", "kiali")


def install(vm):
    require_cluster(vm)
    require_mesh(vm)
    if existing_namespace(vm) is None:
        raise RuntimeError(f"Run smoke.py up before installing Kiali, so it can discover {NAMESPACE}")
    receipt = load_receipt(vm)
    if receipt is None:
        if any(name in inventory(vm) for name, _, _, _ in CHARTS):
            raise RuntimeError("Dashboard Helm release exists without this experiment's receipt")
        receipt = {"schemaVersion": 1, "guestIP": vm.IP, "releases": [], "installing": None}
        save_receipt(vm, receipt)
    repositories(vm)
    for name, chart, version, values in CHARTS:
        row = inventory(vm).get(name)
        if name in receipt["releases"]:
            check_release(row or {}, name, chart, version)
            continue
        if row:
            if receipt["installing"] != name:
                raise RuntimeError(f"Unrecorded dashboard release {name}; inspect before retrying")
            check_release(row, name, chart, version, statuses=("deployed", "failed"))
            if row["status"] == "failed":
                helm(vm, "uninstall", name, "-n", "istio-system", "--wait")
            else:
                receipt["releases"].append(name)
                receipt["installing"] = None
                save_receipt(vm, receipt)
                continue
        receipt["installing"] = name
        save_receipt(vm, receipt)
        result = helm(vm, "install", name, chart, "--version", version, "-n", "istio-system",
                      "-f", str(DIRECTORY / values), "--wait", "--timeout", "5m")
        print(result.stdout.strip(), flush=True)
        check_release(inventory(vm).get(name, {}), name, chart, version)
        receipt["releases"].append(name)
        receipt["installing"] = None
        save_receipt(vm, receipt)
    print("Kiali is installed. Run dashboard.py serve to open the local web view.")


def remove(vm):
    require_cluster(vm)
    receipt = load_receipt(vm)
    if receipt is None:
        raise RuntimeError("No owned dashboard installation receipt")
    pending = receipt["installing"]
    if pending:
        name, chart, version, _ = next(row for row in CHARTS if row[0] == pending)
        row = inventory(vm).get(name)
        if row:
            check_release(row, name, chart, version, statuses=("deployed", "failed"))
            helm(vm, "uninstall", name, "-n", "istio-system", "--wait")
        receipt["installing"] = None
        save_receipt(vm, receipt)
    for name, chart, version, _ in reversed(CHARTS):
        if name not in receipt["releases"]:
            continue
        check_release(inventory(vm).get(name, {}), name, chart, version)
        result = helm(vm, "uninstall", name, "-n", "istio-system", "--wait")
        print(result.stdout.strip(), flush=True)
        receipt["releases"].remove(name)
        save_receipt(vm, receipt)
    receipt_path(vm).unlink()


def serve(vm):
    require_cluster(vm)
    receipt = load_receipt(vm)
    if receipt is None or receipt["releases"] != [row[0] for row in CHARTS]:
        raise RuntimeError("Install the dashboard first")
    kube(vm, "-n", "istio-system", "get", "service", "kiali")
    remote = f"sudo k3s kubectl -n istio-system port-forward --address 127.0.0.1 service/kiali {PORT}:{PORT}"
    command = vm.ssh_command(remote)
    host = "myscoutee@" + vm.IP
    command[command.index(host):command.index(host)] = ["-o", "ExitOnForwardFailure=yes",
                                                     "-L", f"127.0.0.1:{PORT}:127.0.0.1:{PORT}"]
    print(f"Kiali: http://127.0.0.1:{PORT}/ (Ctrl+C closes the local tunnel)", flush=True)
    try:
        subprocess.run(command, check=True)
    except KeyboardInterrupt:
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "install", "status", "serve", "remove"))
    parser.add_argument("--project", type=Path,
                        default=Path(os.environ.get("MYSCOUTEE_BACKEND_SOURCE", Path.cwd())))
    args = parser.parse_args()
    vm, _, _ = project_context(args.project)
    if args.action == "plan":
        print(f"Kiali {CHARTS[1][2]} and Prometheus chart {CHARTS[0][2]} in {vm.NAME}; localhost:{PORT}")
        return
    if args.action == "install":
        install(vm)
    elif args.action == "remove":
        remove(vm)
    elif args.action == "serve":
        serve(vm)
    else:
        require_cluster(vm)
        print(json.dumps({"receipt": load_receipt(vm), "releases": {
            name: inventory(vm).get(name) for name, _, _, _ in CHARTS}}, indent=2))


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        print(f"service-mesh dashboard: {error.stderr.strip() or error}", file=sys.stderr)
        raise SystemExit(1)
    except (KeyError, ValueError, RuntimeError, OSError, subprocess.TimeoutExpired) as error:
        print(f"service-mesh dashboard: {error}", file=sys.stderr)
        raise SystemExit(1)
