"""Convert seeds_resolved_v6.parquet → queue parquet (coordinator-compatible).

The v5 audio coordinator (`scripts/knowledge/v5_audio/coordinator.py:88`)
reads exactly 5 columns from its queue parquet:
    mbid, video_id, year, artist_name, title

This script synthesizes a deterministic mbid (uuid5 namespace) from the
canonical_id, picks `year` out of release_date, aliases canonical_artist→
artist_name and canonical_title→title, and outputs a single queue parquet
ready to drop into the existing pipeline.

Optionally shards into N parts and uploads to GCS at the v6 prefix so the
worker fleet can pick up directly.
"""
from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

import polars as pl

# Stable namespace for v6 mbids (so re-runs produce the same uuids)
V6_NAMESPACE = uuid.UUID("ddc1771e-5e91-4b2a-b6e4-4f5e5e7e7e7e")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument(
        "--input",
        type=Path,
        default=Path(__file__).parent / "output" / "seeds_resolved_v6.parquet",
    )
    ap.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).parent / "output" / "queue_v6_stage1.parquet",
    )
    ap.add_argument("--shards", type=int, default=20,
                    help="number of shard parquets to write (default 20 to match worker fleet)")
    ap.add_argument("--shard-prefix", type=Path, default=None,
                    help="if set, also write shard_NNN.parquet files in this dir")
    ap.add_argument("--min-confidence", type=float, default=0.7,
                    help="drop rows with video_id_confidence below this")
    args = ap.parse_args()

    if not args.input.exists():
        print(f"input not found: {args.input}")
        return 2

    df = pl.read_parquet(args.input)
    print(f"input: {len(df):,} resolved seeds")

    # Filter to high-confidence playable rows
    df = df.filter(
        pl.col("video_id").is_not_null()
        & (pl.col("video_id_confidence") >= args.min_confidence)
    )
    print(f"after confidence filter (>={args.min_confidence}): {len(df):,}")

    # Synthesize coordinator schema
    df = df.with_columns(
        pl.col("canonical_id")
        .map_elements(
            lambda cid: str(uuid.uuid5(V6_NAMESPACE, cid)),
            return_dtype=pl.String,
        )
        .alias("mbid"),
        pl.col("release_date").str.slice(0, 4).cast(pl.Int32, strict=False).alias("year"),
        pl.col("canonical_artist").alias("artist_name"),
        pl.col("canonical_title").alias("title"),
    )

    queue = df.select(["mbid", "video_id", "year", "artist_name", "title",
                       "bpm", "key_camelot", "genre", "label", "isrc",
                       "canonical_id", "video_id_confidence"])

    args.output.parent.mkdir(parents=True, exist_ok=True)
    queue.write_parquet(args.output, compression="zstd")
    print(f"wrote {args.output} ({queue.estimated_size('mb'):.2f} MB)")

    # Optional shard writeout
    if args.shard_prefix:
        args.shard_prefix.mkdir(parents=True, exist_ok=True)
        per = max(1, len(queue) // args.shards)
        for i in range(args.shards):
            start = i * per
            end = (i + 1) * per if i < args.shards - 1 else len(queue)
            shard = queue.slice(start, end - start)
            shard_path = args.shard_prefix / f"shard_{i:03d}.parquet"
            shard.write_parquet(shard_path, compression="zstd")
            print(f"  shard {i:03d}: {len(shard)} rows → {shard_path.name}")

    print(f"\n=== queue summary ===")
    print(f"  rows: {len(queue):,}")
    print(f"  unique mbids: {queue['mbid'].n_unique():,}")
    print(f"  year populated: {queue.filter(pl.col('year').is_not_null()).height:,}")
    print(f"  bpm populated: {queue.filter(pl.col('bpm').is_not_null()).height:,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
