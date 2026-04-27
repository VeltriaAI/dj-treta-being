#!/bin/bash
# Worker VM startup. Reads metadata, installs deps, runs worker.py.
# Designed to be idempotent — survives spot preemption + auto-restart.
set -e
exec > /var/log/startup.log 2>&1

# ── Read VM metadata ──────────────────────────────────────────────────
META_URL="http://metadata.google.internal/computeMetadata/v1/instance/attributes"
HDR='Metadata-Flavor: Google'
get_meta() { curl -sf -H "$HDR" "$META_URL/$1"; }

export GCP_PROJECT=$(get_meta GCP_PROJECT)
export GCS_BUCKET=$(get_meta GCS_BUCKET)
export DATASET_VERSION=$(get_meta DATASET_VERSION)
export RUN_ID=$(get_meta RUN_ID)
export SHARD_ID=$(get_meta SHARD_ID)
export KEEP_AUDIO_HOT=$(get_meta KEEP_AUDIO_HOT)
export WORKERS_PER_VM=4

echo "=== v5 worker shard=$SHARD_ID ==="

# ── Install deps (idempotent) ─────────────────────────────────────────
if [ ! -f /var/lib/v5_setup_done ]; then
    apt-get update -qq
    apt-get install -y -qq python3-pip python3-venv ffmpeg python3-dev build-essential
    python3 -m venv /opt/venv
    /opt/venv/bin/pip install -q --upgrade pip
    /opt/venv/bin/pip install -q yt-dlp polars pyarrow google-cloud-storage \
        librosa numpy "numpy<2.0" scipy
    # Essentia binary wheel (CPU)
    /opt/venv/bin/pip install -q essentia || /opt/venv/bin/pip install -q essentia-tensorflow
    touch /var/lib/v5_setup_done
fi

# ── Fetch worker code ─────────────────────────────────────────────────
cd /opt
gsutil cp "gs://${GCS_BUCKET}/${DATASET_VERSION}/code/${RUN_ID}/worker.py" worker.py

# ── Run worker (foreground, restart on crash) ─────────────────────────
while true; do
    /opt/venv/bin/python /opt/worker.py && break
    echo "worker exited non-zero — sleeping 30s and retrying..."
    sleep 30
done

echo "=== shard $SHARD_ID complete, idling for coordinator cleanup ==="
sleep infinity
