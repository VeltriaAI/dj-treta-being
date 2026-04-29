#!/usr/bin/env bash
#
# DJClaw installer — the AI DJ Being you can run on your own machine.
#
# Usage:
#   curl -fsSL https://dj.treta.life/install.sh | sh
#
# Or, if you want to inspect first (recommended on any pipe-to-shell):
#   curl -fsSL https://dj.treta.life/install.sh -o install.sh
#   less install.sh    # read it
#   sh install.sh
#
# What gets installed (no sudo, all under your home dir):
#
#   ~/.local/share/djclaw/
#     ├── venv/                    Python virtualenv (replaceable)
#     ├── mixxx/<version>/         Mixxx-Treta binary (replaceable)
#     ├── mixxx/current → ...      symlink to active version
#     ├── db/djtreta.db            SQLite library  (preserved across re-runs)
#     ├── runtime/                 IPC files       (state.json, etc.)
#     └── version.txt              installed agent version
#
#   ~/.config/djclaw/
#     ├── config.yaml              user-editable    (preserved)
#     ├── secrets.env              API keys, 600    (preserved)
#     ├── litellm.yaml             provider routes  (preserved)
#     └── token                    /ws/agent/* gate (preserved)
#
#   ~/.local/bin/djclaw            CLI symlink      (replaceable)
#   ~/Music/DJTreta/               music library    (preserved)
#
# Re-running this script upgrades binaries + venv but never touches
# config, music, db, or secrets.
#
# Supported platforms:
#   - macOS arm64 (Apple Silicon)
#   - macOS x64   (Intel)
#   - Linux x64   (Debian/Ubuntu — uses dpkg-deb to extract; no apt needed)
#
# Operator mode (Linux only — runs DJ Treta as systemd services that push
# audio to upstream Icecast + relay state to a remote receiver):
#
#   curl -fsSL https://dj.treta.life/install.sh | sudo bash -s -- \
#       --operator \
#       --prefix /opt/djclaw \
#       --data-dir /var/lib/djclaw \
#       --config-dir /etc/djclaw \
#       --service-user djclaw \
#       --music-dir /var/lib/djclaw/music \
#       --stream-url icecast://source:PASS@127.0.0.1:8000/live \
#       --hls-dir /var/lib/djclaw/hls \
#       --relay-url wss://dj.treta.life/ws/relay \
#       --relay-token "<token>" \
#       --headless --yes

set -eu

# ─── Defaults (override via env before piping to sh) ───────────────────

DJCLAW_VERSION="${DJCLAW_VERSION:-9.1.2}"
MIXXX_VERSION="${MIXXX_VERSION:-v2.6.0-treta-5}"

DJCLAW_REPO="${DJCLAW_REPO:-VeltriaAI/dj-treta-being}"
DJCLAW_REF="${DJCLAW_REF:-v$DJCLAW_VERSION}"     # tag OR branch — used to fetch templates
MIXXX_REPO="${MIXXX_REPO:-VeltriaAI/mixxx}"

PREFIX="${DJCLAW_PREFIX:-$HOME/.local/share/djclaw}"
CONFIG_DIR="${DJCLAW_CONFIG_DIR:-$HOME/.config/djclaw}"
BIN_DIR="${DJCLAW_BIN_DIR:-$HOME/.local/bin}"

# ─── Operator-mode flags (filled in parse_args) ────────────────────────

OPERATOR=0
DATA_DIR=""
LOGS_DIR=""
SVC_USER=""
MUSIC_DIR=""
STREAM_URL=""
HLS_DIR=""
RELAY_URL=""
RELAY_TOKEN=""
HEADLESS=0
DISPLAY_NUM="${DISPLAY_NUM:-99}"
NON_INTERACTIVE=0
STREAM_PUBLIC_URL="${STREAM_PUBLIC_URL:-https://dj.treta.life/}"

# ─── Argument parsing ──────────────────────────────────────────────────

