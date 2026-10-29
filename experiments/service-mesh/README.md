# Isolated service mesh experiment

This directory is an opt-in Istio Ambient smoke test for the existing MyScoutee
K3s lab. It creates only `myscoutee-mesh-smoke`: a Redis server, a meshed client,
an unmeshed client, and a namespace-local `PeerAuthentication` policy. The meshed
client must receive `PONG`; the unmeshed client must be denied by strict mTLS.
It uses the consuming product's pinned Redis image and the lab's authenticated
guest-side `k3s kubectl` command.
The same script works from the backend and template repository roots.

The application chart, product namespace, services, data, entry point, deployment
scripts and regular startup do not call this experiment. `plan` is offline and
read-only. `up`, `verify` and `down` require an already running lab and Istio
Ambient installation. `down` deletes only the experiment namespace after checking
its ownership label. No test writes to application databases.

Istio's control plane, CNI and ztunnel are **cluster-scoped infrastructure**. Even
with the application pods outside the mesh, installing them changes the cluster
and consumes resources. Use a dedicated lab cluster if full runtime isolation
from the existing K3s lab is required. This experiment does not prove MyScoutee
HTTP transport, gateway, WebSocket, MongoDB, Redis application traffic or STUN/TURN
behavior under a mesh; those require a separate application-level trial.

## Plan

From the backend or template root:

```sh
python3 -B ../myscoutee-microservices/experiments/service-mesh/smoke.py plan
```

The script reads the existing runtime configuration and uses the recorded VM
identity. If a fully recorded VM is stopped and the current Terraform recipe
would replace its bootstrap volume, the additive `vm.py resume` action boots it
without changing that volume:

```sh
python3 -B ../myscoutee-microservices/tools/host.py start
python3 -B ../myscoutee-microservices/tools/vm.py resume
```

## Install Istio Ambient in an explicitly selected lab

The pinned K3s release is 1.37.1. Istio 1.31.3 supports Kubernetes 1.37. Istio
runs in K3s as Helm-managed Deployments and DaemonSets; it needs no Docker Compose
service. The versioned installer checks the Gateway API CRD, refuses an existing
unowned `istio-system` namespace, installs the four pinned charts in order, and
records each release in private lab state. Run it from the consuming product root
with that lab already started:

```sh
python3 -B ../myscoutee-microservices/experiments/service-mesh/control_plane.py plan
python3 -B ../myscoutee-microservices/experiments/service-mesh/control_plane.py install
python3 -B ../myscoutee-microservices/experiments/service-mesh/control_plane.py status
```

The installer requires an existing Gateway API bundle of at least v1.5 and does
not modify that separate cluster-level dependency. Inspect its owner and follow
the [Istio prerequisite](https://istio.io/latest/docs/ambient/install/helm/) if
the check fails. The CNI chart uses Istio's
[K3s platform profile](https://istio.io/latest/docs/ambient/install/platform-prerequisites/)
because K3s stores its CNI files outside the standard directories. Check VM
memory before installing the additional components.

Use a separately pinned `istioctl` 1.31.3 for optional control-plane checks.
Do not add the `istio.io/dataplane-mode` label to the product namespace. The
experiment creates its own labeled namespace. Its opt-out client carries the
pod label `istio.io/dataplane-mode=none`.

## Run and remove the smoke test

```sh
python3 -B ../myscoutee-microservices/experiments/service-mesh/smoke.py up
python3 -B ../myscoutee-microservices/experiments/service-mesh/smoke.py verify
```

`up` checks the Istio CRD, `istiod` and `ztunnel` before creating the namespace.
`verify` waits for all three deployments and checks both the successful and
denied connection. For additional transport evidence, inspect the ztunnel
workloads and the `connection_security_policy="mutual_tls"` TCP metric as shown
in [Istio's verification guide](https://istio.io/latest/docs/ambient/usage/verify-mtls-enabled/).
`down` is safe to repeat.

## Open the web view

The optional dashboard installs pinned Prometheus and Kiali Helm charts into
`istio-system` after the smoke namespace exists. Prometheus scrapes only the
Istio pods, keeps two hours of data in ephemeral storage, and has no ingress.
Kiali is read-only. Its anonymous UI is exposed only through a local SSH tunnel,
on `127.0.0.1:20001`; it does not alter the application namespace or its
traffic. Run from the consuming product root:

```sh
python3 -B ../myscoutee-microservices/experiments/service-mesh/dashboard.py install
python3 -B ../myscoutee-microservices/experiments/service-mesh/dashboard.py serve
```

Open [Kiali](http://127.0.0.1:20001/) and select the
`myscoutee-mesh-smoke` namespace in the Traffic Graph. In another terminal,
generate traffic for the graph:

```sh
python3 -B ../myscoutee-microservices/experiments/service-mesh/smoke.py traffic --seconds 120
```

The graph uses the ztunnel metrics collected by Prometheus; allow a scrape
interval for the first edge to appear. `Ctrl+C` closes the local Kiali tunnel.
The dashboard uses [Kiali 2.34](https://kiali.io/docs/installation/installation-guide/prerequisites/),
which supports Istio 1.31, and the standalone
[Kiali Helm chart](https://kiali.io/docs/installation/installation-guide/install-with-helm/).

To remove the optional UI and then the smoke workload:

```sh
python3 -B ../myscoutee-microservices/experiments/service-mesh/dashboard.py remove
python3 -B ../myscoutee-microservices/experiments/service-mesh/smoke.py down
```

If this installation was made solely for the experiment, remove its recorded
cluster components after the dashboard and smoke namespace:

```sh
python3 -B ../myscoutee-microservices/experiments/service-mesh/control_plane.py remove
```

The remover checks its receipt and refuses to proceed if any namespace or pod is
still enrolled in Ambient. Helm does not delete CRDs when uninstalling a chart.
Inspect any remaining Istio CRDs and their users before deciding whether to
remove them. The script does not delete CRDs or `istio-system`.

References: [Istio 1.31 support matrix](https://istio.io/latest/docs/releases/supported-releases/),
[Ambient Helm installation](https://istio.io/latest/docs/ambient/install/helm/),
[pod-level Ambient opt-in](https://istio.io/latest/docs/ambient/usage/add-workloads/),
[mTLS verification](https://istio.io/latest/docs/ambient/usage/verify-mtls-enabled/).
