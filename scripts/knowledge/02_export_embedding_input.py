"""K0 step 2 — Export JSONL of embedding inputs to GCS.

Reads the v3 parquet, builds a short embedding-text per row, writes a JSONL
file per 500k rows directly to gs://${DJTRETA_GCS_BUCKET}/embeddings/input/
and the index-JSONL that maps row_id → mbid (needed to stitch output back).

Embedding text format:
    "{artist_name} - {title} ({year}) [{yt_matched_album}]"

Vertex AI batch-prediction input schema for text-embedding-005:
    {"content": "...", "task_type": "RETRIEVAL_DOCUMENT"}
"""
from __future__ import annotations

import os

import json
import sys
from pathlib import Path

import polars as pl
from google.cloud import storage

PARQUET = Path.home() / "Music" / "DJTreta" / "knowledge" / "dj_treta_library.parquet"
BUCKET = os.environ.get("DJTRETA_GCS_BUCKET") or sys.exit("DJTRETA_GCS_BUCKET required")
INPUT_PREFIX = "embeddings/input/"
INDEX_PREFIX = "embeddings/index/"
CHUNK_SIZE = 500_000


def build_text(row: dict) -> str:
    parts = []
    artist = (row.get("artist_name") or "").strip()
    title = (row.get("title") or "").strip()
    year = row.get("year")
    album = (row.get("yt_matched_album") or "").strip()
    styles = row.get("dvi_styles") or []
    labels = row.get("dvi_labels") or []

    head = f"{artist} - {title}".strip(" -")
    if year:
        head += f" ({year})"
    parts.append(head)
    if album and album.lower() != title.lower():
        parts.append(f"[{album}]")
    if styles:
        parts.append("styles: " + ", ".join(styles[:3]))
    if labels:
        parts.append("labels: " + ", ".join(labels[:2]))
    return " ".join(parts)[:500]  # cap at 500 chars, Vertex input limit is higher


def main() -> int:
    print(f"[k0.2] scanning {PARQUET}")
    lf = pl.scan_parquet(PARQUET).select(
        "mbid",
        "title",
        "artist_name",
        "year",
        "yt_matched_album",
        "dvi_styles",
        "dvi_labels",
    )
    total = lf.select(pl.len()).collect().item()
    print(f"[k0.2] {total:,} rows to embed")

    client = storage.Client(project=PROJECT)
    bucket = client.bucket(BUCKET)

    # Idempotency: skip chunks already uploaded with matching size.
    existing = {b.name for b in bucket.list_blobs(prefix=INPUT_PREFIX)}
    print(f"[k0.2] {len(existing)} chunk(s) already in gs://{BUCKET}/{INPUT_PREFIX}")

    chunks = (total + CHUNK_SIZE - 1) // CHUNK_SIZE
    for chunk_idx in range(chunks):
        offset = chunk_idx * CHUNK_SIZE
        chunk_name = f"{INPUT_PREFIX}chunk_{chunk_idx:04d}.jsonl"
        index_name = f"{INDEX_PREFIX}chunk_{chunk_idx:04d}.jsonl"

        if chunk_name in existing and index_name in existing:
            print(f"[k0.2] chunk {chunk_idx + 1}/{chunks} already uploaded — skip")
            continue

        # Pull this slice into memory (~500k rows × ~80 bytes ≈ 40 MB).
        slice_df = lf.slice(offset, CHUNK_SIZE).collect()

        input_lines = []
        index_lines = []
        for row in slice_df.iter_rows(named=True):
            mbid = row["mbid"]
            text = build_text(row)
            if not text.strip():
                continue
            input_lines.append(
                json.dumps(
                    {"content": text, "task_type": "RETRIEVAL_DOCUMENT"},
                    ensure_ascii=False,
                )
            )
            index_lines.append(json.dumps({"mbid": mbid, "text": text}, ensure_ascii=False))

        # Upload both (input + index) for this chunk.
        bucket.blob(chunk_name).upload_from_string(
            "\n".join(input_lines) + "\n",
            content_type="application/x-ndjson",
        )
        bucket.blob(index_name).upload_from_string(
            "\n".join(index_lines) + "\n",
            content_type="application/x-ndjson",
        )
        print(
            f"[k0.2] chunk {chunk_idx + 1}/{chunks} uploaded — "
            f"{len(input_lines):,} rows → gs://{BUCKET}/{chunk_name}"
        )

    print(f"[k0.2] done — {chunks} chunks at gs://{BUCKET}/{INPUT_PREFIX}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
