#!/bin/bash
# Detects preempted spot workers for the active prod run and respawns them.
# Idempotent — safe to call repeatedly.
set -e
cd "$(dirname "$0")"

PROJECT=fandorab2w3
RUN_ID="${1:-v5-prod-2026-04-30}"
TOTAL_SHARDS="${2:-20}"
IMAGE=dj-treta-v5-worker-1
BUCKET=djtreta-music-v5-mumbai

FALLBACK_ZONES=(asia-south1-a us-central1-a europe-west1-c europe-west1-b asia-east1-b us-central1-b)

# Cleanup TERMINATED preempted workers
for n in $(gcloud compute instances list --project="$PROJECT" \
    --filter="name~v5w-${RUN_ID} AND status=TERMINATED" \
    --format="value(name,zone)" 2>/dev/null); do
  IFS=$'\t' read -r name zone <<< "$n"
  [ -n "$name" ] && gcloud compute instances delete "$name" --zone="$zone" \
    --project="$PROJECT" --quiet >/dev/null 2>&1 &
done
wait

# Find missing shards
gcloud compute instances list --project="$PROJECT" \
  --filter="name~v5w-${RUN_ID} AND status=RUNNING" \
  --format="value(name)" 2>/dev/null \
  | awk -F'-' '{print $NF + 0}' | sort -nu > /tmp/_alive.txt

seq 0 $((TOTAL_SHARDS - 1)) | sort -n > /tmp/_all.txt
missing=$(comm -23 /tmp/_all.txt /tmp/_alive.txt)

if [ -z "$missing" ]; then
  echo "all $TOTAL_SHARDS shards have live workers"
  exit 0
fi

echo "respawning: $(echo $missing | tr '\n' ' ')"

for shard in $missing; do
  shard_pad=$(printf "%03d" "$shard")
  name="v5w-${RUN_ID}-${shard_pad}"
  meta="GCP_PROJECT=${PROJECT},GCS_BUCKET=${BUCKET},DATASET_VERSION=v5,RUN_ID=${RUN_ID},SHARD_ID=${shard},WORKERS_PER_VM=3,KEEP_AUDIO_HOT=true"

  for zone in "${FALLBACK_ZONES[@]}"; do
    if gcloud compute instances create "$name" \
        --project="$PROJECT" --zone="$zone" \
        --machine-type=n2-standard-4 --boot-disk-size=50GB \
        --image="$IMAGE" --image-project="$PROJECT" \
        --scopes=cloud-platform \
        --provisioning-model=SPOT --instance-termination-action=DELETE \
        --metadata="$meta" \
        --metadata-from-file=startup-script=startup_worker.sh \
        --no-restart-on-failure 2>&1 | grep -q Created; then
      echo "  shard $shard → $zone"
      break
    fi
  done
done
