# Track C — Can DJ Treta play Spotify / Apple Music inside Mixxx "like rekordbox does"?

Date: 2026-09-14 · Author: Treta · Status: research only, no code changes
Scope: web sources as of Sep 2026 + local read of `~/workspace/mixxx-treta` (fork of mixxxdj/mixxx, GPLv2).

## 1. State of streaming in DJ software (Sep 2026)

**rekordbox 7** supports six services: Beatport Streaming (LINK), Beatsource (absorbed into Beatport, Mar 2026), TIDAL,
SoundCloud Go+, Apple Music (7.1+), Spotify. Every one is a *partner integration*: the vendor's closed client/SDK, an
encrypted cache at most (Beatport/TIDAL offline lockers; Apple Music and Spotify have **no** offline locker, online only),
and the host app is certified by the service. There is no self-serve SDK for any of them. [1][2][3]

**Spotify** — the 2025 announcement *did ship*: desktop rekordbox 7 / Serato 3.3.5 / djay on 24 Sep 2025, mobile
rekordbox + djay on 11 Dec 2025. 51 markets. Requires Premium; **in India it requires the Premium Platinum tier** (Spotify's
own India page lists India, Indonesia, Saudi, South Africa, UAE under "Premium Platinum third-party DJ integration"). Online
only. Spotify's support page: "Public performance, including playing mixes in clubs, venues, events, or livestreams isn't
permitted." Spotify names exactly three partners and offers no path for other apps; its Developer Terms forbid
"modifying, editing, altering, creating derivative works" of content and allow audio caching only for approved conditional
downloads — i.e. no tempo/pitch/loop on Spotify audio outside a certified partner. Spotify pulled out of DJ apps in
July 2020 for the same licensing reasons; nothing prevents a repeat. [4][5][6][7][8]

**Apple Music** — "DJ with Apple Music" launched 25 Mar 2025 with AlphaTheta (rekordbox), Serato, inMusic (Engine DJ,
Denon, Numark, Rane) and Algoriddim djay Pro. Subscription required, no offline locker, no stems. The only public
developer surface is **MusicKit**, which exposes a controllable player, never decoded audio — useless for a deck. There is
no application form for the DJ program; partners were hand-picked ("the industry's leading DJ software and hardware"). [9][10][11]

**Beatport / SoundCloud** — Beatport's v4 API is partner-gated (portal login, NDA-style); its published partner list
(Serato, rekordbox, Traktor, Engine, DJUCED, VirtualDJ, djay, Cross DJ, DJ.Studio…) does not include Mixxx. SoundCloud
Go+ streaming inside DJ apps exists only in Serato and rekordbox; the public SoundCloud API forbids stream capture and
does not carry the Go+ (major-label) catalogue. [12][13][14]

## 2. Mixxx side

- **Code:** upstream and our fork contain **zero** streaming-service code. `grep -ril 'spotify|beatport|tidal|soundcloud'`
  in `src/` hits only `src/widget/findonwebmenuservices/findonwebmenusoundcloud.*` — a right-click "search this track on
  SoundCloud in the browser" link (PR #4772, 2022). `src/library/` has importers for iTunes, Rekordbox, Serato, Traktor,
  Banshee, Rhythmbox — all local-file libraries. Third-party blogs claiming "Mixxx has native SoundCloud Go+" are wrong;
  those pages sell DRM-stripping converters, which are illegal under Spotify/Apple terms and out of the question for us.
- **Maintainer position:** Issue #6277 "Stream Mix integration (youtube, spotify…)" open since 2012, labelled
  `confirmed/cloud`, no work. Issue #14918 "Beatport integration" (Jun 2025): core member Swiftb0y answered with the
  project's 2020 post *Why Mixxx will not support streaming*; member acolombier: "I don't think we should integrate any
  providers within the software directly." The 2020 post's reasons: partners must disable recording/broadcast (core Mixxx
  features — "Mixxx will never work for a company against your wishes"), "all this hard work could be wasted at the whim
  of the streaming company" (Spotify 2020, Pulselocker shutdown), unreliable internet at gigs, and reputational risk from
  forks that strip the restrictions. [15][16][17]
- **The GPL point, verified:** Mixxx is GPLv2 (`COPYING`). GPLv2 §2(b)/§3 require the work "as a whole" to be
  licensed under the GPL with complete source; the only carve-out is for "major components (compiler, kernel, and so on)
  of the operating system". A vendor DRM SDK under NDA cannot be linked in and redistributed. So even if Spotify handed us
  an SDK, a Spotify-enabled Mixxx binary could not be shipped as Mixxx. The maintainers' reason is mainly the
  recording/whim/reliability argument above; the GPL makes it structurally impossible on top. [18]
- **The one live thread:** proposals PR #2 "Library plugins" (acolombier, draft, opened Mar 2024, last touched Dec 2025)
  proposes an out-of-process gRPC plugin so a provider could live outside the GPL boundary. His PoC plugin uses
  **librespot** (reverse-engineered Spotify client, Premium only) whose own README says "Using this code to connect to
  Spotify's API is probably forbidden by them." Not merged, not on a roadmap, and account-ban territory. [19][20][21]

## 3. Alternatives, ranked

