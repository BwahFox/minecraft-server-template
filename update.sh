#!/usr/bin/env bash
# update.sh — Copy updated scripts into the servermanager directory.
# Run this after pulling changes from the repo.

set -euo pipefail

GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()    { echo -e "${BLUE}[INFO]${NC} $*"; }
success() { echo -e "${GREEN}[OK]${NC}   $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $*"; }

MINECRAFT_DIR="$HOME/minecraft"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="$MINECRAFT_DIR/servermanager"

if [ ! -d "$DEST" ]; then
    echo "Error: $DEST not found. Run setup.sh first."
    exit 1
fi

# ── Copy scripts ───────────────────────────────────────────────────────────────
if [ "$(realpath "$SCRIPT_DIR")" = "$(realpath "$DEST")" ]; then
    warn "update.sh is running from inside servermanager — skipping file copy."
    warn "To copy updated files, run update.sh from a separate repo clone."
else
    info "Copying scripts to $DEST..."
    cp "$SCRIPT_DIR/mc_server.py"        "$DEST/"
    cp "$SCRIPT_DIR/mc_vault.py"         "$DEST/"
    cp "$SCRIPT_DIR/mc_status_server.py" "$DEST/"
    cp "$SCRIPT_DIR/backup_nightly.sh"   "$DEST/"
    cp "$SCRIPT_DIR/test_backup.sh"      "$DEST/"
    cp "$SCRIPT_DIR/minecraft.service"   "$DEST/"
    chmod +x "$DEST/backup_nightly.sh"
    chmod +x "$DEST/test_backup.sh"
    success "Scripts updated."
fi

# ── Optionally update systemd service ─────────────────────────────────────────
echo ""
read -rp "Update the installed systemd service too? [y/N]: " UPDATE_SERVICE
UPDATE_SERVICE="${UPDATE_SERVICE:-N}"
if [[ "$UPDATE_SERVICE" =~ ^[Yy]$ ]]; then
    SERVICE_DEST="/etc/systemd/system/minecraft.service"

    # Detect the user from the existing installed service
    INSTALLED_USER=$(grep "^User=" "$SERVICE_DEST" 2>/dev/null | cut -d= -f2 || echo "")
    if [ -z "$INSTALLED_USER" ]; then
        warn "Could not detect user from existing service — using current user ($(whoami))."
        INSTALLED_USER="$(whoami)"
    fi

    # Detect whether WireGuard lines are present in the installed service
    SED_ARGS=(-e "s|/home/user|/home/$INSTALLED_USER|g" -e "s|User=user|User=$INSTALLED_USER|g")
    if ! grep -q "wg-quick" "$SERVICE_DEST" 2>/dev/null; then
        SED_ARGS+=(-e "/wg-quick@wg0/d")
    fi

    sed "${SED_ARGS[@]}" "$DEST/minecraft.service" | sudo tee "$SERVICE_DEST" > /dev/null
    sudo systemctl daemon-reload
    success "minecraft.service updated and reloaded."
else
    info "Skipping service update."
fi

echo ""
success "Update complete."
