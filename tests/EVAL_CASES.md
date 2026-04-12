# DJ Treta — Complete Eval Test Cases

## How to Read This

Each test case has: ID, category, what we test, input context, expected behavior, and which genres it applies to.

**Genres tested:** Melodic Techno (MT), Dark Techno (DT), Progressive House (PH), Deep House (DH), Psytrance (PSY), Ambient/Chill (AMB)

---

## 1. TRANSITION TIMING — When to Mix

| ID | Test | Input Context | Expected | Genres | Source |
|----|------|---------------|----------|--------|--------|
| TT-01 | Mix at breakdown | Track at 70%, next section=BREAKDOWN(3) | Call schedule_transition at breakdown start | MT, PH, DH | Pro standard |
| TT-02 | Mix at outro | Track at 85%, next section=OUTRO(2) | Call schedule_transition at outro start | All | Safest point |
| TT-03 | Wait during drop | Track at 60%, current=DROP(9) | Say "waiting" — never mix during a drop | MT, DT, PSY | Energy rule |
| TT-04 | Wait during buildup | Track at 75%, current=BUILDUP(7) | Say "waiting" — buildup leads to drop | All | Ruins the moment |
| TT-05 | Wait if too early | Track at 30%, idle ready | Say "waiting" — let track develop | All | Patience |
| TT-06 | Force at <30s | Track has 25s remaining, idle ready | Auto-transition fires (P2 safety) | All | Music must never stop |
| TT-07 | Phrase alignment | Transition at position 180s (8-bar boundary at 128 BPM) | at_position aligns to phrase boundary | MT, DT, PH | 8-bar phrase rule |

## 2. TRANSITION TECHNIQUE — How to Mix

| ID | Test | Input Context | Expected Technique | Genres | Source |
|----|------|---------------|-------------------|--------|--------|
| TC-01 | Default crossfade | Two melodic tracks, similar energy | crossfade, duration 30-60s | MT, PH, DH | Long blends for melodic |
| TC-02 | Bass swap at high energy | Both tracks energy >7, same BPM | bass_swap | MT, DT | Avoid two basslines |
| TC-03 | Filter sweep for tension | Track with lots of elements, building tension | filter_sweep | PH, MT | Progressive reveal |
| TC-04 | Hard cut for genre change | Mood change requested, BPM gap >10 | hard_cut | DT, PSY | Clean genre pivot |
| TC-05 | Echo out for tempo change | Transitioning to significantly different BPM | echo_out | All | Creates space |
| TC-06 | Short blend for psytrance | PSY tracks at 140+ BPM | Duration <=20s or hard_cut | PSY | Quick cuts at drops |
| TC-07 | Long blend for deep house | Deep house at 120 BPM, chill vibe | Duration 45-90s | DH, AMB | Ultra-long for atmosphere |

## 3. TRACK SELECTION — What to Play Next

| ID | Test | Input Context | Expected | Genres | Source |
|----|------|---------------|----------|--------|--------|
| TS-01 | BPM within ±6 | Current track 125 BPM | Next track 119-131 BPM | All | Ideal range |
| TS-02 | BPM never >±10 | Current track 125 BPM | Reject tracks outside 115-135 | All | Hard limit |
| TS-03 | Key compatible (Camelot ±1) | Current key 8A | Accept 7A, 8A, 9A, 8B | MT, PH, DH | Harmonic mixing |
| TS-04 | Key ignored for short blend | Hard cut transition | Any key acceptable | DT, PSY | No harmonic content during cut |
| TS-05 | Energy jump ≤2 | Current energy 6 | Next track energy 4-8 | All | No jarring jumps |
| TS-06 | No repeat in set | Track X already played | Never load Track X again | All | Variety |
| TS-07 | Artist diversity | Last track by Anyma | Don't play Anyma next | All | Don't repeat same artist back to back |
| TS-08 | Mood match | Mood="psychill" | Search/select psychill tracks, NOT melodic-techno | All | Explicit mood overrides |
| TS-09 | Respect directive | Planner directive: "find 3 bhojpuri tracks" | Search for bhojpuri | All | Being → Planner |
| TS-10 | Use library first | 5 compatible tracks in library | Pick from library before YouTube search | All | Cost + latency |

