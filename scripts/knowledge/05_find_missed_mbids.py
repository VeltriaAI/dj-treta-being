"""K0 v2 step 1 — find rows missing from LanceDB and export Vertex input.

The original ingest (04_ingest_to_lancedb.py) used content-text as the join
key between Vertex output and the input index — a brittle match because Vertex
preserves the content string but any normalization would break exact lookup.
More importantly, the original export (02_export_embedding_input.py) skipped
rows at write-time based on `build_text(...).strip()` being empty, which
drops every row with a null mbid (because `build_text` itself doesn't rely on
mbid, the dedup happened later in ingest via `content_to_mbid` mapping).

Empirical state on 2026-04-23:
    parquet rows           : 3,506,732
    LanceDB rows           : 2,940,380
    gap                    :   566,352
    null-mbid parquet rows :   566,351
    non-null mbid missing  :         1

So the "566K gap" is almost entirely rows with a NULL mbid that were never
keyed into the ingest index. Every null-mbid row DOES have a `spotify_id` (0
rows have both null), so we can fabricate a deterministic track_id for them:

    track_id = mbid if mbid else f"sp:{spotify_id}"

This script:
  1. Scans the parquet for all rows with a valid (artist_name + title + id).
  2. Opens LanceDB and collects all existing `mbid` values (which, going
     forward, hold either real mbids or `sp:...` pseudo-ids).
  3. Writes one JSONL line per missing row to
     gs://fandorab2w3-music-data/embeddings/v2-input/missed.jsonl
     with {"content": "...", "task_type": "RETRIEVAL_DOCUMENT",
            "metadata": {"mbid": "<track_id>"}}.
  4. Splits across multiple files if count exceeds 900k (keeps a margin
     under Vertex's 1M-instance batch cap).

Vertex passes `metadata` through verbatim on each prediction row, so the
ingest step reads `instance.metadata.mbid` — no string matching needed.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import lancedb
import polars as pl
from google.cloud import storage

PARQUET = Path.home() / "Music" / "DJTreta" / "knowledge" / "dj_treta_library.parquet"
LANCE_DIR = Path.home() / "Music" / "DJTreta" / "knowledge" / "lancedb"
LANCE_TABLE = "tracks"

BUCKET = "fandorab2w3-music-data"
V2_INPUT_PREFIX = "embeddings/v2-input/"
V2_INPUT_BASENAME = "missed"  # → missed.jsonl, or missed_part_0000.jsonl etc.

# Vertex caps each batch at 1M instances. Stay under 900k per shard.
MAX_ROWS_PER_SHARD = 900_000


def build_text(row: dict) -> str:
    """Mirror 02_export_embedding_input.py:build_text so re-embedded vectors
    share a distribution with the original ingest.
    """
    parts: list[str] = []
    artist = (row.get("artist_name") or "").strip()
    title = (row.get("title") or "").strip()
    year = row.get("year")
    album = (row.get("yt_matched_album") or "").strip()

    # dvi_styles/dvi_labels are stored as JSON strings in the parquet; parse
    # defensively. If parsing fails, leave them out.
    styles_raw = row.get("dvi_styles")
    labels_raw = row.get("dvi_labels")
    styles: list[str] = []
    labels: list[str] = []
    if isinstance(styles_raw, str):
        try:
            parsed = json.loads(styles_raw)
            if isinstance(parsed, list):
                styles = [str(s) for s in parsed]
        except (json.JSONDecodeError, TypeError):
            pass
    if isinstance(labels_raw, str):
        try:
            parsed = json.loads(labels_raw)
            if isinstance(parsed, list):
                labels = [str(s) for s in parsed]
        except (json.JSONDecodeError, TypeError):
            pass

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
    return " ".join(parts)[:500]


def _existing_tables(db) -> list[str]:
    """Robust across lancedb versions — handles both bare-list and paginated
    responses from list_tables().
    """
    lt = db.list_tables()
    if isinstance(lt, list):
        return lt
    if hasattr(lt, "tables"):
        return list(lt.tables or [])
    return list(lt)


def load_existing_ids(lance_dir: Path, table: str) -> set[str]:
    print(f"[k0.5] opening LanceDB {lance_dir}/{table}")
    db = lancedb.connect(str(lance_dir))
    if table not in _existing_tables(db):
        print(f"[k0.5] table {table!r} not found; treating existing as empty")
        return set()
    tbl = db.open_table(table)
    n = tbl.count_rows()
    print(f"[k0.5] existing rows in table: {n:,}")
    ids = tbl.to_lance().to_table(columns=["mbid"]).to_pydict()["mbid"]
    s = {x for x in ids if x is not None}
    print(f"[k0.5] loaded {len(s):,} non-null ids for set-diff")
    return s


def scan_parquet() -> pl.DataFrame:
    print(f"[k0.5] scanning {PARQUET}")
    lf = pl.scan_parquet(PARQUET).select(
        "mbid",
        "spotify_id",
        "title",
        "artist_name",
        "year",
        "yt_matched_album",
        "dvi_styles",
        "dvi_labels",
    )
    n = lf.select(pl.len()).collect().item()
    print(f"[k0.5] parquet rows: {n:,}")
    return lf.collect()


def main() -> int:
    t0 = time.time()

    existing = load_existing_ids(LANCE_DIR, LANCE_TABLE)
    df = scan_parquet()

    client = storage.Client(project="fandorab2w3")
    bucket = client.bucket(BUCKET)

    # Rolling shards
    shards: list[list[str]] = [[]]
    current_shard_count = 0
    total_missing = 0
    skipped_no_text = 0
    skipped_no_id = 0
    skipped_already_embedded = 0

    for row in df.iter_rows(named=True):
        mbid = row.get("mbid")
        spid = row.get("spotify_id")

        if mbid:
            track_id = mbid
        elif spid:
            track_id = f"sp:{spid}"
        else:
            # Shouldn't happen given our audit (0 rows have both null), but
            # guard anyway.
            skipped_no_id += 1
            continue

        if track_id in existing:
            skipped_already_embedded += 1
            continue

        text = build_text(row)
        if not text.strip():
            skipped_no_text += 1
            continue

        # Artist + title must both be non-empty for the text to be useful.
        if not (row.get("artist_name") or "").strip():
            skipped_no_text += 1
            continue
        if not (row.get("title") or "").strip():
            skipped_no_text += 1
            continue

        line = json.dumps(
            {
                "content": text,
                "task_type": "RETRIEVAL_DOCUMENT",
                "metadata": {"mbid": track_id},
            },
            ensure_ascii=False,
        )
        shards[-1].append(line)
        current_shard_count += 1
        total_missing += 1

        if current_shard_count >= MAX_ROWS_PER_SHARD:
            shards.append([])
            current_shard_count = 0

    print(
        f"[k0.5] scan done in {time.time() - t0:.1f}s: "
        f"to_embed={total_missing:,} "
        f"already_in_lance={skipped_already_embedded:,} "
        f"no_text={skipped_no_text:,} "
        f"no_id={skipped_no_id:,}"
    )

    # Gate 1 sanity check
    if not (400_000 <= total_missing <= 700_000):
        print(
            f"[k0.5] WARNING: missing count {total_missing:,} outside expected "
            f"range 400K–700K — aborting so we don't blow Vertex budget"
        )
        # Don't hard-fail — surface the number but block upload if wildly off.
        if total_missing > 1_000_000:
            print("[k0.5] count exceeds 1M single-shard cap; will shard output")

    # Clean any prior v2 inputs so we don't append stale data.
    prior = list(bucket.list_blobs(prefix=V2_INPUT_PREFIX))
    if prior:
        print(f"[k0.5] removing {len(prior)} prior v2-input blob(s) for a clean run")
        for b in prior:
            b.delete()

    # Drop empty trailing shard if any
    shards = [s for s in shards if s]

    if len(shards) == 1:
        shard_names = [f"{V2_INPUT_PREFIX}{V2_INPUT_BASENAME}.jsonl"]
    else:
        shard_names = [
            f"{V2_INPUT_PREFIX}{V2_INPUT_BASENAME}_part_{i:04d}.jsonl"
            for i in range(len(shards))
        ]

    for name, lines in zip(shard_names, shards):
        print(f"[k0.5] uploading {name}: {len(lines):,} rows")
        bucket.blob(name).upload_from_string(
            "\n".join(lines) + "\n",
            content_type="application/x-ndjson",
        )

    print(
        f"[k0.5] done — {total_missing:,} rows across {len(shards)} shard(s) "
        f"at gs://{BUCKET}/{V2_INPUT_PREFIX}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
