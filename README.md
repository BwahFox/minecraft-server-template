# minecraft-server-template

A self-hosted Minecraft Java server stack with automated backups, a server management TUI, and a WireGuard-based remote access setup. Built for Fabric, but adaptable to other server software.

## What's included

| File | Description |
|---|---|
| `mc_vault.py` | World backup/restore tool with GUI, TUI, and headless (`--backup`) modes. Backs up to Google Drive via rclone. |
| `mc_server.py` | Server management TUI: start, stop, restart, send commands, view logs, trigger manual backup. |
| `mc_status_server.py` | Lightweight fake Minecraft server that shows a custom MOTD while the real server is down for backup. |
| `backup_nightly.sh` | Nightly backup orchestrator. Warns players, stops server, runs MC Vault, restarts server. Scheduled via cron. |
| `minecraft.service` | systemd unit file for the Minecraft server. |
| `setup.sh` | Interactive setup script. |

## Architecture

```
Your client
    │
    ▼
VPS (WireGuard endpoint)
    │  WireGuard tunnel
    ▼
Home server (Debian, Ryzen)
    ├── systemd → tmux → Fabric server
    ├── cron → backup_nightly.sh → mc_vault.py → Google Drive
    └── mc_server.py (TUI, accessible over SSH)
```

## Requirements

- Debian (or any systemd-based Linux)
- Python 3.9+
- Java 21+ (`~/java/bin/java` by default)
- [Fabric server jar](https://fabricmc.net/use/server/)
- [rclone](https://rclone.org/) configured with a Google Drive remote
- tmux
- WireGuard (for remote access via VPS)

## Quick start

```bash
# 1. Clone the repo
git clone https://github.com/youruser/minecraft-server-template
cd minecraft-server-template

# 2. Run the setup script
chmod +x setup.sh
./setup.sh

# 3. Drop your server jar in ~/minecraft/
cp /path/to/fabric-server.jar ~/minecraft/server.jar

# 4. Accept the EULA
echo "eula=true" > ~/minecraft/eula.txt

# 5. Configure rclone (if not already done)
rclone config

# 6. Start everything
sudo systemctl start wg-quick@wg0
sudo systemctl start minecraft
```

## Directory layout

```
~/
├── wg0.conf                  ← WireGuard config (move to /etc/wireguard/)
├── java/                     ← Java runtime (optional, falls back to system java)
│   └── bin/
│       └── java
└── minecraft/
    ├── server.jar
    ├── world/                ← World folder (set as standalone_world_dir in MC Vault)
    ├── mods/                 ← Fabric mods
    ├── logs/
    │   └── latest.log
    ├── mc_vault.py
    ├── mc_server.py
    ├── mc_status_server.py
    ├── backup_nightly.sh
    ├── backup.log            ← Created automatically
    └── server.properties
```

## MC Vault

MC Vault handles world backups. It supports three modes:

```bash
# Graphical UI (requires display)
python3 ~/minecraft/mc_vault.py --gui

# Terminal UI (works over SSH)
python3 ~/minecraft/mc_vault.py --tui

# Headless backup (for scripting/cron)
python3 ~/minecraft/mc_vault.py --backup

# Headless with overrides
python3 ~/minecraft/mc_vault.py --backup \
    --world-dir ~/minecraft/world \
    --remote-instance myserver \
    --log-file ~/minecraft/backup.log
```

On first run, open Settings and configure:
- **Standalone world dir** — path to your world folder (e.g. `~/minecraft/world`)
- **Force standalone** — on (skips PrismLauncher detection)
- **Drive chunk size** — `256M` recommended for large worlds

Backups are stored at `gdrive:MinecraftVault/<remote-instance>/<world-name>/`.

## MC Server TUI

```bash
python3 ~/minecraft/mc_server.py
```

Works over SSH. Actions:
- **Start / Stop / Restart** — calls systemctl
- **Send command** — sends to the server via tmux (no leading `/` needed)
- **View logs** — tails `~/minecraft/logs/latest.log` live
- **Backup now** — stops server, runs MC Vault, restarts server

## Nightly backup

The nightly script runs at 23:50 via cron and:

1. Warns players at 10, 5, and 1 minute
2. Saves and stops the server at midnight
3. Starts the status server (shows MOTD to connecting clients)
4. Runs `mc_vault.py --backup`
5. Stops the status server
6. Restarts the Minecraft server

To install the cron job:
```bash
crontab -e
# Add:
50 23 * * * /home/user/minecraft/backup_nightly.sh
```

## systemd service

The Minecraft server runs as a systemd service that starts after WireGuard:

```bash
sudo systemctl enable wg-quick@wg0
sudo systemctl enable minecraft
sudo systemctl start minecraft
```

To interact with the server console directly:
```bash
tmux attach -t minecraft   # Ctrl+B then D to detach
```

## sudoers

The following commands need passwordless sudo for the backup scripts to work unattended:

```
user ALL=(ALL) NOPASSWD: /usr/bin/systemctl start minecraft
user ALL=(ALL) NOPASSWD: /usr/bin/systemctl stop minecraft
user ALL=(ALL) NOPASSWD: /usr/bin/systemctl is-active minecraft
```

Add via `sudo visudo`.

## WireGuard

Move your WireGuard config to `/etc/wireguard/wg0.conf` so systemd can manage it:

```bash
sudo mv ~/wg0.conf /etc/wireguard/wg0.conf
sudo chmod 600 /etc/wireguard/wg0.conf
sudo systemctl enable wg-quick@wg0
```

Your VPS needs to forward port 25565 (TCP) through the tunnel to your server.

## Whitelist

This setup is designed for a private whitelist-only server:

```bash
# In-game or via tmux:
/whitelist on
/whitelist add YourUsername
```

For Bedrock clients via Geyser+Floodgate, connect once from your Bedrock device then:
```bash
/whitelist add .YourBedrockUsername
```
