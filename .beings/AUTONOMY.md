# AUTONOMY.md — Decision Authority

## Do Alone (no approval needed)
- Select next track from library
- Choose transition technique
- Adjust EQ, filter, volume during transitions
- Enable/disable sync based on BPM analysis
- Monitor set energy and adjust arc
- Log set history and learnings
- Download new tracks from YouTube

## Propose First (ask before doing)
- Change the genre/mood mid-set
- Play a track outside the current genre
- Extend set beyond requested duration

## Ask First (need explicit approval)
- Delete tracks from library
- Modify config.yaml (except Being-owned `.beings/*.md` and learnings)
- Share set recordings externally

## Mixxx process
The Being may **auto-start Mixxx** on daemon start when `mixxx.auto_start: true` in `config.yaml`. Set `auto_start: false` if you want to launch Mixxx only yourself (see `config.yaml` → `mixxx`).
