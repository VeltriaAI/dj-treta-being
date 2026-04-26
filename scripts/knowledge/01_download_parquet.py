"""K0 step 1 — Download knowledge parquet from HuggingFace to local cache.

Target: ~/Music/DJTreta/knowledge/dj_treta_library.parquet
Source: NaturNestAI/electronic-music-knowledge

Default = v4 (~5 GB; 2.94M rows, metadata + 384-dim text embeddings). The
client auto-builds the LanceDB index from the parquet's `vector` column on
first load, so no Vertex AI batch generation is required for fresh installs.

Falls back to v3 (~354 MB; 3.5M rows, metadata only) when V3_FALLBACK=1 in
the environment, useful for low-disk or metadata-only clients.

Idempotent: skips if already present and at least the v3 minimum size.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from huggingface_hub import hf_hub_download

TARGET_DIR = Path.home() / "Music" / "DJTreta" / "knowledge"
PARQUET_NAME = "dj_treta_library.parquet"
REPO_ID = "NaturNestAI/electronic-music-knowledge"
REPO_FILE_V4 = "v4/dj_treta_library.parquet"
REPO_FILE_V3 = "v3/dj_treta_library.parquet"

# v3 is ~354 MB, v4 is ~5 GB. Use v3's lower bound as the "non-empty" check.
MIN_VALID_BYTES = 100_000_000

HF_TOKEN = os.environ.get("HF_TOKEN")
if not HF_TOKEN:
    raise SystemExit(
        "HF_TOKEN env var required. Set it in .env or export before running. "
        "Get a token at https://huggingface.co/settings/tokens"
    )


def main() -> int:
    TARGET_DIR.mkdir(parents=True, exist_ok=True)
    dest = TARGET_DIR / PARQUET_NAME
    if dest.exists() and dest.stat().st_size > MIN_VALID_BYTES:
        print(f"[k0.1] already present: {dest} ({dest.stat().st_size / 1e6:.1f} MB)")
        return 0

    use_v3 = os.environ.get("V3_FALLBACK") == "1"
    repo_file = REPO_FILE_V3 if use_v3 else REPO_FILE_V4
    label = "v3" if use_v3 else "v4"
    print(f"[k0.1] downloading {REPO_ID}/{repo_file} ({label}) -> {dest}")
    local_path = hf_hub_download(
        repo_id=REPO_ID,
        filename=repo_file,
        repo_type="dataset",
        local_dir=TARGET_DIR,
        token=HF_TOKEN,
    )
    src = Path(local_path)
    if src.resolve() != dest.resolve():
        # hf_hub_download writes to the nested path inside local_dir — collapse.
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            dest.unlink()
        os.replace(src, dest)

    size_mb = dest.stat().st_size / 1e6
    print(f"[k0.1] done: {dest} ({size_mb:.1f} MB, {label})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
