#!/usr/bin/env bash
# test_backup.sh — Exercises the full mc_vault backup pipeline with a fake rclone.
# mc_vault --backup runs for real, so every log() → _write_status() call is the
# actual production code.  Nothing is uploaded to Google Drive.

set -euo pipefail

MINECRAFT_DIR="$HOME/minecraft"
SERVERMANAGER_DIR="$MINECRAFT_DIR/servermanager"
STATUS_SERVER="$SERVERMANAGER_DIR/mc_status_server.py"
MC_VAULT="$SERVERMANAGER_DIR/mc_vault.py"
LOG="$SERVERMANAGER_DIR/backup.log"
TMUX_SESSION="minecraft"
FAKE_WORLD="/tmp/mc_test_world"
STATUS_FILE="/tmp/mcvault_status"

# Seconds between each rclone progress line (8 lines total → 8× this = upload sim time)
FAKE_RCLONE_STEP=2

# Isolated scratch space — never touches the real config or rclone
TMPDIR_ROOT="/tmp/mc_vault_test_$$"
FAKE_RCLONE="$TMPDIR_ROOT/rclone"
FAKE_HOME="$TMPDIR_ROOT/home"

STATUS_PID=""

cleanup() {
    [[ -n "$STATUS_PID" ]] && { kill "$STATUS_PID" 2>/dev/null; wait "$STATUS_PID" 2>/dev/null || true; }
    rm -rf "$FAKE_WORLD" "$TMPDIR_ROOT"
    rm -f "$STATUS_FILE"
}
trap cleanup EXIT

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [TEST] $*" | tee -a "$LOG"
}

mc_cmd() {
    tmux send-keys -t "$TMUX_SESSION" "$1" Enter 2>/dev/null || true
}

server_running() {
    tmux has-session -t "$TMUX_SESSION" 2>/dev/null
}

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  MC Backup Test"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# ── Ask whether to stop the real server ───────────────────────────────────────
read -rp "Stop the real Minecraft server during the test? [y/N]: " STOP_SERVER
STOP_SERVER="${STOP_SERVER:-N}"

# ── Create fake world ──────────────────────────────────────────────────────────
log "Creating fake test world at $FAKE_WORLD..."
mkdir -p "$FAKE_WORLD"
dd if=/dev/urandom of="$FAKE_WORLD/level.dat"  bs=1K count=128 2>/dev/null
dd if=/dev/urandom of="$FAKE_WORLD/region.mca" bs=1K count=512 2>/dev/null
log "Fake world ready (640 KB)."

# ── Write fake rclone ─────────────────────────────────────────────────────────
mkdir -p "$TMPDIR_ROOT"
cat > "$FAKE_RCLONE" << 'RCLONE_EOF'
#!/usr/bin/env python3
"""
Fake rclone for mc_vault testing.

Handles the three subcommands mc_vault uses during a backup:
  copyto --progress  — emits realistic \r-joined progress lines
  lsf                — returns 4 existing backups (triggers pruning with keep=3)
  deletefile         — succeeds silently

Everything else succeeds silently too.
"""
import os, sys, time

SUBCMD = sys.argv[1] if len(sys.argv) > 1 else ""

if SUBCMD == "copyto" and "--progress" in sys.argv:
    STEPS     = 8
    TOTAL_GiB = 0.640
    STEP_SECS = float(os.environ.get("FAKE_RCLONE_STEP", "1"))
    for i in range(1, STEPS + 1):
        pct  = i * 100 // STEPS
        done = round(TOTAL_GiB * i / STEPS, 3)
        eta  = int((STEPS - i) * STEP_SECS)
        spd  = round(2.0 + i * 0.2, 1)
        # Real rclone --progress joins the per-file line and the summary line with
        # a \r so they overwrite each other in a terminal.  Python's line iterator
        # in stream_cmd splits on \n, so both arrive as a single string with \r in
        # the middle — match what the regex in mc_vault's log() expects.
        print(
            f" *\tmc_test_world.zip:{pct}% /{TOTAL_GiB:.3f}Gi, {spd}Mi/s, {eta}s\r"
            f"Transferred:\t    {done:.3f} GiB / {TOTAL_GiB:.3f} GiB,"
            f" {pct}%, {spd} MiB/s, ETA {eta}s",
            flush=True,
        )
        time.sleep(STEP_SECS)
    # Final file-count line (no byte sizes → regex won't match, falls back to
    # "uploading to cloud..." which is fine at 100%)
    print("Transferred:\t       1 / 1, 100%", flush=True)
    print(f"Elapsed time:   {int(STEPS * STEP_SECS)}s", flush=True)

