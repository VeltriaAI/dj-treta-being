"""K0 v2 step 3 — Wait for the v2 batch job and ingest its output into LanceDB.

Unlike 04_ingest_to_lancedb.py this joins on the `instance.metadata.mbid`
field that 05_find_missed_mbids.py embedded into every input row. No string
matching against content text — mbid (or sp:spotify_id fallback) comes back
verbatim.

Appends to the existing `tracks` table. Rebuilds the IVF-PQ index after
ingest because adding rows invalidates the previous one.
"""
from __future__ import annotations

import os

import json
import sys
import time
from pathlib import Path
from typing import Iterable

import lancedb
import pyarrow as pa
from google.cloud import aiplatform, storage

PROJECT = os.environ.get("DJTRETA_VERTEX_PROJECT") or sys.exit("DJTRETA_VERTEX_PROJECT required")
LOCATION = "us-central1"
BUCKET = os.environ.get("DJTRETA_GCS_BUCKET") or sys.exit("DJTRETA_GCS_BUCKET required")

LANCE_DIR = Path.home() / "Music" / "DJTreta" / "knowledge" / "lancedb"
LANCE_TABLE = "tracks"

TARGET_DIM = 384
BATCH_SIZE = 50_000
POLL_INTERVAL = 60  # seconds — long-running batch, keep polling relaxed

JOB_ID_FILE = Path(__file__).parent / ".job_id_v2"


def wait_for_job(resource_name: str) -> str:
    """Poll the single v2 job every POLL_INTERVAL seconds. Return its GCS
    output directory when SUCCEEDED; raise if FAILED/CANCELLED/EXPIRED.
    """
    aiplatform.init(project=PROJECT, location=LOCATION)
    start = time.time()
    last_state = ""
    while True:
        job = aiplatform.BatchPredictionJob(resource_name)
        state = str(job.state).split(".")[-1]
        if state != last_state:
            elapsed = int(time.time() - start)
            print(f"[k0.7] T+{elapsed}s {resource_name.split('/')[-1]}: {state}")
            last_state = state
        if job.state == aiplatform.compat.types.job_state.JobState.JOB_STATE_SUCCEEDED:
            oi = job.output_info
            if not oi.gcs_output_directory:
                raise RuntimeError(f"{resource_name}: SUCCEEDED but no gcs_output_directory")
            return oi.gcs_output_directory
        if job.state in (
            aiplatform.compat.types.job_state.JobState.JOB_STATE_FAILED,
            aiplatform.compat.types.job_state.JobState.JOB_STATE_CANCELLED,
            aiplatform.compat.types.job_state.JobState.JOB_STATE_EXPIRED,
        ):
            raise RuntimeError(f"{resource_name}: {state} — {job.error}")
        time.sleep(POLL_INTERVAL)


def stream_output(bucket, output_dir_gs: str) -> Iterable[tuple[str, list[float]]]:
    assert output_dir_gs.startswith(f"gs://{BUCKET}/"), output_dir_gs
    prefix = output_dir_gs[len(f"gs://{BUCKET}/"):]
    blobs = sorted(
        (b for b in bucket.list_blobs(prefix=prefix) if b.name.endswith(".jsonl")),
        key=lambda b: b.name,
    )
    print(f"[k0.7] found {len(blobs)} output shard(s) at {prefix}")
    for blob in blobs:
        print(f"[k0.7] streaming {blob.name}")
        with blob.open("r") as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                inst = obj.get("instance") or {}
                meta = inst.get("metadata") or {}
                track_id = meta.get("mbid")
                preds = obj.get("predictions") or []
                if not track_id or not preds:
                    continue
                emb = preds[0].get("embeddings") or {}
                values = emb.get("values") or []
                if not values:
                    continue
                yield track_id, values


def build_schema() -> pa.Schema:
    return pa.schema([
        pa.field("mbid", pa.string()),
        pa.field("vector", pa.list_(pa.float32(), TARGET_DIM)),
    ])


def main() -> int:
    if not JOB_ID_FILE.exists():
        print(f"[k0.7] no job id at {JOB_ID_FILE}; run 06_submit_v2_batch.py first")
        return 1
    resource_name = JOB_ID_FILE.read_text().strip()
    print(f"[k0.7] polling v2 job {resource_name}")
    output_dir = wait_for_job(resource_name)
    print(f"[k0.7] job SUCCEEDED; output dir: {output_dir}")

    storage_client = storage.Client(project=PROJECT)
    bucket = storage_client.bucket(BUCKET)

    db = lancedb.connect(str(LANCE_DIR))

    lt = db.list_tables()
    existing_tables = (
        lt if isinstance(lt, list)
        else list(lt.tables or []) if hasattr(lt, "tables")
        else list(lt)
    )
    if LANCE_TABLE not in existing_tables:
        print(f"[k0.7] table {LANCE_TABLE!r} not found — creating")
        tbl = db.create_table(LANCE_TABLE, schema=build_schema(), mode="create")
        existing_ids: set[str] = set()
    else:
        tbl = db.open_table(LANCE_TABLE)
        n = tbl.count_rows()
        print(f"[k0.7] existing table has {n:,} rows — loading ids for dedup")
        ids = tbl.to_lance().to_table(columns=["mbid"]).to_pydict()["mbid"]
        existing_ids = {x for x in ids if x is not None}
        print(f"[k0.7] loaded {len(existing_ids):,} existing ids")

    buffer: list[dict] = []
    inserted = 0
    skipped_dupe = 0
    start = time.time()

    for track_id, values in stream_output(bucket, output_dir):
        if track_id in existing_ids:
            skipped_dupe += 1
            continue
        buffer.append({"mbid": track_id, "vector": values[:TARGET_DIM]})
        existing_ids.add(track_id)  # prevent intra-batch dupe
        if len(buffer) >= BATCH_SIZE:
            tbl.add(buffer)
            inserted += len(buffer)
            elapsed = time.time() - start
            rate = inserted / max(elapsed, 1)
            print(
                f"[k0.7] inserted {inserted:,} rows "
                f"({rate:,.0f}/s, dupe_skipped={skipped_dupe}, elapsed={elapsed:.0f}s)"
            )
            buffer.clear()

    if buffer:
        tbl.add(buffer)
        inserted += len(buffer)

    final = tbl.count_rows()
    print(f"[k0.7] ingest done: inserted={inserted:,} dupe_skipped={skipped_dupe:,} final_rows={final:,}")

    print("[k0.7] rebuilding IVF-PQ index (replace=True)...")
    t0 = time.time()
    tbl.create_index(
        metric="cosine",
        num_partitions=512,
        num_sub_vectors=48,
        vector_column_name="vector",
        replace=True,
    )
    print(f"[k0.7] index rebuilt in {time.time() - t0:.0f}s")

    print(f"[k0.7] final row count: {final:,}")
    print(f"[k0.7] table: {LANCE_DIR}/{LANCE_TABLE}")

    # Smoke test
    print("[k0.7] running ANN smoke test...")
    try:
        sample = tbl.to_lance().to_table(columns=["vector"], limit=1).to_pydict()["vector"][0]
        t0 = time.time()
        results = tbl.search(sample).limit(5).to_list()
        latency_ms = (time.time() - t0) * 1000
        print(f"[k0.7] smoke: {len(results)} results in {latency_ms:.1f}ms")
    except Exception as e:
        print(f"[k0.7] smoke test failed (non-fatal): {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
