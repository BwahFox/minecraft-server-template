#!/usr/bin/env python3
"""
mc_server.py — Minecraft server management TUI.
Companion tool to MC Vault. Manages the Fabric server via systemd + tmux.

Requires:
  - Python 3.9+
  - tmux
  - systemd (for start/stop/restart)
  - mc_vault.py (for backup)
  - mc_status_server.py (for backup status display)
"""

import curses
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# =============================================================================
# Configuration
# =============================================================================

SYSTEMD_SERVICE  = "minecraft"
TMUX_SESSION     = "minecraft"
MC_ROOT          = Path(__file__).parent.parent.resolve()
MCVAULT          = MC_ROOT / "servermanager" / "mc_vault.py"
STATUS_SERVER    = MC_ROOT / "servermanager" / "mc_status_server.py"
BACKUP_LOG       = MC_ROOT / "servermanager" / "backup.log"
ADMIN_LOG_FILE   = MC_ROOT / "servermanager" / "admin.log"
SERVER_LOG_FILE  = MC_ROOT / "server" / "logs" / "latest.log"
CONFIG_FILE      = MC_ROOT / "servermanager" / "config.json"

APP_NAME         = "MC Server"
APP_VERSION      = "2.0.0"

SERVER_JAR       = MC_ROOT / "server" / "server.jar"
MODS_DIR         = MC_ROOT / "server" / "mods"
WORLDS_DIR       = MC_ROOT / "server" / "worlds"
WORLD_LINK       = MC_ROOT / "server" / "world"
INSTANCES_DIR    = MC_ROOT / "instances"
SERVER_LINK      = MC_ROOT / "server"

_FABRIC_META     = "https://meta.fabricmc.net/v2"
_MODRINTH        = "https://api.modrinth.com/v2"
_UA              = "minecraft-server-manager/1.0 (mc_server.py)"

# =============================================================================
# Colour pairs
# =============================================================================

_CP_HEADER   = 1
_CP_SELECTED = 2
_CP_DIM      = 3
_CP_ERROR    = 4
_CP_OK       = 5
_CP_WARNING  = 6


def _init_colors() -> None:
    if not curses.has_colors():
        return
    curses.start_color()
    curses.use_default_colors()
    bg = -1
    try:
        curses.init_pair(_CP_HEADER,   curses.COLOR_BLUE,   bg)
        curses.init_pair(_CP_SELECTED, curses.COLOR_BLACK,  curses.COLOR_BLUE)
        curses.init_pair(_CP_DIM,      curses.COLOR_WHITE,  bg)
        curses.init_pair(_CP_ERROR,    curses.COLOR_RED,    bg)
        curses.init_pair(_CP_OK,       curses.COLOR_GREEN,  bg)
        curses.init_pair(_CP_WARNING,  curses.COLOR_YELLOW, bg)
    except Exception:
        pass


# =============================================================================
# System helpers
# =============================================================================

def _run(cmd: List[str], capture: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
        text=True,
    )


def server_running() -> bool:
    """True if the minecraft tmux session exists."""
    r = _run(_tmux(["has-session", "-t", TMUX_SESSION]), capture=True)
    return r.returncode == 0


def server_status() -> str:
    """One-line status string."""
    r = _run(["sudo", "systemctl", "is-active", SYSTEMD_SERVICE], capture=True)
    state = (r.stdout or "").strip()
    running = server_running()
    if state == "active" and running:
        return "● running"
    elif state == "active" and not running:
        return "? active (no tmux)"
    elif state == "inactive":
        return "○ stopped"
    elif state == "failed":
        return "✗ failed"
    return f"? {state}"


def mc_cmd(command: str) -> None:
    """Send a command to the Minecraft server via tmux."""
    _run(_tmux(["send-keys", "-t", TMUX_SESSION, command, "Enter"]))


def systemctl(action: str) -> int:
    r = _run(["sudo", "systemctl", action, SYSTEMD_SERVICE], capture=True)
    return r.returncode


try:
    ADMIN_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
except OSError:
    pass

_CURRENT_USER: str = os.environ.get("USER") or os.environ.get("LOGNAME") or "unknown"


