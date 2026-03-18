#!/usr/bin/env bash
# backup_nightly.sh — Nightly Minecraft backup orchestrator.
#
# Schedule with cron:
#   50 23 * * * /home/user/minecraft/servermanager/backup_nightly.sh
#
# What it does:
#   23:50 — warn players: 10 minutes
#   23:55 — warn players: 5 minutes
#   23:59 — warn players: 1 minute
#   00:00 — stop server, start status server, run MC Vault backup,
#            stop status server, restart main server.

set -euo pipefail

# ── Configuration ────────────────────────────────────────────────────────────
MCVAULT="$HOME/minecraft/servermanager/mc_vault.py"
STATUS_SERVER="$HOME/minecraft/servermanager/mc_status_server.py"
LOG="$HOME/minecraft/servermanager/backup.log"
ADMIN_LOG="$HOME/minecraft/servermanager/admin.log"
TMUX_SESSION="minecraft"
# ─────────────────────────────────────────────────────────────────────────────

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG"
}

admin_log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [cron] $*" >> "$ADMIN_LOG"
}

mc_cmd() {
    # Send a command to the running Minecraft server via tmux.
    # Silently does nothing if the session doesn't exist.
    tmux send-keys -t "$TMUX_SESSION" "$1" Enter 2>/dev/null || true
}

server_running() {
    tmux has-session -t "$TMUX_SESSION" 2>/dev/null
}

# ── Warning phase ─────────────────────────────────────────────────────────────
log "Backup scheduler started."
admin_log "nightly backup started"

if server_running; then
    mc_cmd "say §eServer will go down for backup in §b10 minutes§e."
    log "Sent 10-minute warning."
fi

sleep 300   # wait 5 minutes → 23:55

if server_running; then
    mc_cmd "say §eServer will go down for backup in §b5 minutes§e."
    log "Sent 5-minute warning."
fi

sleep 240   # wait 4 minutes → 23:59

if server_running; then
    mc_cmd "say §eServer will go down for backup in §b1 minute§e."
    log "Sent 1-minute warning."
fi

sleep 60    # wait 1 minute → 00:00

# ── Shutdown ──────────────────────────────────────────────────────────────────
log "Stopping Minecraft server..."
if server_running; then
    mc_cmd "kick @a Server going down for backup now. See you soon!"
    mc_cmd "stop"
    # Wait for the tmux session to exit (server stopped)
    for i in $(seq 1 60); do
        server_running || break
        sleep 1
    done
    if server_running; then
        log "WARN: Server did not stop cleanly after 60s — killing tmux session."
        tmux kill-session -t "$TMUX_SESSION" 2>/dev/null || true
    fi
fi
log "Server stopped."

# ── Status server ─────────────────────────────────────────────────────────────
log "Starting status server..."
python3 "$STATUS_SERVER" \
    --motd "§e⚙ Backup in progress §7— §aback soon!" \
    --status-file /tmp/mcvault_status &
STATUS_PID=$!
log "Status server PID: $STATUS_PID"

# ── Backup ────────────────────────────────────────────────────────────────────
log "Starting MC Vault backup..."
set +e
python3 "$MCVAULT" --backup --log-file "$LOG"
BACKUP_EXIT=$?
set -e

if [ "$BACKUP_EXIT" -eq 0 ]; then
    log "Backup completed successfully."
    admin_log "nightly backup complete"
else
    log "ERROR: Backup failed (exit code $BACKUP_EXIT). Server will still restart."
    admin_log "nightly backup FAILED (exit $BACKUP_EXIT)"
fi

# ── Tear down status server ───────────────────────────────────────────────────
log "Stopping status server..."
kill "$STATUS_PID" 2>/dev/null || true
wait "$STATUS_PID" 2>/dev/null || true
log "Status server stopped."

# ── Restart Minecraft ─────────────────────────────────────────────────────────
log "Restarting Minecraft server..."
sudo systemctl start minecraft
log "Minecraft server started."

log "Nightly backup sequence complete."
exit "$BACKUP_EXIT"
