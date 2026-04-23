"""K0 step 3b — Poll existing batch-prediction job.

Reads scripts/knowledge/.job_id for the resource name, polls every 60s,
exits 0 on success / 1 on failure.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

from google.cloud import aiplatform

PROJECT = "fandorab2w3"
LOCATION = "us-central1"

JOB_ID_FILE = Path(__file__).parent / ".job_id"


def main() -> int:
    resource_name = JOB_ID_FILE.read_text().strip()
    aiplatform.init(project=PROJECT, location=LOCATION)
    print(f"[k0.3b] polling {resource_name}")

    last_state = None
    start = time.time()
    while True:
        # Re-instantiate each tick to pull fresh state from the API.
        job = aiplatform.BatchPredictionJob(resource_name)
        state = str(job.state).split(".")[-1]
        elapsed = int(time.time() - start)
        if state != last_state:
            print(f"[k0.3b] T+{elapsed}s state={state}")
            last_state = state
        if job.state in (
            aiplatform.compat.types.job_state.JobState.JOB_STATE_SUCCEEDED,
            aiplatform.compat.types.job_state.JobState.JOB_STATE_FAILED,
            aiplatform.compat.types.job_state.JobState.JOB_STATE_CANCELLED,
        ):
            break
        time.sleep(60)

    if job.state == aiplatform.compat.types.job_state.JobState.JOB_STATE_SUCCEEDED:
        oi = job.output_info
        print(f"[k0.3b] SUCCESS — output at {oi.gcs_output_directory}")
        return 0
    print(f"[k0.3b] FAILED — {job.error}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