def _admin_log(action: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [{_CURRENT_USER}] {action}\n"
    try:
        with ADMIN_LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


# =============================================================================
# Config (quick commands persistence)
# =============================================================================

def _load_config() -> Dict:
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_config(cfg: Dict) -> None:
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


_SERVER_USER_CACHE: Optional[str] = None


def _server_user() -> str:
    global _SERVER_USER_CACHE
    if _SERVER_USER_CACHE is None:
        cfg = _load_config()
        _SERVER_USER_CACHE = cfg.get("server_user", "")
    return _SERVER_USER_CACHE


def _need_sudo_u() -> bool:
    su = _server_user()
    return bool(su) and su != _CURRENT_USER


def _tmux(args: List[str]) -> List[str]:
    """Build a tmux command, prefixed with sudo -u <server_user> when needed."""
    if _need_sudo_u():
        return ["sudo", "-u", _server_user(), "tmux"] + args
    return ["tmux"] + args


def _as_server(cmd: List[str]) -> List[str]:
    """Prefix a command with sudo -u <server_user> when needed."""
    if _need_sudo_u():
        return ["sudo", "-u", _server_user()] + cmd
    return cmd


# =============================================================================
# Player list
# =============================================================================

_JOIN_RE       = re.compile(r"^\[[\d:]+\] \[.*?INFO\]: ([A-Za-z0-9_]+) joined the game", re.MULTILINE)
_LEAVE_RE      = re.compile(r"^\[[\d:]+\] \[.*?INFO\]: ([A-Za-z0-9_]+) (?:left the game|lost connection:)", re.MULTILINE)
_WORLD_NAME_RE = re.compile(r'^[A-Za-z0-9_-]+$')


def _parse_online_players() -> List[str]:
    """Return sorted list of players currently online, derived from latest.log."""
    if not SERVER_LOG_FILE.exists():
        return []
    try:
        text = SERVER_LOG_FILE.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    events: List[tuple] = []
    for m in _JOIN_RE.finditer(text):
        events.append((m.start(), "join", m.group(1)))
    for m in _LEAVE_RE.finditer(text):
        events.append((m.start(), "leave", m.group(1)))

    online: set = set()
    for _, kind, name in sorted(events):
        if kind == "join":
            online.add(name)
        else:
            online.discard(name)
    return sorted(online)


# =============================================================================
# Update checker
# =============================================================================

def _http_get(url: str) -> Any:
    """Fetch JSON from url. Raises urllib.error.HTTPError / URLError on failure."""
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def _detect_fabric_loader_version() -> Optional[str]:
    """Return installed Fabric loader version string, or None."""
    _ver_re = re.compile(r"fabric-loader-(\d+\.\d+\.\d+)")
    fabric_dir = MC_ROOT / "server" / ".fabric" / "server"
    if fabric_dir.exists():
        for p in fabric_dir.glob("fabric-loader-*.jar"):
            m = _ver_re.search(p.name)
            if m:
                return m.group(1)
    return _load_config().get("fabric_loader_version") or None


def _check_fabric_update(mc_version: str) -> Dict:
    """Return Fabric loader update info dict."""
    try:
        loaders = _http_get(f"{_FABRIC_META}/versions/loader/{mc_version}")
        stable  = [l for l in loaders if l["loader"]["stable"]]
        latest_loader = (stable or loaders)[0]["loader"]["version"]

        installers   = _http_get(f"{_FABRIC_META}/versions/installer")
        stable_inst  = [i for i in installers if i["stable"]]
        latest_inst  = (stable_inst or installers)[0]["version"]

        current = _detect_fabric_loader_version()
        url = (f"{_FABRIC_META}/versions/loader/{mc_version}"
               f"/{latest_loader}/{latest_inst}/server/jar")
        return {
            "kind": "fabric", "name": "Fabric loader",
            "current": current, "latest": latest_loader, "url": url,
            "update_available": current != latest_loader, "error": None,
        }
    except Exception as e:
        return {
            "kind": "fabric", "name": "Fabric loader",
            "current": None, "latest": None, "url": None,
            "update_available": False, "error": str(e),
        }


def _check_mod_update(mod: Dict, mc_version: str) -> Dict:
    """Return Modrinth mod update info dict."""
    base: Dict = {
        "kind": "mod", "mod": mod,
        "latest_version": None, "latest_file": None,
        "latest_url": None, "latest_size": None,
        "update_available": False, "only_beta": False, "error": None,
    }
    try:
        params = urllib.parse.urlencode({
            "game_versions": json.dumps([mc_version]),
            "loaders":       json.dumps(["fabric"]),
        })
        versions = _http_get(f"{_MODRINTH}/project/{mod['id']}/version?{params}")
        if not versions:
            base["error"] = f"no versions for {mc_version}/fabric"
            return base

        allow_beta = mod.get("allow_beta", False)
        releases   = [v for v in versions if v["version_type"] == "release"]
        betas      = [v for v in versions if v["version_type"] == "beta"]

        if not releases:
            if betas:
                base["only_beta"] = True
                if not allow_beta:
                    return base           # no usable version without beta flag
                candidates = betas
            else:
                base["error"] = f"no release/beta for {mc_version}/fabric"
                return base
        else:
            candidates = releases

        latest = candidates[0]
        files  = latest.get("files", [])
        if not files:
            base["error"] = "no files attached to latest version"
            return base

        primary = next((f for f in files if f.get("primary")), files[0])
        base["latest_version"]  = latest["version_number"]
        base["latest_file"]     = primary["filename"]
        base["latest_url"]      = primary["url"]
        base["latest_size"]     = primary.get("size")
        base["update_available"] = mod.get("installed_file") != primary["filename"]
        return base
    except Exception as e:
        base["error"] = str(e)
        return base


def _lookup_modrinth_project(slug_or_id: str) -> Dict:
    """Return {"id", "title", "slug", "error"} dict."""
    try:
        proj = _http_get(f"{_MODRINTH}/project/{slug_or_id}")
        return {"id": proj["id"], "title": proj["title"],
                "slug": proj["slug"], "error": None}
    except Exception as e:
        return {"id": None, "title": None, "slug": None, "error": str(e)}


def _download_file(url: str, dest: Path,
                   progress_cb: Callable[[int, int], None]) -> None:
    """Download url → dest (atomic via .part temp). Calls progress_cb(done, total)."""
    part = dest.with_suffix(dest.suffix + ".part")
    req  = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            total = int(r.headers.get("Content-Length") or 0)
            done  = 0
            with part.open("wb") as f:
                while True:
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    progress_cb(done, total)
        part.rename(dest)
    except Exception:
        part.unlink(missing_ok=True)
        raise


# =============================================================================
# TUI
# =============================================================================

class ServerTUI:

    def __init__(self) -> None:
        self._log_lines: List[str] = []
        self._scr: "curses.window" = None  # type: ignore[assignment]

    # ------------------------------------------------------------------ run
    def run(self) -> None:
        curses.wrapper(self._main)

    def _main(self, scr: "curses.window") -> None:
        self._scr = scr
        curses.curs_set(0)
        _init_colors()
        self.log(f"{APP_NAME} v{APP_VERSION} ready.")
        self.log(f"Service: {SYSTEMD_SERVICE}  |  Status: {server_status()}")
        self._main_menu()

    # ------------------------------------------------------------------ log
    def log(self, msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        self._log_lines.append(f"[{ts}] {msg}")
        if len(self._log_lines) > 2000:
            self._log_lines = self._log_lines[-2000:]
        if self._scr is not None:
            self._draw_log()
            self._scr.refresh()

    def clear_log(self) -> None:
        self._log_lines.clear()
        if self._scr is not None:
            self._draw_log()
            self._scr.refresh()

    # ------------------------------------------------------------------ layout
    def _dimensions(self):
        rows, cols = self._scr.getmaxyx()
        log_rows = max(4, int(rows * 0.55))
        menu_row = log_rows + 2
        return rows, cols, log_rows, menu_row

    def _draw_chrome(self) -> None:
        rows, cols, log_rows, menu_row = self._dimensions()
        status = server_status()
        title = f" {APP_NAME} v{APP_VERSION}  —  {status} "
        try:
            self._scr.addstr(0, 0, title.ljust(cols),
                             curses.color_pair(_CP_HEADER) | curses.A_BOLD)
        except curses.error:
            pass
        try:
            sep_row = log_rows + 2
            self._scr.addstr(sep_row, 0, "─" * (cols - 1),
                             curses.color_pair(_CP_DIM))
        except curses.error:
            pass

    def _draw_log(self) -> None:
        if self._scr is None:
            return
        rows, cols, log_rows, menu_row = self._dimensions()
        total = len(self._log_lines)
        start = max(0, total - log_rows)
        visible = self._log_lines[start:]
        for i in range(log_rows):
            screen_row = i + 1
            try:
                self._scr.move(screen_row, 0)
                self._scr.clrtoeol()
            except curses.error:
                pass
            if i < len(visible):
                line = visible[i]
                attr = 0
                if curses.has_colors():
                    lo = line.lower()
                    if "error" in lo or "failed" in lo or "warn" in lo:
                        attr = curses.color_pair(_CP_ERROR)
                    elif "✓" in line or "complete" in lo or "started" in lo:
                        attr = curses.color_pair(_CP_OK)
                try:
                    self._scr.addnstr(screen_row, 0, line, cols - 1, attr)
                except curses.error:
                    pass

    def _draw_menu(self, items: List[str], cursor: int,
                   prompt: str = "", first_menu_row: int = 0) -> None:
        rows, cols, log_rows, menu_row = self._dimensions()
        for r in range(first_menu_row, rows - 1):
            try:
                self._scr.move(r, 0)
                self._scr.clrtoeol()
            except curses.error:
                pass
        if prompt:
            try:
                self._scr.addnstr(first_menu_row, 0, prompt, cols - 1,
                                  curses.color_pair(_CP_DIM))
            except curses.error:
                pass
            first_menu_row += 1
        available = rows - first_menu_row - 1
        list_start = max(0, cursor - available + 1) if cursor >= available else 0
        for i, item in enumerate(items[list_start: list_start + available]):
            r = first_menu_row + i
            if r >= rows - 1:
                break
            idx = list_start + i
            if idx == cursor:
                attr = curses.color_pair(_CP_SELECTED) | curses.A_BOLD
                text = f" > {item} "
            else:
                attr = 0
                text = f"   {item} "
            try:
                self._scr.addnstr(r, 0, text, cols - 1, attr)
            except curses.error:
                pass
        hint = " ↑↓/jk navigate  Enter select  Esc back "
        try:
            self._scr.addnstr(rows - 1, 0, hint[:cols - 1], cols - 1,
                              curses.color_pair(_CP_DIM))
        except curses.error:
            pass

    # ------------------------------------------------------------------ primitives
    def pick(self, prompt: str, items: List[str]) -> Optional[str]:
        if not items:
            return None
        scr = self._scr
        rows, cols, log_rows, menu_row = self._dimensions()
        cursor = 0
        while True:
            scr.erase()
            self._draw_chrome()
            self._draw_log()
            self._draw_menu(items, cursor, prompt=prompt,
                            first_menu_row=menu_row + 1)
            scr.refresh()
            key = scr.getch()
            if key in (curses.KEY_UP, ord("k")):
                cursor = max(0, cursor - 1)
            elif key in (curses.KEY_DOWN, ord("j")):
                cursor = min(len(items) - 1, cursor + 1)
            elif key in (ord("\n"), ord("\r"), curses.KEY_ENTER):
                return items[cursor]
            elif key in (27, ord("q")):
                return None
            elif key == curses.KEY_RESIZE:
                rows, cols, log_rows, menu_row = self._dimensions()

    def enter_text(self, prompt: str, initial: str = "") -> Optional[str]:
        scr = self._scr
        rows, cols, log_rows, menu_row = self._dimensions()
        curses.curs_set(1)
        buf = list(initial)
        input_row = menu_row + 2
        while True:
            scr.erase()
            self._draw_chrome()
            self._draw_log()
            for r in range(menu_row, rows - 1):
                try:
                    scr.move(r, 0)
                    scr.clrtoeol()
                except curses.error:
                    pass
            try:
                scr.addnstr(menu_row + 1, 0, prompt, cols - 1,
                            curses.color_pair(_CP_DIM))
                scr.addnstr(input_row, 0, "> " + "".join(buf) + " ", cols - 1)
                hint = " Enter confirm  Esc cancel  Backspace delete "
                scr.addnstr(rows - 1, 0, hint[:cols - 1], cols - 1,
                            curses.color_pair(_CP_DIM))
            except curses.error:
                pass
            try:
                scr.move(input_row, min(2 + len(buf), cols - 2))
            except curses.error:
                pass
            scr.refresh()
            key = scr.getch()
            if key in (ord("\n"), ord("\r"), curses.KEY_ENTER):
                curses.curs_set(0)
                result = "".join(buf).strip()
                return result if result else None
            elif key == 27:
                curses.curs_set(0)
                return None
            elif key in (curses.KEY_BACKSPACE, 127, 8):
                if buf:
                    buf.pop()
            elif key == curses.KEY_RESIZE:
                rows, cols, log_rows, menu_row = self._dimensions()
                input_row = menu_row + 2
            elif 32 <= key <= 126:
                buf.append(chr(key))

    def _wait_for_key(self, msg: str = "Press any key to continue...") -> None:
        rows, cols, log_rows, menu_row = self._dimensions()
        self._scr.erase()
        self._draw_chrome()
        self._draw_log()
        try:
            self._scr.addnstr(rows - 1, 0, f" {msg} "[:cols - 1], cols - 1,
                              curses.color_pair(_CP_DIM))
        except curses.error:
            pass
        self._scr.refresh()
        self._scr.getch()

    # ------------------------------------------------------------------ log viewer
    def _view_logs(self) -> None:
        """Stream latest.log into the log panel. Q to quit, / to send a command."""
        self.clear_log()
        if not SERVER_LOG_FILE.exists():
            self.log(f"ERROR: Log file not found: {SERVER_LOG_FILE}")
            self._wait_for_key()
            return
        self.log(f"Streaming {SERVER_LOG_FILE.name} — Q stop  / command")
        self._scr.nodelay(True)

        proc = subprocess.Popen(
            ["tail", "-n", "50", "-f", str(SERVER_LOG_FILE)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )

        def reader():
            assert proc.stdout
            for line in proc.stdout:
                self.log(line.rstrip("\n"))

        t = threading.Thread(target=reader, daemon=True)
        t.start()

        try:
            while True:
                self._scr.erase()
                self._draw_chrome()
                self._draw_log()
                hint = " Q stop  / command "
                rows, cols, _, _ = self._dimensions()
                try:
                    self._scr.addnstr(rows - 1, 0, hint[:cols - 1], cols - 1,
                                      curses.color_pair(_CP_DIM))
                except curses.error:
                    pass
                self._scr.refresh()
                key = self._scr.getch()
                if key in (ord("q"), ord("Q"), 27):
                    break
                elif key == ord("/"):
                    self._scr.nodelay(False)
                    cmd = self._log_command_input()
                    self._scr.nodelay(True)
                    if cmd:
                        if server_running():
                            mc_cmd(cmd)
                            self.log(f"Sent: {cmd}")
                            _admin_log(f"command (log viewer): {cmd}")
                        else:
                            self.log("ERROR: Server is not running.")
                time.sleep(0.1)
        finally:
            proc.terminate()
            self._scr.nodelay(False)

    def _log_command_input(self) -> Optional[str]:
        """Inline command prompt rendered over the log view bottom bar."""
        scr = self._scr
        buf: List[str] = []
        curses.curs_set(1)
        while True:
            rows, cols, _, _ = self._dimensions()
            scr.erase()
            self._draw_chrome()
            self._draw_log()
            prompt = "> " + "".join(buf)
            try:
                scr.addnstr(rows - 1, 0, prompt[:cols - 1], cols - 1)
                scr.move(rows - 1, min(len(prompt), cols - 2))
            except curses.error:
                pass
            scr.refresh()
            key = scr.getch()
            if key in (ord("\n"), ord("\r"), curses.KEY_ENTER):
                curses.curs_set(0)
                result = "".join(buf).strip()
                return result if result else None
            elif key == 27:
                curses.curs_set(0)
                return None
            elif key in (curses.KEY_BACKSPACE, 127, 8):
                if buf:
                    buf.pop()
            elif key == curses.KEY_RESIZE:
                pass
            elif 32 <= key <= 126:
                buf.append(chr(key))

    # ------------------------------------------------------------------ admin log viewer
    def _view_admin_log(self) -> None:
        self.clear_log()
        if not ADMIN_LOG_FILE.exists():
            self.log("No admin activity recorded yet.")
            self._wait_for_key()
            return
        self.log(f"Admin log — {ADMIN_LOG_FILE}")
        try:
            lines = ADMIN_LOG_FILE.read_text(encoding="utf-8").splitlines()
            for line in lines[-200:]:
                self.log(line)
        except OSError as e:
            self.log(f"ERROR reading admin log: {e}")
        self._wait_for_key()

    # ------------------------------------------------------------------ player list
    def _view_players(self) -> None:
        """Display online players parsed from latest.log, auto-refreshing."""
        if not server_running():
            self.log("ERROR: Server is not running.")
            self._wait_for_key()
            return
        scr = self._scr
        scr.nodelay(True)
        try:
            while True:
                players = _parse_online_players()
                rows, cols, _, _ = self._dimensions()
                scr.erase()
                title = f" Online Players  —  {len(players)} connected "
                try:
                    scr.addstr(0, 0, title.ljust(cols),
                               curses.color_pair(_CP_HEADER) | curses.A_BOLD)
                except curses.error:
                    pass
                if not players:
                    try:
                        scr.addnstr(2, 2, "No players online.", cols - 3)
                    except curses.error:
                        pass
                else:
                    for i, name in enumerate(players):
                        row = 2 + i
                        if row >= rows - 1:
                            break
                        try:
                            scr.addnstr(row, 2, f"● {name}", cols - 3,
                                        curses.color_pair(_CP_OK))
                        except curses.error:
                            pass
                hint = " Q back  (refreshes from server log) "
                try:
                    scr.addnstr(rows - 1, 0, hint[:cols - 1], cols - 1,
                                curses.color_pair(_CP_DIM))
                except curses.error:
                    pass
                scr.refresh()
                key = scr.getch()
                if key in (ord("q"), ord("Q"), 27):
                    break
                time.sleep(0.5)
        finally:
            scr.nodelay(False)

    # ------------------------------------------------------------------ tps
    def _check_tps(self) -> None:
        if not server_running():
            self.log("Server is not running.")
            self._wait_for_key()
            return

        log_pos = SERVER_LOG_FILE.stat().st_size if SERVER_LOG_FILE.exists() else 0
        mc_cmd("tick query")
        self._run_with_spinner("Querying TPS...", lambda: time.sleep(1))

        if not SERVER_LOG_FILE.exists():
            self.log("ERROR: latest.log not found.")
            self._wait_for_key()
            return

        with SERVER_LOG_FILE.open("rb") as f:
            f.seek(log_pos)
            new_text = f.read().decode("utf-8", errors="replace")

        status_m = re.search(r"The game is (.+)", new_text)
        rate_m   = re.search(r"Target tick rate: ([\d.]+) per second", new_text)
        mspt_m   = re.search(r"Average time per tick: ([\d.]+)ms \(Target: ([\d.]+)ms\)", new_text)
        pct_m    = re.search(r"Percentiles: P50: ([\d.]+)ms P95: ([\d.]+)ms P99: ([\d.]+)ms\. Sample: (\d+)", new_text)

        if not any([status_m, rate_m, mspt_m, pct_m]):
            self.log("No TPS data received — server may not support 'tick query'.")
            self._wait_for_key()
            return

        self.clear_log()
        if status_m:
            self.log(f"Status : {status_m.group(1)}")
        target_tps = float(rate_m.group(1)) if rate_m else 20.0
        if rate_m:
            self.log(f"Target : {target_tps:.0f} TPS")
        if mspt_m:
            avg_mspt = float(mspt_m.group(1))
            tps = min(target_tps, 1000.0 / avg_mspt)
            self.log(f"TPS    : {tps:.1f} / {target_tps:.0f}")
            self.log(f"Avg    : {avg_mspt:.1f} ms/tick  (target {mspt_m.group(2)} ms)")
        if pct_m:
            self.log(f"P50    : {pct_m.group(1)} ms")
            self.log(f"P95    : {pct_m.group(2)} ms")
            self.log(f"P99    : {pct_m.group(3)} ms")
            self.log(f"Sample : {pct_m.group(4)} ticks")
        self._wait_for_key()

    # ------------------------------------------------------------------ spinner helper
    def _run_with_spinner(self, label: str, fn: Callable[[], None],
                          status_fn: Optional[Callable[[], str]] = None) -> Optional[str]:
        """Run fn() in a background thread while showing a spinner. Returns error or None."""
        err:  List[Optional[str]] = [None]
        done: List[bool]          = [False]

        def worker() -> None:
            try:
                fn()
            except Exception as e:
                err[0] = str(e)
            finally:
                done[0] = True

        t = threading.Thread(target=worker, daemon=True)
        t.start()

        spin  = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        spin_i = 0
        scr = self._scr
        scr.nodelay(True)
        try:
            while not done[0]:
                scr.erase()
                self._draw_chrome()
                self._draw_log()
                rows, cols, _, _ = self._dimensions()
                frame = spin[spin_i % len(spin)]
                msg   = f" {frame} {status_fn() if status_fn else label} "
                try:
                    scr.addnstr(rows - 1, 0, msg[:cols - 1], cols - 1,
                                curses.color_pair(_CP_DIM))
                except curses.error:
                    pass
                scr.refresh()
                scr.getch()
                time.sleep(0.1)
                spin_i += 1
        finally:
            scr.nodelay(False)
        t.join()
        return err[0]

    # ------------------------------------------------------------------ shared stop helper
    def _stop_for_install(self, reason: str) -> bool:
        """If server is running, prompt to stop it. Returns False if user cancels."""
        if not server_running():
            return True
        ans = self.pick(
            f"Server must be stopped to {reason}.",
            ["Yes, stop server and continue", "Cancel"],
        )
        if not ans or ans.startswith("Cancel"):
            return False
        self.log(f"Stopping server to {reason}...")
        mc_cmd("stop")
        for _ in range(60):
            if not server_running():
                break
            time.sleep(1)
        if server_running():
            _run(_tmux(["kill-session", "-t", TMUX_SESSION]))
        return True

    # ------------------------------------------------------------------ update: apply fabric
    def _apply_fabric_update(self, cfg: Dict, result: Dict) -> bool:
        if not self._stop_for_install("update the Fabric loader"):
            return False

        self.log(f"Downloading Fabric server.jar  (loader {result['latest']})...")
        dl = [0, 0]

        def do_dl() -> None:
            _download_file(result["url"], SERVER_JAR,
                           lambda done, total: dl.__setitem__(0, done) or dl.__setitem__(1, total))

        err = self._run_with_spinner(
            "Downloading Fabric server.jar", do_dl,
            status_fn=lambda: (
                f"Downloading Fabric  {dl[0]/1e6:.1f}/{dl[1]/1e6:.1f} MB"
                if dl[1] else f"Downloading Fabric  {dl[0]/1e6:.1f} MB"
            ),
        )
        if err:
            self.log(f"ERROR: {err}")
            return False

        cfg["fabric_loader_version"] = result["latest"]
        _save_config(cfg)
        result["update_available"] = False
        result["current"] = result["latest"]
        self.log(f"✓ Fabric loader updated to {result['latest']}.")
        _admin_log(f"fabric loader updated to {result['latest']}")

        ans = self.pick("Restart server now?", ["Yes, restart", "Leave stopped"])
        if ans and ans.startswith("Yes"):
            rc = systemctl("start")
            self.log("✓ Server restarted." if rc == 0
                     else f"ERROR: systemctl start failed (exit {rc}).")
        return True

    # ------------------------------------------------------------------ update: apply mod
    def _apply_mod_update(self, cfg: Dict, result: Dict) -> bool:
        mod         = result["mod"]
        name        = mod["name"]
        latest_file = result["latest_file"]
        dest        = MODS_DIR / latest_file

        if not self._stop_for_install(f"update {name}"):
            return False

        if not MODS_DIR.exists():
            self.log(f"ERROR: mods/ directory not found: {MODS_DIR}")
            return False

        self.log(f"Downloading {name}  {result['latest_version']}...")
        dl = [0, 0]

        def do_dl() -> None:
            _download_file(result["latest_url"], dest,
                           lambda done, total: dl.__setitem__(0, done) or dl.__setitem__(1, total))

        size_hint = (f"{result['latest_size'] // 1_000_000} MB"
                     if result.get("latest_size") else "? MB")
        err = self._run_with_spinner(
            f"Downloading {name}", do_dl,
            status_fn=lambda: (
                f"Downloading {name}  {dl[0]/1e6:.1f}/{dl[1]/1e6:.1f} MB"
                if dl[1] else f"Downloading {name}  {dl[0]/1e6:.1f} MB  ({size_hint})"
            ),
        )
        if err:
            self.log(f"ERROR: {err}")
            dest.with_suffix(dest.suffix + ".part").unlink(missing_ok=True)
            return False

        old_file = mod.get("installed_file") or ""
        if old_file and old_file != latest_file:
            old_path = MODS_DIR / old_file
            if old_path.exists():
                old_path.unlink()
                self.log(f"Removed old: {old_file}")

        mod["installed_file"] = latest_file
        _save_config(cfg)
        result["update_available"] = False
        self.log(f"✓ {name} updated to {result['latest_version']}.")
        _admin_log(f"mod updated: {name} → {result['latest_version']}")
        ans = self.pick("Restart server now?", ["Yes, restart", "Leave stopped"])
        if ans and ans.startswith("Yes"):
            rc = systemctl("start")
            self.log("✓ Server restarted." if rc == 0
                     else f"ERROR: systemctl start failed (exit {rc}).")
        return True

    # ------------------------------------------------------------------ check for updates
    def _check_updates_menu(self) -> None:
        cfg = _load_config()
        mc_version: Optional[str] = cfg.get("mc_version")
        if not mc_version:
            mc_version = self.enter_text("Enter Minecraft version (e.g. 1.21.11):")
            if not mc_version:
                return
            cfg["mc_version"] = mc_version
            _save_config(cfg)

        mods: List[Dict] = cfg.get("mods", [])
        self.clear_log()
        self.log(f"Checking for updates  —  MC {mc_version}  ({len(mods)} mod(s) tracked)")

        results: List[Dict] = []

        def fetch_all() -> None:
            results.append(_check_fabric_update(mc_version))   # type: ignore[arg-type]
            for mod in mods:
                results.append(_check_mod_update(mod, mc_version))  # type: ignore[arg-type]

        self._run_with_spinner("Fetching update info", fetch_all)

        # Log summary
        for r in results:
            if r.get("error"):
                name = r.get("name") or r.get("mod", {}).get("name", "?")
                self.log(f"  ERROR   {name}: {r['error']}")
            elif r["kind"] == "fabric":
                cur = r["current"] or "unknown"
                if r["update_available"]:
                    self.log(f"  ↑       Fabric loader  {cur} → {r['latest']}")
                else:
                    self.log(f"  ✓       Fabric loader  {cur}")
            else:
                mod = r["mod"]
                if r["only_beta"] and not mod.get("allow_beta"):
                    self.log(f"  [β]     {mod['name']}  no release — beta {r['latest_version']} available")
                elif r["update_available"]:
                    beta_tag = " [β]" if r.get("only_beta") else ""
                    self.log(f"  ↑       {mod['name']}  → {r['latest_version']}{beta_tag}")
                else:
                    self.log(f"  ✓       {mod['name']}  {mod.get('installed_file', '')}")

        # Interactive update loop
        while True:
            pending    = [r for r in results
                          if r.get("update_available")
                          or (r.get("only_beta") and not r.get("mod", {}).get("allow_beta"))]
            actionable = [r for r in pending if r.get("update_available")]

            if not pending:
                self.log("All up to date.")
                self._wait_for_key()
                return

            items: List[str] = []
            for r in pending:
                if r["kind"] == "fabric":
                    items.append(f"↑ Fabric loader  {r['current'] or '?'} → {r['latest']}  ⚠ restart")
                else:
                    mod = r["mod"]
                    if r["only_beta"] and not r["update_available"]:
                        items.append(f"[β] {mod['name']}  no release — enable beta (→ {r['latest_version']})")
                    else:
                        beta_tag = " [β]" if r.get("only_beta") else ""
                        items.append(f"↑ {mod['name']}  → {r['latest_version']}{beta_tag}")
            if actionable:
                items.append(f"Apply all  ({len(actionable)} update(s))")
            items.append("← Back")

            choice = self.pick(f"Updates  —  {len(actionable)} pending:", items)
            if choice is None or choice.startswith("←"):
                return

            if choice.startswith("Apply all"):
                for r in list(actionable):
                    if r["kind"] == "fabric":
                        self._apply_fabric_update(cfg, r)
                    else:
                        self._apply_mod_update(cfg, r)
                self._wait_for_key("Updates applied — press any key.")
                return

            idx = items.index(choice)
            if idx >= len(pending):
                continue
            r = pending[idx]

            if r.get("only_beta") and not r["update_available"]:
                # Offer to enable beta
                ans = self.pick(
                    f"Only beta releases for {r['mod']['name']} on MC {mc_version}.",
                    ["Enable beta and update now", "Enable beta only", "Cancel"],
                )
                if not ans or ans.startswith("Cancel"):
                    continue
                r["mod"]["allow_beta"] = True
                _save_config(cfg)
                if ans.startswith("Enable beta and update now"):
                    # Re-fetch to get download URL now that allow_beta=True
                    re_checked: List[Dict] = [{}]
                    def do_recheck() -> None:
                        re_checked[0] = _check_mod_update(r["mod"], mc_version)  # type: ignore[arg-type]
                    self._run_with_spinner(f"Re-checking {r['mod']['name']}", do_recheck)
                    rc = re_checked[0]
                    if rc.get("update_available"):
                        self._apply_mod_update(cfg, rc)
                        results[results.index(r)] = rc
                    else:
                        self.log(rc.get("error") or "No update found after enabling beta.")
                        self._wait_for_key()
                else:
                    self.log(f"Beta enabled for {r['mod']['name']}.")
                    # Update the result so it drops out of pending on next loop
                    r["only_beta"] = False   # re-check will happen on next open
            elif r["kind"] == "fabric":
                self._apply_fabric_update(cfg, r)
            else:
                self._apply_mod_update(cfg, r)

    # ------------------------------------------------------------------ manage mods
    def _manage_mods_menu(self) -> None:
        while True:
            cfg  = _load_config()
            mods: List[Dict] = cfg.get("mods", [])
            mc_version = cfg.get("mc_version") or "not set"

            items: List[str] = []
            for m in mods:
                beta      = "  [β]" if m.get("allow_beta") else ""
                installed = m.get("installed_file") or "(not tracked)"
                if len(installed) > 38:
                    installed = "…" + installed[-37:]
                items.append(f"{m['name']:<24}{installed}{beta}")
            items += ["✚  Add mod", "← Back"]

            choice = self.pick(f"Tracked mods  —  MC {mc_version}:", items)
            if choice is None or choice.startswith("←"):
                return
            if choice.startswith("✚"):
                self._add_mod_flow(cfg)
            else:
                idx = items.index(choice)
                if idx < len(mods):
                    self._mod_actions(cfg, mods, idx)

    def _mod_actions(self, cfg: Dict, mods: List[Dict], idx: int) -> None:
        mod        = mods[idx]
        beta_state = "enabled" if mod.get("allow_beta") else "disabled"
        installed  = mod.get("installed_file") or "not tracked"
        choice = self.pick(
            f"{mod['name']}  |  {installed}",
            [f"Toggle beta releases  (currently: {beta_state})",
             "✕  Remove from tracking",
             "← Back"],
        )
        if choice is None or choice.startswith("←"):
            return
        if choice.startswith("Toggle"):
            mod["allow_beta"] = not mod.get("allow_beta", False)
            _save_config(cfg)
            state = "enabled" if mod["allow_beta"] else "disabled"
            self.log(f"Beta releases for {mod['name']}: {state}.")
        elif choice.startswith("✕"):
            ans = self.pick(
                f"Remove '{mod['name']}' from tracking?",
                ["Remove from list only",
                 "Remove from list AND delete jar",
                 "Cancel"],
            )
            if not ans or ans.startswith("Cancel"):
                return
            if ans.startswith("Remove from list AND"):
                inst = mod.get("installed_file") or ""
                if inst:
                    p = MODS_DIR / inst
                    if p.exists():
                        p.unlink()
                        self.log(f"Deleted: {inst}")
            mods.pop(idx)
            cfg["mods"] = mods
            _save_config(cfg)
            self.log(f"Removed {mod['name']} from tracking.")

    def _add_mod_flow(self, cfg: Dict) -> None:
        mc_version: Optional[str] = cfg.get("mc_version")
        if not mc_version:
            mc_version = self.enter_text("Enter Minecraft version (e.g. 1.21.11):")
            if not mc_version:
                return
            cfg["mc_version"] = mc_version
            _save_config(cfg)

        slug = self.enter_text("Modrinth project slug or ID (e.g. 'sodium', 'AANobbMI'):")
        if not slug:
            return

        self.log(f"Looking up '{slug}'...")
        proj: List[Dict] = [{}]

        def do_lookup() -> None:
            proj[0] = _lookup_modrinth_project(slug)

        self._run_with_spinner(f"Looking up {slug}", do_lookup)

        if proj[0].get("error"):
            self.log(f"ERROR: {proj[0]['error']}")
            self._wait_for_key()
            return

        name    = proj[0]["title"]
        proj_id = proj[0]["id"]
        self.log(f"Found: {name}  (id: {proj_id})")

        mods: List[Dict] = cfg.get("mods", [])
        if any(m["id"] == proj_id for m in mods):
            self.log(f"'{name}' is already tracked.")
            self._wait_for_key()
            return

        # Check available versions
        mod_draft: Dict = {"id": proj_id, "name": name,
                           "installed_file": None, "allow_beta": False}
        info: List[Optional[Dict]] = [None]

        def do_check() -> None:
            info[0] = _check_mod_update(mod_draft, mc_version)  # type: ignore[arg-type]

        self._run_with_spinner(f"Checking versions for {name}", do_check)
        r = info[0]

        if r is None or r.get("error"):
            self.log(f"Warning: {(r or {}).get('error', 'network error')}")
            ans = self.pick("Could not check versions. Add anyway?",
                            ["Add to list without downloading", "Cancel"])
            if not ans or ans.startswith("Cancel"):
                return
            r = None

        elif r.get("only_beta"):
            self.log(f"No release versions for MC {mc_version} — "
                     f"beta available: {r.get('latest_version')}")
            ans = self.pick(
                f"'{name}' has no stable release for {mc_version}, only beta.",
                ["Allow beta and download", "Allow beta, track only", "Cancel"],
            )
            if not ans or ans.startswith("Cancel"):
                return
            mod_draft["allow_beta"] = True
            if ans.startswith("Allow beta and download"):
                re_info: List[Optional[Dict]] = [None]
                def do_recheck() -> None:
                    re_info[0] = _check_mod_update(mod_draft, mc_version)  # type: ignore[arg-type]
                self._run_with_spinner(f"Re-checking {name} (beta)", do_recheck)
                r = re_info[0]
            else:
                r = None  # track only

        # Download prompt
        installed_file: Optional[str] = None
        if r and r.get("latest_file"):
            size_str = (f"{r['latest_size'] // 1_000_000} MB"
                        if r.get("latest_size") else "? MB")
            ans = self.pick(
                f"Download {name}  {r['latest_version']}  ({size_str})?",
                ["Yes, download", "Track only (add without downloading)", "Cancel"],
            )
            if not ans or ans.startswith("Cancel"):
                return
            if ans.startswith("Yes"):
                if not self._stop_for_install(f"install {name}"):
                    return
                if not MODS_DIR.exists():
                    self.log(f"ERROR: mods/ directory not found: {MODS_DIR}")
                    self._wait_for_key()
                    return
                dest = MODS_DIR / r["latest_file"]
                dl   = [0, 0]

                def do_dl() -> None:
                    _download_file(r["latest_url"], dest,    # type: ignore[index]
                                   lambda done, total:
                                   dl.__setitem__(0, done) or dl.__setitem__(1, total))

                err = self._run_with_spinner(
                    f"Downloading {name}", do_dl,
                    status_fn=lambda: (
                        f"Downloading {name}  {dl[0]/1e6:.1f}/{dl[1]/1e6:.1f} MB"
                        if dl[1] else f"Downloading {name}  {dl[0]/1e6:.1f} MB"
                    ),
                )
                if err:
                    self.log(f"ERROR: Download failed: {err}")
                    dest.with_suffix(dest.suffix + ".part").unlink(missing_ok=True)
                    self._wait_for_key()
                    return
                installed_file = r["latest_file"]
                self.log(f"✓ Downloaded: mods/{installed_file}")
                ans2 = self.pick("Restart server now?", ["Yes, restart", "Leave stopped"])
                if ans2 and ans2.startswith("Yes"):
                    rc = systemctl("start")
                    self.log("✓ Server restarted." if rc == 0
                             else f"ERROR: systemctl start failed (exit {rc}).")

        mods.append({
            "id":             proj_id,
            "name":           name,
            "installed_file": installed_file,
            "allow_beta":     mod_draft.get("allow_beta", False),
        })
        cfg["mods"] = mods
        _save_config(cfg)
        self.log(f"✓ {name} added to tracked mods.")
        _admin_log(f"mod added: {name} ({proj_id})")
        self._wait_for_key()

    # ------------------------------------------------------------------ worlds
    def _active_world(self) -> Optional[str]:
        if WORLD_LINK.is_symlink():
            return Path(os.readlink(str(WORLD_LINK))).name
        return None

    def _list_worlds(self) -> List[str]:
        if not WORLDS_DIR.is_dir():
            return []
        return sorted(p.name for p in WORLDS_DIR.iterdir() if p.is_dir())

    def _switch_world(self, name: str) -> None:
        if server_running():
            ans = self.pick(
                f"Server must be stopped to switch to world '{name}'.",
                ["Yes, stop server and continue", "Cancel"],
            )
            if not ans or ans.startswith("Cancel"):
                return
            mc_cmd("kick @a Server is restarting to switch worlds. Please stand by.")
            self.log(f"Stopping server to switch to world '{name}'...")
            mc_cmd("stop")
            for _ in range(60):
                if not server_running():
                    break
                time.sleep(1)
            if server_running():
                _run(_tmux(["kill-session", "-t", TMUX_SESSION]))
        WORLD_LINK.unlink(missing_ok=True)
        WORLD_LINK.symlink_to(Path("worlds") / name)
        self.log(f"Active world → {name}")
        ans = self.pick("World switched. Start server now?", ["Yes", "No"])
        if ans and ans.startswith("Yes"):
            self._do_start()

    def _create_world(self) -> None:
        name = self.enter_text("New world name (letters, numbers, _ -):")
        if not name:
            return
        if not _WORLD_NAME_RE.match(name):
            self.log("Invalid name. Use letters, numbers, _ or - only.")
            self._wait_for_key()
            return
        dest = WORLDS_DIR / name
        if dest.exists():
            self.log(f"World '{name}' already exists.")
            self._wait_for_key()
            return
        dest.mkdir(parents=True)
        self.log(f"Created world '{name}'.")
        if not WORLD_LINK.is_symlink():
            WORLD_LINK.symlink_to(Path("worlds") / name)
            self.log(f"'{name}' set as active world.")
        else:
            ans = self.pick(f"Switch to '{name}' now?", ["Yes", "No"])
            if ans and ans.startswith("Yes"):
                self._switch_world(name)

    def _rename_world(self, name: str, is_active: bool) -> None:
        if is_active and not self._stop_for_install(f"rename world '{name}'"):
            return
        new_name = self.enter_text(f"Rename '{name}' to:", initial=name)
        if not new_name or new_name == name:
            return
        if not _WORLD_NAME_RE.match(new_name):
            self.log("Invalid name. Use letters, numbers, _ or - only.")
            self._wait_for_key()
            return
        dest = WORLDS_DIR / new_name
        if dest.exists():
            self.log(f"World '{new_name}' already exists.")
            self._wait_for_key()
            return
        (WORLDS_DIR / name).rename(dest)
        if is_active:
            WORLD_LINK.unlink(missing_ok=True)
            WORLD_LINK.symlink_to(Path("worlds") / new_name)
        self.log(f"Renamed '{name}' → '{new_name}'.")
        if is_active:
            ans = self.pick("World renamed. Start server now?", ["Yes", "No"])
            if ans and ans.startswith("Yes"):
                self._do_start()

    def _delete_world(self, name: str) -> None:
        ans = self.pick(
            f"Delete '{name}'? This cannot be undone.",
            ["Yes, delete permanently", "Cancel"],
        )
        if not ans or ans.startswith("Cancel"):
            return
        shutil.rmtree(WORLDS_DIR / name)
        self.log(f"Deleted world '{name}'.")
        _admin_log(f"world deleted: {name}")

    def _world_actions(self, name: str, is_active: bool) -> None:
        label = f"{name}  [active]" if is_active else name
        opts: List[str] = []
        if not is_active:
            opts.append("Switch to this world")
        opts.append("Rename")
        if not is_active:
            opts.append("Delete")
        opts.append("Cancel")
        ans = self.pick(f"World: {label}", opts)
        if not ans or ans.startswith("Cancel"):
            return
        if ans.startswith("Switch"):
            self._switch_world(name)
        elif ans.startswith("Rename"):
            self._rename_world(name, is_active)
        elif ans.startswith("Delete"):
            self._delete_world(name)

    def _manage_worlds_menu(self) -> None:
        # ── Migration: plain 'world' dir → worlds/default ─────────────────────
        if WORLD_LINK.is_dir() and not WORLD_LINK.is_symlink():
            ans = self.pick(
                "Found existing 'world' folder — migrate to multi-world layout?"
                " It will be moved to 'worlds/default'.",
                ["Yes, migrate", "Cancel"],
            )
            if not ans or ans.startswith("Cancel"):
                self.log("Multi-world requires migration. Cancelled.")
                return
            if not self._stop_for_install("migrate to multi-world layout"):
                return
            WORLDS_DIR.mkdir(parents=True, exist_ok=True)
            WORLD_LINK.rename(WORLDS_DIR / "default")
            WORLD_LINK.symlink_to(Path("worlds") / "default")
            self.log("Migrated 'world' → 'worlds/default'. Active world: default.")
            _admin_log("migrated world → worlds/default")

        WORLDS_DIR.mkdir(parents=True, exist_ok=True)

        while True:
            active = self._active_world()
            worlds = self._list_worlds()
            items: List[str] = []
            for w in worlds:
                marker = "  [active]" if w == active else ""
                items.append(f"  {w}{marker}")
            items.append("＋  New world")
            items.append("←  Back")

            title = f"Worlds  (active: {active})" if active else "Worlds  (none active)"
            choice = self.pick(title, items)
            if choice is None or choice.startswith("←"):
                return
            elif choice.startswith("＋"):
                self._create_world()
            else:
                name = choice.strip().split("  ")[0]
                self._world_actions(name, name == active)

    # ------------------------------------------------------------------ instances
    def _active_instance(self) -> Optional[str]:
        if SERVER_LINK.is_symlink():
            return Path(os.readlink(str(SERVER_LINK))).name
        return None

    def _list_instances(self) -> List[str]:
        if not INSTANCES_DIR.is_dir():
            return []
        return sorted(p.name for p in INSTANCES_DIR.iterdir() if p.is_dir())

    def _switch_instance(self, name: str) -> None:
        if server_running():
            ans = self.pick(
                f"Server must be stopped to switch to instance '{name}'.",
                ["Yes, stop server and continue", "Cancel"],
            )
            if not ans or ans.startswith("Cancel"):
                return
            mc_cmd("kick @a Server is restarting to switch instances. Please stand by.")
            self.log(f"Stopping server to switch to instance '{name}'...")
            mc_cmd("stop")
            for _ in range(60):
                if not server_running():
                    break
                time.sleep(1)
            if server_running():
                _run(_tmux(["kill-session", "-t", TMUX_SESSION]))
        SERVER_LINK.unlink(missing_ok=True)
        SERVER_LINK.symlink_to(Path("instances") / name)
        self.log(f"Active instance → {name}")
        ans = self.pick("Instance switched. Start server now?", ["Yes", "No"])
        if ans and ans.startswith("Yes"):
            self._do_start()

    def _create_instance(self) -> None:
        name = self.enter_text("New instance name (letters, numbers, _ -):")
        if not name:
            return
        if not _WORLD_NAME_RE.match(name):
            self.log("Invalid name. Use letters, numbers, _ or - only.")
            self._wait_for_key()
            return
        dest = INSTANCES_DIR / name
        if dest.exists():
            self.log(f"Instance '{name}' already exists.")
            self._wait_for_key()
            return

        mc_version = self.enter_text("Minecraft version (e.g. 1.21.11):")
        if not mc_version:
            return

        eula = self.pick(
            "Accept the Minecraft EULA? (https://aka.ms/MinecraftEULA)",
            ["Yes, I accept", "Cancel"],
        )
        if not eula or eula.startswith("Cancel"):
            return

        (dest / "mods").mkdir(parents=True)

        # Fetch Fabric versions
        fabric: List[Dict] = [{}]
        def do_fetch() -> None:
            fabric[0] = _check_fabric_update(mc_version)
        err = self._run_with_spinner(f"Fetching Fabric versions for {mc_version}", do_fetch)
        if err or fabric[0].get("error"):
            self.log(f"ERROR: {err or fabric[0]['error']}")
            shutil.rmtree(dest)
            self._wait_for_key()
            return

        # Download server.jar
        jar_dest = dest / "server.jar"
        dl = [0, 0]
        def do_dl() -> None:
            _download_file(fabric[0]["url"], jar_dest,
                           lambda done, total: dl.__setitem__(0, done) or dl.__setitem__(1, total))
        err = self._run_with_spinner(
            f"Downloading Fabric {mc_version}", do_dl,
            status_fn=lambda: (
                f"Downloading Fabric  {dl[0]/1e6:.1f}/{dl[1]/1e6:.1f} MB"
                if dl[1] else f"Downloading Fabric  {dl[0]/1e6:.1f} MB"
            ),
        )
        if err:
            self.log(f"ERROR: {err}")
            shutil.rmtree(dest)
            self._wait_for_key()
            return

        (dest / "eula.txt").write_text("eula=true\n")
        self.log(f"✓ Created instance '{name}'  (Fabric loader {fabric[0]['latest']}).")
        _admin_log(f"instance created: {name} mc={mc_version} fabric={fabric[0]['latest']}")

        if not SERVER_LINK.is_symlink():
            SERVER_LINK.symlink_to(Path("instances") / name)
            self.log(f"'{name}' set as active instance.")
        else:
            ans = self.pick(f"Switch to '{name}' now?", ["Yes", "No"])
            if ans and ans.startswith("Yes"):
                self._switch_instance(name)

    def _rename_instance(self, name: str, is_active: bool) -> None:
        if is_active and not self._stop_for_install(f"rename instance '{name}'"):
            return
        new_name = self.enter_text(f"Rename '{name}' to:", initial=name)
        if not new_name or new_name == name:
            return
        if not _WORLD_NAME_RE.match(new_name):
            self.log("Invalid name. Use letters, numbers, _ or - only.")
            self._wait_for_key()
            return
        dest = INSTANCES_DIR / new_name
        if dest.exists():
            self.log(f"Instance '{new_name}' already exists.")
            self._wait_for_key()
            return
        (INSTANCES_DIR / name).rename(dest)
        if is_active:
            SERVER_LINK.unlink(missing_ok=True)
            SERVER_LINK.symlink_to(Path("instances") / new_name)
        self.log(f"Renamed '{name}' → '{new_name}'.")
        if is_active:
            ans = self.pick("Instance renamed. Start server now?", ["Yes", "No"])
            if ans and ans.startswith("Yes"):
                self._do_start()

    def _delete_instance(self, name: str) -> None:
        ans = self.pick(
            f"Delete instance '{name}'? This cannot be undone.",
            ["Yes, delete permanently", "Cancel"],
        )
        if not ans or ans.startswith("Cancel"):
            return
        shutil.rmtree(INSTANCES_DIR / name)
        self.log(f"Deleted instance '{name}'.")
        _admin_log(f"instance deleted: {name}")

    def _instance_actions(self, name: str, is_active: bool) -> None:
        label = f"{name}  [active]" if is_active else name
        opts: List[str] = []
        if not is_active:
            opts.append("Switch to this instance")
        opts.append("Rename")
        if not is_active:
            opts.append("Delete")
        opts.append("Cancel")
        ans = self.pick(f"Instance: {label}", opts)
        if not ans or ans.startswith("Cancel"):
            return
        if ans.startswith("Switch"):
            self._switch_instance(name)
        elif ans.startswith("Rename"):
            self._rename_instance(name, is_active)
        elif ans.startswith("Delete"):
            self._delete_instance(name)

    def _manage_instances_menu(self) -> None:
        # ── Migration: plain 'server' dir → instances/default ─────────────────
        if SERVER_LINK.is_dir() and not SERVER_LINK.is_symlink():
            ans = self.pick(
                "Found existing 'server' folder — migrate to multi-instance layout?"
                " It will be moved to 'instances/default'.",
                ["Yes, migrate", "Cancel"],
            )
            if not ans or ans.startswith("Cancel"):
                self.log("Multi-instance requires migration. Cancelled.")
                return
            if not self._stop_for_install("migrate to multi-instance layout"):
                return
            INSTANCES_DIR.mkdir(parents=True, exist_ok=True)
            SERVER_LINK.rename(INSTANCES_DIR / "default")
            SERVER_LINK.symlink_to(Path("instances") / "default")
            self.log("Migrated 'server' → 'instances/default'. Active instance: default.")
            _admin_log("migrated server → instances/default")

        INSTANCES_DIR.mkdir(parents=True, exist_ok=True)

        while True:
            active = self._active_instance()
            instances = self._list_instances()
            items: List[str] = []
            for i in instances:
                marker = "  [active]" if i == active else ""
                items.append(f"  {i}{marker}")
            items.append("＋  New instance")
            items.append("←  Back")

            title = f"Instances  (active: {active})" if active else "Instances  (none active)"
            choice = self.pick(title, items)
            if choice is None or choice.startswith("←"):
                return
            elif choice.startswith("＋"):
                self._create_instance()
            else:
                name = choice.strip().split("  ")[0]
                self._instance_actions(name, name == active)

    # ------------------------------------------------------------------ menus
    def _main_menu(self) -> None:
        ITEMS = [
            "⬆  Start server",
            "⬇  Stop server",
            "↺  Restart server",
            "⌨  Send command",
            "⚡  Quick commands",
            "👥  Online players",
            "📊  Check TPS",
            "📋  View logs",
            "💾  Backup now",
            "🔄  Check for updates",
            "🧩  Manage mods",
            "🌍  Manage worlds",
            "🖥  Manage instances",
            "📜  View admin log",
            "✕  Quit",
        ]
        while True:
            choice = self.pick("MC Server — choose an action:", ITEMS)
            if choice is None or choice.startswith("✕"):
                break
            elif choice.startswith("⬆"):
                self._do_start()
            elif choice.startswith("⬇"):
                self._do_stop()
            elif choice.startswith("↺"):
                self._do_restart()
            elif choice.startswith("⌨"):
                self._do_send_command()
            elif choice.startswith("⚡"):
                self._quick_commands_menu()
            elif choice.startswith("👥"):
                self._view_players()
            elif choice.startswith("📊"):
                self._check_tps()
            elif choice.startswith("📋"):
                self._view_logs()
            elif choice.startswith("💾"):
                self._do_backup()
            elif choice.startswith("🔄"):
                self._check_updates_menu()
            elif choice.startswith("🧩"):
                self._manage_mods_menu()
            elif choice.startswith("🌍"):
                self._manage_worlds_menu()
            elif choice.startswith("🖥"):
                self._manage_instances_menu()
            elif choice.startswith("📜"):
                self._view_admin_log()

    # ------------------------------------------------------------------ quick commands
    def _quick_commands_menu(self) -> None:
        cfg = _load_config()
        while True:
            cmds: List[Dict] = cfg.get("quick_commands", [])

            items = [f"⚡ {c['name']}  ({c['cmd']})" for c in cmds]
            items += ["✚  Add command", "✎  Edit / remove commands", "← Back"]

            choice = self.pick("Quick commands — select to run:", items)
            if choice is None or choice.startswith("←"):
                return

            if choice.startswith("✚"):
                self._qc_add(cfg, cmds)
            elif choice.startswith("✎"):
                self._qc_edit_menu(cfg, cmds)
            else:
                # Run the selected quick command
                idx = items.index(choice)
                entry = cmds[idx]
                if not server_running():
                    self.log("ERROR: Server is not running.")
                    self._wait_for_key()
                    continue
                mc_cmd(entry["cmd"])
                self.log(f"Quick command sent: {entry['name']}  →  {entry['cmd']}")
                _admin_log(f"quick command: {entry['name']} ({entry['cmd']})")

    def _qc_add(self, cfg: Dict, cmds: List[Dict]) -> None:
        name = self.enter_text("Quick command name (label shown in menu):")
        if not name:
            return
        cmd = self.enter_text(f"Command to send for '{name}' (no leading /):")
        if not cmd:
            return
        cmds.append({"name": name, "cmd": cmd})
        cfg["quick_commands"] = cmds
        _save_config(cfg)
        self.log(f"Saved quick command: {name}  →  {cmd}")

    def _qc_edit_menu(self, cfg: Dict, cmds: List[Dict]) -> None:
        if not cmds:
            self.log("No quick commands saved yet.")
            self._wait_for_key()
            return
        items = [f"{c['name']}  ({c['cmd']})" for c in cmds] + ["← Back"]
        choice = self.pick("Edit / remove — pick a command:", items)
        if choice is None or choice.startswith("←"):
            return
        idx = items.index(choice)
        entry = cmds[idx]

        action = self.pick(
            f"'{entry['name']}'  →  {entry['cmd']}",
            ["✎  Rename", "⌨  Change command", "✕  Delete", "← Back"],
        )
        if action is None or action.startswith("←"):
            return

        if action.startswith("✎"):
            new_name = self.enter_text("New name:", initial=entry["name"])
            if new_name:
                entry["name"] = new_name
                cfg["quick_commands"] = cmds
                _save_config(cfg)
                self.log(f"Renamed to: {new_name}")
        elif action.startswith("⌨"):
            new_cmd = self.enter_text("New command:", initial=entry["cmd"])
            if new_cmd:
                entry["cmd"] = new_cmd
                cfg["quick_commands"] = cmds
                _save_config(cfg)
                self.log(f"Updated command: {new_cmd}")
        elif action.startswith("✕"):
            confirm = self.pick(f"Delete '{entry['name']}'?", ["Yes, delete", "Cancel"])
            if confirm and confirm.startswith("Yes"):
                cmds.pop(idx)
                cfg["quick_commands"] = cmds
                _save_config(cfg)
                self.log(f"Deleted: {entry['name']}")

    # ------------------------------------------------------------------ actions
    def _do_start(self) -> None:
        if server_running():
            self.log("Server is already running.")
            return
        self.log("Starting server...")
        rc = systemctl("start")
        if rc == 0:
            self.log("✓ Server started.")
            _admin_log("start server")
        else:
            self.log(f"ERROR: systemctl start failed (exit {rc}).")
            _admin_log(f"start server FAILED (exit {rc})")

    def _do_stop(self) -> None:
        confirm = self.pick("Stop server?", ["Yes, stop the server", "Cancel"])
        if not confirm or confirm.startswith("Cancel"):
            return
        self.log("Stopping server...")
        if server_running():
            mc_cmd("stop")
            # Wait for tmux session to exit
            for _ in range(60):
                if not server_running():
                    break
                time.sleep(1)
        rc = systemctl("stop")
        if rc == 0:
            self.log("✓ Server stopped.")
            _admin_log("stop server")
        else:
            self.log(f"ERROR: systemctl stop failed (exit {rc}).")
            _admin_log(f"stop server FAILED (exit {rc})")

    def _do_restart(self) -> None:
        confirm = self.pick("Restart server?", ["Yes, restart", "Cancel"])
        if not confirm or confirm.startswith("Cancel"):
            return
        self.log("Restarting server...")
        if server_running():
            mc_cmd("kick @a Server Restarting")
            mc_cmd("stop")
            for _ in range(60):
                if not server_running():
                    break
                time.sleep(1)
        rc = systemctl("start")
        if rc == 0:
            self.log("✓ Server restarted.")
            _admin_log("restart server")
        else:
            self.log(f"ERROR: systemctl start failed (exit {rc}).")
            _admin_log(f"restart server FAILED (exit {rc})")

    def _do_send_command(self) -> None:
        if not server_running():
            self.log("ERROR: Server is not running.")
            return
        cmd = self.enter_text("Enter server command (without leading /):")
        if not cmd:
            return
        mc_cmd(cmd)
        self.log(f"Sent: {cmd}")
        _admin_log(f"command: {cmd}")

    def _do_backup(self) -> None:
        choice = self.pick(
            "⚠ This will stop the server, run a backup, then restart.\nChoose backup type:",
            ["Local backup  (~/minecraft/backups)", "Cloud backup  (rclone)", "Cancel"],
        )
        if not choice or choice.startswith("Cancel"):
            return
        backend_arg = ["--backend", "local"] if choice.startswith("Local") else []

        _admin_log(f"manual backup initiated ({'local' if backend_arg else 'cloud'})")
        self.clear_log()

        # Stop
        self.log("Stopping server...")
        if server_running():
            mc_cmd("kick @a Manual backup starting — server will restart shortly.")
            mc_cmd("stop")
            for _ in range(60):
                if not server_running():
                    break
                time.sleep(1)
            if server_running():
                self.log("WARN: Server did not stop cleanly — killing tmux session.")
                _run(_tmux(["kill-session", "-t", TMUX_SESSION]))
        self.log("Server stopped.")

        # Status server
        self.log("Starting status server...")
        status_proc = subprocess.Popen(_as_server([
            "python3", str(STATUS_SERVER),
            "--motd", "§e⚙ Backup in progress §7— §aback soon!",
            "--status-file", "/tmp/mcvault_status",
        ]))
        self.log(f"Status server PID: {status_proc.pid}")

        # Backup — stream output to log panel in a thread
        self.log("Starting MC Vault backup...")
        backup_exit = [1]

        def run_backup():
            proc = subprocess.Popen(
                _as_server(["python3", str(MCVAULT), "--backup", "--log-file", str(BACKUP_LOG)]
                + backend_arg),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )
            assert proc.stdout
            for line in proc.stdout:
                self.log(line.rstrip("\n"))
            backup_exit[0] = proc.wait()

        t = threading.Thread(target=run_backup)
        t.start()

        # Keep screen refreshed while backup runs
        self._scr.nodelay(True)
        while t.is_alive():
            self._scr.erase()
            self._draw_chrome()
            self._draw_log()
            rows, cols, _, _ = self._dimensions()
            try:
                self._scr.addnstr(rows - 1, 0,
                                  " Backup in progress — please wait... "[:cols - 1],
                                  cols - 1, curses.color_pair(_CP_DIM))
            except curses.error:
                pass
            self._scr.refresh()
            time.sleep(0.2)
        self._scr.nodelay(False)
        t.join()

        # Tear down status server
        self.log("Stopping status server...")
        status_proc.terminate()
        status_proc.wait()
        self.log("Status server stopped.")

        if backup_exit[0] == 0:
            self.log("✓ Backup completed successfully.")
            _admin_log("manual backup complete")
        else:
            self.log(f"ERROR: Backup failed (exit {backup_exit[0]}). Restarting server anyway.")
            _admin_log(f"manual backup FAILED (exit {backup_exit[0]})")

        # Restart
        self.log("Restarting server...")
        rc = systemctl("start")
        self.log("✓ Server restarted." if rc == 0
                 else f"ERROR: systemctl start failed (exit {rc}).")

        self._wait_for_key("Done — press any key to return to menu.")


# =============================================================================
# Entry point
# =============================================================================

if __name__ == "__main__":
    if not sys.stdout.isatty():
        print("mc_server.py must be run in a terminal.", file=sys.stderr)
        sys.exit(1)
    ServerTUI().run()
