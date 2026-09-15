# Track D — rekordbox: what can DJ Treta actually do with it? (2026-09-14)

**Verdict: yes, there is a real, non-hack door — but it is the *controller* door, not the *library* door we
kept knocking on.** rekordbox accepts a second, generic MIDI device alongside the FLX4 (documented), sends
LED-state back over MIDI OUT, and speaks Ableton Link for tempo + beat phase. That is enough for Treta to
be a bar-aligned second pair of hands on deck 2 inside rekordbox — with Manish's Spotify/Apple Music
library in play. What it does **not** give: which track is playing, position in the track, or true BPM
of a non-LINKed deck. Library writes (pyrekordbox) stay dropped, per Manish's 08-14 verdict.

## 0. Prior art — what we already found, and why it stalled

| Date | Finding |
|---|---|
| 2026-06-25 | "Control any DJ software" design pass. Conclusion then: closed software = OS-layer hacks (drag-drop load, AX/OCR state, **virtual MIDI for control, Ableton Link as the one cross-vendor sync**, Pro DJ Link ecosystem-locked). Parked: "moat = the brain, adapters swappable; hacks are for validation". |
| 2026-07-12 / 07-29 | Our `rekordbox.xml` export pipeline built, then fixed (Artist, Kind/Size/DateAdded, musical key not Camelot). Import path documented: Preferences → View → tick "rekordbox xml" → Advanced → Database → point at file. **Works.** |
| 2026-08-08 | Smart CFX / Smart Fader are rekordbox-only DSP; Mixxx can only emulate. |
| 2026-08-14 | `rekordbox-mcp` / pyrekordbox on the SQLCipher `master.db`: reads with rekordbox open, writes need it closed, "assume all risk". **Manish: "hamare kaam ka nahi hai" — dropped, do not re-research.** Only revisit reason: DJ history as taste data. Also the measurement lesson: `system_profiler` is empty in this sandbox — use `ioreg`. |
| memory-graph `mixxx-remote-control` | MIDI cannot carry a file path; `LoadSelectedTrack` loads the *highlighted* row. The same limitation applies to rekordbox's `Load` function. |
| memory-graph `dj-treta-skill` | `controller.py` already opened a virtual MIDI port named "DJ Treta" via rtmidi for Mixxx — the pattern exists in our own code. |
| memory-graph `djtreta-channel-fader-mixing` | Manish mixes crossfader-centred on channel faders + EQ; Sarathi = suggest-only. Any rekordbox control must keep that. |

**Why every pass stalled:** each one attacked the *library* (db, xml) or assumed Mixxx as the only surface.
None tested rekordbox as a **MIDI-mapped target with Link for state**. That is the reason that changed.

## 1. Installed reality (verified tonight, read-only)

- **rekordbox 7.2.18.0311** at `/Applications/rekordbox 7/rekordbox.app` — **running** (pid 85270) while I looked; `rekordboxAgent` (Electron) on TCP 30001. Links CoreMIDI directly; **not Qt**; UI is Metal-rendered (`default.metallib`, Syphon).
- **Library is live:** `~/Library/Pioneer/rekordbox/master.db` 4.1 MB, WAL touched 23:16 today; header is not SQLite plaintext → SQLCipher, as on 08-14. Rolling `master.backup{,2,3}.db`.
- **Streaming configured:** `SpotifySetting.txt` (touched 23:03 tonight), `AppleMusicSetting.txt`, `TidalSetting.txt`, `.beatport/` under `~/Library/Application Support/Pioneer/rekordbox6/`. Manish is using Spotify-in-rekordbox *now*.
- **FLX4 connected** (`ioreg -p IOUSB` → `DDJ-FLX4`); CoreMIDI shows `DDJ-FLX4` in+out.
- **Mapping store:** `…/rekordbox6/MidiMappings/DDJ-FLX4.midi.csv` and `Manish-iPhone Bluetooth.midi.csv` — both stubs (`@file,1,<name>`), i.e. rekordbox auto-registered a *second, non-Pioneer CoreMIDI device* (iPhone, 29 Aug). Generic devices are enumerated next to the FLX4 — confirmed on this Mac, not just in docs.
- **Shipped mapping schema** (69 files in the app bundle): `#name,function,type,input,deck1..4,output,deck1..4,option,comment`. FLX4 file: 223 rows, **147 carry MIDI-OUT codes**.
- **Network:** no UDP 50000–50002 (Pro DJ Link) and no UDP 20808 (Ableton Link) sockets open → neither is on right now.
- **Python:** `/usr/bin/python3` has `python-rtmidi 1.5.8`; the dj-treta venv has neither rtmidi nor mido. PyPI has python-rtmidi 1.5.8, mido 1.3.3, aalink 0.2.3, pyrekordbox 0.4.4.
- **The one probe:** `rtmidi.MidiOut().open_virtual_port("DJ Treta")` → CoreMIDI listed `['DDJ-FLX4', 'DJ Treta']` → closed. No IAC bus needed.
- ~/Music/rekordbox (39 MB: Recording/, Sampler/) and ~/Music/PioneerDJ (11 MB demo tracks) — nothing of ours there.
- AX probe: System Events could not reach the rekordbox process from this sandbox (permission) → **inconclusive**, see 2e.

