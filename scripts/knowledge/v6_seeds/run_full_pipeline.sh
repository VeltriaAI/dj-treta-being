#!/bin/bash
# v6 seed pipeline — end-to-end orchestrator.
#
# Runs the steps in order; each is idempotent and resumable so re-running
# this script after a partial failure picks up where it stopped.
#
#   01 → scrape Beatport (top-100 + 50 pages × 15 genres)
#   10 → canonicalize (deterministic dedup)
#   11 → resolve YT video_ids (ytmusicapi, 8-thread)
#   12 → build coordinator-compatible queue parquet
#   13 → add text-embedding-005 vectors (Vertex AI, ~$2-30 depending on row count)
#   --   merge result parquets from GCS once worker fleet completes
#   --   re-run 13 to add vectors to the merged result, push to HF
#
# Usage:
#   bash run_full_pipeline.sh stage1   # 1.5K curated
#   bash run_full_pipeline.sh stage2   # full multi-source ~150K
#
# After steps 01-13, manually launch the worker fleet via
#   ../v5_audio/spawn_nonspot_fleet.sh v6-${STAGE}-${DATE} v6
# and wait for ckpts to flow before re-running merge + embed.
set -euo pipefail
cd "$(dirname "$0")"

STAGE="${1:-stage1}"
PY="/Users/manish.pratap/beings/dj-treta/.venv/bin/python"
OUT_DIR="output"

if [ "$STAGE" = "stage1" ]; then
    PAGES_PER_GENRE=20
    CURATED_FLAG="--curated-only"
elif [ "$STAGE" = "stage2" ]; then
    PAGES_PER_GENRE=120
    CURATED_FLAG=""
else
    echo "usage: $0 stage1|stage2"
    exit 2
fi

echo "=== v6 ${STAGE} pipeline ==="

# 01 — Beatport scrape (uses cached HTML if rerun)
if [ ! -f "$OUT_DIR/seeds_raw_beatport.parquet" ] || [ "$STAGE" = "stage2" ]; then
    echo
    echo "[01] scraping Beatport (pages-per-genre=$PAGES_PER_GENRE)..."
    "$PY" 01_scrape_beatport.py --pages-per-genre "$PAGES_PER_GENRE" --delay 1.5
fi

# 10 — canonicalize
echo
echo "[10] canonicalizing..."
"$PY" 10_canonicalize.py

# 11 — resolve YouTube IDs
echo
echo "[11] resolving YT video_ids (this may take 20-60min for stage2)..."
"$PY" 11_resolve_youtube_ids.py $CURATED_FLAG --threads 8

# 12 — build coordinator-compat queue
echo
echo "[12] building queue parquet..."
"$PY" 12_build_queue.py --shards 20 --shard-prefix "$OUT_DIR/shards_v6_${STAGE}"

# 13 — add text embeddings
echo
echo "[13] generating text embeddings..."
"$PY" 13_add_text_embeddings.py \
    --input "$OUT_DIR/queue_v6_stage1.parquet" \
    --output "$OUT_DIR/queue_v6_${STAGE}_with_vec.parquet"

echo
echo "=== seed pipeline done ==="
echo "next: spawn worker fleet:"
echo "  cd ../v5_audio && bash spawn_nonspot_fleet.sh v6-${STAGE}-\$(date +%Y-%m-%d) v6"
echo "  (and upload shards to GCS first via gsutil cp)"
