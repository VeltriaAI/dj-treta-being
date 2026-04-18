# Track Characters — what each fixture track *actually sounds like*

Scenario rationales in `transitions.yaml` cite this doc. BPM/key/energy
tells you the numbers; this doc tells you the feel — what kind of
transitions make sense and why.

Confidence levels:
- **K** (known): widely recognized track, character documented in mix
  sets, interviews, or DJ commentary
- **A** (analyzed): character inferred from librosa timeline (section
  lengths, energy curve) without outside reference
- **G** (guess): low-confidence extrapolation from artist + genre

---

## 123 BPM cluster (the big pocket)

### `massano_the_feeling` — 9A, energy 9, melodic-techno  (K)
Driving, euphoric Afterlife-style melodic techno. Long 114s buildup
(64-178), short breakdown at 178-188, major second buildup 188-242,
then deep ~80s breakdown 242-320 (the memorable "pause" where the melody
hangs), then the final peak push from 320-447. Outro 447-507.

**Transition character:** peak-time anchor. Works as either the
incoming track (arrive at 178 breakdown) or the outgoing track (exit
at 447 outro). Bass is authoritative — bass_swap is natural because
the kick pattern is textbook 4/4. Peak-time bass_swap duration is
short (8-16 beats = 4-8s) because both tracks are locked; a long blend
dilutes the energy.

### `raul_music_krishna_vasudevaya` — 9A, energy 8, melodic-techno  (K)
Spiritual-vocal melodic techno with Krishna chant. Short track (240s).
Buildup 80-120, long breakdown 120-188 dominated by chant, final peak
buildup 188-212, outro 212-240.

**Transition character:** chant breakdown is the feature. Blends
beautifully with massano (both 9A/123 BPM) — the vocal floats over
massano's synth hooks. Bass_swap at 120 (breakdown start) is cleanest;
duration 8-16s because the vocal carries the blend.

### `anyma_the_sign` — 4A, energy 7, melodic-techno  (K)
Hypnotic, ethereal, slower-feeling than its BPM. Long breakdowns
(94-192 is one continuous introspective section). Outro 272-307.

**Transition character:** key 4A (Fm) is far from the 9A pocket (5
Camelot steps). This track does NOT blend with 9A tracks — bass_swap
or crossfade exposes the clash. Hard_cut at outro or echo_out is the
only path out. Within its own 4A family (yotto_aviate 4A), crossfade
and filter_sweep work.

### `anima_ft_sheera_moon` — 10B, energy 8, melodic-techno  (A)
Lots of short breakdowns (many 12-30s segments). Outro 395-449.

**Transition character:** 10B is diagonally adjacent to 9A (1 step)
and 11B (1 step). Blends with both 9A melodic-techno pocket and 11B
progressive house. Short breakdowns mean the DJ has many entry points
— can target almost any 30-second window.

### `anyma_chris_avantgarde_consciousness` — 6A, energy 9, melodic-techno  (K)
Dark, driving Afterlife peak-hour. 6A is central on the inner wheel.
Breakdowns at 140-180 and 194-206. Outro 242-274.

**Transition character:** 6A is 3 Camelot steps from 9A (approachable
via relative-major 6B→9B if the mix allows). Against 9A tracks, best
via echo_out or filter_sweep to tame the harmonic gap. Against 10B
(anima_moon) it's 4 steps — too far. Against charlotte_doppler (6A,
136 BPM) it shares key but BPM gap forces echo_out.

### `kasst_the_first_time` — 11A, energy 8, melodic-techno  (G)
Lots of sectioning in the timeline (12 sections). Outro 399-453,
track total 453s.

**Transition character:** 11A is 2 Camelot steps from 9A (relative
close). Crossfade to 9A tracks works; filter_sweep also fine.

### `maceo_plex_conjure_balearia` — 9A, energy 8, deep-house  (K)
Classic Maceo Plex deep-house. Balearic/tribal flavor. Long grooves
with short breakdowns (52-64) and a MASSIVE mid-section breakdown
(254-363 = ~110s continuous). Outro 445-506.

**Transition character:** same 9A/123 pocket as massano/raul. Deep-
house → melodic-techno cross-genre bridge sits naturally because
tempo and key lock. The 254-363 breakdown is a perfect window to
introduce a melodic-techno track via bass_swap or filter_sweep.

### `eric_prydz_opus` — 11B, energy 7, progressive-house  (K)
Legendary 2014 prog-house slow-burner. Known for its famous ~2-minute
buildup. Breakdowns 56-156 (first extended moody section), 164-224
(the iconic one), 256-292 (minor break before the peak). Outro 479-543
(very long tail).

**Transition character:** Opus IS slow and atmospheric. Not a bass-
swap track — the energy is too restrained for a punchy swap. Filter
sweep works because Opus is all about texture lifting over time.
Crossfade also fits its long breathy character. 11B is 1 step from
10B (anima_moon) — smooth harmonic move.

### `camelphat_elderbrook_cola` — 3A, energy 9, tech-house  (K)
Iconic tech-house with the sticky "Cola" vocal hook. 3A is FAR from
the 9A pocket (6 Camelot steps = tritone). Outro 367-416.

**Transition character:** isolated harmonically. Does NOT blend with
anything in 9A/10B/11B/6A cluster via smooth techniques. Hard_cut at
outro is the honest move. Key-shift filter_sweep is possible but
jarring. Useful as a negative-scenario source.

