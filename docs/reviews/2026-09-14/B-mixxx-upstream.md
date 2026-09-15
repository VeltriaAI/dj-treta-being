# Track B — Bringing upstream Mixxx into the treta fork (assessment, 2026-09-14)

Read-only assessment. Nothing merged, rebased, stashed or reset. Working tree untouched
(uncommitted: `CMakeLists.txt`, `src/library/djtreta/dlgdjtretachat.{cpp,h}` — the
optional-WebSockets patch, 39 lines, still present). `upstream` remote already existed;
`git fetch upstream --tags` run.

## 1. Where we are

| | |
|---|---|
| Repo / branch | `~/workspace/mixxx-treta`, checked-out branch `build/optional-websockets` (== `treta`, same HEAD `be116ddfca`) |
| Forked from | **upstream/main** (not 2.6). Merge-base `ac984506d2`, 2026-05-21 ("sync-branch-2.6-to-main") |
| Our version | `project(mixxx VERSION 2.7.0)` + `alpha` — i.e. **2.7.0-alpha**. The memory-graph note `djtreta-mixxx-3-migration.md` says "3.0"; that is wrong — upstream has no 3.0. Fix the note. |
| vs upstream/main | **1031 behind, 40 ahead** (39 non-merge; ~7 are duplicates from the 2026-05 merge `259e1dcb9a` — real patch set ≈ 32 commits) |
| vs upstream/2.6 | 521 behind, 817 ahead (meaningless — we carry main-only history; 2.6 is not our ancestor) |
| upstream/main tip | `4b80e23a89`, 2026-09-13 |
| Latest upstream **release** | **2.5.6** (2026-03-27). 2.6 branch is still `2.6.0-beta` (no 2.6.0 tag); tags `2.6-beta` (2025-05), `2.7-alpha` (2025-04). CHANGELOG: 2.6.0 and 2.7.0 both "Unreleased". |
| Our delta | 23 files, +23,497 / −14 (≈23k of that is vendored `lib/httplib/httplib.h`) |
| Upstream churn since fork | 772 files, +119,728 / −85,322 |

Files we touch: `.github/workflows/{build.yml,release-treta.yml}`, `CMakeLists.txt`, `lib/httplib/httplib.h`,
`res/controllers/Pioneer-DDJ-FLX4{-script.js,.midi.xml}`, `res/images/library/ic_library_djtreta.svg`,
`res/mixxx.qrc`, `res/qml/{SarathiPanel.qml,main.qml}`, `src/api/apiserver.{h,cpp}`,
`src/coreservices.{cpp,h}`, `src/library/library.cpp`, `src/library/djtreta/*` (8 files).

## 2. Conflict forecast (`git merge-tree --write-tree upstream/main HEAD`, no worktree change)

Only **2 files conflict**; everything else auto-merges.

