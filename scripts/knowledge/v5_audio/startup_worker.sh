#!/bin/bash
# Worker VM startup. Heavy install — Essentia, madmom, demucs, basic-pitch,
# silero-vad, faster-whisper, laion_clap, musicnn, librosa.
# Runs idempotent — survives spot preemption + auto-restart.
set -e
exec > /var/log/startup.log 2>&1

META_URL="http://metadata.google.internal/computeMetadata/v1/instance/attributes"
HDR='Metadata-Flavor: Google'
get_meta() { curl -sf -H "$HDR" "$META_URL/$1"; }

export GCP_PROJECT=$(get_meta GCP_PROJECT)
export GCS_BUCKET=$(get_meta GCS_BUCKET)
export DATASET_VERSION=$(get_meta DATASET_VERSION)
export RUN_ID=$(get_meta RUN_ID)
export SHARD_ID=$(get_meta SHARD_ID)
export WORKERS_PER_VM=$(get_meta WORKERS_PER_VM 2>/dev/null || echo 3)

echo "=== v5 worker shard=$SHARD_ID ==="

# Mumbai's GCE metadata endpoint negotiates HTTPS first. Newer google-auth
# tries https://metadata.google.internal:443 and fails on the internal CA.
# Force the legacy HTTP path + pin older google-auth.
export GCE_METADATA_HOST_USE_HTTPS=False
export SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
export REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt

# ── Install OS deps + python (idempotent) ────────────────────────────
if [ ! -f /var/lib/v5_setup_done ]; then
    apt-get update -qq
    apt-get install -y -qq \
        python3-pip python3-venv python3-dev build-essential \
        ffmpeg libsndfile1 git pkg-config \
        libssl-dev libffi-dev cmake \
        ca-certificates
    update-ca-certificates

    python3 -m venv /opt/venv
    /opt/venv/bin/pip install -q --upgrade pip wheel setuptools
    # numpy<2 first to lock it
    /opt/venv/bin/pip install -q "numpy<2.0"
    # Pin urllib3<2 and older google-auth to avoid Mumbai HTTPS metadata bug
    /opt/venv/bin/pip install -q "urllib3<2" certifi
    /opt/venv/bin/pip install -q "google-auth==2.29.0" "google-cloud-storage==2.18.0"
    # Core libs
    /opt/venv/bin/pip install -q \
        polars pyarrow scipy soundfile \
        yt-dlp librosa
    # Essentia (binary wheel for x86_64 Linux)
    /opt/venv/bin/pip install -q essentia || /opt/venv/bin/pip install -q essentia-tensorflow
    # Madmom needs cython 0.29.x specifically (NOT 3.x)
    /opt/venv/bin/pip install -q "cython<3.0"
    /opt/venv/bin/pip install -q madmom || echo "madmom install failed — phrase detection disabled"
    # Demucs (htdemucs)
    /opt/venv/bin/pip install -q demucs
    # basic-pitch (MIDI extraction)
    /opt/venv/bin/pip install -q basic-pitch || echo "basic-pitch failed"
    # silero-vad
    /opt/venv/bin/pip install -q silero-vad torch torchaudio --index-url https://download.pytorch.org/whl/cpu || \
        /opt/venv/bin/pip install -q silero-vad torch torchaudio
    # faster-whisper (CPU-friendly, lighter than openai-whisper)
    /opt/venv/bin/pip install -q faster-whisper
    # CLAP audio embedding
    /opt/venv/bin/pip install -q laion-clap || echo "laion-clap failed — CLAP disabled"
    # musicnn (genre/mood tags)
    /opt/venv/bin/pip install -q musicnn || echo "musicnn failed"
    touch /var/lib/v5_setup_done
fi

# ── Pre-cache models (downloaded once on first track, but warm them now) ──
mkdir -p /opt/models
export TORCH_HOME=/opt/models/torch
export HF_HOME=/opt/models/hf
export XDG_CACHE_HOME=/opt/models/cache

# ── Fetch worker code from GCS ────────────────────────────────────────
gsutil cp "gs://${GCS_BUCKET}/${DATASET_VERSION}/code/${RUN_ID}/worker.py" /opt/worker.py

# ── Run worker (foreground, restart on crash, but cap retries) ────────
RETRIES=0
while true; do
    /opt/venv/bin/python /opt/worker.py && break
    RETRIES=$((RETRIES + 1))
    if [ "$RETRIES" -gt 5 ]; then
        echo "worker failed 5+ times, giving up"
        break
    fi
    echo "worker exited non-zero — sleeping 60s and retrying ($RETRIES/5)..."
    sleep 60
done

echo "=== shard $SHARD_ID complete, idling for coordinator cleanup ==="
sleep infinity
