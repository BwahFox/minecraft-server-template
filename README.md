# minecraft-server-template

A self-hosted Minecraft Java server stack with automated backups, a server management TUI, multi-world and multi-instance support, and WireGuard-based remote access. Built for Fabric.

> **Warning:** This software is under active development. Things may change or break at any time. I am not responsible for your server breaking — make sure you have backups.

## What's included

| File | Description |
|---|---|
| `mc_server.py` | Server management TUI: start/stop/restart, commands, logs, backups, TPS, mod/world/instance management. |
| `mc_vault.py` | World backup/restore tool with GUI, TUI, and headless (`--backup`) modes. Backs up to Google Drive via rclone. |
| `mc_status_server.py` | Lightweight fake Minecraft server that shows a custom MOTD while the real server is down for backup. |
| `backup_nightly.sh` | Nightly backup orchestrator. Warns players, stops server, runs MC Vault, restarts server. Scheduled via cron. |
| `minecraft.service` | systemd unit file for the Minecraft server. |
| `setup.sh` | Interactive setup script. Detects distro, installs dependencies, downloads Fabric, configures everything. |

## Architecture

```
Your client
    │
    ▼
VPS (WireGuard endpoint)
    │  WireGuard tunnel
    ▼
Home server (Linux, systemd)
    ├── systemd → tmux → Fabric server
    ├── cron → backup_nightly.sh → mc_vault.py → Google Drive
    └── mc_server.py (TUI, accessible over SSH)
```

## Requirements

- Linux with systemd (Debian/Ubuntu, Fedora/RHEL, or Arch/Manjaro)
- Python 3.9+
- Java 21+ (setup.sh can download Eclipse Temurin 21 automatically)
- [rclone](https://rclone.org/) configured with a Google Drive remote
- tmux
- WireGuard (optional, for remote access via VPS)

## Quick start

```bash
# 1. Clone the repo
git clone https://github.com/BwahFox/minecraft-server-template
cd minecraft-server-template

# 2. Run the setup script — it handles everything else
chmod +x setup.sh
./setup.sh

# 3. Configure rclone for Google Drive backups (if not already done)
rclone config

# 4. Manage the server
python3 ~/minecraft/servermanager/mc_server.py
```

`setup.sh` will:
- Detect your distro and install dependencies
- Download the Fabric server jar for your chosen MC version
- Accept the EULA
- Write systemd service and sudoers entries
- Optionally install the nightly backup cron job
- Optionally start the server

## Directory layout

```
~/minecraft/
├── server/                       ← Active server dir (symlink after multi-instance migration)
│   ├── server.jar
│   ├── world/                    ← Active world (symlink after multi-world migration)
│   ├── worlds/                   ← All worlds (created by world manager)
│   │   ├── survival/
│   │   └── creative/
│   ├── mods/
│   ├── logs/
│   │   └── latest.log
│   └── eula.txt
├── instances/                    ← All instances (created by instance manager)
│   ├── survival/
│   └── creative/
└── servermanager/
    ├── mc_server.py
    ├── mc_vault.py
    ├── mc_status_server.py
    ├── backup_nightly.sh
    ├── config.json
    └── backup.log
```

## MC Server TUI

```bash
python3 ~/minecraft/servermanager/mc_server.py
```

Works over SSH. Menu options:

| Option | Description |
|---|---|
| Start / Stop / Restart | Manages the server via systemctl |
| Send command | Sends a command to the server via tmux |
| Quick commands | Saved shortcuts for frequently used commands |
| Online players | Live view of connected players (parsed from logs) |
| Check TPS | Queries `tick query` and displays TPS, MSPT, and percentiles |
| View logs | Live tail of `latest.log` |
| Backup now | Stops server, runs MC Vault, restarts server |
| Check for updates | Checks and applies Fabric loader and mod updates via Modrinth |
| Manage mods | Add, remove, and toggle beta channel for tracked mods |
| Manage worlds | Create, switch, rename, and delete worlds within an instance |
| Manage instances | Create, switch, rename, and delete server instances |
| View admin log | Shows the admin action log |

## Multi-world and multi-instance

**Multi-world** allows multiple worlds within a single server instance. The active world is symlinked as `server/world`. Switch worlds from the TUI — the server stops, the symlink is updated, and the server can be restarted.

**Multi-instance** allows multiple completely separate server setups (different MC versions, different mod lists, etc.). The active instance is symlinked as `minecraft/server`. Switch instances from the TUI the same way.

On first opening either manager, you will be prompted to migrate your existing `world/` or `server/` folder to the new layout.

## Fabric / mod updater

The TUI can check for and apply updates to the Fabric loader and any tracked mods:

- Mods are tracked by Modrinth project ID or slug
- Per-mod `allow_beta` flag for mods that only publish beta releases (e.g. Distant Horizons)
- Downloads are atomic (`.part` file renamed on completion)
- Server is stopped before any install or update

## Nightly backup

The nightly script runs at 23:50 via cron and:

1. Warns players at 10, 5, and 1 minute
2. Kicks all players and stops the server at midnight
3. Starts the status server (shows MOTD to connecting clients)
4. Runs `mc_vault.py --backup`
5. Stops the status server
6. Restarts the Minecraft server

The cron job can be installed automatically by `setup.sh`, or manually:
```bash
crontab -e
# Add:
50 23 * * * /home/user/minecraft/servermanager/backup_nightly.sh
```

## systemd service

```bash
sudo systemctl enable minecraft
sudo systemctl start minecraft
```

To interact with the server console directly:
```bash
tmux attach -t minecraft   # Ctrl+B then D to detach
```

## WireGuard

Move your WireGuard config to `/etc/wireguard/wg0.conf`:

```bash
sudo mv ~/wg0.conf /etc/wireguard/wg0.conf
sudo chmod 600 /etc/wireguard/wg0.conf
sudo systemctl enable wg-quick@wg0
```

Your VPS needs to forward port 25565 (TCP) through the tunnel to your server.

## Whitelist

This setup is designed for a private whitelist-only server:

```bash
/whitelist on
/whitelist add YourUsername
```

For Bedrock clients via Geyser+Floodgate, connect once from your Bedrock device then:
```bash
/whitelist add .YourBedrockUsername
```
