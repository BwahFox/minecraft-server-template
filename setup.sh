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

# ── Distro detection ──────────────────────────────────────────────────────────
detect_distro() {
    local os_id os_id_like
    if [ -f /etc/os-release ]; then
        # shellcheck source=/dev/null
        . /etc/os-release
        os_id="${ID:-unknown}"
        os_id_like="${ID_LIKE:-}"
    else
        error "/etc/os-release not found — cannot detect distro."
        exit 1
    fi

    _has_id() { [[ "$os_id" == "$1" ]] || echo "$os_id_like" | grep -qw "$1"; }

    if _has_id "debian" || _has_id "ubuntu"; then
        PKG_FAMILY="debian"
        PKG_UPDATE="sudo apt-get update -qq"
        PKG_INSTALL="sudo apt-get install -y"
        P_TMUX="tmux" P_PYTHON3="python3" P_PYTHON3_TK="python3-tk"
        P_CURL="curl"  P_WIREGUARD="wireguard"
    elif _has_id "fedora" || _has_id "rhel" || _has_id "centos"; then
        PKG_FAMILY="fedora"
        PKG_UPDATE=""
        PKG_INSTALL="sudo dnf install -y"
        P_TMUX="tmux" P_PYTHON3="python3" P_PYTHON3_TK="python3-tkinter"
        P_CURL="curl"  P_WIREGUARD="wireguard-tools"
    elif _has_id "arch" || _has_id "manjaro"; then
        PKG_FAMILY="arch"
        PKG_UPDATE="sudo pacman -Sy"
        PKG_INSTALL="sudo pacman -S --noconfirm"
        P_TMUX="tmux" P_PYTHON3="python"   P_PYTHON3_TK="tk"
        P_CURL="curl"  P_WIREGUARD="wireguard-tools"
    else
        error "Unsupported distro: ID=$os_id  ID_LIKE=$os_id_like"
        error "Supported: debian/ubuntu, fedora/rhel/centos, arch/manjaro"
        exit 1
    fi

    info "Detected distro family: $PKG_FAMILY (ID=$os_id)"
}

detect_distro

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  minecraft-server-template — setup"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# ── Confirm username ──────────────────────────────────────────────────────────
CURRENT_USER=$(whoami)
read -rp "Username to run the server as [$CURRENT_USER]: " INPUT_USER
SERVER_USER="${INPUT_USER:-$CURRENT_USER}"

# ── Dedicated server user (experimental) ──────────────────────────────────────
USE_DEDICATED_USER=false
echo ""
warn "Advanced: you can run the server as a dedicated 'minecraft' system user."
warn "This is experimental — less tested than the default (running as yourself)."
read -rp "Create and use a dedicated 'minecraft' system user? [y/N]: " _DED
_DED="${_DED:-N}"
if [[ "$_DED" =~ ^[Yy]$ ]]; then
    if [ "$(id -u)" -ne 0 ]; then
        error "Dedicated user setup requires root. Re-run as: sudo bash setup.sh"
        exit 1
    fi
    USE_DEDICATED_USER=true
    SERVER_USER="minecraft"
    MINECRAFT_DIR="/home/minecraft"
    info "Will use system user 'minecraft' with home $MINECRAFT_DIR."
fi

# ── Confirm minecraft dir ─────────────────────────────────────────────────────
if [ "$USE_DEDICATED_USER" = false ]; then
    read -rp "Minecraft directory [$MINECRAFT_DIR]: " INPUT_DIR
    MINECRAFT_DIR="${INPUT_DIR:-$MINECRAFT_DIR}"
fi

# ── Java detection ────────────────────────────────────────────────────────────
echo ""
info "Detecting Java..."
JAVA_FOUND=""
if command -v java &>/dev/null; then
    JAVA_FOUND=$(command -v java)
elif [ -n "${JAVA_HOME:-}" ] && [ -x "$JAVA_HOME/bin/java" ]; then
    JAVA_FOUND="$JAVA_HOME/bin/java"
elif [ -x "$MINECRAFT_DIR/java/bin/java" ]; then
    JAVA_FOUND="$MINECRAFT_DIR/java/bin/java"
fi

DOWNLOAD_TEMURIN=false
if [ -n "$JAVA_FOUND" ]; then
    JAVA_VER=$("$JAVA_FOUND" -version 2>&1 | head -1)
    success "Found: $JAVA_FOUND ($JAVA_VER)"
    read -rp "Use this Java? [Y/n]: " _USE
    _USE="${_USE:-Y}"
    if [[ "$_USE" =~ ^[Yy]$ ]]; then
        JAVA_PATH="$JAVA_FOUND"
    else
        read -rp "Java executable path: " JAVA_PATH
    fi
