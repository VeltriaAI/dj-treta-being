"""v5 audio coordinator — orchestrates the full pipeline.

Runs once on a coordinator VM. Mac can sleep.

Steps:
  1. Download v4 parquet from HF.
  2. Build priority queue (year, electronic, has video_id).
  3. Shard queue into N parquets, upload to GCS.
  4. Spawn N spot worker VMs.
  5. Poll GCS for completion markers.
  6. Merge result shards into final v5 parquet.
  7. Push v5 to HF.
  8. Delete worker VMs + self.

Env vars (from .env, loaded by startup_coordinator.sh):
  GCP_PROJECT, GCS_BUCKET, GCP_ZONE, GCP_REGION,
  HF_TOKEN, HF_DATASET_REPO,
  DATASET_VERSION (default 'v5'), RUN_ID,
  WORKER_COUNT, WORKER_MACHINE_TYPE, USE_SPOT_WORKERS,
  PRIORITY_MIN_YEAR, PRIORITY_REQUIRE_VIDEO_ID, PRIORITY_LIMIT,
  KEEP_AUDIO_HOT
"""
from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

import polars as pl
from google.cloud import storage
from huggingface_hub import HfApi, hf_hub_download

# ── Config ────────────────────────────────────────────────────────────
GCP_PROJECT = os.environ["GCP_PROJECT"]
GCS_BUCKET = os.environ["GCS_BUCKET"]
GCP_ZONE = os.environ.get("GCP_ZONE", "us-central1-a")
HF_TOKEN = os.environ["HF_TOKEN"]
HF_DATASET_REPO = os.environ.get("HF_DATASET_REPO", "NaturNestAI/electronic-music-knowledge")
DATASET_VERSION = os.environ.get("DATASET_VERSION", "v5")
RUN_ID = os.environ["RUN_ID"]

WORKER_COUNT = int(os.environ.get("WORKER_COUNT", "32"))
WORKER_MACHINE_TYPE = os.environ.get("WORKER_MACHINE_TYPE", "e2-standard-4")
USE_SPOT_WORKERS = os.environ.get("USE_SPOT_WORKERS", "true").lower() == "true"

PRIORITY_MIN_YEAR = int(os.environ.get("PRIORITY_MIN_YEAR", "2020"))
PRIORITY_REQUIRE_VIDEO_ID = os.environ.get("PRIORITY_REQUIRE_VIDEO_ID", "true").lower() == "true"
PRIORITY_LIMIT = int(os.environ.get("PRIORITY_LIMIT", "200000"))

KEEP_AUDIO_HOT = os.environ.get("KEEP_AUDIO_HOT", "false").lower() == "true"

THIS_DIR = Path(__file__).parent
LOCAL_TMP = Path("/tmp/v5_coord")
LOCAL_TMP.mkdir(exist_ok=True)

