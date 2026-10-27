"""Minimal Source-RCON client for Factorio, plus a helper for the claude-bridge mod."""
import json
import os
import socket
import struct
import threading
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"

AUTH, AUTH_RESPONSE, EXEC, RESPONSE = 3, 2, 2, 0


class RconError(Exception):
    pass


def rcon_enabled(cfg) -> bool:
    """RCON is opt-in: config "rcon": true plus a password. Configs from before the "rcon" key existed keep RCON on
    when they carry a password (unchanged behaviour for existing installs)."""
    if not cfg.get("password"):
        return False
    return bool(cfg.get("rcon", True))


def load_config():
    cfg = {"host": "127.0.0.1", "port": 27015, "password": ""}
    if CONFIG_PATH.exists():
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig")))
    for key in ("host", "port", "password"):
        env = os.environ.get(f"FACTORIO_RCON_{key.upper()}")
        if env:
            cfg[key] = env
    return cfg


class Rcon:
    def __init__(self, host, port, password, timeout=15.0):
        self.host, self.port, self.password, self.timeout = host, port, password, timeout
        self.sock = None
        self.next_id = 1
        self.lock = threading.Lock()

    def _send(self, pid, ptype, body):
        data = body.encode("utf-8") + b"\x00\x00"
        self.sock.sendall(struct.pack("<iii", len(data) + 8, pid, ptype) + data)

    def _recv_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise RconError("connection closed by Factorio")
            buf += chunk
        return buf

    def _recv(self):
        (size,) = struct.unpack("<i", self._recv_exact(4))
        pid, ptype = struct.unpack("<ii", self._recv_exact(8))
        body = self._recv_exact(size - 8)
        return pid, ptype, body[:-2].decode("utf-8", errors="replace")

    def connect(self):
        self.close()
        try:
            self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as e:
            raise RconError(
                f"can't reach Factorio RCON at {self.host}:{self.port} ({e}). Is the game hosted as multiplayer "
                f"with --rcon-bind/--rcon-password launch options (config \"rcon\": true)?"
            ) from e
        self._send(1, AUTH, self.password)
        while True:
            pid, ptype, _ = self._recv()
            if ptype == AUTH_RESPONSE:
                if pid == -1:
                    raise RconError("RCON password rejected")
                return

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None

    def command(self, cmd):
        with self.lock:
            for attempt in (1, 2):
                try:
                    if self.sock is None:
                        self.connect()
                    self.next_id = self.next_id % 1_000_000 + 2
                    pid = self.next_id
                    self._send(pid, EXEC, cmd)
                    while True:
                        rid, _, body = self._recv()
                        if rid == pid:
                            return body
                except (OSError, RconError, struct.error):
                    self.close()
                    if attempt == 2:
                        raise


_client = None


def client():
    global _client
    if _client is None:
        cfg = load_config()
        if not rcon_enabled(cfg):
            raise RconError('RCON is off (opt-in: set "rcon": true in config.json; the launcher then starts the game '
                            "with RCON bound to 127.0.0.1)")
        _client = Rcon(cfg["host"], int(cfg["port"]), cfg["password"])
    return _client


def bridge(op, **args):
    """Call an op in the claude-bridge mod. Returns the result or raises RconError."""
    payload = json.dumps({"op": op, "args": {k: v for k, v in args.items() if v is not None}},
                         separators=(",", ":"))
    raw = client().command("/claude " + payload)
    if not raw.strip():
        raise RconError("empty response — is the claude-bridge mod enabled in this save?")
    line = raw.strip().splitlines()[-1]
    try:
        resp = json.loads(line)
    except json.JSONDecodeError:
        raise RconError(f"unexpected response: {raw[:500]}")
    if not resp.get("ok"):
        raise RconError(resp.get("error", "unknown error"))
    return resp.get("result")


_lua_armed = False


def lua(code):
    """Run raw Lua via /silent-command (disables achievements). Use rcon.print() to return data."""
    global _lua_armed
    if not _lua_armed:
        # Factorio swallows the first console command and asks for it to be repeated (achievement
        # warning). Burn that on a harmless probe so the real command isn't silently dropped.
        for _ in range(2):
            if client().command('/silent-command rcon.print("armed")').strip() == "armed":
                break
        _lua_armed = True
    return client().command("/silent-command " + code)
