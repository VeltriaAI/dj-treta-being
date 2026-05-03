#!/bin/bash
# Spawn full non-spot fleet for v5 prod run. Resumes from existing checkpoints.
set -e
cd "$(dirname "$0")"

PROJECT=fandorab2w3
RUN_ID="${1:-v5-prod-2026-04-30}"
DATASET_VERSION="${2:-v5}"
TOTAL_SHARDS="${3:-20}"
IMAGE=dj-treta-v5-worker-1
BUCKET=djtreta-music-v5-mumbai

# Quota-aware distribution. asia-south1 quota is mostly consumed by other
# infra (only 1 IP free). Other regions ~8 free each. 7+6+6+1 = 20.
zone_for_shard() {
  local s="$1"
  if [ "$s" -lt 7 ]; then echo "asia-east1-a"
  elif [ "$s" -lt 13 ]; then echo "europe-west1-b"
  elif [ "$s" -lt 19 ]; then echo "us-central1-a"
  else echo "asia-south1-a"
  fi
}

spawn_one() {
  local shard="$1" zone="$2"
  local shard_pad name meta
  shard_pad=$(printf "%03d" "$shard")
  name="v5w-${RUN_ID}-${shard_pad}"
  meta="GCP_PROJECT=${PROJECT},GCS_BUCKET=${BUCKET},DATASET_VERSION=${DATASET_VERSION},RUN_ID=${RUN_ID},SHARD_ID=${shard},WORKERS_PER_VM=3,KEEP_AUDIO_HOT=true"
  gcloud compute instances create "$name" \
    --project="$PROJECT" --zone="$zone" \
    --machine-type=n2-standard-4 --boot-disk-size=50GB \
    --image="$IMAGE" --image-project="$PROJECT" \
    --scopes=cloud-platform \
    --metadata="$meta" \
    --metadata-from-file=startup-script=startup_worker.sh \
    --no-restart-on-failure 2>&1 | grep -E "Created|ERROR" | head -1
}

for shard in $(seq 0 $((TOTAL_SHARDS - 1))); do
  zone=$(zone_for_shard "$shard")
  spawn_one "$shard" "$zone" &
done
wait

echo
echo "=== fleet up ==="
gcloud compute instances list --project="$PROJECT" --filter="name~v5w-${RUN_ID}" --format="value(zone)" | sort | uniq -c