parse_args() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --operator)        OPERATOR=1 ;;
      --prefix)          PREFIX="$2"; shift ;;
      --data-dir)        DATA_DIR="$2"; shift ;;
      --config-dir)      CONFIG_DIR="$2"; shift ;;
      --bin-dir)         BIN_DIR="$2"; shift ;;
      --logs-dir)        LOGS_DIR="$2"; shift ;;
      --service-user)    SVC_USER="$2"; shift ;;
      --music-dir)       MUSIC_DIR="$2"; shift ;;
      --stream-url)      STREAM_URL="$2"; shift ;;
      --stream-public-url) STREAM_PUBLIC_URL="$2"; shift ;;
      --hls-dir)         HLS_DIR="$2"; shift ;;
      --relay-url)       RELAY_URL="$2"; shift ;;
      --relay-token)     RELAY_TOKEN="$2"; shift ;;
      --display-num)     DISPLAY_NUM="$2"; shift ;;
      --headless)        HEADLESS=1 ;;
      --yes|-y)          NON_INTERACTIVE=1 ;;
      --version)         DJCLAW_VERSION="$2"; DJCLAW_REF="v$2"; shift ;;
      --ref)             DJCLAW_REF="$2"; shift ;;
      --mixxx-version)   MIXXX_VERSION="$2"; shift ;;
      -h|--help)
        sed -n '2,40p' "$0" 2>/dev/null || true
        exit 0 ;;
      *) die "Unknown arg: $1 (use --help)" ;;
    esac
    shift
  done
}

# ─── Pretty output ─────────────────────────────────────────────────────

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  C_RESET='\033[0m'
  C_DIM='\033[2m'
  C_CYAN='\033[36m'
  C_GREEN='\033[32m'
  C_YELLOW='\033[33m'
  C_RED='\033[31m'
  C_BOLD='\033[1m'
else
  C_RESET=''; C_DIM=''; C_CYAN=''; C_GREEN=''; C_YELLOW=''; C_RED=''; C_BOLD=''
fi

say()  { printf '%b\n' "${C_CYAN}::${C_RESET} $*"; }
ok()   { printf '%b\n' "${C_GREEN}✓${C_RESET} $*"; }
warn() { printf '%b\n' "${C_YELLOW}!${C_RESET} $*"; }
die()  { printf '%b\n' "${C_RED}✗${C_RESET} $*" >&2; exit 1; }

banner() {
  printf '%b' "${C_BOLD}${C_CYAN}"
  cat <<'EOF'

  ╭───────────────────────────────╮
  │  DJClaw — install your own DJ │
  ╰───────────────────────────────╯

EOF
  printf '%b' "${C_RESET}"
}

# ─── Platform detection ────────────────────────────────────────────────

detect_platform() {
  local kernel arch
  kernel="$(uname -s)"
  arch="$(uname -m)"

  case "$kernel" in
    Darwin)
      case "$arch" in
        arm64)   PLATFORM=macos-arm64 ;;
        x86_64)  PLATFORM=macos-x64 ;;
        *)       die "Unsupported macOS arch: $arch" ;;
      esac
      ;;
    Linux)
      case "$arch" in
        x86_64)  PLATFORM=linux-x64 ;;
        aarch64|arm64)
          die "Linux arm64 isn't built yet. macOS arm64 + Linux x64 are the v1 platforms."
          ;;
        *)       die "Unsupported Linux arch: $arch" ;;
      esac
      ;;
    MINGW*|MSYS*|CYGWIN*)
      die "Windows isn't supported at v1. Mixxx CI builds an MSI but the agent stack hasn't been ported yet."
      ;;
    *)
      die "Unsupported kernel: $kernel"
      ;;
  esac
}

# ─── Dependency checks ─────────────────────────────────────────────────

require_cmd() {
  for cmd in "$@"; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
      die "Missing required command: $cmd"
    fi
  done
}

# Try explicit versioned binaries first (3.12 is the safe sweet spot —
# librosa / numba / llvmlite ship pre-built wheels for it). Fall back
# to `python3` only if no version-named binary is found, since on
# many systems `python3` is 3.9 (macOS pre-Sonoma, Ubuntu 22.04).
require_python_310() {
  local candidate
  for candidate in python3.12 python3.11 python3.13 python3.10 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
      if "$candidate" -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" 2>/dev/null; then
        PYTHON="$candidate"
        return 0
      fi
    fi
  done
  cat <<EOF >&2

  Python 3.10+ is required but I couldn't find it. Try one of:

    macOS:   brew install python@3.12
    Ubuntu:  sudo apt install python3.12 python3.12-venv
    pyenv:   pyenv install 3.12.7 && pyenv shell 3.12.7

  Then re-run this installer.

EOF
  exit 1
}