---

## 117.5 BPM cluster

### `kinky_sound_lemon_haze` — 9A, energy 9, melodic-techno  (A)
Same 9A pocket as massano but 5.5 BPM slower. Breakdown 216-274, outro
371-420.

**Transition character:** gap of 5.5 BPM into 123 = classic echo_out
territory. Echo tail at outro (371-420) into massano's entry at 178
buildup creates a perfect tempo-shift bridge. Filter sweep across
5.5 BPM is also feasible with sync.

### `daryl_dixon_coming_home` — 8A, energy 4, deep-house  (G)
Low energy (4) unusual for our fixture. Only 3 sections detected
(likely minimal structure). Outro 158-179 on a 179s track — this is
a short interlude track.

**Transition character:** low energy → acts as a "palate cleanser"
between peak moments. 8A is 1 step from 9A. Good for slowing the
floor down with crossfade; NOT for peak-time blending.

### `yotto_aviate` — 4A, energy 9, progressive-house  (K)
Yotto's emotional big-room prog. Breakdowns 66-98, 190-258 (that's a
68-second melodic breath), 359-369. Outro 369-419.

**Transition character:** 4A matches anyma_the_sign (also 4A, 123
BPM) — a same-key 5.5 BPM up transition. Filter sweep or echo_out.
The 190-258 breakdown is long enough for the incoming track to
establish itself before the next drop.

---

## 136 BPM cluster (psytrance + dark-techno peak)

### `charlotte_de_witte_doppler` — 6A, energy 10, dark-techno  (K)
Peak-hour industrial dark-techno. Energy 10 is the fixture's ceiling.
Extended breakdown 200-260 (60s of tension). Outro 383-434.

**Transition character:** same 6A key as consciousness. 136→123 = 13
BPM down, requires echo_out or hard_cut — no beatmatched technique
survives that gap. Doppler as outgoing into consciousness creates a
"come down from peak" moment that's musically valid because the 6A
key continuity preserves harmonic narrative.

### `astrix_heart` — 9B, energy 9, psytrance  (K)
Progressive psytrance classic. 136 BPM rolling bassline.
Breakdowns 168-206 and a massive 250-344 mid-track journey. Outro 425-482.

**Transition character:** psytrance grids are unforgiving — you can't
blend smoothly from 123 BPM techno. Must use echo_out or hard_cut.
Within the 136 pocket (doppler, armin_vini_vici), bass_swap works
because the grids align.

### `armin_van_buuren_vini_vici_feat_hilight_tribe_grea` — 9A, energy 9, psytrance  (K)
"Great Spirit" — tribal-psytrance crossover anthem. 136 BPM, 9A key
(same as massano but a full 13 BPM faster). Outro 403-458.

**Transition character:** the 9A-at-136 twin of massano's 9A-at-123.
Into this track from massano: echo_out bridges the tempo — the 9A key
continuity keeps it musical despite the 13 BPM jump. Classic "drive
into peak time" move.

---

## Extreme outliers

### `bonobo_kerala` — 83 BPM, 6A, energy 8, downtempo  (K)
Bonobo's "Kerala" — UK broken-beat downtempo. 83 BPM is WAY outside
dance tempos. Same 6A key as consciousness and doppler.

**Transition character:** this is NOT an in-mix transition source —
it's a SET-level segue. Use it as a cooldown or opener; transitioning
out of it into 123-136 territory requires hard_cut at silence or a
very long echo_out bridge. No beatmatched technique survives a 40-53
BPM gap. Same-key continuity (6A) is the only harmonic thread.

---

## Quick reference tables

### By BPM
| BPM    | Tracks                                          |
|--------|-------------------------------------------------|
| 83     | bonobo_kerala                                   |
| 117.5  | kinky_sound, daryl_dixon, yotto_aviate          |
| 123    | massano, raul_music, anyma_sign, anima_moon,    |
|        | consciousness, kasst, maceo_plex, opus,         |
|        | camelphat_cola                                  |
| 136    | charlotte_doppler, astrix, armin_vini_vici      |

### By Camelot key
| Key | Tracks                                       | Harmonic family    |
|-----|----------------------------------------------|--------------------|
| 3A  | camelphat_cola                                | isolated (6 steps) |
| 4A  | anyma_sign, yotto_aviate                      | isolated (5 steps) |
| 6A  | consciousness, charlotte_doppler, bonobo      | inner-wheel center |
| 8A  | daryl_dixon                                    | adjacent to 9A     |
| 9A  | massano, raul, kinky, maceo, armin_vini_vici | core cluster       |
| 9B  | astrix                                         | relative of 9A     |
| 10B | anima_moon                                     | adjacent to 11B    |
| 11A | kasst                                          | adjacent to 9A     |
| 11B | eric_prydz_opus                                | adjacent to 10B    |

### Natural transition durations (at 123 BPM, 32-beat phrase)
| Beats | Seconds | When to use                                    |
|-------|---------|-------------------------------------------------|
| 8     | ~4      | Quick bass_swap between locked tracks          |
| 16    | ~8      | Standard peak-time bass_swap / half-phrase      |
| 32    | ~16     | Full phrase — crossfade, filter_sweep, echo_out |
| 64    | ~31     | Long atmospheric blend (Opus-style)             |

A phrase at 123 BPM = (60/123)×32 = 15.6s. So "8-second transition"
= half phrase = perfectly aligned.