logging.basicConfig(
    format="%(asctime)s [coord] %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("coord")

_gcs = None


def gcs():
    global _gcs
    if _gcs is None:
        _gcs = storage.Client(project=GCP_PROJECT)
    return _gcs.bucket(GCS_BUCKET)


# ── Phase 1: build priority queue ─────────────────────────────────────
def build_priority_queue() -> pl.DataFrame:
    log.info(f"downloading v4 parquet from HF...")
    parquet_path = hf_hub_download(
        repo_id=HF_DATASET_REPO,
        filename="v4/dj_treta_library.parquet",
        repo_type="dataset",
        local_dir=str(LOCAL_TMP),
        token=HF_TOKEN,
    )
    df = pl.read_parquet(parquet_path, columns=["mbid", "video_id", "year", "artist_name", "title"])
    log.info(f"v4: {len(df):,} rows")

    if PRIORITY_REQUIRE_VIDEO_ID:
        df = df.filter(pl.col("video_id").is_not_null() & (pl.col("video_id") != ""))
    df = df.filter(
        pl.col("mbid").is_not_null()
        & ((pl.col("year") >= PRIORITY_MIN_YEAR) | pl.col("year").is_null())
    )
    df = df.unique(subset=["mbid"], keep="first")
    log.info(f"after priority filter: {len(df):,} rows")

    # Sort: year DESC (recent first), then by mbid for stability
    df = df.sort(["year", "mbid"], descending=[True, False], nulls_last=True)

    if PRIORITY_LIMIT > 0 and len(df) > PRIORITY_LIMIT:
        df = df.head(PRIORITY_LIMIT)
        log.info(f"limited to top {PRIORITY_LIMIT:,}")

    Path(parquet_path).unlink()
    return df


def shard_and_upload(df: pl.DataFrame):
    log.info(f"sharding {len(df):,} rows into {WORKER_COUNT} shards...")
    per = len(df) // WORKER_COUNT
    for i in range(WORKER_COUNT):
        start = i * per
        end = (i + 1) * per if i < WORKER_COUNT - 1 else len(df)
        shard = df.slice(start, end - start)
        shard_path = LOCAL_TMP / f"shard_{i:03d}.parquet"
        shard.write_parquet(shard_path, compression="zstd")
        gcs().blob(f"{DATASET_VERSION}/queue/{RUN_ID}/shard_{i:03d}.parquet").upload_from_filename(str(shard_path))
        shard_path.unlink()
    log.info("queue uploaded")


# ── Phase 2: spawn workers ────────────────────────────────────────────
def upload_worker_assets():
    """Upload worker.py + startup script to GCS so VMs can fetch them."""
    log.info("uploading worker assets to GCS...")
    for fname in ("worker.py", "startup_worker.sh"):
        src = THIS_DIR / fname
        gcs().blob(f"{DATASET_VERSION}/code/{RUN_ID}/{fname}").upload_from_filename(str(src))
    log.info("worker assets uploaded")


def spawn_workers():
    log.info(f"spawning {WORKER_COUNT} worker VMs...")
    spot_flag = "--provisioning-model=SPOT --instance-termination-action=DELETE" if USE_SPOT_WORKERS else ""

    # Use prebuilt v5 image if it exists (boots ~30s vs ~20min vanilla debian).
    v5_image = os.environ.get("V5_IMAGE_NAME", "dj-treta-v5-worker-1")
    image_check = subprocess.run(
        f"gcloud compute images describe {v5_image} --project={GCP_PROJECT}",
        shell=True, capture_output=True,
    )
    if image_check.returncode == 0:
        image_flags = f"--image={v5_image} --image-project={GCP_PROJECT}"
        log.info(f"workers will boot from prebuilt image: {v5_image}")
    else:
        image_flags = "--image-family=debian-12 --image-project=debian-cloud"
        log.info(f"WARN: image {v5_image} not found — using vanilla debian-12")

    for i in range(WORKER_COUNT):
        name = f"v5w-{RUN_ID.lower().replace('_', '-')}-{i:03d}"[:62]
        metadata = (
            f"GCP_PROJECT={GCP_PROJECT},"
            f"GCS_BUCKET={GCS_BUCKET},"
            f"DATASET_VERSION={DATASET_VERSION},"
            f"RUN_ID={RUN_ID},"
            f"SHARD_ID={i},"
            f"WORKERS_PER_VM={os.environ.get('WORKERS_PER_VM', '3')},"
            f"KEEP_AUDIO_HOT={'true' if KEEP_AUDIO_HOT else 'false'}"
        )
        cmd = (
            f"gcloud compute instances create {name} "
            f"--project={GCP_PROJECT} --zone={GCP_ZONE} "
            f"--machine-type={WORKER_MACHINE_TYPE} --boot-disk-size=50GB "
            f"{image_flags} "
            f"--scopes=cloud-platform "
            f"{spot_flag} "
            f"--metadata={shlex.quote(metadata)} "
            f"--metadata-from-file=startup-script={THIS_DIR / 'startup_worker.sh'} "
            f"--no-restart-on-failure"
        )
        subprocess.run(cmd, shell=True, check=False, capture_output=True)
        log.info(f"worker {i:03d} created")
        time.sleep(0.2)
    log.info(f"all {WORKER_COUNT} workers spawned")


# ── Phase 3: monitor + merge ──────────────────────────────────────────
def wait_for_workers():
    log.info("polling GCS for shard completion...")
    bucket = gcs()
    poll_every = 60
    while True:
        done = sum(1 for _ in bucket.list_blobs(prefix=f"{DATASET_VERSION}/done/{RUN_ID}/"))
        log.info(f"  {done}/{WORKER_COUNT} shards complete")
        if done >= WORKER_COUNT:
            return
        time.sleep(poll_every)


def merge_results():
    log.info("merging result shards...")
    bucket = gcs()
    frames = []
    for blob in bucket.list_blobs(prefix=f"{DATASET_VERSION}/results/{RUN_ID}/"):
        local = LOCAL_TMP / Path(blob.name).name
        blob.download_to_filename(str(local))
        frames.append(pl.read_parquet(local))
        local.unlink()
    if not frames:
        raise RuntimeError("no result shards found")
    results = pl.concat(frames, how="diagonal_relaxed").unique(subset=["mbid"], keep="last")
    log.info(f"merged: {len(results):,} rows")
    return results


def join_and_publish(audio_features: pl.DataFrame):
    log.info("downloading v4 again for join...")
    parquet_path = hf_hub_download(
        repo_id=HF_DATASET_REPO,
        filename="v4/dj_treta_library.parquet",
        repo_type="dataset",
        local_dir=str(LOCAL_TMP),
        token=HF_TOKEN,
    )
    v4 = pl.read_parquet(parquet_path)
    log.info(f"v4: {len(v4):,} rows")

    v5 = v4.join(audio_features, on="mbid", how="left")
    log.info(f"v5: {len(v5):,} rows ({v5.filter(pl.col('bpm_exact').is_not_null()).height:,} analyzed)")

    out_main = LOCAL_TMP / "dj_treta_library.parquet"
    out_features = LOCAL_TMP / "audio_features.parquet"
    v5.write_parquet(out_main, compression="zstd")
    audio_features.write_parquet(out_features, compression="zstd")

    log.info("pushing to HF...")
    api = HfApi(token=HF_TOKEN)
    api.upload_file(
        path_or_fileobj=str(out_main),
        path_in_repo=f"{DATASET_VERSION}/dj_treta_library.parquet",
        repo_id=HF_DATASET_REPO,
        repo_type="dataset",
        commit_message=f"{DATASET_VERSION}: audio analysis (BPM/key/beat-grid/waveform) on {audio_features.height:,} tracks",
    )
    api.upload_file(
        path_or_fileobj=str(out_features),
        path_in_repo=f"{DATASET_VERSION}/audio_features.parquet",
        repo_id=HF_DATASET_REPO,
        repo_type="dataset",
        commit_message=f"{DATASET_VERSION}: audio_features sidecar",
    )

    coverage = {
        "total_tracks": v5.height,
        "analyzed": v5.filter(pl.col("bpm_exact").is_not_null()).height,
        "errors": v5.filter(pl.col("analysis_error").is_not_null()).height,
        "with_audio_path": v5.filter(pl.col("audio_path").is_not_null()).height,
        "run_id": RUN_ID,
        "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    coverage_path = LOCAL_TMP / "coverage.json"
    coverage_path.write_text(json.dumps(coverage, indent=2))
    api.upload_file(
        path_or_fileobj=str(coverage_path),
        path_in_repo=f"{DATASET_VERSION}/coverage.json",
        repo_id=HF_DATASET_REPO,
        repo_type="dataset",
        commit_message=f"{DATASET_VERSION}: coverage metadata",
    )

    log.info(f"v5 published. coverage: {coverage}")


# ── Phase 4: cleanup ──────────────────────────────────────────────────
def delete_workers():
    log.info("deleting worker VMs...")
    pattern = f"v5w-{RUN_ID.lower().replace('_', '-')}-"
    for i in range(WORKER_COUNT):
        name = f"{pattern}{i:03d}"[:62]
        cmd = f"gcloud compute instances delete {name} --project={GCP_PROJECT} --zone={GCP_ZONE} --quiet"
        subprocess.run(cmd, shell=True, capture_output=True)
    log.info("workers deleted")


def self_destruct():
    log.info("self-destruct...")
    cmd = (
        f"gcloud compute instances delete v5-coordinator-{RUN_ID.lower().replace('_', '-')} "
        f"--project={GCP_PROJECT} --zone={GCP_ZONE} --quiet"
    )
    subprocess.run(cmd, shell=True)


# ── Main ──────────────────────────────────────────────────────────────
def main():
    log.info(f"=== v5 coordinator (run_id={RUN_ID}) ===")

    # Phase 1: queue
    if not list(gcs().list_blobs(prefix=f"{DATASET_VERSION}/queue/{RUN_ID}/", max_results=1)):
        df = build_priority_queue()
        shard_and_upload(df)
    else:
        log.info("queue already exists, resuming")

    # Phase 2: workers
    upload_worker_assets()
    if not list(gcs().list_blobs(prefix=f"{DATASET_VERSION}/done/{RUN_ID}/", max_results=1)):
        spawn_workers()
    else:
        log.info("some workers already running, skipping spawn")

    # Phase 3: wait + merge
    wait_for_workers()
    audio_features = merge_results()
    join_and_publish(audio_features)

    # Phase 4: cleanup
    delete_workers()
    self_destruct()


if __name__ == "__main__":
    sys.exit(main())
