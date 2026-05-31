# VDJ Treta — beat-synced visual engine

VDJ Treta is DJ Treta's **live visual layer**: a fullscreen, beat-synced WebGL
show that plays alongside her DJ set. It reads what she's actually playing from
Mixxx in real time and renders generated video footage that breathes with the
music — pulsing on the kick, flying faster as energy rises, bursting on the drop,
floating on the breakdown.

It is **local-first**: runs on the Mac, displayed on a TV via an extended display.
It only *reads* Mixxx — it never controls playback (music-never-stop rule).

---

## How it works (architecture)

```
Mixxx HTTP API (localhost:7778)
   /api/live    ─ per-deck bpm, beat_distance (0→1 phase), beat_active (kick), VU
   /api/status  ─ key, eq
   /api/deck/N/track_info ─ genre, key, artist/title
        │  (polled via serve.py's /mixxx proxy to dodge CORS)
        ▼
   ┌─────────────────────────────────────────────────────────────┐
   │ vdj.js  (the engine — runs in the browser)                   │
   │                                                              │
   │  • PLL beat clock — predicts beat phase from BPM @60fps,     │
   │    hard-snaps on each beat_active. Kick-locked, no stutter.  │
   │  • VU → energy (0–1) + drop / breakdown / buildup detection  │
   │    (client-side, mirrors the daemon's relay perception).     │
   │  • Energy-state machine — flies between 3 "spaces" by live   │
   │    energy + section (see below).                             │
   │  • WebGL2 shader — plays the scene clip as a video texture,  │
   │    warps it to the beat, grades it, fires drop/breakdown FX. │
   │  • Double-buffered crossfade — two <video> + uMix, used for  │
   │    BOTH seamless looping and scene transitions.              │
   └─────────────────────────────────────────────────────────────┘
        ▼
   Fullscreen <canvas> on the TV
```

### The energy-state "space journey"
Scene selection is driven by **live energy + section**, not by genre. The three
clips are altitudes of one journey through space:

| State      | Clip            | When (see `desiredState()` in vdj.js) |
|------------|-----------------|----------------------------------------|
| `float`    | `scenes/float.mp4`    | breakdown, or low energy (calm)  |
| `wormhole` | `scenes/wormhole.mp4` | mid energy (the groove)          |
| `neon`     | `scenes/neon.mp4`     | drop, or high energy (the peak)  |

Hysteresis (wider exit than entry thresholds) + a `MIN_DWELL` (3.5s) stop it
flickering at band edges; a **drop bypasses the dwell** for an instant punch.
The genre clips in `scenes/` (techno, psytrance, melodic-techno, …) are a library
kept for a future "genre-worlds × energy" version; the live engine currently uses
the 3 energy-state clips.

---

## Run it

```bash
cd ~/beings/dj-treta
python3 vdj/serve.py            # serves on http://localhost:8089, proxies Mixxx
```
Open **http://localhost:8089** → drag the window onto the TV (extended display).

**Controls:** `F` fullscreen · `D` debug HUD · `1` manual drop burst ·
`2` toggle breakdown · `3` toggle buildup.

