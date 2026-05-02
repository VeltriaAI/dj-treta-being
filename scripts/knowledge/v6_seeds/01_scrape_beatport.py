"""Scrape Beatport top-tracks per electronic genre, output curated seed parquet.

Pivoted away from 1001Tracklists in Stage 1 (Cloudflare Turnstile blocks plain
HTTP). Beatport's `__NEXT_DATA__` script tag exposes a full-fidelity JSON of
each chart page with BPM, key (already in Camelot!), genre, ISRC, label,
release date, artists, length_ms, slug — best-in-class metadata per track.

Strategy:
  For each electronic genre slug, paginate /genre/{slug}/{id}/tracks?per_page=100
  Extract __NEXT_DATA__ JSON, walk to .props.pageProps.dehydratedState.queries[*]
    .state.data.results, append rows to seeds_raw_v6.parquet with source='beatport'.

Politeness:
  - 1 req every ~2.5s + jitter (lib/http.py)
  - Cache raw HTML so parser changes don't re-fetch
  - Max 50 pages per genre (=5K tracks) for Stage 1 — adjust via PAGES_PER_GENRE

Output: ~12 genres × 50 pages × 100 tracks = ~60K raw rows (canonical-dedup
later in 10_canonicalize.py).

Beatport TOS technically prohibits scraping. Mitigations: conservative rate,
UA rotation, save raw HTML for replay. If we hit blocks, switch to RSS-only
or proxy. Documented as a known risk in the v6 plan.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Make lib importable when run as a script
sys.path.insert(0, str(Path(__file__).parent))

import polars as pl
from lib.http import fetch, make_session

# Beatport's primary electronic genres + their internal IDs
# Verified by browsing https://www.beatport.com/genres in May 2026
GENRES: list[tuple[str, int]] = [
    ("melodic-house-techno", 90),
    ("techno-peak-time-driving", 6),
    ("techno-raw-deep-hypnotic", 92),
    ("house", 5),
    ("tech-house", 11),
    ("deep-house", 12),
    ("progressive-house", 15),
    ("trance-main-floor", 7),
    ("psy-trance", 13),
    ("drum-bass", 1),
    ("dubstep", 18),
    ("electronica", 3),
    ("bass-house", 91),
    ("organic-house-downtempo", 93),
    ("indie-dance", 37),
]

CACHE_DIR = Path(__file__).parent / "raw_html" / "beatport"
OUTPUT = Path(__file__).parent / "output" / "seeds_raw_beatport.parquet"


def parse_next_data(html: str) -> list[dict]:
    """Extract track results array from Beatport's Next.js hydration script."""
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.+?)</script>', html, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return []

    # The track list lives under .props.pageProps.dehydratedState.queries[*]
    # .state.data.results — pick the first 'results' array whose elements have
    # 'bpm' (filters out side queries like featured artists, etc.)
    queries = (
        data.get("props", {})
        .get("pageProps", {})
        .get("dehydratedState", {})
        .get("queries", [])
    )
    for q in queries:
        results = q.get("state", {}).get("data", {}).get("results")
        if isinstance(results, list) and results and isinstance(results[0], dict) and "bpm" in results[0]:
            return results
    return []


def normalize_row(t: dict, genre_slug: str, genre_id: int, fetched_at: str, source_endpoint: str = "tracks") -> dict:
    """Beatport track dict -> seeds_raw schema."""
    artists = t.get("artists") or []
    remixers = t.get("remixers") or []
    key = t.get("key") or {}
    chord = (key.get("chord_type") or {}).get("name", "") if key else ""
    camelot_letter = key.get("camelot_letter", "") if key else ""
    camelot_number = key.get("camelot_number", 0) if key else 0
    key_camelot = f"{camelot_number}{camelot_letter}" if (camelot_number and camelot_letter) else None

    release = t.get("release") or {}
    sub_genre = t.get("sub_genre") or {}

    return {
        "source": "beatport",
        "source_id": str(t.get("id", "")),
        "artist": " & ".join(a.get("name", "") for a in artists if a.get("name")),
        "title": t.get("name", ""),
        "remix_artist": " & ".join(r.get("name", "") for r in remixers if r.get("name")) or None,
        "version": t.get("mix_name") or None,
        "label": (release.get("label") or {}).get("name") if isinstance(release.get("label"), dict) else None,
        "release_date": t.get("new_release_date"),
        "genre": (t.get("genre") or {}).get("name") or genre_slug,
        "subgenre": sub_genre.get("name") if isinstance(sub_genre, dict) else None,
        "bpm": float(t["bpm"]) if t.get("bpm") else None,
        "key_text": f"{camelot_number}{camelot_letter} {chord}".strip() or None,
        "key_camelot": key_camelot,
        "duration_seconds": (t.get("length_ms") or 0) // 1000 or None,
        "isrc": t.get("isrc"),
        "youtube_id": None,
        "spotify_id": None,
        "soundcloud_url": None,
        "beatport_url": f"https://www.beatport.com/track/{t.get('slug', '')}/{t.get('id', '')}",
        "beatport_release_id": str((release.get("id") or "")),
        "release_name": release.get("name"),
        "chart_genre_slug": genre_slug,
        "chart_genre_id": genre_id,
        "source_endpoint": source_endpoint,
        "fetched_at": fetched_at,
    }