# ─── Mixxx binary ──────────────────────────────────────────────────────

mixxx_artifact_name() {
  case "$PLATFORM" in
    macos-arm64) echo "mixxx-${MIXXX_VERSION#v}-macosarm.dmg" ;;
    macos-x64)   echo "mixxx-${MIXXX_VERSION#v}-macosintel.dmg" ;;
    linux-x64)   echo "mixxx-${MIXXX_VERSION#v}-jammy_amd64.deb" ;;
  esac
}

# Resolve the actual download URL — release artifact filenames follow
# Mixxx CI's `cpack` slug, which doesn't perfectly match our
# `mixxx_artifact_name`. We list the release assets via the GitHub API
# and pick the one matching our platform.
fetch_mixxx_for_platform() {
  local mixxx_dir="$PREFIX/mixxx/$MIXXX_VERSION"

  if [ -d "$mixxx_dir" ]; then
    say "Mixxx-Treta $MIXXX_VERSION already installed; skipping download."
  else
    say "Fetching Mixxx-Treta $MIXXX_VERSION for $PLATFORM…"
    mkdir -p "$mixxx_dir"

    # Asset filenames are set by release-treta.yml — DMGs are
    # disambiguated by arch (-arm64 / -x64) because CPack outputs
    # collide on basename.
    local pattern
    case "$PLATFORM" in
      # NB: grep -oE above leaves a trailing `"` on each match, so
      # patterns must NOT anchor to end-of-line.
      macos-arm64) pattern='macos-arm64\.dmg' ;;
      macos-x64)   pattern='macos-x64\.dmg' ;;
      linux-x64)   pattern='\.deb"' ;;
    esac

    # Use the GitHub API to find the matching artifact URL. No auth
    # needed for public releases.
    local api="https://api.github.com/repos/$MIXXX_REPO/releases/tags/$MIXXX_VERSION"
    local url
    url=$(curl -fsSL "$api" \
            | grep -oE '"browser_download_url":\s*"[^"]+"' \
            | grep -E "$pattern" \
            | head -1 \
            | sed 's/.*: *"\(.*\)"/\1/') || true

    if [ -z "${url:-}" ]; then
      die "Couldn't find a $PLATFORM Mixxx artifact at tag $MIXXX_VERSION.\n   Check: https://github.com/$MIXXX_REPO/releases/tag/$MIXXX_VERSION"
    fi

    local archive="$mixxx_dir/$(basename "$url")"
    curl -fsSL --progress-bar -o "$archive" "$url"

    case "$PLATFORM" in
      macos-arm64|macos-x64) install_mixxx_dmg "$archive" "$mixxx_dir" ;;
      linux-x64)             install_mixxx_deb "$archive" "$mixxx_dir" ;;
    esac
    rm -f "$archive"
    ok "Mixxx-Treta installed at $mixxx_dir"
  fi

  # Flip the `current` symlink atomically.
  ln -sfn "$MIXXX_VERSION" "$PREFIX/mixxx/current"
}