Requires Mixxx running with its HTTP API on `:7778` (DJ Treta's normal setup).
With no music playing it falls back to a calm procedural render.

---

## Files

| File | What it is |
|------|------------|
| `vdj.js`        | **The engine.** Beat clock, energy-state machine, WebGL shader, crossfade. |
| `serve.py`      | Local static server + Mixxx CORS proxy (`/mixxx/*` → `:7778`). Has HTTP Range support (Chrome needs 206 to stream MP4). |
| `index.html`    | The live visual page (put this on the TV). |
| `preview.html`  | Clip browser — plays clips one-by-one to review them. |
| `gen-veo.py`    | **Clip generator.** Calls Vertex AI Veo directly (1080p). See below. |
| `scenes/<state>/*.mp4` | The live clip **library**, one folder per energy state (`float/`, `wormhole/`, `neon/`). The engine rotates through each state's clips so a long set never repeats. (`.mp4`s gitignored.) |
| `downloads/`    | Source library of downloaded clips (yt-dlp from CC sources). |
| `explore/`      | Scratch clips under evaluation — NOT wired into the engine. (gitignored) |

### Scene library & rotation
The engine fetches `/scenes` (served by `serve.py`, which scans `scenes/<state>/*.mp4`,
falling back to a flat `scenes/<state>.mp4`). On entering a state it plays that state's
current clip; on each loop-seam it **advances to the next clip in the state**, crossfading —
so the whole library cycles instead of one clip repeating. Add clips by dropping `.mp4`s into
`scenes/float|wormhole|neon/` — no code change, picked up on page reload.

**Sourcing (both wired):** generate originals (`gen-veo.py`, Veo 1080p direct) OR download
licensed footage (`yt-dlp`). Current library is Beeple loops (Creative Commons, free commercial)
+ NASA Orion nebula (public domain — credit NASA). ⚠️ VISUALDON is All-Rights-Reserved — do not
use. Native-4K downloads need Pixabay/Pexels free API keys.
| `experiments/`  | Prompt-optimization harness (see below). |

---

## Generating clips — `gen-veo.py`

```bash
python3 vdj/gen-veo.py "<prompt>" <out.mp4> [duration=8] [resolution=1080p]
```
Hits **Vertex AI Veo 3.0** (`veo-3.0-generate-001`) directly using the gcloud
access token. Why direct and not the genmedia gateway: **the LiteLLM gateway
silently caps Veo at 720p**; Vertex honours `resolution: 1080p`.

- Veo durations: **4 / 6 / 8s only**. Resolution: **720p | 1080p** — 1080p is
  Veo's native ceiling, there is **no true 4K** from the model.
- For 4K, upscale the 1080p output (lanczos is clean for abstract/glowing content):
  `ffmpeg -i in.mp4 -vf "scale=3840:2160:flags=lanczos" -crf 16 out.mp4`
- Billing goes to the active gcloud project (currently `fandorab2w3`); override
  with `VEO_PROJECT` / `VEO_LOCATION`.

---

## Prompt optimization — `experiments/`

A closed loop to learn how to write Veo prompts that hit a target look:
**screenshot → prompt → generate → compare a frame → record the lesson.**

```bash
cd vdj/experiments
./produce.sh <name> "<prompt>" <reference-screenshot.png>
#   → <name>.mp4, <name>-frame.png (mid-frame),
#     <name>-compare.png (reference | generated, side by side)
```
`LEARNINGS.md` is the accumulating cookbook — every round appends what matched,
what missed, and the prompt delta for next time. Read it before writing prompts.

---

## Gotchas already burned (don't re-learn these)
- **Veo native max = 1080p.** True 4K only via upscale. The gateway caps at 720p;
  generate via `gen-veo.py` (direct Vertex) for 1080p.
- **Thin prompt → thin content.** Specific, detailed prompts give structured
  footage; one-liners give sparse footage no upscaler can rescue.
- **A `<video>` must be attached to the DOM** or real Chrome won't decode it
  (stalls at readyState 0). They're appended hidden (2px, not display:none).
- **rAF fully pauses in a backgrounded tab** — section logic + scene selection
  run on a `setInterval` tick, not the render loop. Video also pauses when the
  tab is hidden (handled via `visibilitychange`). The TV must be the foreground.
- **GLSL identifiers can't start with `gl_`** (reserved) — crashes shader compile
  and aborts the whole module.
- **Chroma split must be zero at rest** — a baseline split shreds fine star/point
  detail into RGB noise; only apply it on the kick/drop.
- For smooth looks, tell Veo explicitly: *"no stars, no grain, no dust specks,
  only smooth gradients"* — point-detail is what beat-sync FX turn into noise.

---

## Status (2026-05-31)
Working end-to-end against a live set: PLL beat-lock, energy reactivity,
energy-state scene journey (float/wormhole/neon), drop/breakdown/buildup FX,
crossfades, neon-on-black grade, now-playing overlay. Branch: see git.

**Open next:** dial state thresholds + grade to feel on the TV · regenerate the
3 energy clips at 1080p with strong prompts (current ones were early/thin) ·
optional genre-worlds × energy version · optional AI upscaler for true 4K ·
later: capture the canvas for the public dj.treta.life listener page.

Project context lives in the parent repo's `CLAUDE.md` / `AGENTS.md`, and in
Treta's memory note `project_vdj_treta`.
