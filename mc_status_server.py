#!/usr/bin/env python3
"""
mc_status_server.py — Lightweight fake Minecraft server list ping responder.
Shows a custom MOTD and player count to anyone who pings the server,
without running Fabric/Java at all.

Usage:
    python3 mc_status_server.py [--host HOST] [--port PORT] [--motd "Your message"]
"""

import argparse
import json
import socket
import struct
import threading


DEFAULT_MOTD  = "§e⚙ Backup in progress §7— §aback soon!"
DEFAULT_PORT  = 25565
DEFAULT_HOST  = "0.0.0.0"
PROTOCOL_VERSION = 765   # 1.20.4 — close enough for status pings


def _varint(value: int) -> bytes:
    """Encode an integer as a Minecraft VarInt."""
    out = b""
    while True:
        part = value & 0x7F
        value >>= 7
        if value:
            part |= 0x80
        out += bytes([part])
        if not value:
            break
    return out


def _read_varint(sock: socket.socket) -> int:
    result, shift = 0, 0
    while True:
        b = sock.recv(1)
        if not b:
            raise ConnectionError("Socket closed reading VarInt")
        val = b[0]
        result |= (val & 0x7F) << shift
        if not (val & 0x80):
            return result
        shift += 7
        if shift >= 35:
            raise ValueError("VarInt too large")


def _read_bytes(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("Socket closed mid-read")
        buf += chunk
    return buf


def _send_packet(sock: socket.socket, packet_id: int, payload: bytes) -> None:
    data = _varint(packet_id) + payload
    sock.sendall(_varint(len(data)) + data)


def _build_status_response(motd: str) -> bytes:
    response = {
        "version": {"name": "Backup", "protocol": PROTOCOL_VERSION},
        "players": {"max": 0, "online": 0, "sample": []},
        "description": {"text": motd},
    }
    raw = json.dumps(response, separators=(",", ":")).encode("utf-8")
    # String payload: varint length prefix + utf-8 bytes
    return _varint(len(raw)) + raw


def _handle_client(conn: socket.socket, motd: str) -> None:
    try:
        conn.settimeout(5.0)

        # Read handshake packet
        _length  = _read_varint(conn)
        _pkt_id  = _read_varint(conn)   # 0x00 handshake
        _proto   = _read_varint(conn)   # protocol version
        # server address string
        addr_len = _read_varint(conn)
        _addr    = _read_bytes(conn, addr_len)
        _port    = _read_bytes(conn, 2)  # server port (u16)
        next_state = _read_varint(conn)  # 1 = status, 2 = login

        if next_state != 1:
            # Login attempt — send a disconnect with a message
            disconnect_msg = json.dumps({"text": motd})
            encoded = disconnect_msg.encode("utf-8")
            payload = _varint(len(encoded)) + encoded
            _send_packet(conn, 0x00, payload)
            return

        # Read status request packet (0x00, no payload)
        _length = _read_varint(conn)
        _pkt_id = _read_varint(conn)

        # Send status response
        _send_packet(conn, 0x00, _build_status_response(motd))

        # Read ping packet and echo it back
        try:
            ping_len    = _read_varint(conn)
            ping_pkt_id = _read_varint(conn)   # 0x01
            ping_payload = _read_bytes(conn, ping_len - len(_varint(ping_pkt_id)))
            _send_packet(conn, 0x01, ping_payload)
        except Exception:
            pass

    except Exception:
        pass
    finally:
        try:
            conn.close()
        except OSError:
            pass


def run_status_server(host: str, port: int, motd: str) -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(8)
    print(f"[mc_status_server] Listening on {host}:{port}", flush=True)
    print(f"[mc_status_server] MOTD: {motd}", flush=True)

    try:
        while True:
            conn, _ = srv.accept()
            threading.Thread(
                target=_handle_client, args=(conn, motd), daemon=True
            ).start()
    except KeyboardInterrupt:
        pass
    finally:
        srv.close()
        print("[mc_status_server] Stopped.", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Minecraft status-only server")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--motd", default=DEFAULT_MOTD)
    args = parser.parse_args()
    run_status_server(args.host, args.port, args.motd)