| File | Upstream commits since fork | Verdict |
|---|---|---|
| `CMakeLists.txt` | 82 | **auto-merges** (our HTTPAPI option + src/api sources + djtreta sources). The uncommitted optional-WebSockets hunk sits next to `QT_EXTRA_COMPONENTS` (upstream added `QuickDialogs2` there) — expect a trivial re-apply, not a conflict. |
| `.github/workflows/build.yml` | 13 | **CONFLICT** (lines ~240): upstream bumped `cmake-version: "3.22.x"`; we replaced the action with "use system cmake". Take ours (or upstream's — either builds on macos-15). CI-only, does not affect local build. |
| `res/qml/main.qml` | 9 | **CONFLICT** (whole body): upstream moved the window body into `res/qml/MainWindow.qml` (GSoC LateNightQML). Our 3-line `Skin.SarathiPanel {}` insert has no anchor left. Resolution: take upstream's `main.qml`, re-add the panel in `MainWindow.qml`. Low priority — Manish's call is the QWidget LateNight skin, not QML; the Sarathi QML panel is not on the live path. |
| `src/coreservices.{cpp,h}` | 8 / 1 | auto-merges (our `#ifdef __HTTPAPI__` block is additive) |
| `src/library/library.cpp` | 0 | clean (our `addFeature(new DJTretaFeature…)`) |
| `src/library/djtreta/*`, `src/api/*`, `lib/httplib/*` | 0 | ours only — untouched upstream |
| `res/controllers/Pioneer-DDJ-FLX4*` | 0 | clean; controller JS API churn is 3 commits (none breaking for a MIDI script) |
| `src/library/` (dir) | 114 | see compile break below |
| `src/controllers/` (dir) | 35 | we don't touch C++ here — no conflict |
| `res/skins/LateNight` | 20 | we don't touch — free upgrades |

**One certain compile break (not a git conflict):** `BrowseTableModel` constructor gained a 4th
argument `const char* nameSpace` (upstream `e42e3c744b`). `djtretafeature.h:63` holds a
`BrowseTableModel m_browseModel;` member — its initializer in `djtretafeature.cpp` must pass a
namespace string (e.g. `"[DJTreta]"`). Also `slotInsert` now takes `BrowseTableItems` (only matters
if we connect to it). Other headers we include that changed (`trackcollection.h`, `trackcollectionmanager.h`,
`basetrackplayer.h`, `track.h`) only *added* members — `pPlayerManager->getPlayer()` and the
TrackCollectionManager calls we use survive. Budget for a handful more of these once the compiler runs.

## 3. Build environment changes (this is the expensive part)

- **macOS buildenv name changed**: `mixxx-deps-2.6-arm64-osx-aa78b5a` → **`mixxx-deps-2.7-arm64-osx-1c20f84a`**
  (branch `2.7`, SHA256 `d0c1df3b…`). A new ~3.2 GB zip → ~11 GB extracted. The old aa78b5a is
  not on disk anymore either, so *any* rebuild (even without upgrading) needs a buildenv download.
- **Qt**: no new hard minimum. Upstream gates are the same `6.4/6.7/6.8/6.10` feature checks we already have. The
  2.7 vcpkg manifest (`mixxxdj/vcpkg` branch 2.7) ships qtbase, qtdeclarative, qtmultimedia, qtsvg,
  qt5compat, qtapplicationmanager, qttranslations, qtkeychain-qt6 — **still no `qtwebsockets`**. Trap 1 from
  2026-08-13 stands; the uncommitted optional-WebSockets patch remains required.
- Upstream CI now builds on `macos-15` / `macos-15-intel`; CMake ≥ 3.22 in CI (our CMakeLists still says 3.21).
- FFmpeg find-module renamed (`find_package(FFmpeg COMPONENTS AVCODEC …)`) — inside the buildenv, no action.

## 4. Does upstream make our patches redundant? **No.**

- HTTP/WebSocket control API: `git grep` on upstream/main for `QHttpServer|QWebSocketServer|QTcpServer|REST|json-rpc`
  in `src/` → **zero hits**. No built-in remote-control API. `src/api/` is still ours alone.
- Streaming / cloud libraries: only hit is `findonwebmenusoundcloud.cpp` — a "Find on web" context-menu
  link (opens a browser search; exists alongside Discogs/LastFM). **No Beatport/Tidal/SoundCloud/Spotify/Apple Music
  streaming or cloud-library integration** in code or CHANGELOG. Our YouTube-search/remote-rows cockpit has no upstream equivalent.
- What we *would* gain from main since May: ~60 library commits, QML/LateNightQML work, splash screen, waveform
  and engine fixes, keylock fix, 2.6→main syncs. Nothing DJ Treta is blocked on today.

## 5. Recommendation

**(a) Strategy: merge upstream/main into our branch** (not rebase, not cherry-pick).
- History is already merge-shaped (`259e1dcb9a`) and contains duplicate commits; a rebase would replay ~40
  commits incl. duplicates and stop repeatedly. Cherry-picking ~32 patches onto fresh upstream is what was done
  in May and cost a day. A merge touches 2 conflict files + 1 constructor fix and keeps every SHA the stick
  restore-kit and notes refer to.
- If we ever want clean history for an upstream PR of the API server, do that as a separate squashed branch later.

**(b) Target: `upstream/main`** (2.7.0-alpha). We are already on main lineage; `2.6` is still beta with no
release, so "stable 2.6.x" does not exist as a downgrade target, and moving there means cherry-picking onto a
branch that never had our history. **Do not merge upstream/2.6** (main already contains it).

**(c) Plan — only when disk allows (see d):**
1. Protect WIP: `git checkout -b wip/optional-websockets-2026-09-14 && git add CMakeLists.txt src/library/djtreta/dlgdjtretachat.{cpp,h} && git commit -m "build: make Qt WebSockets optional (DJTRETA_HAVE_WEBSOCKETS)"`. Tag safety point: `git tag treta-pre-upstream-2026-09-14 be116ddfca`. Push both.
2. `git checkout -b treta-upstream-2026-09 wip/optional-websockets-2026-09-14 && git merge upstream/main`.
3. Resolve `build.yml` (keep ours), `main.qml` (take upstream; re-add `Skin.SarathiPanel` in `res/qml/MainWindow.qml` or drop for now).
4. Fix `djtretafeature.cpp` `m_browseModel(...)` → add 4th arg `"[DJTreta]"`; check `slotInsert` signature if referenced.
5. Buildenv: `curl -L --retry-all-errors -C - -o buildenv/mixxx-deps-2.7-arm64-osx-1c20f84a.zip https://downloads.mixxx.org/dependencies/2.7/macOS/mixxx-deps-2.7-arm64-osx-1c20f84a.zip` (cmake's `file(DOWNLOAD)` truncates — May lesson), verify SHA256 `d0c1df3b8c5414ee1d7e444ebbdf1215e3e3e1585b9255c607720af2438b7b96`, unzip, **delete the zip**.
6. `source tools/macos_buildenv.sh setup` (no pipe), then `cmake -B build -G Ninja -DHTTPAPI=ON && cmake --build build -j8`. Expect the WARNING "Qt WebSockets NOT found" — that is correct.
7. Verify: `strings build/mixxx | grep -c /api/status` (must be ≥ 1); start Mixxx with `--settingsPath`, `curl localhost:7778/api/status`, open DJ Treta sidebar, check cockpit polls :7779. Rebuild the stick restore-kit tarball and **test-extract it**.
8. Merge into `treta`, push, update `djtreta-mixxx-3-migration.md` (version is 2.7.0-alpha, buildenv 1c20f84a).

**(d) Effort and disk.**
- Effort: merge + conflicts + constructor fix ≈ 1–2 h; download 3.2 GB; full static build ≈ 1–1.5 h on the M-series Mac; verification + stick kit ≈ 30 min. ~half a day, mostly waiting.
- Disk: **the rebuild cannot happen on the internal disk right now.** `df` at time of check: **4.2 GB free** (98% used; the "12 GB" figure is stale). Needs ≈ 3.2 GB zip + ~11 GB buildenv + ~3 GB build ≈ **17 GB peak (≈14 GB after deleting the zip)**. No external volume is mounted (`/Volumes/` has only Macintosh HD; the DJ-TRETA stick is not plugged in, and FAT32 cannot hold the buildenv's symlinks anyway). Options: free ≥ 20 GB, or put `buildenv/` + `build/` on an APFS/exFAT external SSD (the PortableSSD that holds the library, when it is plugged in) via symlink or `-B /Volumes/…/mixxx-build`.
- Nothing forces this now: upstream adds no feature DJ Treta needs, and our patches are not redundant. The one thing worth doing today, disk-free, is **step 1** (commit the WIP to a branch) so the optional-WebSockets work stops living only in the working tree.