install_mixxx_dmg() {
  local dmg="$1" dest="$2"
  local mount_point rc
  mount_point=$(hdiutil attach -nobrowse -readonly -mountrandom /tmp "$dmg" \
                  | tail -1 | awk '{print $NF}')
  # Save cp's exit, ALWAYS detach — otherwise `set -eu` aborts before
  # detach runs and we leave a phantom mount on the user's Mac that
  # survives reruns. (Caught by code review on PR #79.)
  cp -R "$mount_point"/*.app "$dest/" && rc=0 || rc=$?
  hdiutil detach "$mount_point" >/dev/null 2>&1 || true
  [ "$rc" -eq 0 ] || die "Failed to copy Mixxx.app from DMG (rc=$rc)"
}

install_mixxx_deb() {
  local deb="$1" dest="$2"
  require_cmd dpkg-deb
  # Extract the .deb without installing system-wide. Files land under
  # $dest/usr/bin/mixxx, $dest/usr/share/mixxx/, etc.
  dpkg-deb -x "$deb" "$dest"

  # The .deb declares apt-level dependencies (Qt, PortAudio, taglib,
  # chromaprint, etc.) that dpkg-deb -x does NOT install. On a bare
  # Debian/Ubuntu system the mixxx binary will fail to launch with
  # cryptic loader errors. Run ldd up front and fail fast with a
  # specific apt-install hint instead. (Caught by code review on PR #79.)
  local bin="$dest/usr/bin/mixxx"
  if command -v ldd >/dev/null 2>&1 && [ -x "$bin" ]; then
    local missing
    missing=$(ldd "$bin" 2>/dev/null | awk '/not found/ {print $1}' | sort -u | head -10 || true)
    if [ -n "$missing" ]; then
      cat <<EOF >&2

  ${C_YELLOW}!${C_RESET} Mixxx is installed but missing system libraries:

$(echo "$missing" | sed 's/^/      /')

  On Debian/Ubuntu, install them with:

      sudo apt install -y mixxx
      # (The dependency closure of the apt 'mixxx' package is the
      #  fastest way to pull all Qt/PortAudio/taglib/chromaprint libs;
      #  you'll keep using OUR forked binary at $bin.)

EOF
      die "Mixxx missing system libraries — see above"
    fi
  fi
}

mixxx_binary_path() {
  case "$PLATFORM" in
    macos-arm64|macos-x64)
      printf '%s' "$PREFIX/mixxx/current/Mixxx.app/Contents/MacOS/mixxx" ;;
    linux-x64)
      printf '%s' "$PREFIX/mixxx/current/usr/bin/mixxx" ;;
  esac
}

# ─── Python venv + djclaw ──────────────────────────────────────────────

install_djclaw_venv() {
  local venv="$PREFIX/.venv"

  # On upgrade, replace the venv outright — pip's resolver gets unhappy
  # when stale top-levels (different version numbers) coexist with new
  # ones, and re-creating from scratch is fast enough.
  if [ -n "${UPGRADE:-}" ] && [ -d "$venv" ]; then
    say "Removing old venv (upgrade path)…"
    rm -rf "$venv"
  fi

  if [ ! -d "$venv" ]; then
    say "Creating Python venv at $venv …"
    "$PYTHON" -m venv "$venv"
  fi

  say "Installing djclaw $DJCLAW_VERSION + LiteLLM (this takes ~5 min on first run — librosa is heavy)…"
  "$venv/bin/pip" install --upgrade --quiet pip
  local sdist="https://github.com/$DJCLAW_REPO/releases/download/v$DJCLAW_VERSION/djclaw-$DJCLAW_VERSION.tar.gz"
  "$venv/bin/pip" install --quiet \
    "$sdist" \
    'litellm[proxy]'
  ok "djclaw + LiteLLM installed"
}

# ─── CLI symlink + PATH hint ───────────────────────────────────────────

symlink_cli() {
  mkdir -p "$BIN_DIR"
  ln -sfn "$PREFIX/.venv/bin/djclaw" "$BIN_DIR/djclaw"
  ok "djclaw CLI → $BIN_DIR/djclaw"
}

print_path_hint() {
  case ":$PATH:" in
    *":$BIN_DIR:"*) return 0 ;;
  esac
  local rc_hint=""
  case "$(basename "${SHELL:-}")" in
    zsh)  rc_hint="echo 'export PATH=\"$BIN_DIR:\$PATH\"' >> ~/.zshrc && source ~/.zshrc" ;;
    bash) rc_hint="echo 'export PATH=\"$BIN_DIR:\$PATH\"' >> ~/.bashrc && source ~/.bashrc" ;;
    fish) rc_hint="fish_add_path $BIN_DIR" ;;
    *)    rc_hint="export PATH=\"$BIN_DIR:\$PATH\"" ;;
  esac
  cat <<EOF

  ${C_YELLOW}!${C_RESET} $BIN_DIR isn't on your PATH yet. Run:

      $rc_hint

EOF
}

# ─── First-run setup wizard ────────────────────────────────────────────

maybe_run_setup() {
  if [ -f "$CONFIG_DIR/config.yaml" ]; then
    say "Config exists at $CONFIG_DIR/config.yaml — preserving."
    return 0
  fi
  mkdir -p "$CONFIG_DIR"

  # When the user runs `curl ... | sh`, this script's stdin is the
  # pipe carrying the script — `input()` inside the wizard would
  # EOF immediately and write empty values. Reattach stdin to the
  # controlling terminal if there is one. (Caught by code review.)
  if [ -e /dev/tty ]; then
    say "First-run setup — answer 4 quick questions."
    echo
    "$PREFIX/.venv/bin/djclaw" setup </dev/tty
  else
    cat <<EOF

  ${C_YELLOW}!${C_RESET} No controlling TTY — skipping interactive setup.

  Finish setup later by running:

      djclaw setup

  Then:

      djclaw doctor
      djclaw start

EOF
  fi
}

# ─── Stamp + next steps ────────────────────────────────────────────────

stamp_version() {
  echo "$DJCLAW_VERSION" > "$PREFIX/version.txt"
}

print_next_steps() {
  cat <<EOF

  ${C_GREEN}${C_BOLD}Done.${C_RESET}

  To start DJClaw:
      djclaw start

  Other commands:
      djclaw doctor         check that Mixxx + LiteLLM + API keys are wired up
      djclaw tui            attach the TUI to a running daemon
      djclaw --remote       attach the TUI to a remote daemon at dj.treta.life

  Config lives at:    $CONFIG_DIR/config.yaml
  Music library at:   ${LIBRARY_DIR:-~/Music/DJTreta}
  Logs at:            $PREFIX/runtime/

  Re-run this installer any time to upgrade. Your config + DB + music
  are preserved.

EOF
}

# ─── Operator mode (Linux + systemd) ───────────────────────────────────
#
# Operator mode renders the systemd unit templates from this repo into
# /etc/systemd/system, sets up a PulseAudio virtual sink, drops the
# ezstream config, generates a relay token, and enables the units.
#
# The Being's runtime layout is the same as end-user mode (PREFIX,
# CONFIG_DIR), just relocated to /opt + /etc + /var via flags.

operator_validate() {
  case "$PLATFORM" in
    linux-x64) ;;
    *) die "Operator mode is Linux-only (got $PLATFORM)." ;;
  esac
  [ "$(id -u)" -eq 0 ] || die "Operator mode needs root (use sudo). It writes /etc/systemd/system."
  [ -n "$SVC_USER" ] || die "--service-user is required in operator mode."
  id "$SVC_USER" >/dev/null 2>&1 || die "Service user '$SVC_USER' doesn't exist. Create it first: useradd -m $SVC_USER"
  [ -n "$DATA_DIR" ] || die "--data-dir is required in operator mode (e.g. /var/lib/djclaw)."
  [ -n "$STREAM_URL" ] || die "--stream-url is required (e.g. icecast://source:PASS@127.0.0.1:8000/live)."

  # ezstream is required for stream push; ffmpeg + parec for HLS.
  require_cmd systemctl
  require_cmd ezstream parec lame
  [ "$HEADLESS" -eq 1 ] && require_cmd Xvfb
  [ -n "$HLS_DIR" ] && require_cmd ffmpeg
}

# Parse icecast://user:pass@host:port/mountpoint into 5 globals.
operator_parse_stream_url() {
  local u="${STREAM_URL#icecast://}"
  STREAM_USER="${u%%:*}"; u="${u#*:}"
  STREAM_PASS="${u%%@*}"; u="${u#*@}"
  STREAM_HOST="${u%%:*}"; u="${u#*:}"
  STREAM_PORT="${u%%/*}"
  STREAM_MOUNT="/${u#*/}"
  [ -n "$STREAM_USER" ] && [ -n "$STREAM_HOST" ] && [ -n "$STREAM_MOUNT" ] \
    || die "Invalid --stream-url: '$STREAM_URL' (want icecast://user:pass@host:port/mount)"
}

