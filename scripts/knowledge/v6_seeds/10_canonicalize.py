"""Deterministic dedup of seeds_raw_*.parquet → seeds_canonical_v6.parquet.

For Stage 1 we have only one source (Beatport) and the data is already
clean (separate artist/title/version fields). LLM canonicalization is
overkill — a stable hash of (normalized_artist, normalized_title,
normalized_version) is enough to cluster duplicates.

Stage 2 will need the LLM path when we merge in Discogs / RA / 1001TL
where artist+title strings are messier.

Output schema:
  canonical_id     string  # "v6:" + sha256(normalized triple)[:16]
  canonical_artist string  # raw artist of best row
  canonical_title  string
  canonical_version string  # mix_name, e.g. "Extended Mix" / "Original Mix"
  canonical_remixer string  # extracted from version where possible
  release_date     string
  label            string
  genre            string
  subgenre         string
  bpm              float
  key_camelot      string
  duration_seconds int
  isrc             string
  beatport_url     string
  source_endpoints list[string]  # which endpoints produced this row
  source_genre_slugs list[string]  # which genre charts this track appeared in
  is_curated       bool  # True if any row came from /top-100
  source_count     int
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

import polars as pl


def normalize_str(s: str | None) -> str:
    """Lowercase, collapse whitespace, strip punctuation that varies between sources."""
    if not s:
        return ""
    s = s.lower()
    s = re.sub(r"[‘’]", "'", s)  # smart quotes
    s = re.sub(r"[“”]", '"', s)
    s = re.sub(r"&amp;", "&", s)
    # collapse all non-alphanumeric runs to single space
    s = re.sub(r"[^a-z0-9&]+", " ", s)
    return s.strip()


def normalize_artist(a: str | None) -> str:
    """Sort multi-artist billing alphabetically so 'A & B' == 'B & A'."""
    s = normalize_str(a)
    if " & " in s:
        parts = sorted(p.strip() for p in s.split(" & ") if p.strip())
        return " & ".join(parts)
    return s


def canonical_id(artist: str, title: str, version: str | None) -> str:
    triple = f"{normalize_artist(artist)}|{normalize_str(title)}|{normalize_str(version)}"
    h = hashlib.sha256(triple.encode()).hexdigest()[:16]
    return f"v6:{h}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument(
        "--input",
        type=Path,
        default=Path(__file__).parent / "output" / "seeds_raw_beatport.parquet",
    )
    ap.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).parent / "output" / "seeds_canonical_v6.parquet",
    )
    args = ap.parse_args()

    if not args.input.exists():
        print(f"input not found: {args.input}")
        return 2

    df = pl.read_parquet(args.input)
    print(f"input: {len(df):,} rows ({df['source'].n_unique()} sources)")

    # Add canonical_id deterministically
    df = df.with_columns(
        pl.struct(["artist", "title", "version"])
        .map_elements(
            lambda s: canonical_id(s["artist"], s["title"], s["version"]),
            return_dtype=pl.String,
        )
        .alias("canonical_id")
    )
    print(f"unique canonical_ids: {df['canonical_id'].n_unique():,}")

    # Group by canonical_id, pick the most-curated row per cluster
    # Priority: top-100 endpoint > tracks endpoint
    df = df.with_columns(
        (pl.col("source_endpoint") == "top-100").cast(pl.Int8).alias("_is_top"),
    )

    # Collapse to one row per canonical_id, prefer top-100 source
    canonical = (
        df.sort("_is_top", descending=True)
        .unique(subset=["canonical_id"], keep="first")
        .drop("_is_top")
    )

    # Aggregate provenance from all sibling rows
    aggs = (
        df.group_by("canonical_id")
        .agg(
            pl.col("source_endpoint").unique().alias("source_endpoints"),
            pl.col("chart_genre_slug").unique().alias("source_genre_slugs"),
            (pl.col("source_endpoint") == "top-100").any().alias("is_curated"),
            pl.len().alias("source_count"),
        )
    )
    canonical = canonical.join(aggs, on="canonical_id", how="left")

    # Final shape
    out = canonical.select(
        [
            "canonical_id",
            pl.col("artist").alias("canonical_artist"),
            pl.col("title").alias("canonical_title"),
            pl.col("version").alias("canonical_version"),
            pl.col("remix_artist").alias("canonical_remixer"),
            "release_date",
            "label",
            "genre",
            "subgenre",
            "bpm",
            "key_camelot",
            "duration_seconds",
            "isrc",
            "beatport_url",
            "source_endpoints",
            "source_genre_slugs",
            "is_curated",
            "source_count",
        ]
    )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    out.write_parquet(args.output, compression="zstd")

    print(f"\nwrote {args.output} ({out.estimated_size('mb'):.1f} MB)")
    print(f"  canonical tracks: {len(out):,}")
    print(f"  is_curated (top-100): {out.filter(pl.col('is_curated')).height:,}")
    print(f"  bpm populated: {out.filter(pl.col('bpm').is_not_null()).height:,}")
    print(f"  key_camelot populated: {out.filter(pl.col('key_camelot').is_not_null()).height:,}")
    print(f"  isrc populated: {out.filter(pl.col('isrc').is_not_null() & (pl.col('isrc') != '')).height:,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
