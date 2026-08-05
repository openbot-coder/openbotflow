#!/bin/bash
# botflow Supervisor deployment script
# Usage: ./deploy.sh [--port PORT] [--workspace PATH]

set -euo pipefail

# Defaults
DEPLOY_DIR="/mnt/deploy/botflow"
PROJECT_DIR="/home/openbot/workspace/projects/openbotflow"
PORT="${1:-4000}"
SUPERVISOR_CONF="/etc/supervisor/conf.d/botflow.conf"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log() { echo -e "${GREEN}[deploy]${NC} $1"; }
warn() { echo -e "${YELLOW}[deploy]${NC} $1"; }
err() { echo -e "${RED}[deploy]${NC} $1" >&2; exit 1; }

# Check prerequisites
command -v supervisord >/dev/null 2>&1 || err "supervisord not found. Install: apt install supervisor"
command -v uv >/dev/null 2>&1 || err "uv not found. Install: curl -LsSf https://astral.sh/uv/install.sh | sh"

# Stop existing service
if sudo supervisorctl status botflow 2>/dev/null | grep -q RUNNING; then
    log "Stopping existing botflow service..."
    sudo supervisorctl stop botflow
fi

# Backup and sync code
log "Syncing code to ${DEPLOY_DIR}..."
mkdir -p "${DEPLOY_DIR}"
rsync -av --delete \
    --exclude='.venv' \
    --exclude='__pycache__' \
    --exclude='.pytest_cache' \
    --exclude='.git' \
    --exclude='node_modules' \
    "${PROJECT_DIR}/" "${DEPLOY_DIR}/"

# Ensure workspace structure
mkdir -p "${DEPLOY_DIR}/data" "${DEPLOY_DIR}/logs"

# Setup Python environment
log "Setting up Python environment..."
if [ ! -d "${DEPLOY_DIR}/.venv" ]; then
    cd "${DEPLOY_DIR}" && uv sync --no-dev
else
    cd "${DEPLOY_DIR}" && uv sync --no-dev
fi

# Generate supervisor config
log "Generating supervisor config..."
cat > "${SUPERVISOR_CONF}" << EOF
[program:botflow]
command=${DEPLOY_DIR}/.venv/bin/botflow run --workspace ${DEPLOY_DIR} --port ${PORT}
user=openbot
directory=${DEPLOY_DIR}
autostart=true
autorestart=true
startretries=3
startsecs=5
stopwaitsecs=10
stderr_logfile=${DEPLOY_DIR}/logs/botflow.err.log
stdout_logfile=${DEPLOY_DIR}/logs/botflow.out.log
environment=HOME="/home/openbot",USER="openbot",BOTFLOW_CORS_ORIGINS="*"
EOF

# Reload and start
log "Reloading supervisor..."
sudo supervisorctl reread
sudo supervisorctl update

log "Starting botflow..."
sudo supervisorctl start botflow

sleep 2

# Verify
if sudo supervisorctl status botflow 2>/dev/null | grep -q RUNNING; then
    log "Deployment successful!"
    log "Service status:"
    sudo supervisorctl status botflow
    log "Health check:"
    curl -s "http://localhost:${PORT}/health" 2>/dev/null || warn "Health check failed (service may still be starting)"
else
    err "Service failed to start. Check logs: ${DEPLOY_DIR}/logs/botflow.err.log"
fi
