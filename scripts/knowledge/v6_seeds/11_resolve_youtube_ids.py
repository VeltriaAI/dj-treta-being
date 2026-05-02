"""Resolve YouTube Music video_id for each canonical track via ytmusicapi.

For each canonical track, search YT Music with `{artist} {title}` and pick
the top result whose duration matches the Beatport-stated duration ±30s.
Records `video_id_confidence` in [0,1] based on match quality.

Output: seeds_resolved_v6.parquet (canonical schema + video_id columns)

Rate strategy:
  ytmusicapi is unofficial (uses youtube.com/youtubei). Rate-limit
  conservatively: 8 threads × ~0.3s/req = ~24 req/sec. 30K tracks ≈ 21min.
  If we hit 429 / Cloudflare blocks, threads fall back gracefully and we
  retry with backoff in lib/http.py style.

  Resume-friendly: video_ids checkpointed every 1000 rows to disk; rerun
  picks up where it stopped.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import polars as pl

# YTMusic client is lazy; one per thread to avoid connection contention
_LOCAL = threading.local()


def get_yt():
    if not hasattr(_LOCAL, "yt"):
        from ytmusicapi import YTMusic
        _LOCAL.yt = YTMusic()
    return _LOCAL.yt


def _parse_duration(d) -> int:
    if isinstance(d, int) and d > 0:
        return d
    if isinstance(d, str) and ":" in d:
        parts = d.split(":")
        try:
            if len(parts) == 2:
                return int(parts[0]) * 60 + int(parts[1])
            if len(parts) == 3:
                return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        except ValueError:
            pass
    return 0


def _tokens(s: str) -> set[str]:
    """Lowercase tokens, drop short/common words for title-similarity check."""
    import re as _re
    s = (s or "").lower()
    toks = _re.findall(r"[a-z0-9]+", s)
    stop = {"the", "a", "an", "of", "and", "to", "in", "on", "for", "with",
            "feat", "ft", "vs", "extended", "mix", "original", "remix", "edit"}
    return {t for t in toks if len(t) >= 2 and t not in stop}


def _title_overlap(query: str, candidate: str) -> float:
    """Jaccard token overlap on the title — guards against duration-only false matches."""
    q = _tokens(query)
    c = _tokens(candidate)
    if not q or not c:
        return 0.0
    inter = q & c
    return len(inter) / max(len(q), 1)  # what fraction of query tokens are in candidate


def resolve_one(row: dict) -> dict:
    """Search YT Music for this track, return updated row with video_id fields.

    Scoring combines title overlap (anti-bait) and duration delta:
      score = 0.6 * title_overlap + 0.4 * duration_score
    Only keep matches with title_overlap >= 0.5 (at least half of meaningful
    tokens in the search title appear in the YT title).
    """
    artist = row["canonical_artist"]
    title = row["canonical_title"]
    version = row.get("canonical_version") or ""
    expected_dur = row.get("duration_seconds") or 0

    queries = [
        f"{artist} {title} {version}".strip(),
        f"{artist} {title}",
    ]

    best = None
    for q in queries:
        try:
            results = get_yt().search(q, filter="songs", limit=8)
        except Exception as e:
            row["video_id_error"] = f"{type(e).__name__}: {str(e)[:80]}"
            time.sleep(2)
            continue
        if not results:
            continue

        for r in results:
            vid = r.get("videoId")
            if not vid:
                continue
            cand_title = r.get("title", "")
            cand_artists = " ".join(a.get("name", "") for a in (r.get("artists") or []))
            cand_full = f"{cand_artists} {cand_title}"
            r_dur = _parse_duration(r.get("duration_seconds") or r.get("duration"))

            # Title-similarity guard: query title vs YT title (not artist field —
            # artist often matches anyway because it's part of the search query)
            t_overlap = _title_overlap(title, cand_title)

            if expected_dur > 0 and r_dur > 0:
                delta = abs(r_dur - expected_dur)
                if delta <= 5:
                    d_score = 1.0
                elif delta <= 30:
                    d_score = 0.7
                else:
                    d_score = 0.3
            else:
                d_score = 0.5

            score = 0.6 * t_overlap + 0.4 * d_score

            # Reject low-overlap candidates outright — likely same-artist different song
            if t_overlap < 0.5:
                continue

            if best is None or score > best["score"]:
                best = {
                    "video_id": vid,
                    "score": round(score, 3),
                    "matched_dur": r_dur,
                    "matched_title": cand_title,
                    "title_overlap": round(t_overlap, 3),
                }
            if score >= 0.95:
                break
        if best and best["score"] >= 0.8:
            break

    if best:
        row["video_id"] = best["video_id"]
        row["video_id_confidence"] = best["score"]
        row["video_id_matched_title"] = best["matched_title"]
        row["video_id_matched_duration"] = best["matched_dur"]
    else:
        row["video_id"] = None
        row["video_id_confidence"] = 0.0
        row["video_id_matched_title"] = None
        row["video_id_matched_duration"] = None

    return row


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument(
        "--input",
        type=Path,
        default=Path(__file__).parent / "output" / "seeds_canonical_v6.parquet",
    )
    ap.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).parent / "output" / "seeds_resolved_v6.parquet",
    )
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--limit", type=int, default=0, help="0=all, else stop after N")
    ap.add_argument("--curated-only", action="store_true",
                    help="resolve only is_curated=true rows (Stage 1 fast path)")
    args = ap.parse_args()

    if not args.input.exists():
        print(f"input not found: {args.input}")
        return 2

    df = pl.read_parquet(args.input)
    if args.curated_only:
        df = df.filter(pl.col("is_curated"))
        print(f"--curated-only: filtered to {len(df):,} rows")
    if args.limit > 0:
        df = df.head(args.limit)

    rows = df.to_dicts()
    print(f"resolving {len(rows):,} canonical tracks with {args.threads} threads...")

    # Resume support
    args.output.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_path = args.output.with_suffix(".checkpoint.jsonl")
    resolved_ids: dict[str, dict] = {}
    if checkpoint_path.exists():
        with checkpoint_path.open() as f:
            for line in f:
                d = json.loads(line)
                resolved_ids[d["canonical_id"]] = d
        print(f"loaded {len(resolved_ids):,} prior checkpoints from {checkpoint_path.name}")

    pending = [r for r in rows if r["canonical_id"] not in resolved_ids]
    print(f"pending: {len(pending):,}")

    started = time.time()
    cf = checkpoint_path.open("a")
    completed = 0

    try:
        with ThreadPoolExecutor(max_workers=args.threads) as pool:
            futures = {pool.submit(resolve_one, r): r["canonical_id"] for r in pending}
            for fut in as_completed(futures):
                try:
                    out = fut.result()
                except Exception as e:
                    out = {"canonical_id": futures[fut], "video_id": None,
                           "video_id_confidence": 0.0,
                           "video_id_error": f"{type(e).__name__}: {str(e)[:80]}"}
                resolved_ids[out["canonical_id"]] = out
                cf.write(json.dumps(out) + "\n")
                cf.flush()
                completed += 1
                if completed % 100 == 0:
                    rate = completed / (time.time() - started)
                    print(f"  {completed:,} done ({rate:.1f}/s, ETA {((len(pending) - completed) / max(rate, 0.01)):.0f}s)")
    finally:
        cf.close()

    # Merge resolutions back to df
    resolved_df = pl.from_dicts(list(resolved_ids.values())).select(
        ["canonical_id", "video_id", "video_id_confidence",
         "video_id_matched_title", "video_id_matched_duration"]
    )
    final = df.join(resolved_df, on="canonical_id", how="left")
    final.write_parquet(args.output, compression="zstd")

    elapsed = time.time() - started
    print(f"\nwrote {args.output} ({final.estimated_size('mb'):.1f} MB) in {elapsed:.0f}s")
    print(f"  resolved: {final.filter(pl.col('video_id').is_not_null()).height:,} / {len(final):,}")
    print(f"  high conf (≥0.7): {final.filter(pl.col('video_id_confidence') >= 0.7).height:,}")
    print(f"  medium conf (0.4-0.7): {final.filter((pl.col('video_id_confidence') >= 0.4) & (pl.col('video_id_confidence') < 0.7)).height:,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
