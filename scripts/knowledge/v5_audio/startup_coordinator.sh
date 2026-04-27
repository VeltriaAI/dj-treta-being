#!/bin/bash
# Coordinator VM startup. Reads metadata + .env via metadata, installs deps,
# fetches scripts from GCS, runs coordinator.py.
set -e
exec > /var/log/startup.log 2>&1

META_URL="http://metadata.google.internal/computeMetadata/v1/instance/attributes"
HDR='Metadata-Flavor: Google'
get_meta() { curl -sf -H "$HDR" "$META_URL/$1"; }

# All env vars come through VM metadata (set by launch_coordinator.sh).
for var in GCP_PROJECT GCS_BUCKET GCP_ZONE GCP_REGION HF_TOKEN HF_DATASET_REPO \
           DATASET_VERSION RUN_ID WORKER_COUNT WORKER_MACHINE_TYPE \
           USE_SPOT_WORKERS PRIORITY_MIN_YEAR PRIORITY_REQUIRE_VIDEO_ID \
           PRIORITY_LIMIT KEEP_AUDIO_HOT; do
    val=$(get_meta "$var" || echo "")
    if [ -n "$val" ]; then
        export "$var=$val"
    fi
done

echo "=== v5 coordinator (run_id=$RUN_ID) ==="

# ── Install deps ──────────────────────────────────────────────────────
if [ ! -f /var/lib/v5_coord_setup_done ]; then
    apt-get update -qq
    apt-get install -y -qq python3-pip python3-venv
    python3 -m venv /opt/venv
    /opt/venv/bin/pip install -q --upgrade pip
    /opt/venv/bin/pip install -q polars pyarrow google-cloud-storage huggingface_hub
    touch /var/lib/v5_coord_setup_done
fi

# ── Fetch coordinator + worker scripts from GCS ───────────────────────
mkdir -p /opt/v5
cd /opt/v5
gsutil cp "gs://${GCS_BUCKET}/${DATASET_VERSION}/code/${RUN_ID}/coordinator.py" .
gsutil cp "gs://${GCS_BUCKET}/${DATASET_VERSION}/code/${RUN_ID}/worker.py" .
gsutil cp "gs://${GCS_BUCKET}/${DATASET_VERSION}/code/${RUN_ID}/startup_worker.sh" .

# ── Run coordinator ───────────────────────────────────────────────────
/opt/venv/bin/python /opt/v5/coordinator.py
