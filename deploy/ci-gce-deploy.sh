#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

: "${GCP_PROJECT:?Set GCP_PROJECT}"
: "${GCP_ZONE:=asia-east2-a}"
: "${GCP_INSTANCE:=ant-monitor}"

mkdir -p work
ARCHIVE="work/github-actions-source.tar.gz"
REMOTE_ARCHIVE="ant-monitor-source-${GITHUB_SHA:-manual}.tar.gz"

TAR_ARGS=()
if tar --help 2>&1 | grep -q -- '--no-xattrs'; then
  TAR_ARGS+=(--no-xattrs)
fi
if tar --help 2>&1 | grep -q -- '--no-mac-metadata'; then
  TAR_ARGS+=(--no-mac-metadata)
fi

COPYFILE_DISABLE=1 tar "${TAR_ARGS[@]}" \
  --exclude='.env*' \
  --exclude='.git' \
  --exclude='.venv' \
  --exclude='data' \
  --exclude='work' \
  --exclude='frontend/node_modules' \
  --exclude='frontend/dist' \
  --exclude='__pycache__' \
  --exclude='.pytest_cache' \
  -czf "$ARCHIVE" .

gcloud compute scp "$ARCHIVE" "$GCP_INSTANCE:$REMOTE_ARCHIVE" \
  --project="$GCP_PROJECT" \
  --zone="$GCP_ZONE" \
  --tunnel-through-iap \
  --quiet \
  --ssh-key-expire-after=15m

REMOTE_COMMAND=$(cat <<'REMOTE_SCRIPT'
set -euo pipefail

REMOTE_ARCHIVE_PLACEHOLDER="__REMOTE_ARCHIVE__"
APP_DIR=/opt/ant-monitor
NEXT_DIR=/opt/ant-monitor.next
PREV_DIR=/opt/ant-monitor.previous

sudo test -f "$APP_DIR/.env"
sudo rm -rf "$NEXT_DIR" "$PREV_DIR"
sudo mkdir -p "$NEXT_DIR"
sudo tar -xzf "$REMOTE_ARCHIVE_PLACEHOLDER" -C "$NEXT_DIR"
rm -f "$REMOTE_ARCHIVE_PLACEHOLDER"
sudo install -m 600 "$APP_DIR/.env" "$NEXT_DIR/.env"
sudo chown -R root:root "$NEXT_DIR"
sudo mv "$APP_DIR" "$PREV_DIR"
sudo mv "$NEXT_DIR" "$APP_DIR"
cd "$APP_DIR"
if command -v docker-compose >/dev/null 2>&1; then
  sudo docker-compose -f compose.yaml up -d --build --remove-orphans
  sudo docker-compose -f compose.yaml ps
else
  sudo docker compose -f compose.yaml up -d --build --remove-orphans
  sudo docker compose -f compose.yaml ps
fi
sudo rm -rf "$PREV_DIR"
REMOTE_SCRIPT
)
REMOTE_COMMAND=${REMOTE_COMMAND/__REMOTE_ARCHIVE__/$REMOTE_ARCHIVE}

gcloud compute ssh "$GCP_INSTANCE" \
  --project="$GCP_PROJECT" \
  --zone="$GCP_ZONE" \
  --tunnel-through-iap \
  --quiet \
  --ssh-key-expire-after=15m \
  --command="$REMOTE_COMMAND"
