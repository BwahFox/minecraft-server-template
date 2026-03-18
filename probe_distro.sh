#!/usr/bin/env bash
# probe_distro.sh — Dry-run probe for multi-distro support.
#
# Detects the current Linux distro and prints exactly what setup.sh
# would install and how, without actually running any package manager.
#
# Usage:
#   bash probe_distro.sh
#
# To simulate a different distro (for testing the detection logic):
#   DISTRO_ID=fedora bash probe_distro.sh
#   DISTRO_ID=arch   bash probe_distro.sh

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

ok()   { echo -e "${GREEN}  ✓${NC} $*"; }
fail() { echo -e "${RED}  ✗${NC} $*"; }
info() { echo -e "${BLUE}  ·${NC} $*"; }
warn() { echo -e "${YELLOW}  !${NC} $*"; }

section() {
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  $1"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
}

# ── 1. Distro detection ───────────────────────────────────────────────────────

section "1. Distro detection"

# Allow override for testing on a box you don't want to run apt/dnf/pacman on.
if [ -n "${DISTRO_ID:-}" ]; then
    warn "DISTRO_ID override: '$DISTRO_ID' (ignoring /etc/os-release)"
    OS_ID="$DISTRO_ID"
    OS_ID_LIKE="${DISTRO_ID_LIKE:-}"
elif [ -f /etc/os-release ]; then
    # shellcheck source=/dev/null
    . /etc/os-release
    OS_ID="${ID:-unknown}"
    OS_ID_LIKE="${ID_LIKE:-}"
    info "/etc/os-release: ID=$OS_ID  ID_LIKE=$OS_ID_LIKE"
    info "Pretty name: ${PRETTY_NAME:-unknown}"
else
    fail "/etc/os-release not found — cannot detect distro."
    exit 1
fi

# ── 2. Package manager resolution ─────────────────────────────────────────────

section "2. Package manager resolution"

PKG_MGR=""
PKG_UPDATE=""
PKG_INSTALL_BASE=""  # no package names yet

# Helper: check if ID or ID_LIKE contains a token
has_id() {
    local token="$1"
    [[ "$OS_ID" == "$token" ]] || echo "$OS_ID_LIKE" | grep -qw "$token"
}

if has_id "debian" || has_id "ubuntu"; then
    PKG_MGR="apt-get"
    PKG_UPDATE="sudo apt-get update -qq"
    PKG_INSTALL_BASE="sudo apt-get install -y"
    PKG_FAMILY="debian"
elif has_id "fedora" || has_id "rhel" || has_id "centos"; then
    PKG_MGR="dnf"
    PKG_UPDATE=""   # dnf install -y handles updates itself
    PKG_INSTALL_BASE="sudo dnf install -y"
    PKG_FAMILY="fedora"
elif has_id "arch" || has_id "manjaro"; then
    PKG_MGR="pacman"
    PKG_UPDATE="sudo pacman -Sy"
    PKG_INSTALL_BASE="sudo pacman -S --noconfirm"
    PKG_FAMILY="arch"
else
    fail "Unrecognised distro: ID=$OS_ID  ID_LIKE=$OS_ID_LIKE"
    info "Supported families: debian/ubuntu, fedora/rhel/centos, arch/manjaro"
    exit 1
fi

if command -v "$PKG_MGR" &>/dev/null; then
    ok "Package manager: $PKG_MGR  ($(command -v "$PKG_MGR"))"
else
    fail "Package manager '$PKG_MGR' not found in PATH — distro detection may be wrong."
fi

# ── 3. Package name mapping ────────────────────────────────────────────────────

section "3. Package name mapping"

case "$PKG_FAMILY" in
    debian)
        P_TMUX="tmux"
        P_PYTHON3="python3"
        P_PYTHON3_TK="python3-tk"
        P_CURL="curl"
        P_WIREGUARD="wireguard"
        ;;
    fedora)
        P_TMUX="tmux"
        P_PYTHON3="python3"
        P_PYTHON3_TK="python3-tkinter"
        P_CURL="curl"
        P_WIREGUARD="wireguard-tools"
        ;;
    arch)
        P_TMUX="tmux"
        P_PYTHON3="python"
        P_PYTHON3_TK="tk"
        P_CURL="curl"
        P_WIREGUARD="wireguard-tools"
        ;;
esac

printf "  %-18s → %s\n" "python3"     "$P_PYTHON3"
printf "  %-18s → %s\n" "python3-tk"  "$P_PYTHON3_TK"
printf "  %-18s → %s\n" "tmux"        "$P_TMUX"
printf "  %-18s → %s\n" "curl"        "$P_CURL"
printf "  %-18s → %s\n" "wireguard"   "$P_WIREGUARD"

# ── 4. Simulated install commands ─────────────────────────────────────────────

section "4. Simulated install commands (dry-run — nothing actually executed)"

BASE_PKGS="$P_TMUX $P_PYTHON3 $P_PYTHON3_TK $P_CURL"
WG_PKGS="$BASE_PKGS $P_WIREGUARD"

if [ -n "$PKG_UPDATE" ]; then
    info "Update step:      $PKG_UPDATE"
fi
info "Without WireGuard: $PKG_INSTALL_BASE $BASE_PKGS"
info "With WireGuard:    $PKG_INSTALL_BASE $WG_PKGS"

# ── 5. Check already-installed packages ───────────────────────────────────────

section "5. Currently installed binaries (informational)"

check_bin() {
    local name="$1"
    local cmd="${2:-$1}"
    if command -v "$cmd" &>/dev/null; then
        ok "$name: $(command -v "$cmd")"
    else
        warn "$name: not found in PATH"
    fi
}

check_bin "python3" "python3"
check_bin "python (arch alias)" "python"
check_bin "tmux"
check_bin "curl"
check_bin "rclone"

# Check for tkinter by trying to import it
if python3 -c "import tkinter" &>/dev/null 2>&1; then
    ok "python3 tkinter: importable"
elif python3 -c "import tkinter" 2>&1 | grep -q "No module"; then
    warn "python3 tkinter: NOT importable — will be installed by setup.sh"
else
    warn "python3 not available — cannot check tkinter"
fi

# ── 6. systemd check ──────────────────────────────────────────────────────────

section "6. systemd"

if command -v systemctl &>/dev/null; then
    if systemctl is-system-running &>/dev/null || systemctl status &>/dev/null 2>&1; then
        ok "systemctl found and system appears to be running systemd"
    else
        ok "systemctl found (may be container — systemd not init)"
    fi
else
    fail "systemctl not found — systemd required for minecraft.service"
fi

# ── 7. WireGuard kernel module ────────────────────────────────────────────────

section "7. WireGuard kernel module (optional)"

if modinfo wireguard &>/dev/null 2>&1; then
    ok "wireguard kernel module available"
elif [ -d /sys/module/wireguard ]; then
    ok "wireguard module already loaded"
else
    info "wireguard module not detected — normal if WireGuard is not being used"
    info "(on Fedora/Arch it ships in-kernel since 5.6; on older Debian install wireguard-dkms)"
fi

# ── Summary ───────────────────────────────────────────────────────────────────

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Probe complete."
echo ""
printf "  Distro family : %s\n" "$PKG_FAMILY"
printf "  Package mgr   : %s\n" "$PKG_MGR"
printf "  Base packages : %s\n" "$BASE_PKGS"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
