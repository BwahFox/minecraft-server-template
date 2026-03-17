#!/usr/bin/env bash
# setup.sh — Interactive setup script for minecraft-server-template.
# Run once after cloning the repo.

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

info()    { echo -e "${BLUE}[INFO]${NC} $*"; }
success() { echo -e "${GREEN}[OK]${NC}   $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $*"; }
error()   { echo -e "${RED}[ERR]${NC}  $*"; }

MINECRAFT_DIR="$HOME/minecraft"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  minecraft-server-template — setup"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# ── Confirm username ──────────────────────────────────────────────────────────
CURRENT_USER=$(whoami)
read -rp "Username to run the server as [$CURRENT_USER]: " INPUT_USER
SERVER_USER="${INPUT_USER:-$CURRENT_USER}"

# ── Confirm minecraft dir ─────────────────────────────────────────────────────
read -rp "Minecraft directory [$MINECRAFT_DIR]: " INPUT_DIR
MINECRAFT_DIR="${INPUT_DIR:-$MINECRAFT_DIR}"

# ── Confirm Java path ─────────────────────────────────────────────────────────
DEFAULT_JAVA="$HOME/java/bin/java"
read -rp "Java executable path [$DEFAULT_JAVA]: " INPUT_JAVA
JAVA_PATH="${INPUT_JAVA:-$DEFAULT_JAVA}"

# ── Install dependencies ──────────────────────────────────────────────────────
echo ""
info "Installing dependencies..."
sudo apt-get update -qq
sudo apt-get install -y tmux wireguard python3 curl
success "Dependencies installed."

# ── Create minecraft directory ────────────────────────────────────────────────
info "Creating $MINECRAFT_DIR..."
mkdir -p "$MINECRAFT_DIR"
success "Directory ready."

# ── Copy scripts ──────────────────────────────────────────────────────────────
info "Copying scripts to $MINECRAFT_DIR..."
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cp "$SCRIPT_DIR/mc_vault.py"        "$MINECRAFT_DIR/"
cp "$SCRIPT_DIR/mc_server.py"       "$MINECRAFT_DIR/"
cp "$SCRIPT_DIR/mc_status_server.py" "$MINECRAFT_DIR/"
cp "$SCRIPT_DIR/backup_nightly.sh"  "$MINECRAFT_DIR/"
chmod +x "$MINECRAFT_DIR/backup_nightly.sh"
success "Scripts copied."

# ── Patch backup_nightly.sh with correct username ─────────────────────────────
info "Configuring backup_nightly.sh..."
# Already uses $HOME so no patching needed — just confirm MCVAULT path
success "backup_nightly.sh ready."

# ── Install systemd service ───────────────────────────────────────────────────
info "Installing minecraft.service..."
SERVICE_SRC="$SCRIPT_DIR/minecraft.service"
SERVICE_DEST="/etc/systemd/system/minecraft.service"

# Substitute placeholders
sed \
    -e "s|/home/user|/home/$SERVER_USER|g" \
    -e "s|User=user|User=$SERVER_USER|g" \
    -e "s|\$HOME/java/bin/java|$JAVA_PATH|g" \
    "$SERVICE_SRC" | sudo tee "$SERVICE_DEST" > /dev/null

sudo systemctl daemon-reload
sudo systemctl enable minecraft
success "minecraft.service installed and enabled."

# ── WireGuard ─────────────────────────────────────────────────────────────────
echo ""
info "WireGuard setup:"
if [ -f "$HOME/wg0.conf" ]; then
    warn "Found ~/wg0.conf — moving to /etc/wireguard/wg0.conf"
    sudo mv "$HOME/wg0.conf" /etc/wireguard/wg0.conf
    sudo chmod 600 /etc/wireguard/wg0.conf
    sudo systemctl enable wg-quick@wg0
    success "WireGuard configured."
elif [ -f "/etc/wireguard/wg0.conf" ]; then
    success "WireGuard config already at /etc/wireguard/wg0.conf."
    sudo systemctl enable wg-quick@wg0
else
    warn "No wg0.conf found. Copy your WireGuard config to /etc/wireguard/wg0.conf manually."
fi

# ── sudoers ───────────────────────────────────────────────────────────────────
echo ""
info "Configuring sudoers for passwordless systemctl..."
SUDOERS_LINE="$SERVER_USER ALL=(ALL) NOPASSWD: /usr/bin/systemctl start minecraft, /usr/bin/systemctl stop minecraft, /usr/bin/systemctl is-active minecraft"
SUDOERS_FILE="/etc/sudoers.d/minecraft"

echo "$SUDOERS_LINE" | sudo tee "$SUDOERS_FILE" > /dev/null
sudo chmod 440 "$SUDOERS_FILE"
success "sudoers configured at $SUDOERS_FILE."

# ── Cron job ──────────────────────────────────────────────────────────────────
echo ""
read -rp "Install nightly backup cron job? (runs at 23:50) [Y/n]: " INSTALL_CRON
INSTALL_CRON="${INSTALL_CRON:-Y}"
if [[ "$INSTALL_CRON" =~ ^[Yy]$ ]]; then
    CRON_LINE="50 23 * * * $MINECRAFT_DIR/backup_nightly.sh"
    ( crontab -l 2>/dev/null | grep -v "backup_nightly"; echo "$CRON_LINE" ) | crontab -
    success "Cron job installed."
else
    info "Skipping cron job. Add manually with: crontab -e"
fi

# ── rclone ────────────────────────────────────────────────────────────────────
echo ""
if command -v rclone &>/dev/null; then
    success "rclone is already installed."
else
    read -rp "rclone not found. Install it now? [Y/n]: " INSTALL_RCLONE
    INSTALL_RCLONE="${INSTALL_RCLONE:-Y}"
    if [[ "$INSTALL_RCLONE" =~ ^[Yy]$ ]]; then
        curl -fsSL https://rclone.org/install.sh | sudo bash
        success "rclone installed."
    else
        warn "Install rclone manually and run 'rclone config' to set up Google Drive."
    fi
fi

if command -v rclone &>/dev/null; then
    if ! rclone listremotes | grep -q "gdrive:"; then
        warn "No 'gdrive:' remote found in rclone config."
        info "Run 'rclone config' to set up Google Drive, then name the remote 'gdrive'."
    else
        success "rclone gdrive remote found."
    fi
fi

# ── eula ─────────────────────────────────────────────────────────────────────
if [ ! -f "$MINECRAFT_DIR/eula.txt" ]; then
    echo ""
    read -rp "Accept the Minecraft EULA? (https://aka.ms/MinecraftEULA) [Y/n]: " ACCEPT_EULA
    ACCEPT_EULA="${ACCEPT_EULA:-Y}"
    if [[ "$ACCEPT_EULA" =~ ^[Yy]$ ]]; then
        echo "eula=true" > "$MINECRAFT_DIR/eula.txt"
        success "EULA accepted."
    else
        warn "You must accept the EULA before starting the server."
    fi
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Setup complete!"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "Next steps:"
echo "  1. Copy your server jar:  cp /path/to/fabric-server.jar $MINECRAFT_DIR/server.jar"
echo "  2. Configure MC Vault:    python3 $MINECRAFT_DIR/mc_vault.py --tui"
echo "     - Set standalone world dir → $MINECRAFT_DIR/world"
echo "     - Toggle force standalone → on"
echo "  3. Start WireGuard:       sudo systemctl start wg-quick@wg0"
echo "  4. Start the server:      sudo systemctl start minecraft"
echo "  5. Manage the server:     python3 $MINECRAFT_DIR/mc_server.py"
echo ""
