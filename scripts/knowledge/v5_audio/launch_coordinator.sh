#!/bin/bash
# Local-Mac launcher. Reads .env, uploads code to GCS, creates coordinator VM.
# Mac can sleep after this exits.
set -e

cd "$(dirname "$0")"

if [ ! -f .env ]; then
    echo "ERROR: copy config.example.env to .env and fill in values"
    exit 1
fi
set -a
source .env
set +a

# ── Sanity ────────────────────────────────────────────────────────────
: "${GCP_PROJECT:?must be set}"
: "${GCS_BUCKET:?must be set}"
: "${HF_TOKEN:?must be set}"
: "${RUN_ID:?must be set}"

GCP_ZONE="${GCP_ZONE:-us-central1-a}"
DATASET_VERSION="${DATASET_VERSION:-v5}"
COORDINATOR_MACHINE_TYPE="${COORDINATOR_MACHINE_TYPE:-e2-standard-2}"
COORD_NAME="v5-coordinator-${RUN_ID//_/-}"
COORD_NAME=$(echo "$COORD_NAME" | tr '[:upper:]' '[:lower:]' | cut -c1-62)

echo "=== v5 launch (run_id=$RUN_ID, coord=$COORD_NAME) ==="

# ── Ensure GCS bucket exists ─────────────────────────────────────────
if ! gsutil ls "gs://${GCS_BUCKET}" >/dev/null 2>&1; then
    GCP_REGION="${GCP_REGION:-us-central1}"
    echo "creating bucket gs://${GCS_BUCKET} in ${GCP_REGION}..."
    gsutil mb -p "$GCP_PROJECT" -l "$GCP_REGION" "gs://${GCS_BUCKET}"
fi

# ── Upload code to GCS ────────────────────────────────────────────────
echo "uploading code to gs://${GCS_BUCKET}/${DATASET_VERSION}/code/${RUN_ID}/..."
gsutil -q cp coordinator.py worker.py startup_worker.sh "gs://${GCS_BUCKET}/${DATASET_VERSION}/code/${RUN_ID}/"

# ── Build metadata flag for VM ────────────────────────────────────────
META_PARTS=(
    "GCP_PROJECT=${GCP_PROJECT}"
    "GCS_BUCKET=${GCS_BUCKET}"
    "GCP_ZONE=${GCP_ZONE}"
    "GCP_REGION=${GCP_REGION:-us-central1}"
    "HF_TOKEN=${HF_TOKEN}"
    "HF_DATASET_REPO=${HF_DATASET_REPO:-NaturNestAI/electronic-music-knowledge}"
    "DATASET_VERSION=${DATASET_VERSION}"
    "RUN_ID=${RUN_ID}"
    "WORKER_COUNT=${WORKER_COUNT:-32}"
    "WORKER_MACHINE_TYPE=${WORKER_MACHINE_TYPE:-e2-standard-4}"
    "USE_SPOT_WORKERS=${USE_SPOT_WORKERS:-true}"
    "PRIORITY_MIN_YEAR=${PRIORITY_MIN_YEAR:-2020}"
    "PRIORITY_REQUIRE_VIDEO_ID=${PRIORITY_REQUIRE_VIDEO_ID:-true}"
    "PRIORITY_LIMIT=${PRIORITY_LIMIT:-200000}"
    "KEEP_AUDIO_HOT=${KEEP_AUDIO_HOT:-false}"
)
META=$(IFS=,; echo "${META_PARTS[*]}")

# ── Create coordinator VM ─────────────────────────────────────────────
# Use the prebuilt v5 image when available — boots in ~30s with all deps
# already installed and ML models cached. Falls back to vanilla debian-12
# (slow apt+pip install per VM) if the image hasn't been built yet.
V5_IMAGE_NAME="${V5_IMAGE_NAME:-dj-treta-v5-worker-1}"
if gcloud compute images describe "$V5_IMAGE_NAME" --project="$GCP_PROJECT" >/dev/null 2>&1; then
    IMAGE_FLAGS="--image=$V5_IMAGE_NAME --image-project=$GCP_PROJECT"
    echo "using prebuilt image: $V5_IMAGE_NAME"
else
    IMAGE_FLAGS="--image-family=debian-12 --image-project=debian-cloud"
    echo "WARN: image $V5_IMAGE_NAME not found — using vanilla debian-12 (slow first boot)"
    echo "      run ./build_image.sh once to make all future runs ~20 min faster per VM"
fi

gcloud compute instances create "$COORD_NAME" \
    --project="$GCP_PROJECT" \
    --zone="$GCP_ZONE" \
    --machine-type="$COORDINATOR_MACHINE_TYPE" \
    --boot-disk-size=50GB \
    $IMAGE_FLAGS \
    --scopes=cloud-platform \
    --metadata="$META" \
    --metadata-from-file=startup-script=startup_coordinator.sh

echo
echo "=== coordinator VM created: $COORD_NAME ==="
echo "monitor with:"
echo "  gcloud compute ssh $COORD_NAME --zone=$GCP_ZONE --project=$GCP_PROJECT --command='sudo tail -f /var/log/startup.log'"
echo
echo "your Mac can sleep now. coordinator will:"
echo "  1. download v4, build queue, spawn workers"
echo "  2. wait ~18h for workers to complete"
echo "  3. merge + push v5 to HF"
echo "  4. delete all VMs (including itself)"
