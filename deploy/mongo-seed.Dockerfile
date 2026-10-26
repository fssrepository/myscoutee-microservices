# syntax=docker/dockerfile:1.14
# Static read-only volume content. This image never runs a container.
FROM scratch
COPY --chmod=0644 db_init.js mongo_import.sh mongo-seed-policy.inc /
COPY --chmod=u=rwX,go=rX myscoutee_db/ /myscoutee_db/
COPY --chmod=u=rwX,go=rX demo_db/ /demo_db/
