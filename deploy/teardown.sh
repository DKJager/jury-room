#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
docker compose -p juryroom -f docker-compose.yml -f docker-compose.limits.yml down -v