## 4. ENERGY ARC — Set-Level Energy Management

| ID | Test | Input Context | Expected | Genres | Source |
|----|------|---------------|----------|--------|--------|
| EA-01 | Opening track calm | First track of set | Energy ≤5 | All | Warm-up rule |
| EA-02 | No peak in first 25% | Set just started, 3 tracks in | Don't play energy 9-10 track | All | Build, don't peak early |
| EA-03 | Max 3 consecutive peaks | 3 tracks at energy 8-10 already played | Next must be energy ≤7 | All | Release after peak |
| EA-04 | Drop ≥2 after peak | Just played 3 peak tracks | Drop energy by at least 2 levels | All | Contrast is key |
| EA-05 | Main peak at 60-75% | 2-hour set, 75 min in | This is the time for biggest tracks | MT, DT | Set structure |
| EA-06 | Closing track not peak | Last track of set | Energy ≤7 | All | Resolve the journey |
| EA-07 | Monotone detection | 5 consecutive tracks same energy (±1) | Agent should vary | All | Boring = bad |
| EA-08 | Warm-up phase ≥15% | Set duration 120 min | First 18+ min should be energy ≤6 | All | Don't rush |

## 5. BPM MANAGEMENT — Set-Level Tempo

| ID | Test | Input Context | Expected | Genres | Source |
|----|------|---------------|----------|--------|--------|
| BM-01 | BPM keep after transition | Default transition | bpm_after="keep" — no reset | All | Set-level BPM |
| BM-02 | BPM range ≤15 in 2hr set | Full set analysis | Total BPM span ≤15 BPM | MT, PH, DH | Natural progression |
| BM-03 | BPM trends up first 75% | Set 60 min in | Average BPM should be higher than opening | MT, DT | Energy builds |
| BM-04 | Emergency resets rate | Cold start emergency play | rate_ratio=1.0 on deck | All | Clean slate |
| BM-05 | No BPM snap | After transition | No sudden BPM change audible | All | Smooth always |
| BM-06 | Per-genre BPM range | Genre=psytrance | BPM 138-148 acceptable | PSY | Genre-specific |

### Genre BPM Ranges

| Genre | Typical BPM | Acceptable Range |
|-------|------------|-----------------|
| Deep House | 118-124 | 115-128 |
| Progressive House | 122-128 | 120-132 |
| Melodic Techno | 120-128 | 118-132 |
| Dark Techno | 128-140 | 125-145 |
| Psytrance | 138-148 | 135-155 |
| Ambient/Chill | 80-110 | 70-120 |

## 6. HARMONIC MIXING — Key Compatibility

| ID | Test | Input Context | Expected | Genres | Source |
|----|------|---------------|----------|--------|--------|
| HM-01 | Same key always safe | 8A → 8A | Compatible (distance 0) | All | Same key |
| HM-02 | Adjacent key safe | 8A → 9A or 7A | Compatible (distance 1) | All | One note different |
| HM-03 | Relative major/minor safe | 8A → 8B | Compatible (same number) | All | Smooth blend |
| HM-04 | Far key rejected for blend | 8A → 3A, long crossfade | Warn: key clash on long blend | MT, PH, DH | Harmonic rule |
| HM-05 | Far key OK for hard cut | 8A → 3A, hard_cut | Acceptable — no harmonic overlap | DT, PSY | Quick cuts OK |
| HM-06 | Percussion-only = any key | Mix during percussion intro (no melody) | Any key acceptable | All | Pivot point |
| HM-07 | 70% harmonic in set | Full set analysis | At least 70% of blends Camelot ≤1 | MT, PH, DH | Pro standard |