operator_fetch_template() {
  local name="$1" dest="$2"
  local url="https://raw.githubusercontent.com/$DJCLAW_REPO/$DJCLAW_REF/bin/systemd/$name"
  curl -fsSL "$url" -o "$dest" || die "Couldn't fetch template $name from $url"
}

operator_render() {
  local src="$1" dst="$2"
  sed \
    -e "s|__SVC_USER__|$SVC_USER|g" \
    -e "s|__INSTALL_DIR__|$PREFIX|g" \
    -e "s|__LOGS_DIR__|$LOGS_DIR|g" \
    -e "s|__HLS_DIR__|$HLS_DIR|g" \
    -e "s|__EZSTREAM_CONFIG__|$CONFIG_DIR/ezstream.xml|g" \
    -e "s|__DISPLAY_NUM__|$DISPLAY_NUM|g" \
    -e "s|__MIXXX_BIN__|$MIXXX_BIN|g" \
    -e "s|__MIXXX_RESOURCE__|$MIXXX_RESOURCE|g" \
    -e "s|__MIXXX_SETTINGS__|$MIXXX_SETTINGS|g" \
    -e "s|__STREAM_HOST__|$STREAM_HOST|g" \
    -e "s|__STREAM_PORT__|$STREAM_PORT|g" \
    -e "s|__STREAM_USER__|$STREAM_USER|g" \
    -e "s|__STREAM_PASS__|$STREAM_PASS|g" \
    -e "s|__STREAM_MOUNT__|$STREAM_MOUNT|g" \
    -e "s|__STREAM_PUBLIC_URL__|$STREAM_PUBLIC_URL|g" \
    "$src" > "$dst"
}

