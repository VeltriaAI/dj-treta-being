#!/usr/bin/env bash
# vm-autoupdate.sh — pull-based release deployer for the DJ Treta production VM.
#
# Runs on a systemd timer. Polls GitHub for the latest djclaw release; if it's
# newer than the installed version, installs it into the venv, hot-swaps the
# agent services, health-checks, and rolls back automatically on failure.
#
# CRITICAL SAFETY RULES (learned the hard way 2026-05-25):
#   - NEVER restart dj-treta-mixxx or dj-treta-stream here. Restarting Mixxx
#     triggers a full library rescan that locks its DB and can deadlock the
#     engine. Deploys hot-swap ONLY the Python services (agent/mcp/litellm);
#     Mixxx + the audio/stream pipeline keep running untouched.
#   - Verify the new code IMPORTS before restarting anything.
#   - After restart, confirm the agent is up AND the stream is producing real
#     audio; if not, roll back to the previous release and restart again.
#
# Deploys are gated on explicit version tags: a deploy happens only when a new
# GitHub Release (vX.Y.Z) appears — i.e. when someone deliberately tags one.
set -uo pipefail

REPO="VeltriaAI/dj-treta-being"
VENV="${VENV:-/opt/djclaw/venv}"
CACHE="/var/lib/djclaw/releases"
LOG="/var/log/djclaw/autoupdate.log"
LOCK="/run/dj-treta-autoupdate.lock"
HLS_DIR="/mnt/data/hls"
SERVICES="dj-treta-agent dj-treta-mcp dj-treta-litellm"

mkdir -p "$CACHE" "$(dirname "$LOG")"
log() { echo "[$(date -u +%FT%TZ)] $*" | tee -a "$LOG"; }

# Single-flight: never let two timer fires overlap a deploy.
exec 9>"$LOCK" || exit 0
flock -n 9 || { log "another run in progress; skip"; exit 0; }

installed=$("$VENV"/bin/python -c "import importlib.metadata as m; print(m.version('djclaw'))" 2>/dev/null || echo "0.0.0")

api=$(curl -fsSL --max-time 15 "https://api.github.com/repos/$REPO/releases/latest" 2>/dev/null || echo "")
latest=$(printf '%s' "$api" | grep -m1 '"tag_name"' | sed -E 's/.*"tag_name":[[:space:]]*"v?([^"]+)".*/\1/')
[ -z "$latest" ] && { log "could not resolve latest release tag; skip"; exit 0; }

# Nothing to do if installed >= latest (sort -V puts the higher semver last).
if [ "$installed" = "$latest" ] || \
   [ "$(printf '%s\n%s\n' "$installed" "$latest" | sort -V | tail -1)" = "$installed" ]; then
  exit 0
fi

log "release available: installed=$installed -> latest=$latest"

fetch() { # fetch <version> -> caches sdist, echoes path (or empty on failure)
  local v="$1" out="$CACHE/djclaw-$1.tar.gz"
  [ -s "$out" ] && { echo "$out"; return 0; }
  curl -fsSL --max-time 60 \
    "https://github.com/$REPO/releases/download/v$v/djclaw-$v.tar.gz" -o "$out" \
    && echo "$out" || echo ""
}

new_sdist=$(fetch "$latest")
[ -z "$new_sdist" ] && { log "download failed for $latest; abort"; exit 1; }

# Pre-cache the CURRENT release's sdist for rollback (best-effort; it's a real
# release tag so it's downloadable). If this fails we still try, but warn.
prev_sdist=$(fetch "$installed")
[ -z "$prev_sdist" ] && log "WARN: could not pre-cache rollback sdist for $installed"

# Install new code into the venv (no deps churn on a live box).
if ! "$VENV"/bin/pip install --force-reinstall --no-deps "$new_sdist" >>"$LOG" 2>&1; then
  log "pip install of $latest failed; running code untouched; abort"
  exit 1
fi

# Import-gate BEFORE any restart. If broken, restore previous files (running
# services still hold the old code in memory, so they're unaffected) and bail.
if ! "$VENV"/bin/python -c "import agent, agent.tools.transitions, agent.playback_applier" >>"$LOG" 2>&1; then
  log "import check FAILED for $latest; restoring $installed (no restart performed)"
  [ -n "$prev_sdist" ] && "$VENV"/bin/pip install --force-reinstall --no-deps "$prev_sdist" >>"$LOG" 2>&1
  exit 1
fi

# Hot-swap restart — agent/mcp/litellm ONLY. Mixxx + stream stay up.
log "imports OK; hot-swapping $SERVICES (Mixxx/stream untouched)"
systemctl restart $SERVICES
sleep 25

# Health gate: agent active AND a fresh, non-silent HLS segment.
healthy=1
systemctl is-active --quiet dj-treta-agent || { healthy=0; log "health: agent not active"; }
newest=$(ls -t "$HLS_DIR"/*.m4s 2>/dev/null | head -1)
if [ -n "$newest" ]; then
  age=$(( $(date +%s) - $(stat -c %Y "$newest") ))
  size=$(stat -c %s "$newest")
  if [ "$age" -ge 20 ] || [ "$size" -le 50000 ]; then
    healthy=0; log "health: stream stale/silent (age=${age}s size=${size}B)"
  fi
else
  healthy=0; log "health: no HLS segments found"
fi

if [ "$healthy" = 1 ]; then
  log "DEPLOY OK: now running $latest (agent active, stream healthy)"
  exit 0
fi

# Roll back.
log "DEPLOY UNHEALTHY at $latest; rolling back to $installed"
if [ -n "$prev_sdist" ] && [ -s "$prev_sdist" ]; then
  "$VENV"/bin/pip install --force-reinstall --no-deps "$prev_sdist" >>"$LOG" 2>&1
  systemctl restart $SERVICES
  log "ROLLED BACK to $installed"
  exit 1
fi
log "CRITICAL: no rollback sdist for $installed — MANUAL INTERVENTION NEEDED"
exit 1
