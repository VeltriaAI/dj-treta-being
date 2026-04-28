"""K0 v2 step 2 — Submit a single Vertex AI batch-prediction job for v2 input.

Reads every *.jsonl blob under gs://fandorab2w3-music-data/embeddings/v2-input/
and submits one BatchPredictionJob pointing at all of them. Vertex accepts
multiple GCS sources per job so one job handles all shards.

Saves the resource_name to scripts/knowledge/.job_id_v2 for step 07.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

from google.cloud import aiplatform, storage

PROJECT = "fandorab2w3"
LOCATION = "us-central1"
MODEL = "publishers/google/models/text-embedding-005"
BUCKET = "fandorab2w3-music-data"
V2_INPUT_PREFIX = "embeddings/v2-input/"
V2_OUTPUT_PREFIX = f"gs://{BUCKET}/embeddings/v2-output/"

JOB_ID_FILE = Path(__file__).parent / ".job_id_v2"


def main() -> int:
    storage_client = storage.Client(project=PROJECT)
    bucket = storage_client.bucket(BUCKET)
    blobs = sorted(
        (b for b in bucket.list_blobs(prefix=V2_INPUT_PREFIX) if b.name.endswith(".jsonl")),
        key=lambda b: b.name,
    )
    if not blobs:
        print(f"[k0.6] no input blobs under gs://{BUCKET}/{V2_INPUT_PREFIX} — run 05 first")
        return 1

    gcs_source = [f"gs://{BUCKET}/{b.name}" for b in blobs]
    for s in gcs_source:
        print(f"[k0.6] input: {s}")

    aiplatform.init(project=PROJECT, location=LOCATION)
    display_name = f"dj-treta-embeddings-v2-{int(time.time())}"
    print(f"[k0.6] submitting {display_name}")
    job = aiplatform.BatchPredictionJob.submit(
        job_display_name=display_name,
        model_name=MODEL,
        gcs_source=gcs_source,
        gcs_destination_prefix=V2_OUTPUT_PREFIX,
        instances_format="jsonl",
        predictions_format="jsonl",
    )
    rn = job.resource_name
    print(f"[k0.6] resource: {rn}")

    JOB_ID_FILE.write_text(rn + "\n")
    print(f"[k0.6] saved to {JOB_ID_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
