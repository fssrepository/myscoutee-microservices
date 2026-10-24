# STUN listens on unprivileged port 3478. Preserve the upstream executable bytes,
# but strip its file capability so exec also works with an empty bounding set.
FROM coturn/coturn:4.18.0-r0-alpine@sha256:c29135c08565810d37053f384584392b7c24b69570fc81a721b878d6fe63e04e
USER root
RUN cp /usr/bin/turnserver /usr/bin/turnserver.unprivileged \
    && chmod 0755 /usr/bin/turnserver.unprivileged \
    && mv /usr/bin/turnserver.unprivileged /usr/bin/turnserver
USER nobody:nogroup
