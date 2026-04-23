"""K0 step 1 — Download v3 parquet from HuggingFace to local cache.

Target: ~/Music/DJTreta/knowledge/dj_treta_library.parquet (354 MB)
Source: NaturNestAI/electronic-music-knowledge/v3/dj_treta_library.parquet

Idempotent: skips if already present and non-empty.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from huggingface_hub import hf_hub_download

TARGET_DIR = Path.home() / "Music" / "DJTreta" / "knowledge"
PARQUET_NAME = "dj_treta_library.parquet"
REPO_ID = "NaturNestAI/electronic-music-knowledge"
REPO_FILE = "v3/dj_treta_library.parquet"

# From handoff — read works anon but token avoids throttle.
HF_TOKEN = os.environ.get("HF_TOKEN") or "HF_TOKEN_REMOVED_FROM_HISTORY"


def main() -> int:
    TARGET_DIR.mkdir(parents=True, exist_ok=True)
    dest = TARGET_DIR / PARQUET_NAME
    if dest.exists() and dest.stat().st_size > 100_000_000:
        print(f"[k0.1] already present: {dest} ({dest.stat().st_size / 1e6:.1f} MB)")
        return 0

    print(f"[k0.1] downloading {REPO_ID}/{REPO_FILE} → {dest}")
    local_path = hf_hub_download(
        repo_id=REPO_ID,
        filename=REPO_FILE,
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
    print(f"[k0.1] done: {dest} ({size_mb:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
