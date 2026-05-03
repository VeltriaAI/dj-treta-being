"""Generate 384-dim text embeddings for each canonical track via Vertex AI
text-embedding-005, add as `vector` column. Drop-in compatible with v3/v4
LanceDB index.

Embedding text: "{artist} {title} {version} {genre} {label}" — same shape
as v4's embedding input (per scripts/knowledge/02_export_embedding_input.py).

Run mode: online prediction (sync, batched 250 per request — model max).
For ~1,300 rows this is ~6 batches × ~2-3s each = under a minute.

Cost: ~$0.025 per 1K input chars × ~80 chars/track × 1296 ≈ $2.60.
GCP-billed (project fandorab2w3) — covered by startup credits if attached.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import polars as pl

# Same model as v4 (per memory: text-embedding-005 Matryoshka, 384-dim)
MODEL = "text-embedding-005"
PROJECT = "fandorab2w3"
LOCATION = "us-central1"
TASK_TYPE = "RETRIEVAL_DOCUMENT"
OUTPUT_DIM = 384  # Matryoshka — model produces 768 by default, we slice/request 384


def build_text(row: dict) -> str:
    """Embedding input text. Same shape as v4 (artist + title + genre + label)."""
    parts = [
        row.get("artist_name") or "",
        row.get("title") or "",
        row.get("canonical_version") if row.get("canonical_version") else "",
        row.get("genre") or "",
        row.get("label") or "",
    ]
    return " ".join(p for p in parts if p).strip()[:1000]


def embed_batch(texts: list[str]) -> list[list[float]]:
    """Call Vertex AI text-embedding-005, return one 384-dim vector per text."""
    from vertexai.language_models import TextEmbeddingModel, TextEmbeddingInput
    model = TextEmbeddingModel.from_pretrained(MODEL)
    inputs = [TextEmbeddingInput(text=t, task_type=TASK_TYPE) for t in texts]
    embeddings = model.get_embeddings(inputs, output_dimensionality=OUTPUT_DIM)
    return [list(e.values) for e in embeddings]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--input", type=Path, required=True,
                    help="merged v6 parquet (with artist_name, title, etc.)")
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--batch-size", type=int, default=250,
                    help="text-embedding-005 max is 250 inputs per request")
    args = ap.parse_args()

    if not args.input.exists():
        print(f"input not found: {args.input}")
        return 2

    import vertexai
    vertexai.init(project=PROJECT, location=LOCATION)

    df = pl.read_parquet(args.input)
    print(f"input: {len(df):,} rows, {len(df.columns)} cols")

    rows = df.to_dicts()
    texts = [build_text(r) for r in rows]
    print(f"sample texts:")
    for t in texts[:3]:
        print(f"  {t[:120]}")

    print(f"\nembedding {len(texts):,} rows, batch={args.batch_size}...")
    started = time.time()
    vectors: list[list[float]] = []
    for i in range(0, len(texts), args.batch_size):
        chunk = texts[i : i + args.batch_size]
        try:
            vecs = embed_batch(chunk)
            vectors.extend(vecs)
        except Exception as e:
            print(f"  batch {i}: failed ({type(e).__name__}: {str(e)[:120]})")
            # fill with zeros so row indices stay aligned
            vectors.extend([[0.0] * OUTPUT_DIM] * len(chunk))
        elapsed = time.time() - started
        rate = (i + len(chunk)) / max(elapsed, 0.01)
        eta = (len(texts) - i - len(chunk)) / max(rate, 0.01)
        print(f"  {i + len(chunk):,} / {len(texts):,} ({rate:.0f}/s, eta {eta:.0f}s)")

    print(f"\ngenerated {len(vectors):,} vectors in {time.time() - started:.0f}s")
    print(f"  dims: {len(vectors[0]) if vectors else 0}")

    # Add as `vector` column (matches v4 schema)
    out = df.with_columns(pl.Series("vector", vectors, dtype=pl.List(pl.Float32)))

    args.output.parent.mkdir(parents=True, exist_ok=True)
    out.write_parquet(args.output, compression="zstd")
    print(f"\nwrote {args.output} ({out.estimated_size('mb'):.1f} MB)")
    print(f"  vector column populated: {out.filter(pl.col('vector').is_not_null()).height:,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
