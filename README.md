# MyScoutee microservices runtime support

The existing domain groups in each product's `config/microservices/assemblies.json`
remain separate JARs and images. `bash ../myscoutee-microservices/tools/lab.sh
assembly-images` checks their actual Gradle sources, resolved dependency bytes,
assembly settings, packaging recipe and runtime image identity before building.
Unchanged groups reuse their exact image IDs; a new repository commit alone does
not invalidate them. Shared source or dependency changes invalidate their consumers.
Use `assembly-images --services help` to check/build just that configured group.

Gradle exports its own source/dependency model through `writeAssemblyInputs`; no
second domain-to-path list is maintained. Image labels retain the original build
commit and the assembly input fingerprint. The current candidate records the
chosen image identities separately, including reused images. Local cache tags are
`<product-prefix><group>:inputs-<sha256>`. CI keeps these candidate caches local. An explicitly configured
`MYSCOUTEE_ASSEMBLY_CACHE_REGISTRY` can read a separately managed remote cache;
`CI_REGISTRY_IMAGE` alone never enables remote cache reads or publication.
Missing caches build normally; registry/auth failures and mismatched cache labels
fail explicitly. `server/build/microservices/assembly-image-plan.json` reports
`build` or `reuse` per group. The first build with this input format establishes
the cache. This does not change VM lifecycle or Helm rollout/migration policy.

The product runtime's `rolloutTimeoutSeconds` bounds Helm readiness waiting,
including Mongo's first-start canonical data import. Backend and template allow
600 seconds; an existing initialized volume is not imported again. The outer
command deadline includes an additional 30 seconds for Helm to report its result.

This repository owns reusable microservices runtime, Gradle assembly/test
conventions, lifecycle, infrastructure, monitoring, chart/container assets and
context/MinIO verification tools. Product domain implementations are not copied
here. Shared business libraries and shared optional adapters belong to base.

Run commands from the consuming backend or template repository root, or select
it explicitly with `MYSCOUTEE_BACKEND_SOURCE`. Product inputs live in that
repository's `config/microservices/`; its optional Java/test sources live under
the owning domain/common source unit. Product QA scenarios are under
`tests/e2e/microservices/`, with operator documentation in
`guides/development/microservices/README.md`.

```sh
../myscoutee-microservices/gradlew --no-daemon --parallel :bootstrap-tests:test --tests '*LocalizationHttpTest'
bash ../myscoutee-microservices/tools/lab.sh render
python3 ../myscoutee-microservices/tools/seed-mongo.py --verify
python3 ../myscoutee-microservices/tools/check-session.py --help
```

### Mongo initialization

The Helm chart mounts the `mongo-seed` **image volume** read-only at
`/docker-entrypoint-initdb.d`. It contains only the existing init scripts and
canonical REAL/DEMO directories, packaged as a data-only OCI artifact. The
upstream Mongo image and its entrypoint remain unchanged; no process runs from
the seed artifact, and no copying init container, host directory, PV or PVC is
needed for these static files. Mongo database data stays on its separate PVCs.
This requires Kubernetes image-volume support and a compatible container runtime
(the pinned K3s/containerd versions provide it).

The product's `volumeImages` in `config/microservices/deploy/runtime.json` selects
the seed artifact. The existing support-image build packages it, the image lock
pins its identity, and the normal image cache/registry delivery includes it.
No download or build is added to Mongo startup. REAL system catalogs/configuration
and the separate production DEMO dataset retain the canonical collection policy;
E2E fixtures are excluded. Database names use the existing environment variables.
Only the first Mongo replica member runs initialization; the second receives
its data through replication.

The official Mongo entrypoint initializes an empty data volume, as in Compose.
Existing data is never reset or reseeded by mounting a new seed artifact. A
previous incomplete installation requires explicit recovery. The older
`seed-mongo.py` commands remain explicit recovery/migration tools for installations
with their bootstrap marker; they are not part of normal application startup.

The isolated JUnit runner also has one implementation under
`tools/run-backend-junit.sh`. Invoke it directly from the consuming application
root: `bash ../myscoutee-microservices/tools/run-backend-junit.sh [level] [Gradle options]`.
`MYSCOUTEE_BACKEND_SOURCE` selects an explicit project when running elsewhere.

Infrastructure safety tests live beside their implementation. From either
application root, run:

```sh
python3 -B ../myscoutee-microservices/tools/test_infra.py
python3 -B ../myscoutee-microservices/tools/test_junit_launcher.py
```

These tests use disposable state and mocked infrastructure; they do not start
a lab. Backend composition, retry-catalog policy and payment-simulator credential
expectations remain in the backend's `tests/e2e/microservices/test_infra.py`.

For an already installed host, an administrator can optionally authorize
password-free socket startup once, from the application checkout:

```sh
sudo python3 ../myscoutee-microservices/tools/host.py authorize-start
python3 ../myscoutee-microservices/tools/host.py start
```

The root-owned sudoers entry permits only the exact `systemctl start` command
for `virtlogd.socket`, `virtlockd.socket` and `libvirtd.socket`. It grants no
permission to execute repository Python code as root. Later starts use `sudo -n`
only if sockets are inactive and fail immediately if authorization is missing.
Already-active sockets need no sudo. No service or VM is enabled at host boot.
Pass `--user myscoutee-ci` to authorize the dedicated CI execution account. This
replaces only the helper's unchanged, recorded startup policy; it does not
transfer host package ownership or add general sudo permission. The CI owner's
`scripts/runner-host.py` creates/configures that account from its ignored dotenv
file and invokes this shared authorization flow for backend and template alike.

The logical `bootstrap-tests` task aggregates the existing domain transport/HTTP
tests. Its source tree is gone; one shared Gradle convention consumes each
product's exact `config/microservices/tests.json` include list. Generated project
descriptors, build/cache, image/chart/monitoring output and evidence are isolated
in that product's ignored `server/build/microservices/`. VM/private lifecycle
state and credentials retain their existing external state directories.

Run `../myscoutee-microservices/gradlew --no-daemon --parallel :bootstrap-tests:test`
from a consuming repository for the full transport suite. The existing test task
also generates `server/build/microservices/bootstrap-tests/reports/jacoco.xml`;
native JUnit XML remains in `bootstrap-tests/test-results/test`. Consumer-owned
`config/sonarqube/microservices.properties` imports both reports. Run tests before
scanning; the central scanner never starts them.

Source-only Sonar configuration is in `config/sonarqube/`; the shared Sonar tool
uses the existing single server. Scanning does not start another server.

Extraction remains in progress under the sibling
[base plan](../myscoutee-base/guides/development/extraction/SHARED_BUSINESS_MODULES_PLAN.md), including review of
hard-coded tool input. Seed, retry-migration execution, Mongo fixtures, native
sessions, resource counters and runtime probes already have one shared owner.
Project seed/migration policy and session route inputs remain in
`config/microservices/seed.json` and `config/microservices/checks/session.json`.

Client-IP bans must return 403 only to the banned address while other clients
retain access. The public NodePort preserves external source IPs. Gateway
readiness checks the existing port 8080 with a native TCP probe: a valid HTTP 403
response is not a failed nginx process. From a configured, running application
checkout, `python3 ../myscoutee-microservices/tools/check-client-ip-ban.py` compares
the former HTTP probe with this behavior in two disposable pods. It verifies the
banned and independent clients through Kubernetes Services and removes its own
pods/services without changing the application's ban file or fixtures.

### Registry-backed guest bootstrap

The pinned Ubuntu Noble cloud image is the KVM guest OS, not another runtime
layer. It already supplies curl, CA certificates and Python. `infra/versions.json`
also pins the QEMU guest agent and its missing liburing2 dependency (about 412 kB
total), with SHA-256 values from Ubuntu's authenticated APT package indexes.
When changing the cloud-image pin, recheck that this package closure is complete.

CI's optional manual infrastructure preparation publishes these DEBs alongside
the existing installers in the GitLab Generic Package Registry. The versioned
CI import helper can seed them from an existing local directory with
`--guest-packages`; missing selected files are downloaded from the pinned URLs.
Startup restores registry bytes and Terraform places the DEBs in the cloud-init
seed. `infra/guest/install-guest-packages.py` verifies them and runs local `dpkg`;
cloud-init no longer refreshes Ubuntu indexes or installs from the internet.
Standalone `vm.py prepare` may obtain missing pinned DEBs directly, while CI's
`MYSCOUTEE_INFRA_OFFLINE=true` requires its prepared registry cache.
Existing initialized guests and application data are never recreated implicitly.
