#!/usr/bin/env bash
# Install DJ Treta as systemd services on a Linux host.
# One-shot bootstrap: fills bin/systemd/*.template with values from
# .env.deploy and writes to /etc/systemd/system/, then enables + starts
# each unit.
#
# Usage (run AS the SVC_USER, sudo'd inline):
#   cp .env.deploy.example .env.deploy   # then edit
#   bash bin/install-vm.sh                # installs all 7 units
#   bash bin/install-vm.sh dj-treta-agent # installs just one unit
#
# Idempotent: re-running re-renders + reloads systemd. Existing units are
# `daemon-reload`ed and `restart`ed if they were running.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_DEPLOY="$REPO_ROOT/.env.deploy"

if [[ ! -f "$ENV_DEPLOY" ]]; then
  echo "ERROR: $ENV_DEPLOY not found. Copy .env.deploy.example first." >&2
  exit 1
fi

# Load deploy config — tolerate `KEY=value` lines + bash-style assignments.
set -a; source "$ENV_DEPLOY"; set +a

# Defaults if not in .env.deploy
SVC_USER="${SVC_USER:-${VM_USER:?missing VM_USER or SVC_USER in .env.deploy}}"
INSTALL_DIR="${INSTALL_DIR:?missing INSTALL_DIR in .env.deploy}"
LOGS_DIR="${LOGS_DIR:-/var/log/dj-treta}"
HLS_DIR="${HLS_DIR:-/var/lib/dj-treta/hls}"
EZSTREAM_CONFIG="${EZSTREAM_CONFIG:-/etc/dj-treta/ezstream.xml}"
DISPLAY_NUM="${DISPLAY_NUM:-99}"
MIXXX_BIN="${MIXXX_BIN:-/opt/mixxx-treta/build/mixxx}"
MIXXX_RESOURCE="${MIXXX_RESOURCE:-/opt/mixxx-treta/res}"
MIXXX_SETTINGS="${MIXXX_SETTINGS:-/home/$SVC_USER/.mixxx-treta}"

echo "rendering templates with:"
echo "  SVC_USER       = $SVC_USER"
echo "  INSTALL_DIR    = $INSTALL_DIR"
echo "  LOGS_DIR       = $LOGS_DIR"
echo "  HLS_DIR        = $HLS_DIR"
echo "  MIXXX_BIN      = $MIXXX_BIN"
echo

ALL_UNITS=(
  dj-treta-xvfb
  dj-treta-mixxx
  dj-treta-litellm
  dj-treta-agent
  dj-treta-mcp
  dj-treta-stream
  dj-treta-hls
)

# Filter to specific unit if argument given.
if [[ "$#" -gt 0 ]]; then
  TARGETS=("$@")
else
  TARGETS=("${ALL_UNITS[@]}")
fi

# Ensure log + hls dirs exist with right ownership.
sudo mkdir -p "$LOGS_DIR" "$HLS_DIR"
sudo chown "$SVC_USER:$SVC_USER" "$LOGS_DIR" "$HLS_DIR"

for unit in "${TARGETS[@]}"; do
  template="$REPO_ROOT/bin/systemd/${unit}.service.template"
  if [[ ! -f "$template" ]]; then
    echo "WARN: template not found, skipping: $template" >&2
    continue
  fi
  rendered="/tmp/${unit}.service"
  sed \
    -e "s|__SVC_USER__|$SVC_USER|g" \
    -e "s|__INSTALL_DIR__|$INSTALL_DIR|g" \
    -e "s|__LOGS_DIR__|$LOGS_DIR|g" \
    -e "s|__HLS_DIR__|$HLS_DIR|g" \
    -e "s|__EZSTREAM_CONFIG__|$EZSTREAM_CONFIG|g" \
    -e "s|__DISPLAY_NUM__|$DISPLAY_NUM|g" \
    -e "s|__MIXXX_BIN__|$MIXXX_BIN|g" \
    -e "s|__MIXXX_RESOURCE__|$MIXXX_RESOURCE|g" \
    -e "s|__MIXXX_SETTINGS__|$MIXXX_SETTINGS|g" \
    "$template" > "$rendered"

  sudo install -m 0644 "$rendered" "/etc/systemd/system/${unit}.service"
  echo "wrote: /etc/systemd/system/${unit}.service"
done

echo
echo "reloading systemd..."
sudo systemctl daemon-reload

echo
echo "enabling + (re)starting:"
for unit in "${TARGETS[@]}"; do
  sudo systemctl enable "${unit}.service" 2>&1 | grep -v "^Created symlink" || true
  if sudo systemctl is-active --quiet "${unit}.service"; then
    sudo systemctl restart "${unit}.service"
    echo "  ${unit}: restarted"
  else
    sudo systemctl start "${unit}.service"
    echo "  ${unit}: started"
  fi
done

echo
echo "status:"
for unit in "${TARGETS[@]}"; do
  state=$(sudo systemctl is-active "${unit}.service" 2>&1)
  printf "  %-25s %s\n" "$unit" "$state"
done