## 2. Control surfaces

### a. MIDI in — Treta as a second controller: **YES, documented, cheap**
- MIDI Learn guide §3.3: "When 2 or more units are connected, select the unit … from the drop-down menu"; each device gets its own mapping file. §3.5: click function → LEARN → operate control. Constraint: "One MIDI code cannot be assigned to multiple functions" (per device).
- Functions we need exist in the shipped CSVs: `PlayPause`, `Cue`, `Sync`, `Master`, `TempoSlider`, `ChannelFader`, `CrossFader`, `EQHigh/Mid/Low`, `CFXParameterCH1/2`, `SmartCFX`, `SmartFader`, `HotCue`, `BeatLoop`, `BeatJump`, `Browse` (rotary), `Forward`/`Back`, `Load` (per deck), `HeadphoneCue`, `MasterLevel`, `AbletonLinkBpmUp/Down/TAP`.
- Codes are hex status+data1: `900B` = note 0x0B ch1 (deck 1 Play); deck 2 = `910B`. `B013` = CC 0x13 ch1 (deck 1 channel fader). `KnobSliderHiRes` = 14-bit CC pairs; a virtual device can send plain 7-bit `KnobSlider`.
- Shortcut to try before 40 LEARN clicks: write `MidiMappings/DJ Treta.midi.csv` from the FLX4 template with our own codes (rekordbox reads the file when the device is selected; verify).
- Legality: it is a MIDI controller. Robustness: high — a 10-year-old feature. Effort: one evening.
- Limit: `Load` loads the **highlighted browser row**. No "load path X".

### b. State out
- **Pro DJ Link: dead end.** rekordbox emits only mixer-style status packets (device 11): BPM of master, no per-deck pitch/beat/track/playing (Deep Symmetry analysis). Not worth a NIC.
- **MIDI OUT feedback: partial yes.** rekordbox sends LED state for mapped rows: `PlayPause`, `Cue`, `Sync`, `HotCue`, `LoadedIndicator` (per deck), `SmartCFX/SmartFader`, plus `Indicator`-type rows — the DDJ-SR mapping has `BeatIndicator1/2/4/8/16/32` + `BeatIndicatorNumerator` (per-deck beat/phrase pulses; verify they are offered to a generic device). `MidiOutInterval` is a mapping parameter. **No BPM value, no position, no title** over MIDI.
- **Ableton Link: yes for tempo + beat phase, with rekordbox's twist.** Performance mode only. Global `LINK` on, then per-deck `LINK` (replaces BEAT SYNC). Link is a *session tempo setpoint*: decks with LINK follow it; with a controller attached the tempo slider does **not** push to Link (rekordbox FAQ) — BPM changes via the Link subscreen or mapped `AbletonLinkBpm*`; forum reports ≥6.5.3 Link follows master-deck pitch (verify on 7.2.18). Binary string: "Ableton Link cannot be turned ON while SMART FADER mode is ON." Net: in a LINKed set Treta knows **the tempo and where beat 1 is** via `aalink` (asyncio: `await link.sync(4)` = next bar). She does not know which track or where in it.
- No OSC. No MIDI clock out (third-party tools bridge Link → clock).

