# Track Characters — what each fixture track *actually sounds like*

Scenario rationales in `transitions.yaml` cite this doc. BPM/key/energy
tells you the numbers; this doc tells you the feel — what kind of
transitions make sense and why.

Confidence levels:
- **K** (known): widely recognized track with external evidence the
  author is willing to cite (mix-set appearances, interviews, DJ
  commentary). Reserved for tracks the author can defend.
- **A** (analyzed): character inferred from librosa timeline
  (section lengths, energy curve) and/or DB metadata (BPM, key,
  energy peak, mood descriptors) — no external claim beyond what the
  numbers say.
- **G** (guess): low-confidence extrapolation from artist + genre, or
  sparse-structure tracks where the timeline under-constrains
  character.

Discipline: do NOT call something "K" unless either the timeline
measurably matches the claim OR you have an external citation.
Stylistic prose without data backing is "A" at best.

---

## 123 BPM cluster (the big pocket)

### `massano_the_feeling` — 9A, energy 9, melodic-techno  (A)
Timeline-measured structure: intro 0-64, first sustained buildup
64-178 (114s), short breakdown 178-188, second buildup 188-242, long
breakdown 242-320 (78s), final buildup 320-447, outro 447-507. Energy
peak 9, mood descriptors Euphoric/Driving.

**Transition character:** energy-9 melodic-techno 123 BPM at 9A — sits
in the core harmonic pocket. Bass_swap is natural for peak-time 4/4
grids; short durations (half-phrase ~8s up to full phrase ~16s) fit a
locked match — a long blend dilutes energy. Two long breakdowns give
clean entry/exit windows.

### `raul_music_krishna_vasudevaya` — 9A, energy 8, melodic-techno  (A)
240s melodic-techno track with a long middle breakdown 120-188 (68s),
final buildup 188-212, outro 212-240. Title suggests a "Krishna
Vasudevaya" Sanskrit motif but the audio-character claim comes from
the timeline shape, not a listening transcript.

**Transition character:** breakdown 120-188 is the natural blend
window. Same 9A/123 BPM pocket as `massano_the_feeling` — bass_swap
or crossfade both valid. Duration: 8-16s when paired with another 9A
track (grids lock; no need for a long overlap).

