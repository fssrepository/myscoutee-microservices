# MyScoutee microservices runtime support

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