else
    warn "Java not found in PATH or JAVA_HOME."
    read -rp "Download Eclipse Temurin 21 to $MINECRAFT_DIR/java/? [Y/n]: " _DL
    _DL="${_DL:-Y}"
    if [[ "$_DL" =~ ^[Yy]$ ]]; then
        DOWNLOAD_TEMURIN=true
        JAVA_PATH="$MINECRAFT_DIR/java/bin/java"
    else
        read -rp "Java executable path: " JAVA_PATH
        if [ -z "$JAVA_PATH" ]; then
            error "Java path is required. Exiting."
            exit 1
        fi
    fi
fi

# ── WireGuard opt-in ──────────────────────────────────────────────────────────
echo ""
read -rp "Use WireGuard VPN for remote access? [y/N]: " USE_WG
USE_WG="${USE_WG:-N}"

# ── Install dependencies ──────────────────────────────────────────────────────
echo ""
info "Installing dependencies..."
[ -n "$PKG_UPDATE" ] && $PKG_UPDATE
if [[ "$USE_WG" =~ ^[Yy]$ ]]; then
    $PKG_INSTALL $P_TMUX $P_WIREGUARD $P_PYTHON3 $P_PYTHON3_TK $P_CURL
else
    $PKG_INSTALL $P_TMUX $P_PYTHON3 $P_PYTHON3_TK $P_CURL
fi
success "Dependencies installed."

# ── Create directory structure ────────────────────────────────────────────────
info "Creating directory structure under $MINECRAFT_DIR..."
mkdir -p "$MINECRAFT_DIR/server"
mkdir -p "$MINECRAFT_DIR/servermanager"
mkdir -p "$MINECRAFT_DIR/java"
success "Directories ready."

# ── Download Temurin if needed ────────────────────────────────────────────────
if [ "$DOWNLOAD_TEMURIN" = true ]; then
    info "Downloading Eclipse Temurin 21..."
    ARCH=$(uname -m)
    case "$ARCH" in
        x86_64)  JDK_ARCH="x64" ;;
        aarch64) JDK_ARCH="aarch64" ;;
        armv7l)  JDK_ARCH="arm" ;;
        *)       error "Unsupported architecture: $ARCH"; exit 1 ;;
    esac
    TEMURIN_URL="https://api.adoptium.net/v3/binary/latest/21/ga/linux/$JDK_ARCH/jdk/hotspot/normal/eclipse"
    TMP_DIR=$(mktemp -d)
    curl -fL --progress-bar "$TEMURIN_URL" -o "$TMP_DIR/temurin.tar.gz"
    tar -xzf "$TMP_DIR/temurin.tar.gz" -C "$TMP_DIR"
    EXTRACTED=$(find "$TMP_DIR" -maxdepth 1 -type d -name "jdk-*" | head -1)
    cp -r "$EXTRACTED/." "$MINECRAFT_DIR/java/"
    rm -rf "$TMP_DIR"
    success "Temurin 21 installed to $MINECRAFT_DIR/java/."
fi

# ── Copy scripts ──────────────────────────────────────────────────────────────
info "Copying scripts to $MINECRAFT_DIR/servermanager..."
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cp "$SCRIPT_DIR/mc_vault.py"         "$MINECRAFT_DIR/servermanager/"
cp "$SCRIPT_DIR/mc_server.py"        "$MINECRAFT_DIR/servermanager/"
cp "$SCRIPT_DIR/mc_status_server.py" "$MINECRAFT_DIR/servermanager/"
cp "$SCRIPT_DIR/backup_nightly.sh"   "$MINECRAFT_DIR/servermanager/"
cp "$SCRIPT_DIR/minecraft.service"   "$MINECRAFT_DIR/servermanager/"
chmod +x "$MINECRAFT_DIR/servermanager/backup_nightly.sh"
success "Scripts copied."

# ── MC Server config ──────────────────────────────────────────────────────────
MC_SERVER_CFG="$MINECRAFT_DIR/servermanager/config.json"
if [ ! -f "$MC_SERVER_CFG" ]; then
    printf '{\n  "server_user": "%s"\n}\n' "$SERVER_USER" > "$MC_SERVER_CFG"
    success "MC Server config written."
fi

# ── MC Vault config ───────────────────────────────────────────────────────────
if [ "$USE_DEDICATED_USER" = true ]; then
    MCVAULT_CFG="$MINECRAFT_DIR/.config/mcvault/config.json"
else
    MCVAULT_CFG="$HOME/.config/mcvault/config.json"
fi
if [ -f "$MCVAULT_CFG" ]; then
    success "MC Vault config already exists — skipping."
