#!/bin/bash
# Runs on the builder VM as a startup script. Installs everything v5_audio
# workers need + pre-caches ML model weights so worker boots are zero-touch.
#
# Writes /var/lib/v5_image_ready when done (build_image.sh polls for this).
set -e
exec > /var/log/startup.log 2>&1

echo "=== v5 image builder ==="

export DEBIAN_FRONTEND=noninteractive
export GCE_METADATA_HOST_USE_HTTPS=False
export SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
export REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt

# ── OS packages ───────────────────────────────────────────────────────
apt-get update -qq
apt-get install -y -qq \
    python3-pip python3-venv python3-dev build-essential \
    ffmpeg libsndfile1 git pkg-config \
    libssl-dev libffi-dev cmake \
    ca-certificates curl wget
update-ca-certificates

# ── Python venv with all v5 deps ─────────────────────────────────────
python3 -m venv /opt/venv
PIP="/opt/venv/bin/pip"

$PIP install -q --upgrade pip wheel setuptools
$PIP install -q "numpy<2.0" "urllib3<2" certifi
$PIP install -q "google-auth==2.29.0" "google-cloud-storage==2.18.0" huggingface_hub
$PIP install -q polars pyarrow scipy soundfile yt-dlp librosa
$PIP install -q essentia || $PIP install -q essentia-tensorflow

# torchaudio<2.9 keeps silero-vad working without torchcodec; torchvision
# is required transitively by laion-clap.
$PIP install -q "torch<2.9" "torchaudio<2.9" "torchvision<0.24" \
    --index-url https://download.pytorch.org/whl/cpu

$PIP install -q demucs
$PIP install -q silero-vad
$PIP install -q faster-whisper
$PIP install -q laion-clap || echo "laion-clap failed"

# Replacements for the 3 dead packages from earlier image:
#   madmom   → allin1   (Sony CSI 2024, downbeats + bars + segment labels)
#   musicnn  → panns_inference (AudioSet 527-class genre/mood/instrument tags)
#   basic-pitch (still used, but with lower-level predict() API in worker.py
#                to bypass predict_and_save TF loader issues)
$PIP install -q allin1 || echo "allin1 failed — phrase/segment detection disabled"
$PIP install -q panns_inference || echo "panns_inference failed — tags disabled"
$PIP install -q basic-pitch || echo "basic-pitch failed"

# ── Pre-cache ML model weights ───────────────────────────────────────
mkdir -p /opt/models /opt/models/torch /opt/models/hf /opt/models/cache
export TORCH_HOME=/opt/models/torch
export HF_HOME=/opt/models/hf
export XDG_CACHE_HOME=/opt/models/cache

# Whisper tiny (faster-whisper)
/opt/venv/bin/python - <<'PY' || echo "whisper cache failed"
from faster_whisper import WhisperModel
WhisperModel("tiny", device="cpu", compute_type="int8")
print("whisper tiny cached")
PY

# silero-vad
/opt/venv/bin/python - <<'PY' || echo "silero-vad cache failed"
from silero_vad import load_silero_vad
load_silero_vad()
print("silero-vad cached")
PY

# CLAP weights (downloaded on first use; pre-fetch here)
/opt/venv/bin/python - <<'PY' || echo "CLAP cache failed (non-fatal)"
import laion_clap
m = laion_clap.CLAP_Module(enable_fusion=False)
m.load_ckpt()
print("CLAP cached")
PY

# htdemucs weights
/opt/venv/bin/python -m demucs.separate --help >/dev/null 2>&1 || echo "demucs --help failed (non-fatal)"
# Force htdemucs model download
/opt/venv/bin/python - <<'PY' || echo "demucs model preload failed"
import torch
from demucs.pretrained import get_model
m = get_model("htdemucs")
print("demucs htdemucs cached")
PY

# basic-pitch model — pre-fetch ICASSP 2022 model
/opt/venv/bin/python - <<'PY' || echo "basic-pitch cache failed"
from basic_pitch import ICASSP_2022_MODEL_PATH
import os
print(f"basic-pitch model path: {ICASSP_2022_MODEL_PATH}, exists: {os.path.exists(ICASSP_2022_MODEL_PATH)}")
PY

# allin1 model (madmom replacement)
/opt/venv/bin/python - <<'PY' || echo "allin1 cache failed"
import allin1  # noqa
print("allin1 ready")
PY

# PANNs model (musicnn replacement) — downloads ~600MB checkpoint on first use
/opt/venv/bin/python - <<'PY' || echo "PANNs cache failed"
from panns_inference import AudioTagging
m = AudioTagging(checkpoint_path=None, device="cpu")
print("PANNs cached")
PY

# Make models directory readable to all (workers run as default user)
chmod -R a+rX /opt/models /opt/venv

# ── Persistent env vars for booting workers ──────────────────────────
cat > /etc/profile.d/v5_audio.sh <<'EOF'
export TORCH_HOME=/opt/models/torch
export HF_HOME=/opt/models/hf
export XDG_CACHE_HOME=/opt/models/cache
export GCE_METADATA_HOST_USE_HTTPS=False
export SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
export REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt
EOF
chmod +x /etc/profile.d/v5_audio.sh

echo "=== image build complete ==="
# Markers — both startup scripts check these and skip the install step
touch /var/lib/v5_setup_done /var/lib/v5_coord_setup_done /var/lib/v5_image_ready