## 7. BEING AGENT — Conversation + Directives

| ID | Test | Input Message | Expected | Source |
|----|------|---------------|----------|--------|
| BE-01 | Mood change | "play some psytrance" | set_mood("psytrance") + set_planner_directive | Core behavior |
| BE-02 | Seed track | "play Argy - Ketuvim" | search_music("Argy Ketuvim") | Seed track mode |
| BE-03 | Energy up | "energy badhao" | set_dj_directive mentioning energy/bass_swap | Being directs DJ |
| BE-04 | Just conversation | "what are you playing?" | Conversational response, NO tool calls | Don't over-act |
| BE-05 | Readonly mode | Message + READONLY tag | Respond, NO directive/mood tools called | Live web safety |
| BE-06 | Hindi response | "bhojpuri bajao yaar" | Uses "aap" form, not "tu/tum" | Language respect |
| BE-07 | Implicit like | "this track is fire" | save_learning with positive context | Feedback recognition |
| BE-08 | Implicit skip | "not feeling this one" | Suggests skip or mood change | Negative feedback |
| BE-09 | Don't override mood | Mood=psychill, learnings say melodic-techno | Follow psychill, NOT melodic-techno | Explicit > learned |

## 8. CONSCIOUSNESS — Self-Reflection

| ID | Test | Input Context | Expected | Source |
|----|------|---------------|----------|--------|
| CO-01 | Calm = HEARTBEAT_OK | Set running well, no issues | "HEARTBEAT_OK" | Don't act unnecessarily |
| CO-02 | Concrete proposal | Many auto-transitions detected | propose_change with specific file + line reference | Not abstract |
| CO-03 | No gibberish | Long-running session | Output >30% unique words | Degeneration guard |
| CO-04 | Genuine learning | Energy flat for 5 tracks | save_learning with specific insight | Meaningful only |
| CO-05 | Stay grounded | Open "think freely" prompt | Propose DJ-related improvements only | No body tracking |
| CO-06 | No spam analysis | Just reflected 2 ticks ago | HEARTBEAT_OK, don't repeat same analysis | Rate limit |

## 9. EMERGENCY RECOVERY

| ID | Test | Input Context | Expected | Source |
|----|------|---------------|----------|--------|
| ER-01 | Silence detection | No deck playing | Emergency play within 5s | P1 priority |
| ER-02 | Recovery track loads | Library empty after emergency | Download + load within 90s | Music never stops |
| ER-03 | Rate reset on emergency | Emergency play triggers | rate_ratio=1.0 on emergency deck | Clean BPM |
| ER-04 | Recovery BPM ≤±8 | Current set at 126 BPM | Emergency track within 118-134 BPM | Don't jar the crowd |
| ER-05 | Double emergency guard | Emergency already running | Don't start second emergency | Prevent chaos |

## 10. ANTI-PATTERNS — Things That Must NEVER Happen

| ID | Anti-Pattern | Detection | Severity |
|----|-------------|-----------|----------|
| AP-01 | Two basslines playing | Both decks at full bass EQ during crossfade | Critical |
| AP-02 | BPM jump >10 between tracks | |current_bpm - next_bpm| > 10 | High |
| AP-03 | Peak track as opener | First track of set has energy ≥8 | High |
| AP-04 | 4+ consecutive peaks | 4 tracks in a row at energy 8-10 | Medium |
| AP-05 | Key clash on long blend | Camelot distance >2 during 16+ bar crossfade | High |
| AP-06 | Track repeat in set | Same track played twice | Critical |
| AP-07 | Transition mid-phrase | schedule_transition at non-phrase-boundary position | Medium |
| AP-08 | Monotone energy 5+ tracks | Same energy level (±1) for 5+ consecutive tracks | Medium |
| AP-09 | Ignore crowd for 3+ tracks | 3 consecutive negative signals without adjustment | High |
| AP-10 | Music stops for >5s | Silence gap between tracks | Critical |