operator_setup_dirs() {
  mkdir -p "$PREFIX" "$CONFIG_DIR" "$DATA_DIR" "$LOGS_DIR" "$DATA_DIR/db" "$DATA_DIR/runtime"
  [ -n "$MUSIC_DIR" ] && mkdir -p "$MUSIC_DIR"
  [ -n "$HLS_DIR" ] && mkdir -p "$HLS_DIR"
  chown -R "$SVC_USER:$SVC_USER" "$PREFIX" "$DATA_DIR" "$LOGS_DIR"
  [ -n "$MUSIC_DIR" ] && chown -R "$SVC_USER:$SVC_USER" "$MUSIC_DIR"
  [ -n "$HLS_DIR" ] && chown -R "$SVC_USER:$SVC_USER" "$HLS_DIR"
  chown -R "$SVC_USER:$SVC_USER" "$CONFIG_DIR"
  chmod 0750 "$CONFIG_DIR"

  # systemd's `StandardOutput=append:/path` opens the file BEFORE the
  # User= drop, so the file gets created root-owned and silently fails
  # to receive writes. Pre-create the log files as SVC_USER so the
  # subsequent open() inherits the right ownership.
  for log in dj-treta-agent dj-treta-litellm dj-treta-mcp; do
    sudo -u "$SVC_USER" touch "$LOGS_DIR/${log}.log"
  done
}

operator_setup_pulse() {
  say "Installing PulseAudio virtual sink config…"
  local tmp
  tmp=$(mktemp)
  operator_fetch_template "pulse-djtreta.pa.template" "$tmp"
  install -m 0644 -o root -g root "$tmp" /etc/pulse/system.pa.d/djtreta.pa
  rm -f "$tmp"

  # Make sure SVC_USER can talk to system pulse.
  usermod -aG pulse-access "$SVC_USER" 2>/dev/null || true
  usermod -aG audio "$SVC_USER" 2>/dev/null || true

  if systemctl list-unit-files pulseaudio.service >/dev/null 2>&1; then
    systemctl restart pulseaudio.service || true
  fi
  ok "PulseAudio sink 'djtreta_out' configured."
}

