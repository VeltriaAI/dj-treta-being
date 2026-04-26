"""K0 step 8 — Export LanceDB embeddings + parquet metadata to a single v4 parquet.

Joins the 384-dim text embeddings (text-embedding-005 Matryoshka) from LanceDB
onto the v3 metadata parquet, inner-joining on `mbid`. Output is a single
self-contained parquet that downstream installs can `hf_hub_download` and use
directly — no Vertex AI batch generation required.

Designed to run on the VM where both source files live:
    parquet:   /mnt/data/library/DJTreta/knowledge/dj_treta_library.parquet
    lancedb:   /mnt/data/library/DJTreta/knowledge/lancedb/tracks.lance/

Output:        /mnt/data/library/DJTreta/knowledge/dj_treta_library_v4.parquet

Memory profile: 16 GB VM hits OOM if we hold the full lance arrow table
(~4.5 GB) AND build a python dict over it AND maintain pyarrow staging
buffers. This implementation pre-materializes lance into a *single sorted
arrow.Table on disk* by using the underlying lance Dataset, then performs the
join via a single pass: stream the metadata parquet in batches, for each
batch fetch the matching vector subset directly from the lance dataset using
a row-id filter. No full-table materialization in memory.

Peak RSS ~3 GB.
"""
from __future__ import annotations

import gc
import sys
import time
from pathlib import Path

import lancedb
import pyarrow as pa
import pyarrow.parquet as pq

PARQUET_IN = Path("/mnt/data/library/DJTreta/knowledge/dj_treta_library.parquet")
LANCE_DIR = Path("/mnt/data/library/DJTreta/knowledge/lancedb")
LANCE_TABLE = "tracks"
PARQUET_OUT = Path("/mnt/data/library/DJTreta/knowledge/dj_treta_library_v4.parquet")

META_BATCH_SIZE = 50_000  # rows per metadata read; matches output row-group
ROW_GROUP_SIZE = 50_000


def main() -> int:
    print(f"[k0.8] reading LanceDB at {LANCE_DIR}")
    db = lancedb.connect(str(LANCE_DIR))
    tbl = db.open_table(LANCE_TABLE)
    n_lance = tbl.count_rows()
    print(f"[k0.8] LanceDB rows: {n_lance:,}")
    print(f"[k0.8] LanceDB schema:\n{tbl.schema}")

    lance_ds = tbl.to_lance()  # underlying lance.Dataset

    # Step 1: pull only the mbid column -> arrow array. ~36 bytes/row =
    # ~110 MB for 2.94M rows.
    print("[k0.8] reading lance mbid column...")
    t0 = time.time()
    mbid_only = lance_ds.to_table(columns=["mbid"])
    print(
        f"[k0.8] lance mbid: {mbid_only.num_rows:,} rows "
        f"(~{mbid_only.nbytes / 1e6:.0f} MB, {time.time() - t0:.0f}s)"
    )

    # Build mbid -> row index dict. ~240 MB python overhead.
    print("[k0.8] indexing mbid -> row position...")
    t0 = time.time()
    mbid_to_row: dict[str, int] = {}
    pos = 0
    for chunk in mbid_only.column("mbid").chunks:
        for m in chunk.to_pylist():
            if m is not None:
                mbid_to_row[m] = pos
            pos += 1
    print(f"[k0.8] indexed {len(mbid_to_row):,} mbids ({time.time() - t0:.0f}s)")
    del mbid_only
    gc.collect()

    # Step 2: get parquet schema and infer joined output schema.
    print(f"[k0.8] opening source parquet at {PARQUET_IN}")
    pq_file = pq.ParquetFile(str(PARQUET_IN))
    n_meta = pq_file.metadata.num_rows
    print(f"[k0.8] source parquet rows: {n_meta:,}")

    meta_schema = pq_file.schema_arrow

    # Get the vector field type (fixed_size_list<float32, 384>) by sampling.
    sample_vec_tbl = lance_ds.to_table(columns=["vector"], limit=1)
    vec_field = pa.field("vector", sample_vec_tbl.column("vector").type)
    del sample_vec_tbl
    out_schema = pa.schema(list(meta_schema) + [vec_field])
    print(f"[k0.8] output schema:\n{out_schema}")

    PARQUET_OUT.parent.mkdir(parents=True, exist_ok=True)
    if PARQUET_OUT.exists():
        PARQUET_OUT.unlink()

    writer = pq.ParquetWriter(
        str(PARQUET_OUT),
        out_schema,
        compression="snappy",
    )

    total_in = 0
    total_out = 0
    start = time.time()
    try:
        for batch in pq_file.iter_batches(batch_size=META_BATCH_SIZE):
            mbids = batch.column("mbid").to_pylist()
            keep_idxs: list[int] = []
            vec_rows: list[int] = []
            for i, m in enumerate(mbids):
                row = mbid_to_row.get(m) if m else None
                if row is not None:
                    keep_idxs.append(i)
                    vec_rows.append(row)
            total_in += batch.num_rows
            if not keep_idxs:
                continue

            # Slice metadata batch to matched rows
            keep_idx_arr = pa.array(keep_idxs, type=pa.int64())
            sliced_meta = batch.take(keep_idx_arr)

            # Fetch only the matching vectors from lance via row-index take.
            vec_subtbl = lance_ds.take(vec_rows, columns=["vector"])
            # Both sliced_meta (RecordBatch) and vec_subtbl (Table) need to
            # be merged into a single Table that pyarrow.ParquetWriter can
            # consume. Convert the metadata batch to a Table and append the
            # vector column.
            meta_tbl = pa.Table.from_batches([sliced_meta])
            joined_tbl = meta_tbl.append_column("vector", vec_subtbl.column("vector"))
            # Ensure column order matches the writer schema.
            joined_tbl = joined_tbl.select(out_schema.names)
            writer.write_table(joined_tbl, row_group_size=ROW_GROUP_SIZE)
            total_out += joined_tbl.num_rows

            del vec_subtbl, sliced_meta, meta_tbl, joined_tbl

            if total_in % (META_BATCH_SIZE * 10) == 0:
                elapsed = time.time() - start
                rate = total_in / max(elapsed, 1)
                pct = 100 * total_in / n_meta
                print(
                    f"[k0.8] scanned {total_in:,}/{n_meta:,} ({pct:.1f}%) "
                    f"matched {total_out:,} @ {rate:,.0f} rows/s "
                    f"(elapsed {elapsed:.0f}s)"
                )
    finally:
        writer.close()
        del mbid_to_row
        gc.collect()

    size_gb = PARQUET_OUT.stat().st_size / 1e9
    print(f"[k0.8] scanned {total_in:,} metadata rows; joined {total_out:,}")
    print(f"[k0.8] wrote {PARQUET_OUT} = {size_gb:.2f} GB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