### `unknown_epic_melodic_techno_2026` — 4A, energy 7, melodic-techno  (A)
Generic "in the style of" upload (YouTube title: "The Sign | Epic
Melodic Techno 2026 (Anyma & Afterlife Style)", uploader "Art Of
Techno Afro"). NOT a real Anyma release — canonical_confidence 0.2 in
DB. Structure: short buildup 38-56, long breakdown 94-192 (98s), brief
buildup 192-202, breakdown 202-230, buildup 230-272, outro 272-307.

**Transition character:** 4A (Fm) is 5 Camelot steps from 9A — too
far for a smooth blend. With 9A tracks, hard_cut at outro or echo_out
is the honest path; a long crossfade exposes the key clash. Against
the 4A neighbor `yotto_aviate`, same-key harmonic lock makes
filter_sweep or crossfade viable across a 5.5 BPM gap.

### `anima_ft_sheera_moon` — 10B, energy 8, melodic-techno  (A)
Highly segmented timeline: 12 sections with many short 12-30s
breakdowns and buildups. Outro 395-449.

**Transition character:** 10B is 1 Camelot step from 9A and 1 from
11B — sits between the melodic-techno 9A core and the progressive
11B pocket (Opus). Many short breakdowns means the DJ has many entry
points — almost any 30-second window works.

### `anyma_chris_avantgarde_consciousness` — 6A, energy 9, melodic-techno  (A)
Energy peak 9 at 6A/123 BPM. Timeline: intro 0-28, buildup 28-66,
drop 66-140 (74s), breakdown 140-180, short buildup/breakdown
alternation to 226, final drop 226-242, outro 242-274. Mood
descriptors: Driving, euphoric, powerful.

**Transition character:** 6A is 3 Camelot steps from 9A — playable
via echo_out or filter_sweep but not a smooth same-pocket move. Same
6A key as `charlotte_de_witte_doppler` (136 BPM) means the 13 BPM
gap between them is bridgeable via echo_out with 6A key continuity.

### `kasst_the_first_time` — 11A, energy 8, melodic-techno  (G)
12 timeline sections with heavy breakdown/buildup alternation. Outro
399-453 on a 453s track. File/DB attributes it as KAS:ST's "The First
Time" remixed by Kerri Chandler (original on Afterlife label AL037).

**Transition character:** 11A is 2 Camelot steps from 9A. Crossfade or
filter_sweep to the 9A pocket both work. The many short sections mean
plenty of valid entry windows but no single "signature" breakdown.

### `maceo_plex_conjure_balearia` — 9A, energy 8, deep-house  (A)
Timeline: intro 0-52, brief breakdown 52-64, long buildup 64-254
(190s), long breakdown 254-363 (109s), final buildup 363-445, outro
445-506. Mood descriptors: Hypnotic, driving, atmospheric.

**Transition character:** same 9A/123 BPM pocket as massano/raul.
Deep-house → melodic-techno cross-genre bridge sits naturally because
tempo and key lock. The 254-363 breakdown is a 109s window — ample
room to introduce a melodic-techno track via bass_swap or filter_sweep.

### `eric_prydz_opus` — 11B, energy 7, progressive-house  (A)
Timeline: intro 0-56, breakdown 56-156 (100s), brief intro 156-164,
breakdown 164-224 (60s), buildup 224-256, breakdown 256-292, buildup
292-330, breakdown 330-344, buildup 344-393, long breakdown 393-479
(86s), outro 479-543. Energy peak 7 — not a peak-time monster at the
measured level; the track is structural / atmospheric.

**Transition character:** many long breakdowns → suited to long,
textural blends rather than punchy swaps. The 100s breakdown
(56-156), the 86s late breakdown (393-479), and the 60s breakdown
(164-224) are all generous entry windows. Filter_sweep and crossfade
suit its atmospheric character; a bass_swap would be against type.
11B is 1 Camelot step from 10B (`anima_ft_sheera_moon`) — smooth
harmonic move.

### `camelphat_elderbrook_cola` — 3A, energy 9, tech-house  (A)
Tech-house at 123 BPM / 3A. Highly segmented timeline: 13 sections
with many buildup/breakdown alternations. Outro 367-416. Mood
descriptors: Energetic, Hypnotic, Groovy.

**Transition character:** 3A is FAR from the 9A pocket (6 Camelot
steps = tritone, the maximum harmonic distance). Does NOT blend
smoothly with anything in 9A/10B/11B/6A cluster — hard_cut at outro
is the honest move. Useful as a negative-scenario anchor.

---

## 117.5 BPM cluster

### `kinky_sound_lemon_haze` — 9A, energy 9, melodic-techno  (A)
9A pocket tracks but 5.5 BPM slower than the 123 core. Timeline shows
repeated buildup/intro/buildup pattern to 216, breakdown 216-274,
buildup 274-330, short breakdown, final buildup 338-371, outro
371-420.

**Transition character:** 5.5 BPM gap into 123 is in echo_out's sweet
spot. Same 9A means filter_sweep is also feasible. Outro (371-420)
pairs cleanly with any 123/9A entry.

### `daryl_dixon_coming_home` — 8A, energy 4, deep-house  (G)
Energy-4 is unusual for the fixture (everything else is 7-10). Only 3
sections in timeline: intro 0-18, one long breakdown 18-158, outro
158-179. Total track 179s — short.

**Transition character:** low energy → acts as a palate cleanser
between peak moments. 8A is 1 step from 9A. Good for slowing the floor
with crossfade; not for peak-time blending.

### `yotto_aviate` — 4A, energy 9, progressive-house  (A)
117.5 BPM / 4A progressive-house. Breakdowns 66-98, 190-258 (68s
window), and brief 359-369. Outro 369-419.

**Transition character:** 4A matches `unknown_epic_melodic_techno_2026`
(also 4A, 123 BPM) — a same-key 5.5 BPM-up transition. Filter sweep
or echo_out across the BPM gap is clean because the harmonic axis
locks. The 190-258 breakdown is a 68s window.

---

## 136 BPM cluster (psytrance + dark-techno peak)

### `charlotte_de_witte_doppler` — 6A, energy 10, dark-techno  (A)
Energy peak 10 — the fixture's ceiling. Timeline: intro 0-44, long
buildup 44-90 and 118-200, breakdown 200-260 (60s), final buildup
260-383, outro 383-434. Mood descriptors: Intense, driving, hypnotic.

**Transition character:** same 6A key as
`anyma_chris_avantgarde_consciousness`. 136→123 = 13 BPM down — no
beatmatched technique survives that gap, so echo_out or hard_cut.
Doppler outgoing into consciousness preserves the 6A harmonic thread
while letting the tempo drop.

### `astrix_heart` — 9B, energy 9, psytrance  (A)
136 BPM psytrance. Breakdowns at 168-206 and a long mid-track 250-344
(94s). Outro 425-482. Mood descriptors: Energetic, driving,
psychedelic.

**Transition character:** psytrance grids are unforgiving — can't
blend smoothly from 123 BPM techno. Echo_out or hard_cut only. Inside
the 136 pocket with `charlotte_de_witte_doppler` and
`armin_van_buuren_vini_vici_feat_hilight_tribe_grea`, bass_swap works
because grids align.

### `armin_van_buuren_vini_vici_feat_hilight_tribe_grea` — 9A, energy 9, psytrance  (K)
"Great Spirit" — the widely-covered tribal-psytrance crossover
released 2017 on Armada Music. 136 BPM, 9A — same key as massano but
13 BPM faster. Outro 403-458.

**Transition character:** the 9A-at-136 twin of massano's 9A-at-123.
From massano, echo_out at massano's 447 outro into this track's 136
grid bridges the tempo — the 9A key continuity keeps it musical.

---

## Extreme outliers

### `bonobo_kerala` — 83 BPM, 6A, energy 8, downtempo  (K)
Bonobo's "Kerala" (2017, Ninja Tune, album *Migration*). UK broken-
beat downtempo. 83 BPM is well outside dance-floor tempos. Same 6A
key as `anyma_chris_avantgarde_consciousness` and
`charlotte_de_witte_doppler`.

**Transition character:** NOT an in-mix transition source — it's a
SET-level segue. Use as cooldown or opener; transitioning into 123-
136 territory requires hard_cut at silence or a very long echo_out
bridge. Same-key continuity (6A) is the only harmonic thread across
a 40-53 BPM gap.

---

## Quick reference tables

### By BPM
| BPM    | Tracks                                                 |
|--------|--------------------------------------------------------|
| 83     | bonobo_kerala                                          |
| 117.5  | kinky_sound_lemon_haze, daryl_dixon_coming_home,       |
|        | yotto_aviate                                            |
| 123    | massano_the_feeling, raul_music_krishna_vasudevaya,    |
|        | unknown_epic_melodic_techno_2026, anima_ft_sheera_moon,|
|        | anyma_chris_avantgarde_consciousness,                   |
|        | kasst_the_first_time, maceo_plex_conjure_balearia,     |
|        | eric_prydz_opus, camelphat_elderbrook_cola              |
| 136    | charlotte_de_witte_doppler, astrix_heart,              |
|        | armin_van_buuren_vini_vici_feat_hilight_tribe_grea     |

### By Camelot key
| Key | Tracks                                             | Harmonic family    |
|-----|----------------------------------------------------|--------------------|
| 3A  | camelphat_elderbrook_cola                          | isolated (6 steps) |
| 4A  | unknown_epic_melodic_techno_2026, yotto_aviate     | isolated (5 steps) |
| 6A  | anyma_chris_avantgarde_consciousness,              | inner-wheel center |
|     | charlotte_de_witte_doppler, bonobo_kerala          |                    |
| 8A  | daryl_dixon_coming_home                            | adjacent to 9A     |
| 9A  | massano_the_feeling, raul_music_krishna_vasudevaya,| core cluster       |
|     | kinky_sound_lemon_haze, maceo_plex_conjure_balearia,|                   |
|     | armin_van_buuren_vini_vici_feat_hilight_tribe_grea  |                   |
| 9B  | astrix_heart                                       | relative of 9A     |
| 10B | anima_ft_sheera_moon                               | adjacent to 11B    |
| 11A | kasst_the_first_time                               | adjacent to 9A     |
| 11B | eric_prydz_opus                                    | adjacent to 10B    |

### Natural transition durations (at 123 BPM, 32-beat phrase)
| Beats | Seconds | When to use                                    |
|-------|---------|-------------------------------------------------|
| 8     | ~4      | Quick bass_swap between locked tracks          |
| 16    | ~8      | Standard peak-time bass_swap / half-phrase      |
| 32    | ~16     | Full phrase — crossfade, filter_sweep, echo_out |
| 64    | ~31     | Long atmospheric blend                          |

A phrase at 123 BPM = (60/123)×32 = 15.6s. So "8-second transition"
= half phrase = perfectly aligned.
