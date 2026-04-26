"""K0 step 9 — Upload v4 parquet (metadata + embeddings) to HuggingFace.

Pushes `dj_treta_library_v4.parquet` to
`NaturNestAI/electronic-music-knowledge` under `v4/`, alongside a small
README explaining what changed vs v3.

Run after 08_export_hf_v4.py finishes on the VM.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from huggingface_hub import HfApi

REPO_ID = "NaturNestAI/electronic-music-knowledge"
LOCAL_FILE = Path("/mnt/data/library/DJTreta/knowledge/dj_treta_library_v4.parquet")
PATH_IN_REPO = "v4/dj_treta_library.parquet"

HF_TOKEN = os.environ.get("HF_TOKEN")
if not HF_TOKEN:
    raise SystemExit(
        "HF_TOKEN env var required (write-capable). Set it in .env or export. "
        "Get a token at https://huggingface.co/settings/tokens"
    )

README = b"""# v4 \xe2\x80\x94 Metadata + Text Embeddings

Same 3.5M-track corpus as v3, with 384-dim text embeddings inner-joined on
`mbid`. ~2.94M rows after the join (rows without an embedding text were
dropped during Vertex batch generation).

## Schema additions vs v3

- `vector`: `list<float32, 384>` \xe2\x80\x94 Matryoshka-truncated
  `text-embedding-005` output. Source text:
  `f"{artist_name} - {title} ({year}) [{album}]"`.

All other columns are unchanged from v3 (`mbid`, `title`, `artist_name`,
`duration_ms`, `year`, `source`, `spotify_id`, `tempo`, `key`, `mode`,
`danceability`, `energy`, `valence`, `youtube_url`, `youtube_title`,
`youtube_channel`, `dvi_styles`, `dvi_labels`, `discogs_master_id`,
`video_id`, `youtube_music_url`, `yt_matched_*`).

## Use

```python
import polars as pl
from huggingface_hub import hf_hub_download

p = hf_hub_download(
    repo_id="NaturNestAI/electronic-music-knowledge",
    filename="v4/dj_treta_library.parquet",
    repo_type="dataset",
)
df = pl.read_parquet(p)
```

## ANN with LanceDB (one-time index build)

```python
import lancedb
db = lancedb.connect("./lancedb")
tbl = db.create_table("tracks", data=df.to_arrow())
tbl.create_index(
    metric="cosine",
    num_partitions=512,
    num_sub_vectors=48,
    vector_column_name="vector",
)
```

## Reproducibility

A fresh DJ Treta install no longer needs Vertex AI batch generation \xe2\x80\x94
the parquet download is sufficient to bring up both the metadata scan and
the vector index.
"""


def main() -> int:
    if not LOCAL_FILE.exists():
        print(f"[k0.9] missing {LOCAL_FILE}; run 08_export_hf_v4.py first")
        return 1

    size_gb = LOCAL_FILE.stat().st_size / 1e9
    print(f"[k0.9] uploading {LOCAL_FILE} ({size_gb:.2f} GB) -> {REPO_ID}/{PATH_IN_REPO}")

    api = HfApi(token=HF_TOKEN)
    api.upload_file(
        path_or_fileobj=str(LOCAL_FILE),
        path_in_repo=PATH_IN_REPO,
        repo_id=REPO_ID,
        repo_type="dataset",
        commit_message=(
            "feat: v4 - metadata + 384-dim text embeddings "
            "(text-embedding-005, Matryoshka 384)"
        ),
    )
    print(f"[k0.9] parquet uploaded -> https://huggingface.co/datasets/{REPO_ID}/resolve/main/{PATH_IN_REPO}")

    print("[k0.9] uploading v4/README.md")
    api.upload_file(
        path_or_fileobj=README,
        path_in_repo="v4/README.md",
        repo_id=REPO_ID,
        repo_type="dataset",
        commit_message="docs: v4 README",
    )
    print(f"[k0.9] README uploaded -> https://huggingface.co/datasets/{REPO_ID}/blob/main/v4/README.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
