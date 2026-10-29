#!/usr/bin/env python3
"""Explicit, recorded Istio Ambient lifecycle for one MyScoutee lab."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from smoke import NAMESPACE, existing_namespace, kube, project_context, require_cluster


VERSION = "1.31.3"
REPOSITORY = "https://blob.istio.io/istio-release/charts"
CHARTS = (
    ("istio-base", "base", []),
    ("istiod", "istiod", ["--set", "profile=ambient"]),
    ("istio-cni", "cni", ["--set", "profile=ambient", "--set", "global.platform=k3s"]),
    ("ztunnel", "ztunnel", []),
)


def helm(vm, *args, check=True):
    command = [str(vm.SUPPORT / "tools/helm.sh"), *args]
    return subprocess.run(command, text=True, capture_output=True, check=check,
                          timeout=600, env={**os.environ, "MYSCOUTEE_BACKEND_SOURCE": str(vm.BACKEND)})


def receipt_path(vm):
    return vm.STATE / "service-mesh-experiment.json"


def load_receipt(vm):
    path = receipt_path(vm)
    if not path.is_file():
        return None
    receipt = json.loads(path.read_text())
    prefixes = [[row[0] for row in CHARTS[:index]] for index in range(len(CHARTS) + 1)]
    pending = receipt.get("installing")
    installed = receipt.get("releases")
    next_name = next((row[0] for row in CHARTS[len(installed):len(installed) + 1]), None) \
        if isinstance(installed, list) and installed in prefixes else None
    if receipt.get("schemaVersion") != 1 or receipt.get("guestIP") != vm.IP or \
            receipt.get("version") != VERSION or installed not in prefixes or \
            pending not in (None, next_name):
        raise RuntimeError("Unrecognized service mesh installation receipt")
    return receipt


def save_receipt(vm, receipt):
    vm.private_directory(vm.STATE)
    path = receipt_path(vm)
    temporary = path.with_suffix(".new")
    if temporary.exists():
        raise RuntimeError("Interrupted service mesh receipt write; inspect private state")
    try:
        temporary.write_text(json.dumps(receipt, indent=2) + "\n")
        temporary.chmod(0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def release_inventory(vm):
    result = helm(vm, "list", "-n", "istio-system", "-o", "json")
    return {row["name"]: row for row in json.loads(result.stdout)}


def check_release(row, name, chart):
    if row.get("chart") != f"{chart}-{VERSION}" or row.get("status") != "deployed":
        raise RuntimeError(f"Unexpected Helm release for {name}: {row.get('chart')} / {row.get('status')}")


def check_gateway_api(vm):
    result = kube(vm, "get", "crd", "gateways.gateway.networking.k8s.io", "-o", "json")
    crd = json.loads(result.stdout)
    bundle = crd["metadata"].get("annotations", {}).get("gateway.networking.k8s.io/bundle-version", "")
    if not bundle.startswith("v1.") or int(bundle.split(".")[1]) < 5:
        raise RuntimeError("Gateway API CRDs must be v1.5 or newer; inspect their existing owner")


def install(vm):
    require_cluster(vm)
    check_gateway_api(vm)
    receipt = load_receipt(vm)
    namespace = kube(vm, "get", "namespace", "istio-system", "--ignore-not-found", "-o", "name").stdout.strip()
    if receipt is None:
        if namespace:
            raise RuntimeError("istio-system already exists without this experiment's receipt; refusing adoption")
        receipt = {"schemaVersion": 1, "guestIP": vm.IP, "version": VERSION,
                   "releases": [], "installing": None}
        save_receipt(vm, receipt)
    if receipt.get("version") != VERSION:
        raise RuntimeError("Receipt belongs to another Istio version")
    repositories = json.loads(helm(vm, "repo", "list", "-o", "json").stdout or "[]")
    repo = next((row for row in repositories if row["name"] == "istio"), None)
    if repo and repo["url"] != REPOSITORY:
        raise RuntimeError("Existing Istio Helm repository has a different URL")
    if not repo:
        helm(vm, "repo", "add", "istio", REPOSITORY)
    helm(vm, "repo", "update", "istio")
    for name, chart, values in CHARTS:
        inventory = release_inventory(vm)
        if name in receipt["releases"]:
            check_release(inventory.get(name, {}), name, chart)
            continue
        if name in inventory:
            row = inventory[name]
            if receipt.get("installing") != name or row.get("chart") != f"{chart}-{VERSION}":
                raise RuntimeError(f"Unrecorded Istio release exists: {name}; inspect before retrying")
            if row.get("status") == "deployed":
                receipt["releases"].append(name)
                receipt["installing"] = None
                save_receipt(vm, receipt)
                continue
            if row.get("status") != "failed":
                raise RuntimeError(f"Owned Istio release {name} is {row.get('status')}; inspect before retrying")
            helm(vm, "uninstall", name, "-n", "istio-system", "--wait")
        receipt["installing"] = name
        save_receipt(vm, receipt)
        args = ["install", name, f"istio/{chart}", "--version", VERSION,
                "-n", "istio-system", "--wait", "--timeout", "5m", *values]
        if name == "istio-base":
            args.append("--create-namespace")
        result = helm(vm, *args)
        print(result.stdout.strip(), flush=True)
        check_release(release_inventory(vm).get(name, {}), name, chart)
        receipt["releases"].append(name)
        receipt["installing"] = None
        save_receipt(vm, receipt)
    print("Istio Ambient control plane installed; application namespace was not enrolled.")


def remove(vm):
    require_cluster(vm)
    receipt = load_receipt(vm)
    if receipt is None:
        raise RuntimeError("No owned Istio installation receipt")
    if (vm.STATE / "service-mesh-dashboard.json").exists():
        raise RuntimeError("Remove the recorded Kiali dashboard before removing Istio")
    if existing_namespace(vm) is not None:
        raise RuntimeError(f"Remove the {NAMESPACE} smoke namespace first")
    namespaces = json.loads(kube(vm, "get", "namespaces", "-o", "json").stdout)["items"]
    if any(item["metadata"].get("labels", {}).get("istio.io/dataplane-mode") == "ambient"
           for item in namespaces):
        raise RuntimeError("An Ambient namespace remains; refusing to remove the mesh")
    pods = json.loads(kube(vm, "get", "pods", "--all-namespaces", "-o", "json").stdout)["items"]
    if any(item["metadata"].get("labels", {}).get("istio.io/dataplane-mode") == "ambient"
           for item in pods):
        raise RuntimeError("An Ambient pod remains; refusing to remove the mesh")
    pending = receipt.get("installing")
    if pending:
        chart = next(chart for name, chart, _ in CHARTS if name == pending)
        row = release_inventory(vm).get(pending)
        if row:
            if row.get("chart") != f"{chart}-{VERSION}" or row.get("status") not in ("failed", "deployed"):
                raise RuntimeError(f"Unexpected pending Istio release: {pending}; inspect before removal")
            helm(vm, "uninstall", pending, "-n", "istio-system", "--wait")
        receipt["installing"] = None
        save_receipt(vm, receipt)
    for name, chart, _ in reversed(CHARTS):
        if name not in receipt["releases"]:
            continue
        check_release(release_inventory(vm).get(name, {}), name, chart)
        result = helm(vm, "uninstall", name, "-n", "istio-system", "--wait")
        print(result.stdout.strip(), flush=True)
        receipt["releases"].remove(name)
        save_receipt(vm, receipt)
    receipt_path(vm).unlink()
    print("Owned Helm releases removed. Istio CRDs and istio-system namespace remain for review.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "install", "status", "remove"))
    parser.add_argument("--project", type=Path,
                        default=Path(os.environ.get("MYSCOUTEE_BACKEND_SOURCE", Path.cwd())))
    args = parser.parse_args()
    vm, app_namespace, _ = project_context(args.project)
    if args.action == "plan":
        print(f"Guest: {vm.NAME} ({vm.IP}); application namespace: {app_namespace}")
        print(f"Istio: {VERSION}; releases: {', '.join(name for name, _, _ in CHARTS)}")
        print(f"Receipt: {receipt_path(vm)}")
        return
    if args.action == "install":
        install(vm)
    elif args.action == "remove":
        remove(vm)
    else:
        require_cluster(vm)
        print(json.dumps({"receipt": load_receipt(vm), "releases": release_inventory(vm)}, indent=2))


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        print(f"service-mesh control plane: {error.stderr.strip() or error}", file=sys.stderr)
        raise SystemExit(1)
    except (KeyError, ValueError, RuntimeError, OSError, subprocess.TimeoutExpired) as error:
        print(f"service-mesh control plane: {error}", file=sys.stderr)
        raise SystemExit(1)
