#!/usr/bin/env bash
# Tear down a hand-placed DJ Treta operator setup before migrating to
# install.sh. Music never stops mid-track — caller is expected to know
# the stream WILL go silent for the duration of this script.
#
# Idempotent. Safe to run twice.
#
# What it does:
#   1. Stops + disables the legacy systemd units.
#   2. Removes /etc/systemd/system/dj-treta-*.service files.
#   3. Removes /etc/dj-treta/, /etc/pulse/system.pa.d/djtreta.pa.
#   4. Leaves alone: the music library, the SQLite DB, the LanceDB
#      knowledge dir, and any /mnt/data/* data dirs. Those get re-
#      pointed at by the new install — never destroyed.
#
# Usage:
#   sudo bash bin/cleanup-vm.sh             # interactive confirm
#   sudo bash bin/cleanup-vm.sh --yes       # skip confirm
set -euo pipefail

YES=0
[[ "${1:-}" == "--yes" ]] && YES=1

UNITS=(
  dj-treta-agent
  dj-treta-mcp
  dj-treta-litellm
  dj-treta-stream
  dj-treta-hls
  dj-treta-mixxx
  dj-treta-xvfb
)

C='\033[36m'; G='\033[32m'; Y='\033[33m'; R='\033[31m'; N='\033[0m'

echo -e "${C}::${N} DJ Treta legacy-cleanup"
echo
echo "  Will stop + remove these units (if present):"
for u in "${UNITS[@]}"; do echo "    - $u"; done
echo
echo "  Will remove these config paths (if present):"
echo "    - /etc/systemd/system/dj-treta-*.service"
echo "    - /etc/dj-treta/"
echo "    - /etc/pulse/system.pa.d/djtreta.pa"
echo
echo -e "  ${Y}!${N} Music library, DB, knowledge LanceDB, /mnt/data/* are NOT touched."
echo

if [[ $YES -ne 1 ]]; then
  read -r -p "Proceed? [y/N] " ans
  case "$ans" in y|Y|yes|YES) ;; *) echo "Aborted."; exit 1 ;; esac
fi

echo
echo -e "${C}::${N} Stopping + disabling units…"
for u in "${UNITS[@]}"; do
  if systemctl list-unit-files "${u}.service" >/dev/null 2>&1; then
    systemctl stop "${u}.service" 2>/dev/null || true
    systemctl disable "${u}.service" 2>/dev/null || true
    echo -e "  ${G}✓${N} ${u} stopped + disabled"
  fi
done

echo
echo -e "${C}::${N} Removing unit files…"
for f in /etc/systemd/system/dj-treta-*.service; do
  [ -f "$f" ] || continue
  rm -f "$f"
  echo -e "  ${G}✓${N} removed $f"
done

echo
echo -e "${C}::${N} Removing config paths…"
[ -d /etc/dj-treta ]                       && rm -rf /etc/dj-treta                       && echo -e "  ${G}✓${N} /etc/dj-treta removed"
[ -f /etc/pulse/system.pa.d/djtreta.pa ]   && rm -f  /etc/pulse/system.pa.d/djtreta.pa   && echo -e "  ${G}✓${N} /etc/pulse/system.pa.d/djtreta.pa removed"

echo
echo -e "${C}::${N} systemctl daemon-reload"
systemctl daemon-reload

echo
echo -e "${G}Done.${N}  Now run install.sh in operator mode to lay down the new setup."
