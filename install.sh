#!/bin/bash
# DJClaw — Install your own AI DJ Being
# Usage: ./install.sh
set -e

echo ""
echo "  DJClaw — Installing..."
echo ""

# Check Python 3.10+
python3 -c "import sys; assert sys.version_info >= (3, 10), f'Python 3.10+ required (found {sys.version})'" 2>/dev/null || {
    echo "  Python 3.10+ required."
    echo "  Install via: pyenv install 3.12 && pyenv local 3.12"
    exit 1
}

# Create venv
if [ ! -d ".venv" ]; then
    echo "  Creating virtual environment..."
    python3 -m venv .venv
fi

source .venv/bin/activate

# Install package
echo "  Installing dependencies..."
pip install -q -e .

# Create music directory
MUSIC_DIR="${HOME}/Music/DJTreta"
mkdir -p "$MUSIC_DIR"
echo "  Music directory: $MUSIC_DIR"

# First-time setup
if [ ! -f ".beings/SOUL.md" ] || [ ! -s ".beings/SOUL.md" ]; then
    echo ""
    djclaw init
else
    echo ""
    echo "  Already initialized. Run: djclaw start"
fi

echo ""
echo "  Done!"
echo "  Usage:"
echo "    source .venv/bin/activate"
echo "    djclaw start"
echo "    djclaw talk 'play something deep'"
echo ""
echo "  Prerequisites:"
echo "    - Mixxx (forked) with HTTP API on :7778"
echo "    - LLM API key: export DJTRETA_LLM_API_KEY='your-key'"
echo ""
