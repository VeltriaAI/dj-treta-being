#!/bin/bash
# Spawn full non-spot fleet for v5 prod run. Resumes from existing checkpoints.
set -e
cd "$(dirname "$0")"

PROJECT=fandorab2w3
RUN_ID="${1:-v5-prod-2026-04-30}"
DATASET_VERSION="${2:-v5}"
TOTAL_SHARDS="${3:-20}"
IMAGE=dj-treta-v5-worker-1
BUCKET="${BUCKET:-djtreta-music-v6-multi}"

# 10 YouTube-unblocked regions × 2 workers each. Keeps per-region IP
# concentration low (<3 workers) so YouTube's per-IP rate limit doesn't
# trip. Verified 2026-05-07 via realistic 5-track probe.
# asia-east1, asia-south2, me-west1 are YT-blocked — DO NOT USE.
zone_for_shard() {
  local s="$1"
  case $((s / 2)) in
    0) echo "asia-south1-a" ;;
    1) echo "asia-southeast1-a" ;;
    2) echo "asia-southeast2-a" ;;
    3) echo "asia-northeast1-a" ;;
    4) echo "asia-northeast3-a" ;;
    5) echo "asia-east2-a" ;;
    6) echo "europe-west1-b" ;;
    7) echo "europe-west2-a" ;;
    8) echo "europe-west3-a" ;;
    9) echo "europe-west4-a" ;;
    *) echo "asia-south1-a" ;;
  esac
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