| # | Option | Legal / DRM | Effort | Verdict |
|---|--------|-------------|--------|---------|
| (a) | **Drive rekordbox** (Spotify+Apple Music via the certified host) — Track D | Clean if Manish holds Premium Platinum / Apple Music; online-only; no public performance/livestream of Spotify audio | Track D scope | The only "like rekordbox does" answer *is* rekordbox |
| (d) | **Kavya originals + CC0/CC-BY catalogue** as "our library" | Fully clean; recordable, streamable, sellable | Low — `dj_generate_track` exists; add a curated CC folder + licence manifest | **Do this.** It is also the only story where Treta owns the music |
| (b) | **Route Spotify/Apple Music app → BlackHole/Loopback → Mixxx Aux input** | Personal practice fine; Spotify/Apple terms still ban public performance; macOS needs a virtual device (BlackHole free/GPL, Loopback ~$99) | 0.5–1 day routing; no beatgrid/seek/sync/waveform on that channel | Useful **only** for a B2B *practice* demo: Manish plays Spotify on the Aux, Treta beat-matches her local deck to it by ear — we'd need our own live BPM tracker on the aux tap (1–2 wks, fragile). Not a product feature |
| (c) | **Open-API catalogues** | Jamendo API v3 (CC, streamable, 35k req/mo free non-commercial, commercial = quote); FMA API shut down (contact them); Bandcamp has no public API; Beatport partner-only; SoundCloud public API = no Go+ and no capture | Jamendo: ~1 wk via our existing `djtretaremotemodel` remote-track path | Nice-to-have for breadth; not what Manish is asking for |

Mixxx has no native support for any row above; (b) uses its existing Aux input, (c)/(d) would go through our fork's
`src/library/djtreta/` remote model, which already fetches tracks from our daemon.

## 4. Verdict

**No — not now, and not on any roadmap.** Spotify and Apple Music inside a DJ app exist only as closed, certified
partner integrations (rekordbox, Serato, djay, Engine DJ); neither service offers an SDK or application path to other
apps, both are online-only, and Spotify explicitly forbids public performance/livestreams of those mixes. Mixxx has no
streaming code at all, its maintainers have said in writing (2020, re-affirmed Jun 2025) that they will not add providers,
and because Mixxx is GPLv2 a vendor DRM SDK could not be linked into it even if offered — so our fork cannot do it either
without becoming something that is not Mixxx and violating both licences. The only routes that hold up: (1) if the
requirement is literally "Spotify/Apple Music in a DJ deck", that is rekordbox with Premium Platinum — Track D; (2) for
a two-deck demo, a BlackHole aux feed from the Spotify app is a practice-room trick, not a feature; (3) the honest DJ
Treta story is *her* library — Kavya originals plus a CC-licensed catalogue — which is the one thing rekordbox can't say.
What to tell Manish: "Spotify/Apple Music in Mixxx is not possible, for licence reasons that no amount of engineering
fixes; if you want those catalogues on a deck, that's rekordbox (Track D). Treta's deck will play her own music."

## Sources

1. rekordbox streaming FAQ — https://rekordbox.com/en/support/faq/streaming-5/
2. Digital DJ Tips, "Best streaming services for DJs 2026" — https://www.digitaldjtips.com/best-music-streaming-services/
3. Crossfader, Apple Music in DJ apps (no offline locker, no stems) — https://wearecrossfader.co.uk/blog/apple-music-update/
4. Spotify Newsroom, 24 Sep 2025 (+11 Dec 2025 mobile note) — https://newsroom.spotify.com/2025-09-24/dj-software-integration-premium/
5. rekordbox, Spotify support announcement + country list — https://rekordbox.com/en/2025/09/rekordbox-for-mac-win-spotify-support/
6. Spotify India DJ integration page (Premium Platinum) — https://www.spotify.com/in-en/dj-integration/
7. Spotify Support, "Using Spotify with DJ software" (online only, no public performance) — https://support.spotify.com/in-en/article/dj-integration/
8. Spotify Developer Terms (no derivative works; conditional downloads only) — https://developer.spotify.com/terms
9. Music Week, DJ with Apple Music launch, 25 Mar 2025 — https://www.musicweek.com/digital/read/dj-with-apple-music-launches-to-enable-subscribers-to-mix-their-own-sets/091655
10. rekordbox, Apple Music support (7.1+) — https://rekordbox.com/en/2025/03/apple-music-support/
11. Apple MusicKit (player only) — https://developer.apple.com/musickit/
12. Beatport Streaming partner list — https://stream.beatport.com/
13. Beatport API partner gating — https://github.com/api-evangelist/beatport ; https://partnerportal.beatport.com/hc/en-us
14. SoundCloud API Terms of Use — https://developers.soundcloud.com/docs/api/terms-of-use
15. mixxxdj/mixxx issue #6277 — https://github.com/mixxxdj/mixxx/issues/6277
16. mixxxdj/mixxx issue #14918 (maintainer replies) — https://github.com/mixxxdj/mixxx/issues/14918
17. "Why Mixxx will not support streaming" (2020-05-22) — https://github.com/Be-ing/website/blob/4090671ad668970e9eab8596a1a097443d369027/pages/news/2020-05-22-why-mixxx-will-not-support-streaming.html
18. GPLv2 §2(b), §3 — `~/workspace/mixxx-treta/LICENSE`; GNU GPL FAQ "GPLIncompatibleLibs" — https://www.gnu.org/licenses/gpl-faq.html#GPLIncompatibleLibs
19. mixxxdj/proposals PR #2 "Library plugins" — https://github.com/mixxxdj/proposals/pull/2
20. librespot Mixxx plugin PoC — https://github.com/acolombier/mixxx-plugin-librespot
21. librespot README disclaimer — https://github.com/librespot-org/librespot
22. BlackHole (macOS loopback driver) — https://github.com/existentialaudio/blackhole
23. Jamendo API terms — https://devportal.jamendo.com/api_terms_of_use ; FMA developers — https://freemusicarchive.org/app-developers ; Bandcamp API — https://bandcamp.com/developer
24. Local: `~/workspace/mixxx-treta/src/library/`, `src/widget/findonwebmenuservices/` (grep, 2026-09-14)
