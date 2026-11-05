#!/usr/bin/env bash
# Fetch the Switch standalone compose file pinned at the commit Jury Room was built and tested against.
# Switch is Apache-2.0 + Commons Clause (SandboxAQ); it is not vendored here.
set -euo pipefail
cd "$(dirname "$0")"
COMMIT=2d72c57
URL="https://raw.githubusercontent.com/sandbox-quantum/switch/${COMMIT}/console/apps/switch-console-desktop/src/main/core/managed-switch-server/resources/standalone-docker-compose.pinned.yml"
curl -fsSL "$URL" -o docker-compose.yml
echo "fetched $(sha256sum docker-compose.yml)"