else
    echo ""
    info "Creating MC Vault config..."
    read -rp "Google Drive folder name for backups [MinecraftVault]: " REMOTE_NAME
    REMOTE_NAME="${REMOTE_NAME:-MinecraftVault}"
    read -rp "Number of cloud backups to keep [3]: " KEEP_N
    KEEP_N="${KEEP_N:-3}"
    DEVICE_ID=$(python3 -c "import uuid; print(uuid.uuid4())")
    NOW_UTC=$(python3 -c "from datetime import datetime, timezone; print(datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))")
    mkdir -p "$(dirname "$MCVAULT_CFG")"
    cat > "$MCVAULT_CFG" << CFG_EOF
{
  "config_version": 1,
  "dark_mode": false,
  "default_backend": "rclone",
  "device_id": "$DEVICE_ID",
  "dh_policy": "exclude",
  "dh_remember_choice": false,
  "drive_chunk_size": "256M",
  "force_standalone": true,
  "keep_backups": $KEEP_N,
  "last_modified_utc": "$NOW_UTC",
  "rclone_cmd": "rclone",
  "remote_root": "gdrive:$REMOTE_NAME",
  "standalone_world_dir": "$MINECRAFT_DIR/server/world",
  "usb_root": "",
  "usb_vault_name": "MinecraftVault"
}
CFG_EOF
    success "MC Vault config written to $MCVAULT_CFG."
fi

# ── Create dedicated system user (user created here; chown deferred until after all writes) ──
if [ "$USE_DEDICATED_USER" = true ]; then
    if id "minecraft" &>/dev/null; then
        success "System user 'minecraft' already exists."
    else
        info "Creating system user 'minecraft'..."
        sudo useradd -r -M -d "$MINECRAFT_DIR" -s /bin/bash minecraft
        success "System user 'minecraft' created."
    fi
fi

# ── Install systemd service ───────────────────────────────────────────────────
info "Installing minecraft.service..."
SERVICE_DEST="/etc/systemd/system/minecraft.service"

if [[ "$USE_WG" =~ ^[Yy]$ ]]; then
    WG_DEPS="After=network.target wg-quick@wg0.service
Wants=wg-quick@wg0.service"
else
    WG_DEPS="After=network.target"
fi

sudo tee "$SERVICE_DEST" > /dev/null << SERVICE_EOF
[Unit]
Description=Minecraft Fabric Server
$WG_DEPS

[Service]
Type=forking
User=$SERVER_USER
WorkingDirectory=$MINECRAFT_DIR/server

ExecStart=/usr/bin/tmux new-session -d -s minecraft '$JAVA_PATH -Xmx8192M -Xms8192M -jar $MINECRAFT_DIR/server/server.jar nogui'
ExecStop=/usr/bin/tmux send-keys -t minecraft "stop" Enter
TimeoutStopSec=60

Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
SERVICE_EOF

sudo systemctl daemon-reload
sudo systemctl enable minecraft
success "minecraft.service installed and enabled."

# ── WireGuard ─────────────────────────────────────────────────────────────────
if [[ "$USE_WG" =~ ^[Yy]$ ]]; then
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
else
    info "Skipping WireGuard setup."
fi

# ── sudoers ───────────────────────────────────────────────────────────────────
echo ""
info "Configuring sudoers for passwordless systemctl..."
SUDOERS_FILE="/etc/sudoers.d/minecraft"
SYSTEMCTL_LINE="$CURRENT_USER ALL=(ALL) NOPASSWD: /usr/bin/systemctl start minecraft, /usr/bin/systemctl stop minecraft, /usr/bin/systemctl is-active minecraft"

if [ "$USE_DEDICATED_USER" = true ]; then
    TMUX_LINE="$CURRENT_USER ALL=(minecraft) NOPASSWD: /usr/bin/tmux, /usr/bin/python3"
    printf '%s\n%s\n' "$SYSTEMCTL_LINE" "$TMUX_LINE" | sudo tee "$SUDOERS_FILE" > /dev/null
else
    echo "$SYSTEMCTL_LINE" | sudo tee "$SUDOERS_FILE" > /dev/null
fi
sudo chmod 440 "$SUDOERS_FILE"
success "sudoers configured at $SUDOERS_FILE."

# ── Cron job ──────────────────────────────────────────────────────────────────
echo ""
read -rp "Install nightly backup cron job? (runs at 23:50) [Y/n]: " INSTALL_CRON
INSTALL_CRON="${INSTALL_CRON:-Y}"
if [[ "$INSTALL_CRON" =~ ^[Yy]$ ]]; then
    CRON_LINE="50 23 * * * $MINECRAFT_DIR/servermanager/backup_nightly.sh"
    if [ "$USE_DEDICATED_USER" = true ]; then
        CRONTAB_CMD=(sudo -u minecraft crontab)
    else
        CRONTAB_CMD=(crontab)
    fi
    EXISTING_CRON=$("${CRONTAB_CMD[@]}" -l 2>/dev/null || true)
    {
        if [[ -n "$EXISTING_CRON" ]]; then
            echo "$EXISTING_CRON" | grep -v "backup_nightly" || true
        fi
        echo "$CRON_LINE"
    } | "${CRONTAB_CMD[@]}" -
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
if [ ! -f "$MINECRAFT_DIR/server/eula.txt" ]; then
    echo ""
    read -rp "Accept the Minecraft EULA? (https://aka.ms/MinecraftEULA) [Y/n]: " ACCEPT_EULA
    ACCEPT_EULA="${ACCEPT_EULA:-Y}"
    if [[ "$ACCEPT_EULA" =~ ^[Yy]$ ]]; then
        echo "eula=true" > "$MINECRAFT_DIR/server/eula.txt"
        success "EULA accepted."
    else
        warn "You must accept the EULA before starting the server."
    fi
