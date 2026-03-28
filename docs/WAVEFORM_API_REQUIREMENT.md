# Requirement: Full-Resolution Waveform from Mixxx API

## For: dj.treta.life live waveform visualization

## Problem

The Mixxx HTTP API (`apiserver.cpp`) downsamples the waveform summary to ~200 points:

```cpp
int targetPoints = 200;
int step = std::max(1, dataSize / targetPoints);
```

The actual waveform has `data_size = 3840` points. The 200-point summary works for a track overview but is too coarse for a zoomed-in scrolling waveform view (like Mixxx's own scrolling waveform display).

## Request

Change the downsampling in `~/workspace/mixxx-treta/src/api/apiserver.cpp` line 142-143:

```cpp
// Current:
int targetPoints = 200;
int step = std::max(1, dataSize / targetPoints);

// Requested — return all points:
int step = 1;
```

This will return all 3840 points in the `waveform_summary` field of `/api/deck/:id/track_info`.

## Impact

- Response size increases from ~2KB to ~40KB for the waveform arrays (3840 * 3 arrays * ~3 bytes per JSON number)
- Only fetched on track change (not polled continuously), so no performance concern
- Enables dj.treta.life to render a dense, Mixxx-quality scrolling waveform

## Alternative

Add a separate endpoint `/api/deck/:id/waveform_full` that returns the full 3840-point waveform, keeping `waveform_summary` at 200 for backward compatibility.

## After Change

Rebuild Mixxx: `cd ~/workspace/mixxx-treta/build && cmake --build . --parallel`
