#!/usr/bin/env bash
# Deploy local dev → VM production. Hot-swap safe (music never stops).
# Usage:  bin/deploy-vm.sh         (deploys current HEAD)
#         bin/deploy-vm.sh --dry   (shows what would change)
set -euo pipefail

VM_USER="manish.pratap"
VM_HOST="34.93.92.241"
VM_PATH="/mnt/data/dj-treta"
SSH_KEY="$HOME/.ssh/google_compute_engine"
LOCAL_REPO="$(cd "$(dirname "$0")/.." && pwd)"

GIT_HEAD=$(cd "$LOCAL_REPO" && git rev-parse HEAD)
GIT_BRANCH=$(cd "$LOCAL_REPO" && git rev-parse --abbrev-ref HEAD)
GIT_DIRTY=$(cd "$LOCAL_REPO" && git status --short | head -1)

echo "deploy: $GIT_BRANCH @ ${GIT_HEAD:0:8}"
[[ -n "$GIT_DIRTY" ]] && echo "WARN: working tree has uncommitted changes"

DRY=""
[[ "${1:-}" == "--dry" ]] && DRY="--dry-run -v"

# 1. rsync agent + scripts + mcp_server (NOT .venv, .beings, recordings, db files)
rsync -avP --delete $DRY \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  --exclude '.venv/' \
  --exclude '.beings/session.json' \
  --exclude '.beings/session-PRE-RESET.json' \
  --exclude 'djtreta.db' \
  --exclude 'djtreta.db-wal' \
  --exclude 'djtreta.db-shm' \
  --exclude '.git/' \
  --exclude 'recordings/' \
  --exclude 'tests/__pycache__/' \
  -e "ssh -i $SSH_KEY" \
  "$LOCAL_REPO/" \
  "$VM_USER@$VM_HOST:$VM_PATH/" \
  | tail -20

[[ -n "$DRY" ]] && exit 0

# 2. Stamp the deploy
ssh -i "$SSH_KEY" "$VM_USER@$VM_HOST" \
  "echo '$GIT_BRANCH @ $GIT_HEAD ($(date -u +%Y-%m-%dT%H:%M:%SZ))' > $VM_PATH/DEPLOYED_FROM"

# 3. Hot-swap restart (Mixxx + stream + hls UNTOUCHED)
ssh -i "$SSH_KEY" "$VM_USER@$VM_HOST" '
  sudo systemctl restart dj-treta-agent dj-treta-mcp
  sleep 5
  systemctl is-active dj-treta-agent dj-treta-mcp
'

echo ""
echo "deployed $GIT_BRANCH @ ${GIT_HEAD:0:8} → $VM_HOST"
echo "watch:   gcloud compute ssh dj-treta-live --zone=asia-south1-a --command='sudo journalctl -fu dj-treta-agent'"
echo "rollback:  git checkout <prev-sha> && bin/deploy-vm.sh"