### c. Library
- **pyrekordbox:** key extraction broken since rekordbox 6.6.5 (obfuscated `app.asar`); macOS needs a RekordLocksmith memory dump; writes need rekordbox closed. The 08-14 verdict stands — **not re-proposed**.
- **XML bridge:** our pipeline already works. rekordbox needs a manual **Reload** (circular arrow on the xml node) — no watch folder. So "Treta writes a *Next Up* playlist → Manish reloads once per set → `Back`/`Browse`/`Forward`/`Load` over MIDI" is feasible; the weak spot is **blind navigation** (no cursor feedback). Mitigation: one-track playlist + fixed key sequence from root.
- **Keyboard route is deterministic:** rekordbox KeyMappings expose `Search for tracks in Collections`, `Load track to Deck 1/2` (shift+←/→), `Locate track loaded on deck` (⌘L). Keystroke injection with rekordbox frontmost = UI automation (2e), but the less brittle kind because rekordbox defines the bindings.

### d. Streaming through rekordbox
- Spotify Premium in rekordbox Mac since 2025-09-24 (51 markets); Apple Music, TIDAL, Beatport too. Manish's is configured.
- Via MIDI: `Browse/Forward/Back/Load` work on the Spotify tree like any node → **pre-made Spotify playlists are reachable by Treta**. Free-text search needs the keyboard (2c) — not MIDI.
- Terms: Premium required, online only, **no recording, no stems, no offline**, one device per account, "personal use" — public-performance licensing is the venue's problem, and **we cannot record a Spotify set**. Nothing forbids a MIDI controller driving it; nothing grants us more than a human DJ has.

### e. Accessibility / AppleScript — last resort
- No AppleScript dictionary expected; Metal-rendered custom UI → thin AX tree at best. Could not confirm from the sandbox (System Events denied). Brittle across the monthly 7.2.x releases. Use only the key-binding subset (2c).

### f. Two-app topology
- FLX4 = one app's audio + MIDI. **(i) Both in rekordbox** — Manish on FLX4, Treta as the virtual "DJ Treta" device, audio unchanged: **viable, zero new hardware.**
- (ii) rekordbox + Mixxx into an external mixer/aggregate device: needs a second interface, Mixxx loses the FLX4, Treta's Mixxx work doesn't transfer — **not viable for this rig.**

## 3. Ranked recommendation

1. **Virtual MIDI device → rekordbox (second controller) + Ableton Link for tempo/phase + MIDI-OUT LEDs for play/cue/loaded state.** PoC below.
2. Then the **XML "Next Up" playlist** bridge for loads (manual reload; blind nav). Only if 1 holds.
3. Then **keystroke injection** for Spotify search / deterministic Load. Accept brittleness; keep behind a flag.

**Honestly not possible:** knowing the playing track / position / real BPM of an un-LINKed deck; beat-accurate blends unless the set runs on LINK; recording Spotify sets; pyrekordbox writes; Pro DJ Link state from rekordbox; Smart CFX/Smart Fader *listening* (only triggering).

## 4. One-evening proof of concept

**Setup:** rekordbox Performance mode, FLX4 attached. `pip install python-rtmidi==1.5.8 aalink==0.2.3` into the dj-treta venv — both have `cp312 macosx_11_0_arm64` wheels (verified by `pip download`, not installed). aalink is a compiled module with no stub: `await link.sync(n)` is documented; confirm the tempo/beat/phase attribute names on first import (`dir(link)`).

