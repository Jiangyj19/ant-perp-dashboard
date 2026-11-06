#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
: "${GCP_PROJECT:?Set GCP_PROJECT explicitly}"
ANT_INSTANCE="${GCP_INSTANCE:-ant-monitor}"
ANT_ZONE="${GCP_ZONE:-asia-east2-a}"
ANT_ENV_FILE="${ANT_ENV_FILE:-.env.cloud}"
[[ -f "$ANT_ENV_FILE" ]] || { echo 'Environment file missing'; exit 2; }
chmod 600 "$ANT_ENV_FILE"
gcloud compute scp "$ANT_ENV_FILE" "$ANT_INSTANCE":ant-monitor.env --project="$GCP_PROJECT" --zone="$ANT_ZONE" --tunnel-through-iap --quiet
gcloud compute ssh "$ANT_INSTANCE" --project="$GCP_PROJECT" --zone="$ANT_ZONE" --tunnel-through-iap --quiet --command='set -eu; sudo install -m 600 ant-monitor.env /opt/ant-monitor/.env; rm -f ant-monitor.env; cd /opt/ant-monitor; COMPOSE_ARGS="-f compose.yaml"; if sudo grep -Eq "^PUBLIC_DOMAIN=.+$" .env; then COMPOSE_ARGS="$COMPOSE_ARGS -f deploy/compose.public.yaml"; fi; if command -v docker-compose >/dev/null 2>&1; then sudo docker-compose $COMPOSE_ARGS up -d --force-recreate --remove-orphans; sudo docker-compose $COMPOSE_ARGS ps; else sudo docker compose $COMPOSE_ARGS up -d --force-recreate --remove-orphans; sudo docker compose $COMPOSE_ARGS ps; fi'
