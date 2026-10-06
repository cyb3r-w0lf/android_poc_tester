#!/bin/bash
# Generates a random SU_NAME into .env (preserving other keys), then starts the stack with podman.
cd "$(dirname "$0")" || exit 1
touch .env
grep -v '^SU_NAME=' .env > .env.tmp || true
echo "SU_NAME=su-$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')" >> .env.tmp
mv .env.tmp .env

# Prefer native podman-compose (drives podman CLI directly, no API socket needed).
# Fall back to `podman compose` (docker-compose provider) only if podman-compose is absent.
if command -v podman-compose >/dev/null 2>&1; then
  podman-compose up --build
else
  podman compose up --build
fi
