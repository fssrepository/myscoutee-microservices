# The canonical dev image owns Python/OpenSSL; package its existing fixture files.
ARG RELEASE_SIMULATOR_BASE_IMAGE
FROM ${RELEASE_SIMULATOR_BASE_IMAGE}
COPY --from=backend-source server/docker/scripts/start_release_simulator.sh /workspace/server/docker/scripts/start_release_simulator.sh
COPY --from=backend-source tests/qa/categories/Operator/tools/operator-update-release-server.py /workspace/tests/qa/categories/Operator/tools/operator-update-release-server.py