## 11. GENRE-SPECIFIC BEHAVIOR

### Melodic Techno (120-128 BPM)

| ID | Test | Expected |
|----|------|----------|
| MT-01 | Blend duration | 30-60s crossfade (16-32 bars) |
| MT-02 | Harmonic mixing strict | Camelot distance ≤1 for all blends |
| MT-03 | Breakdown transitions | Schedule at breakdowns, not drops |
| MT-04 | Energy arc wave | Sinusoidal, not linear upward |

### Dark Techno (128-140 BPM)

| ID | Test | Expected |
|----|------|----------|
| DT-01 | Shorter transitions | 15-30s, more aggressive |
| DT-02 | Bass swap preferred | Over crossfade for high energy |
| DT-03 | Hard cuts acceptable | At drops for maximum impact |
| DT-04 | Higher sustained energy | Energy 6-9, fewer dips than melodic |

### Progressive House (122-128 BPM)

| ID | Test | Expected |
|----|------|----------|
| PH-01 | Extra long blends | 45-120s transitions |
| PH-02 | Atmospheric focus | Filter sweeps for texture |
| PH-03 | Gradual energy build | Slow, patient energy arc |

### Psytrance (138-148 BPM)

| ID | Test | Expected |
|----|------|----------|
| PSY-01 | Quick transitions | ≤20s or hard cuts |
| PSY-02 | Drop-based mixing | Transition at drops, not breakdowns |
| PSY-03 | High BPM tolerance | Accept 135-155 BPM range |
| PSY-04 | Key less important | Speed makes key clashes less noticeable |

### Deep House (118-124 BPM)

| ID | Test | Expected |
|----|------|----------|
| DH-01 | Ultra smooth blends | 45-90s, never jarring |
| DH-02 | Groove priority | Maintain groove, never break the pocket |
| DH-03 | Vocal awareness | Don't cut during vocal phrases |

### Ambient/Chill (80-110 BPM)

| ID | Test | Expected |
|----|------|----------|
| AMB-01 | Texture blending | 60-180s, layers of texture |
| AMB-02 | No drops/peaks | Energy stays 2-5 range |
| AMB-03 | BPM very flexible | Wider BPM tolerance (±15) |

---

## Summary Stats

| Category | Test Count |
|----------|-----------|
| Transition Timing | 7 |
| Transition Technique | 7 |
| Track Selection | 10 |
| Energy Arc | 8 |
| BPM Management | 6 |
| Harmonic Mixing | 7 |
| Being Agent | 9 |
| Consciousness | 6 |
| Emergency Recovery | 5 |
| Anti-Patterns | 10 |
| Genre: Melodic Techno | 4 |
| Genre: Dark Techno | 4 |
| Genre: Progressive House | 3 |
| Genre: Psytrance | 4 |
| Genre: Deep House | 3 |
| Genre: Ambient/Chill | 3 |
| **TOTAL** | **96** |

---

## Key Numbers Reference

| Parameter | Value | Source |
|-----------|-------|--------|
| Max BPM delta per transition | 10 BPM (ideal ≤6) | DJ.Studio, Digital DJ Tips |
| Phrase length | 8 bars / 32 beats | Universal EDM structure |
| Transition length (melodic) | 16-32 bars (30-60s) | Pro standard |
| Max consecutive peak tracks | 3 | Mixed In Key |
| Energy drop after peak | ≥2 levels | DJ.Studio |
| Main peak position in set | 60-75% through | Set structure research |
| Camelot max distance (long blend) | 1 | Harmonic mixing standard |
| Recovery time after problem | ≤8 bars | DJ TechTools |
| Tracks per hour (house/techno) | 8-15 | Pioneer DJ |
| Opening track energy max | 5 | Set structure |
| 70% harmonic mixing threshold | ≥70% of blends Camelot ≤1 | Pro standard |
| Warm-up phase | ≥15% of set duration | DJoid |
