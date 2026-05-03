"""Pull electronic-genre master releases from Discogs API.

Discogs is the most authoritative open electronic music catalog (CC0).
Free API, 60 req/min unauthenticated. Each `/database/search` page returns
up to 100 master releases. We iterate over (style, year-range) pairs and
paginate within each.

Discogs returns RELEASES not TRACKS — each master is typically a single,
EP, or album. For singles/EPs the master title ≈ track title; for albums
we get a release-level seed that we'll need to drill into for tracklists
(deferred to a later step). For now we treat each master as one seed row
and let the canonicalize step later merge with Beatport's track-level rows.

Output: appends to seeds_raw_v6.parquet (alongside Beatport rows) with
source='discogs'. Set --output to a separate file if you want them split.

Auth: optional. Set DISCOGS_TOKEN env for higher rate limits.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).parent))
from lib.http import make_session

DISCOGS_BASE = "https://api.discogs.com"

# Discogs Electronic styles — verified by browsing their genre tree.
# Ordered by DJ-relevance. Each is searched independently.
STYLES = [
    "Techno",
    "House",
    "Tech House",
    "Deep House",
    "Trance",
    "Progressive House",
    "Drum n Bass",
    "Dubstep",
    "Electro",
    "Minimal",
    "Acid House",
    "Acid",
    "Hardcore",
    "Hard Trance",
    "Hardstyle",
    "Breakbeat",
    "Breaks",
    "Garage House",
    "UK Garage",
    "Tribal House",
    "Funky House",
    "Italo-Disco",
    "Disco",
    "Synthwave",
    "Future Garage",
]

CACHE_DIR = Path(__file__).parent / "raw_html" / "discogs"
OUTPUT = Path(__file__).parent / "output" / "seeds_raw_discogs.parquet"


def fetch_page(session, style: str, page: int, year_from: int, year_to: int, token: str | None) -> dict:
    params = {
        "genre": "Electronic",
        "style": style,
        "year": f"{year_from}-{year_to}",
        "type": "master",
        "per_page": 100,
        "page": page,
    }
    if token:
        params["token"] = token

    headers = {"User-Agent": "DJ-Treta/1.0 +https://dj.treta.life"}
    # Politeness — 1.1s between requests = 54 req/min, under the 60/min cap
    time.sleep(1.1)

    r = session.get(f"{DISCOGS_BASE}/database/search", params=params, headers=headers, timeout=30)
    r.raise_for_status()
    return r.json()


def normalize_row(m: dict, style: str, fetched_at: str) -> dict:
    """Discogs master result → seeds_raw schema."""
    title_full = m.get("title", "")
    artist = ""
    title = title_full
    if " - " in title_full:
        # Convention: "Artist - Title"
        artist, title = title_full.split(" - ", 1)
        artist = artist.strip()
        title = title.strip()

    return {
        "source": "discogs",
        "source_id": str(m.get("id", "")),
        "artist": artist,
        "title": title,
        "remix_artist": None,
        "version": None,
        "label": " / ".join(m.get("label") or []),
        "release_date": str(m.get("year", "")) if m.get("year") else None,
        "genre": " / ".join(m.get("genre") or []),
        "subgenre": " / ".join(m.get("style") or []),
        "bpm": None,
        "key_text": None,
        "key_camelot": None,
        "duration_seconds": None,
        "isrc": None,
        "youtube_id": None,
        "spotify_id": None,
        "soundcloud_url": None,
        "beatport_url": None,
        "beatport_release_id": None,
        "release_name": title_full,
        "chart_genre_slug": style.lower().replace(" ", "-"),
        "chart_genre_id": -1,
        "source_endpoint": "discogs-master",
        "fetched_at": fetched_at,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--year-from", type=int, default=2010)
    ap.add_argument("--year-to", type=int, default=2026)
    ap.add_argument("--max-pages", type=int, default=20,
                    help="cap per style per year-range (Discogs returns up to 10K results = 100 pages)")
    ap.add_argument("--styles", type=str, default=None,
                    help="comma-sep subset for testing")
    ap.add_argument("--output", type=Path, default=OUTPUT)
    args = ap.parse_args()

    styles = STYLES
    if args.styles:
        wanted = set(args.styles.split(","))
        styles = [s for s in STYLES if s in wanted]
        if not styles:
            print(f"no matching styles in: {args.styles}")
            return 2

    args.output.parent.mkdir(parents=True, exist_ok=True)
    session = make_session()
    token = os.environ.get("DISCOGS_TOKEN")
    fetched_at = datetime.now(timezone.utc).isoformat()
    seen_ids: set[str] = set()
    all_rows: list[dict] = []
    started = time.time()

    for style in styles:
        print(f"\n=== {style} ({args.year_from}-{args.year_to}) ===")
        new_for_style = 0
        for page in range(1, args.max_pages + 1):
            try:
                data = fetch_page(session, style, page, args.year_from, args.year_to, token)
            except Exception as e:
                print(f"  page {page}: failed ({type(e).__name__}: {str(e)[:120]})")
                continue

            results = data.get("results", [])
            if not results:
                print(f"  page {page}: no results, stopping style")
                break

            new = 0
            for m in results:
                mid = str(m.get("id", ""))
                if not mid or mid in seen_ids:
                    continue
                seen_ids.add(mid)
                try:
                    all_rows.append(normalize_row(m, style, fetched_at))
                    new += 1
                except Exception as e:
                    pass
            new_for_style += new
            print(f"  page {page}: +{new} new (style total={new_for_style}, grand={len(all_rows)})")

            pages_total = data.get("pagination", {}).get("pages", 1)
            if page >= pages_total:
                break

    elapsed = time.time() - started
    print(f"\n=== TOTAL: {len(all_rows):,} discogs masters in {elapsed:.0f}s ===")

    if not all_rows:
        print("nothing to write")
        return 1

    df = pl.DataFrame(all_rows)
    df.write_parquet(args.output, compression="zstd")
    print(f"wrote {args.output} ({df.estimated_size('mb'):.1f} MB)")
    print(f"  unique master_ids: {df['source_id'].n_unique():,}")
    print(f"  with year: {df.filter(pl.col('release_date').is_not_null()).height:,}")
    print(f"  with artist: {df.filter(pl.col('artist') != '').height:,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