operator_setup_ezstream() {
  say "Rendering ezstream.xml at $CONFIG_DIR/ezstream.xml…"
  local tmp
  tmp=$(mktemp)
  operator_fetch_template "ezstream.xml.template" "$tmp"
  operator_render "$tmp" "$CONFIG_DIR/ezstream.xml"
  rm -f "$tmp"
  chown "$SVC_USER:$SVC_USER" "$CONFIG_DIR/ezstream.xml"
  chmod 0640 "$CONFIG_DIR/ezstream.xml"
}

operator_setup_logrotate() {
  say "Installing logrotate config at /etc/logrotate.d/dj-treta…"
  local tmp
  tmp=$(mktemp)
  operator_fetch_template "logrotate.conf.template" "$tmp"
  operator_render "$tmp" /etc/logrotate.d/dj-treta
  rm -f "$tmp"
  chmod 0644 /etc/logrotate.d/dj-treta
}

operator_install_units() {
  say "Rendering systemd units to /etc/systemd/system/…"
  local units="dj-treta-xvfb dj-treta-mixxx dj-treta-litellm dj-treta-agent dj-treta-mcp dj-treta-stream"
  [ -n "$HLS_DIR" ] && units="$units dj-treta-hls"

  for unit in $units; do
    [ "$HEADLESS" -eq 0 ] && [ "$unit" = "dj-treta-xvfb" ] && continue
    local tmp
    tmp=$(mktemp)
    operator_fetch_template "${unit}.service.template" "$tmp"
    operator_render "$tmp" "/etc/systemd/system/${unit}.service"
    rm -f "$tmp"
    chmod 0644 "/etc/systemd/system/${unit}.service"
    ok "→ ${unit}.service"
  done

  systemctl daemon-reload
  ACTIVE_UNITS="$units"
}

operator_write_config() {
  # Minimal config.yaml + secrets + relay token. Preserve any existing.
  if [ -f "$CONFIG_DIR/config.yaml" ]; then
    say "Config exists at $CONFIG_DIR/config.yaml — preserving."
  else
    say "Writing minimal config.yaml…"
    cat > "$CONFIG_DIR/config.yaml" <<EOF
# DJ Treta operator config — generated by install.sh.
# Edit freely; install.sh will not overwrite this file on re-run.
library:
  music_dir: "$MUSIC_DIR"
mixxx:
  url: "http://127.0.0.1:7778"
relay:
  enabled: $([ -n "$RELAY_URL" ] && echo "true" || echo "false")
  url: "$RELAY_URL"
broadcast:
  auto_start: false
knowledge:
  enabled: true
  data_dir: "$DATA_DIR/knowledge"
sources:
  youtube: true
EOF
    chown "$SVC_USER:$SVC_USER" "$CONFIG_DIR/config.yaml"
    chmod 0640 "$CONFIG_DIR/config.yaml"
  fi

  # .env points the systemd EnvironmentFile= at the right place.
  if [ ! -f "$PREFIX/.env" ]; then
    say "Writing $PREFIX/.env (referenced by all units' EnvironmentFile=)…"
    cat > "$PREFIX/.env" <<EOF
DJCLAW_CONFIG=$CONFIG_DIR/config.yaml
DJCLAW_DB_PATH=$DATA_DIR/db/djtreta.db
DJCLAW_RUNTIME_DIR=$DATA_DIR/runtime
DJCLAW_LITELLM_CONFIG=$CONFIG_DIR/litellm.yaml
EOF
    chown "$SVC_USER:$SVC_USER" "$PREFIX/.env"
    chmod 0640 "$PREFIX/.env"
  fi

  if [ -n "$RELAY_TOKEN" ] && [ ! -f "$CONFIG_DIR/relay.token" ]; then
    printf '%s' "$RELAY_TOKEN" > "$CONFIG_DIR/relay.token"
    chown "$SVC_USER:$SVC_USER" "$CONFIG_DIR/relay.token"
    chmod 0600 "$CONFIG_DIR/relay.token"
    ok "Wrote $CONFIG_DIR/relay.token"
  fi
}

