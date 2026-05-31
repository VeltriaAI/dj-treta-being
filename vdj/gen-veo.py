#!/usr/bin/env python3
"""Generate HD (1080p) VJ clips straight from Vertex AI Veo — bypasses the
LiteLLM gateway, which silently caps Veo at 720p. Uses gcloud's access token.

Usage:
    python3 gen-veo.py "<prompt>" <output.mp4> [duration=8] [resolution=1080p]

Veo 3.0 supports durations 4/6/8 and resolution 720p|1080p (no native 4K).
Project/region come from gcloud config (override with VEO_PROJECT / VEO_LOCATION).
"""
import base64, json, os, subprocess, sys, time, urllib.request, urllib.error

MODEL = "veo-3.0-generate-001"

def _token():
    return subprocess.check_output(["gcloud", "auth", "print-access-token"]).decode().strip()

def _project():
    return os.environ.get("VEO_PROJECT") or subprocess.check_output(
        ["gcloud", "config", "get-value", "project"]).decode().strip()

def generate(prompt, out_path, duration=8, resolution="1080p", aspect="16:9"):
    proj = _project(); loc = os.environ.get("VEO_LOCATION", "us-central1")
    tok = _token()
    base = f"https://{loc}-aiplatform.googleapis.com/v1/projects/{proj}/locations/{loc}/publishers/google/models/{MODEL}"
    hdr = {"Authorization": "Bearer " + tok, "Content-Type": "application/json"}

    def post(url, body):
        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=hdr)
        return json.load(urllib.request.urlopen(req, timeout=90))

    op = post(base + ":predictLongRunning", {
        "instances": [{"prompt": prompt}],
        "parameters": {"aspectRatio": aspect, "durationSeconds": int(duration),
                       "sampleCount": 1, "resolution": resolution},
    })
    name = op["name"]
    print(f"  job: …{name.split('/')[-1]}  ({resolution}, {duration}s)", flush=True)
    for _ in range(40):
        d = post(base + ":fetchPredictOperation", {"operationName": name})
        if d.get("done"):
            vids = d.get("response", {}).get("videos", [])
            if not vids:
                raise RuntimeError("no video in response: " + json.dumps(d.get("response", {}))[:300])
            with open(out_path, "wb") as f:
                f.write(base64.b64decode(vids[0]["bytesBase64Encoded"]))
            print(f"  saved {out_path}", flush=True)
            return out_path
        time.sleep(12)
    raise RuntimeError("timed out polling Veo operation")

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__); sys.exit(1)
    prompt, out = sys.argv[1], sys.argv[2]
    dur = int(sys.argv[3]) if len(sys.argv) > 3 else 8
    res = sys.argv[4] if len(sys.argv) > 4 else "1080p"
    generate(prompt, out, dur, res)
