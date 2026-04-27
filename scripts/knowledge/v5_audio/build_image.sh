#!/bin/bash
# One-time: build a custom GCE image with all v5_audio deps pre-installed
# and ML models pre-cached. Subsequent worker/coordinator VMs boot from this
# image in ~30 seconds instead of 15-20 minutes of apt+pip install.
#
# Run from local Mac. Reads .env for GCP_PROJECT, GCP_ZONE, GCP_REGION.
#
# Output: GCE image named dj-treta-v5-worker-1 in your project.
#
# Re-running this with a fresh IMAGE_NAME (or after deleting the existing
# image) will rebuild from scratch.
set -e

cd "$(dirname "$0")"
[ -f .env ] || { echo "ERROR: .env missing"; exit 1; }
set -a; source .env; set +a

: "${GCP_PROJECT:?must be set}"
GCP_ZONE="${GCP_ZONE:-asia-south1-a}"
IMAGE_NAME="${V5_IMAGE_NAME:-dj-treta-v5-worker-1}"
BUILDER_NAME="v5-image-builder"

echo "=== building $IMAGE_NAME in $GCP_PROJECT ($GCP_ZONE) ==="

# Skip if image already exists
if gcloud compute images describe "$IMAGE_NAME" --project="$GCP_PROJECT" >/dev/null 2>&1; then
    echo "image $IMAGE_NAME already exists. delete it first if you want to rebuild:"
    echo "  gcloud compute images delete $IMAGE_NAME --project=$GCP_PROJECT --quiet"
    exit 0
fi

# Cleanup any old builder
gcloud compute instances delete "$BUILDER_NAME" --project="$GCP_PROJECT" \
    --zone="$GCP_ZONE" --quiet 2>/dev/null || true

# ── Spin builder VM (n2-standard-4 for fast install) ────────────────
echo "creating builder VM..."
gcloud compute instances create "$BUILDER_NAME" \
    --project="$GCP_PROJECT" \
    --zone="$GCP_ZONE" \
    --machine-type=n2-standard-4 \
    --boot-disk-size=50GB \
    --boot-disk-type=pd-balanced \
    --image-family=debian-12 \
    --image-project=debian-cloud \
    --scopes=cloud-platform \
    --metadata-from-file=startup-script=image_build_setup.sh

echo "waiting for setup to complete (target: marker /var/lib/v5_image_ready)..."
echo "this is ~30-40 min in Mumbai due to slow apt mirrors + heavy pip installs"

# Poll for completion marker
START=$(date +%s)
while true; do
    if gcloud compute ssh "$BUILDER_NAME" --project="$GCP_PROJECT" --zone="$GCP_ZONE" \
            --ssh-flag="-o ConnectTimeout=15" --command="test -f /var/lib/v5_image_ready" 2>/dev/null; then
        ELAPSED=$(( $(date +%s) - START ))
        echo "  setup complete after ${ELAPSED}s"
        break
    fi
    ELAPSED=$(( $(date +%s) - START ))
    if [ "$ELAPSED" -gt 4800 ]; then
        echo "  TIMEOUT after 80 min — check builder VM logs"
        gcloud compute ssh "$BUILDER_NAME" --project="$GCP_PROJECT" --zone="$GCP_ZONE" \
            --command="sudo tail -30 /var/log/startup.log" 2>&1 | tail -35
        exit 1
    fi
    echo "  T+${ELAPSED}s — still installing..."
    sleep 60
done

# ── Stop builder + snapshot disk → image ────────────────────────────
echo "stopping builder..."
gcloud compute instances stop "$BUILDER_NAME" --project="$GCP_PROJECT" --zone="$GCP_ZONE" --quiet

echo "creating image $IMAGE_NAME..."
gcloud compute images create "$IMAGE_NAME" \
    --project="$GCP_PROJECT" \
    --source-disk="$BUILDER_NAME" \
    --source-disk-zone="$GCP_ZONE" \
    --family=dj-treta-v5 \
    --description="DJ Treta v5 audio pipeline — Essentia + madmom + demucs + basic-pitch + silero-vad + faster-whisper + CLAP + musicnn + cached ML models"

echo "deleting builder VM..."
gcloud compute instances delete "$BUILDER_NAME" --project="$GCP_PROJECT" --zone="$GCP_ZONE" --quiet

echo
echo "=== image $IMAGE_NAME ready ==="
echo "test boot:"
echo "  gcloud compute instances create v5-test-boot --image=$IMAGE_NAME --image-project=$GCP_PROJECT --zone=$GCP_ZONE --machine-type=n2-standard-4"
