#!/bin/bash
# DJ Treta — One-command setup
# Usage: ./install.sh

set -e

echo "🎧 DJ Treta — Installing..."

# Check Python 3.12+
PYTHON_VERSION=$(python3 --version 2>&1 | grep -oP '\d+\.\d+' | head -1)
MAJOR=$(echo "$PYTHON_VERSION" | cut -d. -f1)
MINOR=$(echo "$PYTHON_VERSION" | cut -d. -f2)

if [ "$MAJOR" -lt 3 ] || ([ "$MAJOR" -eq 3 ] && [ "$MINOR" -lt 10 ]); then
    echo "❌ Python 3.10+ required (found $PYTHON_VERSION)"
    echo "   Install via: pyenv install 3.12 && pyenv local 3.12"
    exit 1
fi

# Create venv
if [ ! -d ".venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv .venv
fi

source .venv/bin/activate

# Install dependencies
echo "Installing dependencies..."
pip install -q smolagents[litellm] httpx pyyaml

# Create music directory
MUSIC_DIR="${HOME}/Music/DJTreta"
mkdir -p "$MUSIC_DIR"/{melodic-techno,progressive,vocal,dark-techno,minimal,deep,psychill}
echo "Music directory: $MUSIC_DIR"

# Check config
if [ ! -f "config.yaml" ]; then
    echo "⚠ No config.yaml found — copy from template and edit"
fi

# Verify
echo ""
echo "Verifying installation..."
python3 -c "
from agent.config import load_config
from agent.state import DJState, DJPhase
from agent.camelot import key_compatibility_score
cfg = load_config()
print(f'  Config: OK (model={cfg.llm.model})')
print(f'  Camelot: OK (Am↔Em = {key_compatibility_score(\"Am\", \"Em\")})')
print(f'  State: OK')
"

echo ""
echo "✅ DJ Treta installed!"
echo ""
echo "Usage:"
echo "  source .venv/bin/activate"
echo "  python -m agent --mood techno-deep --duration 60"
echo ""
echo "Prerequisites:"
echo "  - Mixxx (forked) running with HTTP API on :7778"
echo "  - LiteLLM proxy accessible (configure in config.yaml)"
echo "  - Tracks in ~/Music/DJTreta/"