operator_start_units() {
  say "Enabling + starting units…"
  for unit in $ACTIVE_UNITS; do
    systemctl enable "${unit}.service" >/dev/null 2>&1 || true
    if systemctl is-active --quiet "${unit}.service"; then
      systemctl restart "${unit}.service" && echo -e "  ${C_GREEN}✓${C_RESET} ${unit} restarted"
    else
      systemctl start "${unit}.service" && echo -e "  ${C_GREEN}✓${C_RESET} ${unit} started"
    fi
  done
}

operator_print_summary() {
  cat <<EOF

  ${C_GREEN}${C_BOLD}DJ Treta operator install complete.${C_RESET}

  Code:           $PREFIX
  Config:         $CONFIG_DIR
  Data:           $DATA_DIR
  Logs:           $LOGS_DIR
  Music library:  $MUSIC_DIR
  HLS dir:        ${HLS_DIR:-(none)}
  Stream URL:     icecast://$STREAM_USER:***@$STREAM_HOST:$STREAM_PORT$STREAM_MOUNT
  Relay URL:      ${RELAY_URL:-(none)}
  Service user:   $SVC_USER

  Units:
$(for u in $ACTIVE_UNITS; do printf '    %-25s %s\n' "$u" "$(systemctl is-active "${u}.service" 2>/dev/null)"; done)

  To upgrade: re-run the same install.sh command. Config + DB + music + HLS dir are preserved.
  Logs:    journalctl -u dj-treta-agent -f
  State:   sudo -u $SVC_USER cat $DATA_DIR/runtime/state.json | jq .

EOF
}

main_operator() {
  banner
  say "Operator mode — installing systemd-managed DJ Treta."

  # Sane operator defaults if not provided.
  : "${LOGS_DIR:=/var/log/djclaw}"
  : "${BIN_DIR:=/usr/local/bin}"

  detect_platform
  operator_validate
  operator_parse_stream_url

  require_python_310
  say "Python: $PYTHON ($("$PYTHON" --version | awk '{print $2}'))"

  operator_setup_dirs
  fetch_mixxx_for_platform

  # Operator-mode systemd units expect a fixed Mixxx layout.
  MIXXX_BIN="$(mixxx_binary_path)"
  MIXXX_RESOURCE="$PREFIX/mixxx/current/usr/share/mixxx"
  MIXXX_SETTINGS="$DATA_DIR/mixxx-settings"
  mkdir -p "$MIXXX_SETTINGS"
  chown -R "$SVC_USER:$SVC_USER" "$MIXXX_SETTINGS"

  install_djclaw_venv
  chown -R "$SVC_USER:$SVC_USER" "$PREFIX/.venv"

  # CLI symlink at /usr/local/bin so operators can run `djclaw doctor`.
  ln -sfn "$PREFIX/.venv/bin/djclaw" "$BIN_DIR/djclaw"
  ok "djclaw CLI → $BIN_DIR/djclaw"

  operator_setup_pulse
  operator_setup_ezstream
  operator_setup_logrotate
  operator_write_config
  operator_install_units
  operator_start_units

  stamp_version
  operator_print_summary
}

# ─── Main ──────────────────────────────────────────────────────────────

main() {
  banner

  detect_platform
  say "Platform: $PLATFORM"

  require_cmd curl tar uname
  case "$PLATFORM" in
    macos-*) require_cmd hdiutil ;;
    linux-*) require_cmd dpkg-deb ;;
  esac
  require_python_310
  say "Python: $PYTHON ($("$PYTHON" --version | awk '{print $2}'))"

  mkdir -p "$PREFIX/mixxx" "$PREFIX/db" "$PREFIX/runtime"

  if [ -f "$PREFIX/version.txt" ]; then
    local prev
    prev=$(cat "$PREFIX/version.txt")
    if [ "$prev" != "$DJCLAW_VERSION" ]; then
      say "Upgrading: $prev → $DJCLAW_VERSION (preserving config + db + music)"
      UPGRADE=1
    else
      say "Re-running at the same version ($prev); refreshing binaries + venv."
    fi
  fi

  fetch_mixxx_for_platform
  install_djclaw_venv
  symlink_cli

  maybe_run_setup

  stamp_version
  print_path_hint
  print_next_steps
}

parse_args "$@"
if [ "$OPERATOR" -eq 1 ]; then
  main_operator
else
  main
fi