1. Script opens `MidiOut().open_virtual_port("DJ Treta")` and `MidiIn().open_virtual_port("DJ Treta In")`. rekordbox Prefs → Controller → MIDI → dropdown should list **DJ Treta** next to DDJ-FLX4 (guide §3.3).
2. Map six deck-2 functions. Try writing `MidiMappings/DJ Treta.midi.csv` first (FLX4 rows, our codes); fall back to LEARN with the script emitting each code on demand:
   `PlayPause 910B`, `Cue 910C`, `Sync 9158`, `ChannelFader B113`, `EQLow B10F`, `Load 9647`; outputs `PlayPause/Cue/LoadedIndicator` to "DJ Treta In".
3. Global LINK on + deck LINK on Manish's deck. Script: `Link(128)`, `enabled=True`; log tempo/beat/phase per beat.
4. Treta routine: on `LoadedIndicator` for deck 2 → `Sync` on, `EQLow`=0, `ChannelFader`=0 → at `await link.sync(4)`: `PlayPause`; ramp channel fader over 16 bars, bass swap at bar 8 — our `_finish_channel_fader` logic re-expressed as CC writes (crossfader untouched).

**Success = all four:** (a) both devices listed together and the FLX4 keeps working; (b) Play/Cue LED echoes land on "DJ Treta In" within ~50 ms of Manish pressing them on the FLX4; (c) `aalink` tempo equals rekordbox's Link BPM and `sync(4)` fires on the visible bar; (d) one full bar-aligned channel-fader blend into deck 2 with Manish's hands off deck 2.
**Kill criteria:** rekordbox refuses a second device, or LEARN cannot capture from a virtual port → try an IAC bus (Audio MIDI Setup) once, then declare it dead. ~3–4 h.

## Sources
- MIDI LEARN Operation Guide 7.0.5 — https://cdn.rekordbox.com/files/20241203210623/rekordbox7.0.5_midi_learn_operation_guide_EN.pdf (§3.3 multiple units, §3.5 one code per function)
- Pioneer: How to customize rekordbox MIDI mapping — https://forums.pioneerdj.com/hc/en-us/articles/25240734758809
- Shipped mappings: `/Applications/rekordbox 7/rekordbox.app/Contents/Resources/MidiMappings/{DDJ-FLX4,DDJ-400,DDJ-1000,PIONEER DDJ-SR}.midi.csv` (read tonight)
- rekordbox FAQ — Ableton Link — https://rekordbox.com/en/support/faq/ableton-link/
- Pioneer forum: "Ableton Link … Follow the Master Deck BPM option" — https://forums.pioneerdj.com/hc/en-us/community/posts/900002865663
- Deep Symmetry DJ Link analysis, rekordbox status packets — https://djl-analysis.deepsymmetry.org/djl-analysis/vcdj.html ; beat-link — https://github.com/Deep-Symmetry/beat-link
- Rekordbox-Midi-Clock (Link → MIDI clock) — https://github.com/JamesMartinGithub/Rekordbox-Midi-Clock
- aalink — https://github.com/artfwo/aalink ; python-rtmidi — https://pypi.org/project/python-rtmidi/
- pyrekordbox key breakage since 6.6.5 — https://github.com/dylanljones/pyrekordbox/discussions/97 ; RekordLocksmith — https://github.com/Bide-UK/rekordlocksmith
- Spotify in rekordbox — https://rekordbox.com/en/2025/09/rekordbox-for-mac-win-spotify-support/ ; Spotify newsroom — https://newsroom.spotify.com/2025-09-24/dj-software-integration-premium/ ; DJ TechTools — https://djtechtools.com/2025/09/24/spotify-finally-slides-into-rekordbox-serato-and-algoriddim-djay-again-for-better-or-worse/
- rekordbox xml reload is manual — https://www.lexicondj.com/manual/sync-rekordbox-xml
- Prior art: `.beings/memory/2026-06-25.md`, `2026-07-29.md`, `2026-08-08.md`, `2026-08-14.md`; memory-graph `mixxx-remote-control`, `dj-treta-skill`, `djtreta-channel-fader-mixing`, `djtreta-who-loads-tracks`
