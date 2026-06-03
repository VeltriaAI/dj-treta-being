# VDJ Treta — Veo Prompt Cookbook

Closed-loop prompt optimization: screenshot → prompt → generate → compare frame → learn.
Each experiment appends an entry. The goal is a repeatable recipe for hitting a target look.

## Method
- **Text-only** generation (no image-conditioning) so we're truly testing the prompt.
- Compare a mid-frame of the clip against the reference screenshot (tests LOOK, not motion).
- Tool: `./produce.sh <name> "<prompt>" <reference.png>` → `<name>-frame.png` + `<name>-compare.png`.

## Working prompt structure (hypothesis — refine as we learn)
`[subject / form]` + `[material / texture]` + `[specific color palette w/ hex-ish names]`
+ `[motion verb + speed]` + `[lighting / contrast]` + `[camera framing]` + `[mood]`
+ `[negatives: no text, no faces, no people, no objects, no logos]`

## Established facts (before experiments)
- Veo native resolution ceiling = **1080p**. No true 4K from the model; 4K only via upscale
  (lanczos is clean for abstract/glowing content). Resolution is a model param, NOT the gateway —
  the LiteLLM gateway silently caps at 720p, so we generate **direct via Vertex** (`gen-veo.py`).
- Veo durations: **4 / 6 / 8s only**. For frame-compare experiments use 4s (fastest, same look).
- **Thin prompt → thin content.** A one-line prompt yields sparse, low-detail footage that no
  upscaler can rescue. Specificity in the prompt = structure in the output.
- **"no stars, no grain, no dust specks, only smooth gradients"** removes fine point-detail that
  beat-sync shaders shred into RGB noise. Add when the look should be smooth.
- Model on Vertex for this project: `veo-3.0-generate-001` (3.1 not allowlisted). Fast variant:
  `veo-3.0-fast-generate-001` (cheaper/quicker, may drift slightly from quality model).

## Experiments
<!-- newest first; append entries below -->