elif SUBCMD == "lsf" and "--dirs-only" not in sys.argv:
    # One more than KEEP_DEFAULT=3 so the pruning branch is exercised.
    for name in [
        "mc_test_world_2026-03-14_00-00-01.zip",
        "mc_test_world_2026-03-15_00-00-01.zip",
        "mc_test_world_2026-03-16_00-00-01.zip",
        "mc_test_world_2026-03-17_00-00-01.zip",
    ]:
        print(name)

sys.exit(0)
RCLONE_EOF
chmod +x "$FAKE_RCLONE"

# ── Write isolated mc_vault config pointing at the fake rclone ────────────────
# HOME is overridden when running mc_vault so config_local_path() resolves here,
# keeping the real ~/.config/mcvault/config.json completely untouched.
mkdir -p "$FAKE_HOME/.config/mcvault"
cat > "$FAKE_HOME/.config/mcvault/config.json" << CFG_EOF
{
  "config_version": 1,
  "default_backend": "rclone",
  "keep_backups": 3,
  "remote_root": "gdrive:MinecraftVaultTest",
  "rclone_cmd": "$FAKE_RCLONE",
  "dh_policy": "exclude"
}
CFG_EOF

# ── Optionally stop the server ─────────────────────────────────────────────────
if [[ "$STOP_SERVER" =~ ^[Yy]$ ]]; then
    log "Stopping Minecraft server..."
    if server_running; then
        mc_cmd "kick @a Test backup starting — server will restart shortly."
        mc_cmd "stop"
        for i in $(seq 1 30); do
            server_running || break
            sleep 1
        done
        if server_running; then
            log "WARN: Server did not stop cleanly — killing tmux session."
            tmux kill-session -t "$TMUX_SESSION" 2>/dev/null || true
        fi
    fi
    log "Server stopped."
fi

# ── Start status server ────────────────────────────────────────────────────────
log "Starting status server..."
python3 "$STATUS_SERVER" \
    --motd "§e⚙ Backup in progress §7— §aback soon!" \
    --status-file "$STATUS_FILE" &
STATUS_PID=$!
log "Status server PID: $STATUS_PID"

# ── Run mc_vault --backup (real code, fake rclone, isolated HOME) ──────────────
log "Running mc_vault --backup with fake rclone (real log parsing)..."
log "Expected status sequence:"
log "  §e⚙ Backup starting..."
log "  §e⚙ Backup: zipping world..."
log "  §e⚙ Backup: uploading to cloud..."
log "  §e⚙ Backup: uploading 12% (2.2 MiB/s)  ... up to 100%"
log "  §e⚙ Backup: pruning old backups..."
log "  §a✓ Backup complete §7— §aserver restarting soon!"

BACKUP_EXIT=0
FAKE_RCLONE_STEP="$FAKE_RCLONE_STEP" HOME="$FAKE_HOME" \
    python3 "$MC_VAULT" \
        --backup \
        --world-dir "$FAKE_WORLD" \
        --log-file  "$LOG" \
    || BACKUP_EXIT=$?

# ── Stop status server ─────────────────────────────────────────────────────────
log "Stopping status server..."
kill "$STATUS_PID" 2>/dev/null || true
wait "$STATUS_PID" 2>/dev/null || true
STATUS_PID=""
log "Status server stopped."

# ── Optionally restart the server ─────────────────────────────────────────────
if [[ "$STOP_SERVER" =~ ^[Yy]$ ]]; then
    log "Restarting Minecraft server..."
    sudo systemctl start minecraft
    log "Server restarted."
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Test complete (mc_vault exit: $BACKUP_EXIT)."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

exit $BACKUP_EXIT
