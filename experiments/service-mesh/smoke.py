#!/usr/bin/env python3
"""Isolated Istio Ambient smoke test for a MyScoutee lab cluster."""

import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time


NAMESPACE = "myscoutee-mesh-smoke"
OWNER_LABEL = "myscoutee.dev/experiment"
OWNER_VALUE = "service-mesh-smoke"


def project_context(project):
    project = project.resolve()
    os.environ["MYSCOUTEE_BACKEND_SOURCE"] = str(project)
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
    import vm

    config = vm.CONFIG
    app_namespace = config["namespace"]
    if app_namespace == NAMESPACE:
        raise ValueError("Experiment namespace must differ from the application namespace")
    image = config["dependencies"]["redis"]
    return vm, app_namespace, image


def kube(vm, *args, input_text=None, check=True):
    guest_command = shlex.join(["sudo", "k3s", "kubectl", "--request-timeout=30s", *args])
    command = vm.ssh_command(guest_command)
    return subprocess.run(command, input=input_text, text=True, capture_output=True,
                          check=check, timeout=90)


def require_cluster(vm):
    if not vm.is_ready(vm.ownership()):
        raise RuntimeError("The selected project's recorded K3s lab is not ready")


def existing_namespace(vm):
    result = kube(vm, "get", "namespace", NAMESPACE, "-o", "json", check=False)
    if result.returncode:
        if "NotFound" in result.stderr:
            return None
        raise RuntimeError(result.stderr.strip())
    namespace = json.loads(result.stdout)
    if namespace["metadata"].get("labels", {}).get(OWNER_LABEL) != OWNER_VALUE:
        raise RuntimeError("Experiment namespace exists without the expected ownership label")
    if namespace["metadata"].get("labels", {}).get("istio.io/dataplane-mode") != "ambient":
        raise RuntimeError("Owned experiment namespace has lost its Ambient label")
    return namespace


def resources(image):
    labels = {OWNER_LABEL: OWNER_VALUE, "app.kubernetes.io/part-of": NAMESPACE}
    namespace = {
        "apiVersion": "v1", "kind": "Namespace",
        "metadata": {"name": NAMESPACE, "labels": {**labels, "istio.io/dataplane-mode": "ambient"}},
    }
    service_accounts = [
        {"apiVersion": "v1", "kind": "ServiceAccount",
         "metadata": {"name": name, "namespace": NAMESPACE, "labels": labels},
         "automountServiceAccountToken": False}
        for name in ("mesh-server", "mesh-client", "mesh-outsider")
    ]

    def deployment(name, command, *, ambient=True):
        pod_labels = {**labels, "app": name}
        if not ambient:
            pod_labels["istio.io/dataplane-mode"] = "none"
        container = {
            "name": name, "image": image, "imagePullPolicy": "IfNotPresent",
            "command": command,
            "resources": {"requests": {"cpu": "10m", "memory": "32Mi"},
                          "limits": {"cpu": "100m", "memory": "128Mi"}},
            "securityContext": {"runAsNonRoot": True, "runAsUser": 10001,
                                "allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]}},
        }
        if name == "mesh-server":
            container["ports"] = [{"name": "redis", "containerPort": 6379}]
            container["readinessProbe"] = {"tcpSocket": {"port": 6379}, "periodSeconds": 2}
        return {
            "apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": name, "namespace": NAMESPACE, "labels": labels},
            "spec": {"replicas": 1, "selector": {"matchLabels": {"app": name}},
                     "template": {"metadata": {"labels": pod_labels},
                                  "spec": {"serviceAccountName": name,
                                           "automountServiceAccountToken": False,
                                           "securityContext": {"seccompProfile": {"type": "RuntimeDefault"}},
                                           "containers": [container]}}},
        }

    server = deployment("mesh-server", ["redis-server", "--port", "6379",
                                        "--save", "", "--appendonly", "no",
                                        "--protected-mode", "no"])
    client = deployment("mesh-client", ["sh", "-c", "sleep 3600"])
    outsider = deployment("mesh-outsider", ["sh", "-c", "sleep 3600"], ambient=False)
    service = {
        "apiVersion": "v1", "kind": "Service",
        "metadata": {"name": "mesh-server", "namespace": NAMESPACE, "labels": labels},
        "spec": {"selector": {"app": "mesh-server"},
                 "ports": [{"name": "tcp-redis", "port": 6379, "targetPort": 6379}]},
    }
    policy = {
        "apiVersion": "security.istio.io/v1", "kind": "PeerAuthentication",
        "metadata": {"name": "mesh-strict", "namespace": NAMESPACE, "labels": labels},
        "spec": {"mtls": {"mode": "STRICT"}},
    }
    return [namespace, *service_accounts, server, client, outsider, service, policy]