def scrape_genre(session, genre_slug: str, genre_id: int, pages: int, delay_s: float, seen_ids: set[str]) -> list[dict]:
    """Fetch /top-100 (curated chart-toppers) + /tracks pagination (historical breadth).

    `seen_ids` is mutated across genres — same track can chart in multiple genres.
    """
    fetched_at = datetime.now(timezone.utc).isoformat()
    out: list[dict] = []

    urls = [f"https://www.beatport.com/genre/{genre_slug}/{genre_id}/top-100"]
    for page in range(1, pages + 1):
        urls.append(
            f"https://www.beatport.com/genre/{genre_slug}/{genre_id}/tracks"
            f"?per_page=100&page={page}"
        )

    for url in urls:
        is_top = url.endswith("/top-100")
        kind = "top-100" if is_top else f"p{url.split('page=')[-1]}"
        endpoint_tag = "top-100" if is_top else "tracks"
        try:
            html = fetch(session, url, cache_dir=CACHE_DIR, min_delay_s=delay_s)
        except Exception as e:
            print(f"  [{genre_slug} {kind}] fetch failed: {type(e).__name__}: {str(e)[:120]}")
            continue
        tracks = parse_next_data(html)
        if not tracks:
            print(f"  [{genre_slug} {kind}] no tracks parsed — stopping pagination")
            if "tracks?per_page" in url:
                break
            continue
        new = 0
        for t in tracks:
            tid = str(t.get("id", ""))
            if tid in seen_ids:
                continue
            seen_ids.add(tid)
            try:
                out.append(normalize_row(t, genre_slug, genre_id, fetched_at, endpoint_tag))
                new += 1
            except Exception as e:
                print(f"  [{genre_slug} {kind}] row parse failed: {e}")
        print(f"  [{genre_slug} {kind}] +{new} new (total={len(out)})")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--pages-per-genre", type=int, default=50, help="default 50 (=5K/genre)")
    ap.add_argument("--delay", type=float, default=2.5, help="min seconds between requests")
    ap.add_argument("--genres", type=str, default=None, help="comma-sep slug subset for testing")
    ap.add_argument("--output", type=Path, default=OUTPUT)
    args = ap.parse_args()

    genres = GENRES
    if args.genres:
        wanted = set(args.genres.split(","))
        genres = [(s, i) for (s, i) in GENRES if s in wanted]
        if not genres:
            print(f"no matching slugs in: {args.genres}")
            return 2

    args.output.parent.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    session = make_session()
    all_rows: list[dict] = []
    seen_ids: set[str] = set()
    started = time.time()

    for slug, gid in genres:
        print(f"\n=== {slug} (id={gid}) ===")
        rows = scrape_genre(session, slug, gid, args.pages_per_genre, args.delay, seen_ids)
        all_rows.extend(rows)
        print(f"  genre done: +{len(rows)} new (cumulative: {len(all_rows)})")

    elapsed = time.time() - started
    print(f"\n=== TOTAL: {len(all_rows)} raw rows in {elapsed:.0f}s ===")

    if not all_rows:
        print("no rows scraped, not writing parquet")
        return 1

    df = pl.DataFrame(all_rows)
    df.write_parquet(args.output, compression="zstd")
    print(f"wrote {args.output} ({df.estimated_size('mb'):.1f} MB)")
    print(f"  unique titles: {df.select(pl.col('title').n_unique()).item():,}")
    print(f"  unique artists: {df.select(pl.col('artist').n_unique()).item():,}")
    print(f"  bpm populated: {df.filter(pl.col('bpm').is_not_null()).height:,}")
    print(f"  key_camelot populated: {df.filter(pl.col('key_camelot').is_not_null()).height:,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