fi

# ── Fabric server jar ─────────────────────────────────────────────────────────
SERVER_JAR_READY=false
if [ -f "$MINECRAFT_DIR/server/server.jar" ]; then
    success "server.jar already present."
    SERVER_JAR_READY=true
else
    echo ""
    read -rp "Download Fabric server jar? [Y/n]: " DL_FABRIC
    DL_FABRIC="${DL_FABRIC:-Y}"
    if [[ "$DL_FABRIC" =~ ^[Yy]$ ]]; then
        read -rp "Minecraft version (e.g. 1.21.4): " MC_VERSION
        if [ -n "$MC_VERSION" ]; then
            info "Fetching latest Fabric versions..."
            LOADER_TMP=$(mktemp)
            INSTALLER_TMP=$(mktemp)
            curl -fsSL "https://meta.fabricmc.net/v2/versions/loader"    \
                | python3 -c "import json,sys; print(json.load(sys.stdin)[0]['version'])" > "$LOADER_TMP" &
            curl -fsSL "https://meta.fabricmc.net/v2/versions/installer" \
                | python3 -c "import json,sys; print(json.load(sys.stdin)[0]['version'])" > "$INSTALLER_TMP" &
            wait
            LOADER_VER=$(cat "$LOADER_TMP");    rm -f "$LOADER_TMP"
            INSTALLER_VER=$(cat "$INSTALLER_TMP"); rm -f "$INSTALLER_TMP"
            FABRIC_URL="https://meta.fabricmc.net/v2/versions/loader/$MC_VERSION/$LOADER_VER/$INSTALLER_VER/server/jar"
            info "Downloading Fabric $MC_VERSION (loader $LOADER_VER, installer $INSTALLER_VER)..."
            curl -fL --progress-bar "$FABRIC_URL" -o "$MINECRAFT_DIR/server/server.jar"
            success "server.jar downloaded."
            SERVER_JAR_READY=true
        else
            warn "No version entered — copy server.jar to $MINECRAFT_DIR/server/ manually."
        fi
    else
        warn "Copy your Fabric server jar to $MINECRAFT_DIR/server/server.jar before starting."
    fi
fi

# ── Hand ownership to the dedicated user (all writes complete) ────────────────
if [ "$USE_DEDICATED_USER" = true ]; then
    info "Setting ownership of $MINECRAFT_DIR to minecraft..."
    sudo chown -R minecraft:minecraft "$MINECRAFT_DIR"
    success "Ownership set."
fi

# ── Start server now? ─────────────────────────────────────────────────────────
echo ""
START_NOW="N"
if [ "$SERVER_JAR_READY" = true ]; then
    read -rp "Start the Minecraft server now? [Y/n]: " START_NOW
    START_NOW="${START_NOW:-Y}"
    if [[ "$START_NOW" =~ ^[Yy]$ ]]; then
        if [[ "$USE_WG" =~ ^[Yy]$ ]]; then
            sudo systemctl start wg-quick@wg0
            success "WireGuard started."
        fi
        sudo systemctl start minecraft
        success "Minecraft server started."
    fi
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Setup complete!"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

STEP=1
echo "Next steps:"
if [ "$SERVER_JAR_READY" = false ]; then
    echo "  $STEP. Copy server jar:       cp /path/to/fabric-server.jar $MINECRAFT_DIR/server/server.jar"
    STEP=$((STEP + 1))
fi
if [[ "$USE_WG" =~ ^[Yy]$ ]] && [[ ! "$START_NOW" =~ ^[Yy]$ ]]; then
    echo "  $STEP. Start WireGuard:       sudo systemctl start wg-quick@wg0"
    STEP=$((STEP + 1))
fi
if [[ ! "$START_NOW" =~ ^[Yy]$ ]]; then
    echo "  $STEP. Start the server:      sudo systemctl start minecraft"
    STEP=$((STEP + 1))
fi
echo "  $STEP. Manage the server:     python3 $MINECRAFT_DIR/servermanager/mc_server.py"
echo ""
