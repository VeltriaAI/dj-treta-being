#!/usr/bin/env bash
# VDJ Treta — prompt experiment harness.
#
# Generate a clip from a TEXT prompt (Veo, direct Vertex, 1080p), pull a
# representative frame, and build a side-by-side against the reference
# screenshot so we can compare look-vs-prompt and tune.
#
# Usage:
#   ./produce.sh <name> "<prompt>" [reference_image_path]
#
# Env overrides:
#   DUR=4        clip seconds (Veo allows 4/6/8; 4 = fastest/cheapest, same look)
#   RES=1080p    720p | 1080p (Veo native max is 1080p)
#   MODEL=...    passed through to gen-veo.py if set
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
VDJ="$(dirname "$DIR")"
NAME="${1:?usage: produce.sh <name> \"<prompt>\" [reference]}"
PROMPT="${2:?need a prompt}"
REF="${3:-}"
DUR="${DUR:-4}"; RES="${RES:-1080p}"
OUT="$DIR/$NAME.mp4"

# record the exact prompt used (the audit trail for learning)
printf '%s\n' "$PROMPT" > "$DIR/$NAME.prompt.txt"

echo "▶ generating '$NAME'  ($RES, ${DUR}s)…"
python3 "$VDJ/gen-veo.py" "$PROMPT" "$OUT" "$DUR" "$RES"

D=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$OUT" 2>/dev/null || echo 4)
MID=$(python3 -c "print(f'{$D*0.5:.2f}')")
echo "▶ extracting mid frame @ ${MID}s"
ffmpeg -y -loglevel error -ss "$MID" -i "$OUT" -frames:v 1 "$DIR/$NAME-frame.png"

if [ -n "$REF" ] && [ -f "$REF" ]; then
  echo "▶ side-by-side vs reference (left=reference, right=generated)"
  ffmpeg -y -loglevel error -i "$REF" -i "$DIR/$NAME-frame.png" \
    -filter_complex "[0:v]scale=-1:720[a];[1:v]scale=-1:720[b];[a][b]hstack=inputs=2" \
    "$DIR/$NAME-compare.png"
  echo "✓ $NAME-frame.png  +  $NAME-compare.png"
else
  echo "✓ $NAME-frame.png   (no reference given → no side-by-side)"
fi
