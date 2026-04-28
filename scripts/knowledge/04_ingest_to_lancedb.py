"""K0 step 4 — Wait for all batch jobs then stream outputs into LanceDB.

Reads `scripts/knowledge/.job_ids` (one resource name per line). Polls each
every 30s until all complete. Streams predictions from every SUCCEEDED job's
output directory, matching to mbid via the index JSONL shards.

Writes LanceDB table `tracks` at ~/Music/DJTreta/knowledge/lancedb/ with schema:
    { mbid: string, vector: list<float32, 384> }

Vectors are Matryoshka-truncated from 768 → 384 dims. After ingestion, builds
an IVF-PQ index.
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
INDEX_PREFIX = "embeddings/index/"

LANCE_DIR = Path.home() / "Music" / "DJTreta" / "knowledge" / "lancedb"
LANCE_TABLE = "tracks"

TARGET_DIM = 384
BATCH_SIZE = 50_000

JOB_IDS_FILE = Path(__file__).parent / ".job_ids"


def wait_for_jobs(resource_names: list[str]) -> list[str]:
    """Poll every 30s. Return GCS output dirs of SUCCEEDED jobs.

    Raises RuntimeError if any job FAILED (so we don't silently ingest a
    partial dataset).
    """
    aiplatform.init(project=PROJECT, location=LOCATION)
    output_dirs: list[str] = []
    start = time.time()
    last_states: dict[str, str] = {}

    pending = set(resource_names)
    while pending:
        for rn in list(pending):
            job = aiplatform.BatchPredictionJob(rn)
            state = str(job.state).split(".")[-1]
            if state != last_states.get(rn):
                elapsed = int(time.time() - start)
                print(f"[k0.4] T+{elapsed}s {rn.split('/')[-1]}: {state}")
                last_states[rn] = state
            if job.state == aiplatform.compat.types.job_state.JobState.JOB_STATE_SUCCEEDED:
                oi = job.output_info
                if not oi.gcs_output_directory:
                    raise RuntimeError(
                        f"{rn}: SUCCEEDED but no gcs_output_directory"
                    )
                output_dirs.append(oi.gcs_output_directory)
                pending.discard(rn)
            elif job.state in (
                aiplatform.compat.types.job_state.JobState.JOB_STATE_FAILED,
                aiplatform.compat.types.job_state.JobState.JOB_STATE_CANCELLED,
                aiplatform.compat.types.job_state.JobState.JOB_STATE_EXPIRED,
            ):
                raise RuntimeError(f"{rn}: {state} — {job.error}")
        if pending:
            time.sleep(30)

    return output_dirs


def load_content_to_mbid(bucket) -> dict[str, str]:
    print(f"[k0.4] loading index from gs://{BUCKET}/{INDEX_PREFIX}")
    mapping: dict[str, str] = {}
    blobs = sorted(bucket.list_blobs(prefix=INDEX_PREFIX), key=lambda b: b.name)
    for blob in blobs:
        with blob.open("r") as f:
            for line in f:
                obj = json.loads(line)
                mapping[obj["text"]] = obj["mbid"]
        print(f"[k0.4]   loaded {blob.name}: {len(mapping):,} cumulative mappings")
    return mapping


def stream_one_output_dir(bucket, output_dir_gs: str) -> Iterable[tuple[str, list[float]]]:
    assert output_dir_gs.startswith(f"gs://{BUCKET}/"), output_dir_gs
    prefix = output_dir_gs[len(f"gs://{BUCKET}/"):]
    blobs = sorted(
        (b for b in bucket.list_blobs(prefix=prefix) if b.name.endswith(".jsonl")),
        key=lambda b: b.name,
    )
    print(f"[k0.4] found {len(blobs)} output shard(s) at {prefix}")
    for blob in blobs:
        print(f"[k0.4] streaming {blob.name}")
        with blob.open("r") as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                inst = obj.get("instance") or {}
                content = inst.get("content") or ""
                preds = obj.get("predictions") or []
                if not content or not preds:
                    continue
                emb = preds[0].get("embeddings") or {}
                values = emb.get("values") or []
                if not values:
                    continue
                yield content, values


def build_schema() -> pa.Schema:
    return pa.schema([
        pa.field("mbid", pa.string()),
        pa.field("vector", pa.list_(pa.float32(), TARGET_DIM)),
    ])


def main() -> int:
    if not JOB_IDS_FILE.exists():
        print(f"[k0.4] no job ids at {JOB_IDS_FILE}; run 03_submit_batch_job.py first")
        return 1
    resource_names = [
        line.strip()
        for line in JOB_IDS_FILE.read_text().splitlines()
        if line.strip()
    ]
    print(f"[k0.4] polling {len(resource_names)} job(s)...")
    output_dirs = wait_for_jobs(resource_names)
    print(f"[k0.4] all jobs succeeded; output dirs:")
    for d in output_dirs:
        print(f"[k0.4]   {d}")

    storage_client = storage.Client(project=PROJECT)
    bucket = storage_client.bucket(BUCKET)

    content_to_mbid = load_content_to_mbid(bucket)
    print(f"[k0.4] index size: {len(content_to_mbid):,} entries")

    LANCE_DIR.mkdir(parents=True, exist_ok=True)
    db = lancedb.connect(str(LANCE_DIR))
    schema = build_schema()
    if LANCE_TABLE in db.list_tables():
        db.drop_table(LANCE_TABLE)
    tbl = db.create_table(LANCE_TABLE, schema=schema, mode="create")

    buffer: list[dict] = []
    total = 0
    missed = 0
    start = time.time()

    for output_dir in output_dirs:
        for content, values in stream_one_output_dir(bucket, output_dir):
            mbid = content_to_mbid.get(content)
            if not mbid:
                missed += 1
                continue
            buffer.append({"mbid": mbid, "vector": values[:TARGET_DIM]})
            if len(buffer) >= BATCH_SIZE:
                tbl.add(buffer)
                total += len(buffer)
                elapsed = time.time() - start
                rate = total / max(elapsed, 1)
                print(
                    f"[k0.4] inserted {total:,} rows ({rate:,.0f}/s, "
                    f"missed={missed}, elapsed={elapsed:.0f}s)"
                )
                buffer.clear()

    if buffer:
        tbl.add(buffer)
        total += len(buffer)

    print(f"[k0.4] ingestion complete: {total:,} rows (missed {missed})")

    print(f"[k0.4] building IVF-PQ index...")
    t0 = time.time()
    tbl.create_index(
        metric="cosine",
        num_partitions=512,
        num_sub_vectors=48,
        vector_column_name="vector",
    )
    print(f"[k0.4] index built in {time.time() - t0:.0f}s")

    row_count = tbl.count_rows()
    print(f"[k0.4] final row count: {row_count:,}")
    print(f"[k0.4] table: {LANCE_DIR}/{LANCE_TABLE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
