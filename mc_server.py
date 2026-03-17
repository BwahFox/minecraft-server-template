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
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, List, Optional

# =============================================================================
# Configuration
# =============================================================================

SYSTEMD_SERVICE  = "minecraft"
TMUX_SESSION     = "minecraft"
MCVAULT          = Path.home() / "minecraft" / "mc_vault.py"
STATUS_SERVER    = Path.home() / "minecraft" / "mc_status_server.py"
BACKUP_LOG       = Path.home() / "minecraft" / "backup.log"
SERVER_LOG_FILE  = Path.home() / "minecraft" / "logs" / "latest.log"

APP_NAME         = "MC Server"
APP_VERSION      = "1.0.0"

# =============================================================================
# Colour pairs
# =============================================================================

_CP_HEADER   = 1
_CP_SELECTED = 2
_CP_DIM      = 3
_CP_ERROR    = 4
_CP_OK       = 5


def _init_colors() -> None:
    if not curses.has_colors():
        return
    curses.start_color()
    curses.use_default_colors()
    bg = -1
    try:
        curses.init_pair(_CP_HEADER,   curses.COLOR_BLUE,  bg)
        curses.init_pair(_CP_SELECTED, curses.COLOR_BLACK, curses.COLOR_BLUE)
        curses.init_pair(_CP_DIM,      curses.COLOR_WHITE, bg)
        curses.init_pair(_CP_ERROR,    curses.COLOR_RED,   bg)
        curses.init_pair(_CP_OK,       curses.COLOR_GREEN, bg)
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
    r = _run(["tmux", "has-session", "-t", TMUX_SESSION], capture=True)
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
    _run(["tmux", "send-keys", "-t", TMUX_SESSION, command, "Enter"])


def systemctl(action: str) -> int:
    r = _run(["sudo", "systemctl", action, SYSTEMD_SERVICE], capture=True)
    return r.returncode


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
        """Stream latest.log into the log panel. Q to quit."""
        self.clear_log()
        if not SERVER_LOG_FILE.exists():
            self.log(f"ERROR: Log file not found: {SERVER_LOG_FILE}")
            self._wait_for_key()
            return
        self.log(f"Streaming {SERVER_LOG_FILE.name} — press Q to stop.")
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
                hint = " Q to stop streaming "
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
                time.sleep(0.1)
        finally:
            proc.terminate()
            self._scr.nodelay(False)

    # ------------------------------------------------------------------ menus
    def _main_menu(self) -> None:
        ITEMS = [
            "⬆  Start server",
            "⬇  Stop server",
            "↺  Restart server",
            "⌨  Send command",
            "📋  View logs",
            "💾  Backup now",
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
            elif choice.startswith("📋"):
                self._view_logs()
            elif choice.startswith("💾"):
                self._do_backup()

    # ------------------------------------------------------------------ actions
    def _do_start(self) -> None:
        if server_running():
            self.log("Server is already running.")
            return
        self.log("Starting server...")
        rc = systemctl("start")
        if rc == 0:
            self.log("✓ Server started.")
        else:
            self.log(f"ERROR: systemctl start failed (exit {rc}).")

    def _do_stop(self) -> None:
        confirm = self.pick("Stop server?", ["Yes, stop the server", "Cancel"])
        if not confirm or confirm.startswith("Cancel"):
            return
        self.log("Stopping server...")
        if server_running():
            mc_cmd("save-all")
            time.sleep(10)
            mc_cmd("stop")
            # Wait for tmux session to exit
            for _ in range(60):
                if not server_running():
                    break
                time.sleep(1)
        rc = systemctl("stop")
        self.log("✓ Server stopped." if rc == 0
                 else f"ERROR: systemctl stop failed (exit {rc}).")

    def _do_restart(self) -> None:
        confirm = self.pick("Restart server?", ["Yes, restart", "Cancel"])
        if not confirm or confirm.startswith("Cancel"):
            return
        self.log("Restarting server...")
        if server_running():
            mc_cmd("save-all")
            time.sleep(10)
            mc_cmd("stop")
            for _ in range(60):
                if not server_running():
                    break
                time.sleep(1)
        rc = systemctl("start")
        self.log("✓ Server restarted." if rc == 0
                 else f"ERROR: systemctl start failed (exit {rc}).")

    def _do_send_command(self) -> None:
        if not server_running():
            self.log("ERROR: Server is not running.")
            return
        cmd = self.enter_text("Enter server command (without leading /):")
        if not cmd:
            return
        mc_cmd(cmd)
        self.log(f"Sent: {cmd}")

    def _do_backup(self) -> None:
        confirm = self.pick(
            "⚠ This will stop the server, run a backup, then restart.\nContinue?",
            ["Yes, backup now", "Cancel"],
        )
        if not confirm or confirm.startswith("Cancel"):
            return

        self.clear_log()

        # Stop
        self.log("Stopping server...")
        if server_running():
            mc_cmd("say §cManual backup starting — server will restart shortly.")
            mc_cmd("save-all")
            time.sleep(10)
            mc_cmd("stop")
            for _ in range(60):
                if not server_running():
                    break
                time.sleep(1)
            if server_running():
                self.log("WARN: Server did not stop cleanly — killing tmux session.")
                _run(["tmux", "kill-session", "-t", TMUX_SESSION])
        self.log("Server stopped.")

        # Status server
        self.log("Starting status server...")
        status_proc = subprocess.Popen([
            "python3", str(STATUS_SERVER),
            "--motd", "§e⚙ Backup in progress §7— §aback soon!",
        ])
        self.log(f"Status server PID: {status_proc.pid}")

        # Backup — stream output to log panel in a thread
        self.log("Starting MC Vault backup...")
        backup_exit = [1]

        def run_backup():
            proc = subprocess.Popen(
                ["python3", str(MCVAULT), "--backup", "--log-file", str(BACKUP_LOG)],
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
        else:
            self.log(f"ERROR: Backup failed (exit {backup_exit[0]}). Restarting server anyway.")

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
