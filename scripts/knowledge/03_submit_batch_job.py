"""K0 step 3 — Submit Vertex AI batch-prediction jobs for text embeddings.

Vertex batch prediction caps at 1M instances per job. We have 3.5M rows split
into 8 × 500k chunks, so submit 4 jobs of 2 chunks each (= 1M per job) in
parallel. Record all resource names to .job_ids for the polling + ingest steps.

Model: publishers/google/models/text-embedding-005 (multilingual, 768-dim)
Output: gs://${DJTRETA_GCS_BUCKET}/embeddings/output/<job_id>/
"""
from __future__ import annotations

import os

import sys
import time
from pathlib import Path

from google.cloud import aiplatform

PROJECT = os.environ.get("DJTRETA_VERTEX_PROJECT") or sys.exit("DJTRETA_VERTEX_PROJECT required")
LOCATION = "us-central1"
MODEL = "publishers/google/models/text-embedding-005"
BUCKET = os.environ.get("DJTRETA_GCS_BUCKET") or sys.exit("DJTRETA_GCS_BUCKET required")
INPUT_PREFIX = f"gs://{BUCKET}/embeddings/input/"
OUTPUT_PREFIX = f"gs://{BUCKET}/embeddings/output/"

JOB_IDS_FILE = Path(__file__).parent / ".job_ids"

# Vertex AI batch prediction cap is < 1M instances (strict). 8 chunks × 500k
# = one job per chunk keeps us safely under.
CHUNK_GROUPS = [
    [f"chunk_{i:04d}.jsonl"] for i in range(8)
]


def main() -> int:
    aiplatform.init(project=PROJECT, location=LOCATION)

    resource_names: list[str] = []
    for idx, chunks in enumerate(CHUNK_GROUPS):
        display_name = f"dj-treta-embeddings-g{idx}-{int(time.time())}"
        gcs_source = [INPUT_PREFIX + c for c in chunks]
        print(f"[k0.3] submitting {display_name}")
        print(f"[k0.3]   input:  {gcs_source}")
        job = aiplatform.BatchPredictionJob.submit(
            job_display_name=display_name,
            model_name=MODEL,
            gcs_source=gcs_source,
            gcs_destination_prefix=OUTPUT_PREFIX,
            instances_format="jsonl",
            predictions_format="jsonl",
        )
        # submit() returns the job resource directly without the async race.
        rn = job.resource_name
        print(f"[k0.3]   resource: {rn}")
        resource_names.append(rn)

    JOB_IDS_FILE.write_text("\n".join(resource_names) + "\n")
    print(f"[k0.3] submitted {len(resource_names)} job(s); saved to {JOB_IDS_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