def apply(vm, objects):
    payload = "\n---\n".join(json.dumps(item) for item in objects) + "\n"
    result = kube(vm, "apply", "-f", "-", input_text=payload)
    print(result.stdout, end="")


def verify(vm):
    for name in ("mesh-server", "mesh-client", "mesh-outsider"):
        kube(vm, "-n", NAMESPACE, "rollout", "status", f"deployment/{name}", "--timeout=90s")
    command = ["redis-cli", "-t", "2", "-h", "mesh-server", "ping"]
    inside = kube(vm, "-n", NAMESPACE, "exec", "deployment/mesh-client", "--", *command)
    if inside.stdout.strip() != "PONG":
        raise RuntimeError(f"Meshed client did not receive PONG: {inside.stdout.strip()}")
    outside = kube(vm, "-n", NAMESPACE, "exec", "deployment/mesh-outsider", "--",
                   *command, check=False)
    denial = ("Connection reset by peer", "Server closed the connection")
    if outside.returncode == 0 or not any(message in outside.stderr for message in denial):
        raise RuntimeError(f"Unexpected unmeshed client result: {outside.stderr.strip() or outside.stdout.strip()}")
    print("PASS: meshed client received PONG; unmeshed client was denied by STRICT mTLS.")


def traffic(vm, seconds):
    if not 10 <= seconds <= 300:
        raise ValueError("Traffic duration must be between 10 and 300 seconds")
    kube(vm, "-n", NAMESPACE, "rollout", "status", "deployment/mesh-server", "--timeout=90s")
    kube(vm, "-n", NAMESPACE, "rollout", "status", "deployment/mesh-client", "--timeout=90s")
    deadline = time.monotonic() + seconds
    count = 0
    while time.monotonic() < deadline:
        result = kube(vm, "-n", NAMESPACE, "exec", "deployment/mesh-client", "--",
                      "redis-cli", "-t", "2", "-h", "mesh-server", "ping")
        if result.stdout.strip() != "PONG":
            raise RuntimeError(f"Meshed traffic failed after {count} requests")
        count += 1
        time.sleep(min(2, max(0, deadline - time.monotonic())))
    print(f"Sent {count} meshed Redis requests in {seconds} seconds.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "up", "verify", "traffic", "down"))
    parser.add_argument("--seconds", type=int, default=120,
                        help="Duration of traffic generation (10-300 seconds)")
    parser.add_argument("--project", type=Path,
                        default=Path(os.environ.get("MYSCOUTEE_BACKEND_SOURCE", Path.cwd())))
    args = parser.parse_args()
    vm, app_namespace, image = project_context(args.project)
    if args.action == "plan":
        print(f"Project: {args.project.resolve()}\nApplication namespace: {app_namespace}")
        print(f"Experiment namespace: {NAMESPACE}\nImage: {image}")
        print(f"Guest: {vm.NAME} ({vm.IP}); access through the recorded SSH identity")
        return
    require_cluster(vm)
    current = existing_namespace(vm)
    if args.action == "up":
        kube(vm, "get", "crd", "peerauthentications.security.istio.io")
        kube(vm, "-n", "istio-system", "get", "deployment", "istiod")
        kube(vm, "-n", "istio-system", "get", "daemonset", "ztunnel")
        if current is None:
            apply(vm, resources(image)[:1])
        apply(vm, resources(image)[1:])
        verify(vm)
    elif args.action == "verify":
        if current is None:
            raise RuntimeError("Experiment namespace is absent; run up first")
        verify(vm)
    elif args.action == "traffic":
        if current is None:
            raise RuntimeError("Experiment namespace is absent; run up first")
        traffic(vm, args.seconds)
    else:
        if current is None:
            print("Experiment namespace is already absent.")
        else:
            result = kube(vm, "delete", "namespace", NAMESPACE, "--wait=true", "--timeout=120s")
            print(result.stdout, end="")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        print(f"service-mesh smoke: {error.stderr.strip() or error}", file=sys.stderr)
        raise SystemExit(1)
    except (KeyError, ValueError, RuntimeError, OSError, subprocess.TimeoutExpired) as error:
        print(f"service-mesh smoke: {error}", file=sys.stderr)
        raise SystemExit(1)
