#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
: "${GCP_PROJECT:?Set GCP_PROJECT explicitly}"
ANT_ZONE="${GCP_ZONE:-asia-east2-a}"
ANT_INSTANCE="${GCP_INSTANCE:-ant-monitor}"
ANT_NETWORK="ant-monitor-private"
ANT_ENV_FILE="${ANT_ENV_FILE:-.env}"
[[ "$GCP_PROJECT" =~ ^[a-z][a-z0-9-]+$ ]] || exit 2
[[ "$ANT_INSTANCE" =~ ^[a-z][a-z0-9-]+$ ]] || exit 2
[[ "$ANT_ZONE" =~ ^[a-z0-9-]+$ ]] || exit 2
[[ -f "$ANT_ENV_FILE" ]] || { echo 'Create environment file first'; exit 2; }
ANT_PASSWORD_LENGTH=$(awk -F= '$1=="DASHBOARD_PASSWORD" {print length(substr($0,index($0,"=")+1))}' "$ANT_ENV_FILE")
[[ "${ANT_PASSWORD_LENGTH:-0}" -ge 16 ]] || { echo 'DASHBOARD_PASSWORD must contain at least 16 characters'; exit 2; }
gcloud services enable compute.googleapis.com iap.googleapis.com --project="$GCP_PROJECT" --quiet
if ! gcloud compute networks describe "$ANT_NETWORK" --project="$GCP_PROJECT" --format='value(name)' >/dev/null 2>&1; then
  gcloud compute networks create "$ANT_NETWORK" --subnet-mode=auto --project="$GCP_PROJECT" --quiet
fi
if ! gcloud compute firewall-rules describe ant-monitor-iap-ssh --project="$GCP_PROJECT" --format='value(name)' >/dev/null 2>&1; then
  gcloud compute firewall-rules create ant-monitor-iap-ssh --network="$ANT_NETWORK" --allow=tcp:22 --source-ranges=35.235.240.0/20 --target-tags=ant-monitor --project="$GCP_PROJECT" --quiet
fi
if ! gcloud compute instances describe "$ANT_INSTANCE" --zone="$ANT_ZONE" --project="$GCP_PROJECT" --format='value(name)' >/dev/null 2>&1; then
  gcloud compute instances create "$ANT_INSTANCE" --project="$GCP_PROJECT" --zone="$ANT_ZONE" --machine-type=e2-small --image-family=debian-12 --image-project=debian-cloud --boot-disk-size=20GB --boot-disk-type=pd-balanced --network="$ANT_NETWORK" --tags=ant-monitor --no-service-account --no-scopes --shielded-secure-boot --quiet
fi
mkdir -p work
chmod 700 work
COPYFILE_DISABLE=1 tar --exclude='.env*' --exclude=.git --exclude=.venv --exclude=data --exclude=work --exclude=frontend/node_modules --exclude=frontend/dist --exclude=__pycache__ --exclude=.pytest_cache -czf work/source.tar.gz .
chmod 600 work/source.tar.gz "$ANT_ENV_FILE"
gcloud compute scp work/source.tar.gz "$ANT_INSTANCE":source.tar.gz --zone="$ANT_ZONE" --project="$GCP_PROJECT" --tunnel-through-iap --quiet
gcloud compute scp "$ANT_ENV_FILE" "$ANT_INSTANCE":ant-monitor.env --zone="$ANT_ZONE" --project="$GCP_PROJECT" --tunnel-through-iap --quiet
gcloud compute ssh "$ANT_INSTANCE" --zone="$ANT_ZONE" --project="$GCP_PROJECT" --tunnel-through-iap --quiet --command='set -eu; sudo apt-get update -qq; sudo apt-get install -y -qq docker.io docker-compose; sudo systemctl enable --now docker; sudo mkdir -p /opt/ant-monitor; sudo tar -xzf source.tar.gz -C /opt/ant-monitor; sudo install -m 600 ant-monitor.env /opt/ant-monitor/.env; rm -f ant-monitor.env source.tar.gz; cd /opt/ant-monitor; sudo docker-compose -f compose.yaml up -d --build; sudo docker-compose -f compose.yaml ps'
echo "Ready. Tunnel: gcloud compute ssh $ANT_INSTANCE --project=$GCP_PROJECT --zone=$ANT_ZONE --tunnel-through-iap -- -N -L 8787:127.0.0.1:8787"
