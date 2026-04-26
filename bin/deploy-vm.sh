#!/usr/bin/env bash
# Deploy local dev → remote VM. Hot-swap safe (music never stops).
#
# Reads VM_USER, VM_HOST, VM_PATH, SSH_KEY from .env.deploy (gitignored).
# Falls back to CLI args:
#   bin/deploy-vm.sh                 (deploys current HEAD)
#   bin/deploy-vm.sh --dry           (rsync dry-run)
#   bin/deploy-vm.sh --user X --host Y --path Z --ssh-key K  (override .env.deploy)
set -euo pipefail

LOCAL_REPO="$(cd "$(dirname "$0")/.." && pwd)"
ENV_DEPLOY="$LOCAL_REPO/.env.deploy"

# Load .env.deploy if present.
if [[ -f "$ENV_DEPLOY" ]]; then
  set -a; source "$ENV_DEPLOY"; set +a
fi

DRY=""
while [[ "${1:-}" != "" ]]; do
  case "$1" in
    --dry)     DRY="--dry-run -v"; shift ;;
    --user)    VM_USER="$2"; shift 2 ;;
    --host)    VM_HOST="$2"; shift 2 ;;
    --path)    VM_PATH="$2"; shift 2 ;;
    --ssh-key) SSH_KEY="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 1 ;;
  esac
done

# Validate.
: "${VM_USER:?VM_USER required (set in .env.deploy or pass --user)}"
: "${VM_HOST:?VM_HOST required (set in .env.deploy or pass --host)}"
: "${VM_PATH:?VM_PATH required (set in .env.deploy or pass --path)}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519}"

GIT_HEAD=$(cd "$LOCAL_REPO" && git rev-parse HEAD)
GIT_BRANCH=$(cd "$LOCAL_REPO" && git rev-parse --abbrev-ref HEAD)
GIT_DIRTY=$(cd "$LOCAL_REPO" && git status --short | head -1)

echo "deploy: $GIT_BRANCH @ ${GIT_HEAD:0:8} → $VM_USER@$VM_HOST:$VM_PATH"
[[ -n "$GIT_DIRTY" ]] && echo "WARN: working tree has uncommitted changes"

# 1. rsync — exclude secrets, generated state, the venv, and the local DB.
rsync -avP --delete $DRY \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  --exclude '.venv/' \
  --exclude '.env' \
  --exclude '.env.local' \
  --exclude '.env.deploy' \
  --exclude '.beings/session.json' \
  --exclude '.beings/session-PRE-RESET.json' \
  --exclude 'djtreta.db' \
  --exclude 'djtreta.db-wal' \
  --exclude 'djtreta.db-shm' \
  --exclude 'config.local.yaml' \
  --exclude '.git/' \
  --exclude 'recordings/' \
  --exclude 'tests/__pycache__/' \
  -e "ssh -i $SSH_KEY" \
  "$LOCAL_REPO/" \
  "$VM_USER@$VM_HOST:$VM_PATH/" \
  | tail -20

[[ -n "$DRY" ]] && exit 0

# 2. Stamp the deploy.
ssh -i "$SSH_KEY" "$VM_USER@$VM_HOST" \
  "echo '$GIT_BRANCH @ $GIT_HEAD ($(date -u +%Y-%m-%dT%H:%M:%SZ))' > $VM_PATH/DEPLOYED_FROM"

# 3. Hot-swap restart (Mixxx + stream + hls UNTOUCHED — music keeps playing).
ssh -i "$SSH_KEY" "$VM_USER@$VM_HOST" '
  sudo systemctl restart dj-treta-agent dj-treta-mcp dj-treta-litellm
  sleep 5
  systemctl is-active dj-treta-agent dj-treta-mcp dj-treta-litellm
'

echo ""
echo "deployed $GIT_BRANCH @ ${GIT_HEAD:0:8} → $VM_HOST"
echo "watch:   ssh -i $SSH_KEY $VM_USER@$VM_HOST 'sudo journalctl -fu dj-treta-agent'"
echo "rollback:  git checkout <prev-sha> && bin/deploy-vm.sh"
