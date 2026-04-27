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
$PIP install -q "cython<3.0"
$PIP install -q madmom || echo "madmom failed — phrase detection disabled"
$PIP install -q demucs
$PIP install -q basic-pitch || echo "basic-pitch failed"
$PIP install -q silero-vad
$PIP install -q torch torchaudio --index-url https://download.pytorch.org/whl/cpu
$PIP install -q faster-whisper
$PIP install -q laion-clap || echo "laion-clap failed"
$PIP install -q musicnn || echo "musicnn failed"

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

# basic-pitch model
/opt/venv/bin/python - <<'PY' || echo "basic-pitch cache failed"
from basic_pitch.inference import predict
# triggers model load on import indirectly
print("basic-pitch ready")
PY

# musicnn model
/opt/venv/bin/python - <<'PY' || echo "musicnn cache failed"
import musicnn  # noqa
print("musicnn ready")
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
