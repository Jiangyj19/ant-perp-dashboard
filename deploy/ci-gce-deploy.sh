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
if tar --disable-copyfile -cf /dev/null -T /dev/null >/dev/null 2>&1; then
  TAR_ARGS+=(--disable-copyfile)
fi
if tar --no-xattrs -cf /dev/null -T /dev/null >/dev/null 2>&1; then
  TAR_ARGS+=(--no-xattrs)
fi
if tar --no-mac-metadata -cf /dev/null -T /dev/null >/dev/null 2>&1; then
  TAR_ARGS+=(--no-mac-metadata)
fi

if ((${#TAR_ARGS[@]})); then
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
else
  COPYFILE_DISABLE=1 tar \
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
fi

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
if sudo test -f "$APP_DIR/.env.friend"; then
  sudo install -m 600 "$APP_DIR/.env.friend" "$NEXT_DIR/.env.friend"
fi
sudo chown -R root:root "$NEXT_DIR"
sudo mv "$APP_DIR" "$PREV_DIR"
sudo mv "$NEXT_DIR" "$APP_DIR"
cd "$APP_DIR"
COMPOSE_ARGS="-f compose.yaml"
if sudo grep -Eq '^PUBLIC_DOMAIN=.+$' .env; then
  COMPOSE_ARGS="$COMPOSE_ARGS -f deploy/compose.public.yaml"
fi
if sudo test -f .env.friend && sudo grep -Eq '^FRIEND_PUBLIC_DOMAIN=.+$' .env; then
  COMPOSE_ARGS="$COMPOSE_ARGS -f deploy/compose.friend.yaml"
fi
if command -v docker-compose >/dev/null 2>&1; then
  compose() { sudo docker-compose $COMPOSE_ARGS "$@"; }
else
  compose() { sudo docker compose $COMPOSE_ARGS "$@"; }
fi
compose up -d --build --remove-orphans
if sudo grep -Eq '^PUBLIC_DOMAIN=.+$' .env; then
  compose exec -T proxy caddy reload --config /etc/caddy/Caddyfile || compose restart proxy
fi
compose ps
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
