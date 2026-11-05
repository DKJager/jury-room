#!/usr/bin/env bash
# Bring up Switch 0.31.0 on 127.0.0.1 with generated secrets and the resource caps.
set -euo pipefail
cd "$(dirname "$0")"
[ -f docker-compose.yml ] || ./fetch_switch.sh
if [ ! -f .env ]; then
  cp env.example .env
  for k in DB_PASSWORD DB_OWNER_PASSWORD AGENT_REGISTRATION_TOKEN GATEWAY_ADMIN_PASSWORD MATTERMOST_ADMIN_PASSWORD MATTERMOST_USER_PASSWORD JWT_SECRET_KEY CONTROLLER_TOKEN_SECRET; do
    sed -i "s|^${k}=.*|${k}=$(openssl rand -hex 24)|" .env
  done
  sed -i "s|^SECRET_KEYS=.*|SECRET_KEYS=juryroom:$(openssl rand -hex 32)|" .env
  chmod 600 .env
fi
docker compose -p juryroom -f docker-compose.yml -f docker-compose.limits.yml up -d
for i in $(seq 1 40); do [ "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/health)" = 200 ] && { echo "Switch healthy on 127.0.0.1:8000"; exit 0; }; sleep 3; done
echo "Switch did not become healthy"; exit 1
