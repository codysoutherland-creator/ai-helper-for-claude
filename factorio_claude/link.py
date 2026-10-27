"""Talk to the claude-bridge mod: UDP first (works in single-player, no hosting needed), RCON as a fallback.

UDP protocol (see SPEC-v2.md): request parts "Q|<id>|<seq>|<total>|<chunk>", reply parts "A|<id>|<seq>|<total>|<chunk>",
each datagram <= 4000 bytes. The game must be started with --enable-lua-udp <udp_port> (the Factorio (Claude) shortcut does).

    from factorio_claude.link import call, LinkError
    call("ping")  # -> result, or raises LinkError

Config: config.json (udp_port, host/port/password for RCON); env overrides FACTORIO_UDP_PORT, FACTORIO_UDP_TIMEOUT,
FACTORIO_RCON_HOST/PORT/PASSWORD.
"""
import itertools
import json
import os
import socket
import threading
import time

from . import rcon

CHUNK = 3900
NOT_RESPONDING = ("game not responding (not running, paused, or launched without the Factorio (Claude) shortcut)")
_ids = itertools.count(1)
_lock = threading.Lock()
_sock = None
_cfg = None
_rcon_until = 0.0  # prefer RCON until this time after UDP failed and RCON worked


class LinkError(Exception):
    pass


def config():
    global _cfg
    if _cfg is None:
        cfg = rcon.load_config()
        cfg.setdefault("udp_port", 34500)
        if os.environ.get("FACTORIO_UDP_PORT"):
            cfg["udp_port"] = os.environ["FACTORIO_UDP_PORT"]
        cfg["udp_port"] = int(cfg["udp_port"])
        cfg["udp_timeout"] = float(os.environ.get("FACTORIO_UDP_TIMEOUT") or cfg.get("udp_timeout", 4))
        _cfg = cfg
    return _cfg


def _split(data: bytes):
    """Pieces of <= CHUNK bytes, never cutting inside a UTF-8 sequence."""
    parts, i = [], 0
    while i < len(data):
        j = min(i + CHUNK, len(data))
        while i + 1 < j < len(data) and 0x80 <= data[j] < 0xC0:
            j -= 1
        parts.append(data[i:j])
        i = j
    return parts or [b""]


def _socket():
    global _sock
    if _sock is None:
        _sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        _sock.bind(("127.0.0.1", 0))
    return _sock


def _drop_socket():
    global _sock
    if _sock is not None:
        _sock.close()
    _sock = None


def _udp(payload: bytes, port: int, timeout: float):
    """Raw response dict, or None on timeout."""
    sock = _socket()
    rid = f"{os.getpid()}.{next(_ids)}"
    parts = _split(payload)
    for n, p in enumerate(parts, 1):
        sock.sendto(f"Q|{rid}|{n}|{len(parts)}|".encode() + p, ("127.0.0.1", port))
    got, total = {}, None
    deadline = time.monotonic() + timeout
    while total is None or len(got) < total:
        left = deadline - time.monotonic()
        if left <= 0:
            return None
        sock.settimeout(left)
        try:
            pkt, _ = sock.recvfrom(65535)
        except socket.timeout:
            continue
        except ConnectionResetError:  # Windows: ICMP port unreachable = nothing listens on that port
            _drop_socket()
            return None
        bits = pkt.split(b"|", 4)
        if len(bits) != 5 or bits[0] != b"A" or bits[1].decode(errors="replace") != rid:
            continue  # stale reply from an earlier timed-out call
        got[int(bits[2])] = bits[4]
        total = int(bits[3])
    return json.loads(b"".join(got[i] for i in range(1, total + 1)).decode("utf-8"))


def _unwrap(resp):
    if not resp.get("ok"):
        raise LinkError(resp.get("error", "unknown error"))
    return resp.get("result")


def _via_rcon(op, args):
    try:
        return rcon.bridge(op, **args)
    except rcon.RconError as e:
        raise LinkError(str(e)) from e


def call(op, **args):
    """Call an op in the claude-bridge mod. Returns its result or raises LinkError. Thread-safe."""
    global _rcon_until
    args = {k: v for k, v in args.items() if v is not None}
    cfg = config()
    has_rcon = rcon.rcon_enabled(cfg)
    with _lock:
        if has_rcon and time.monotonic() < _rcon_until:
            try:
                return _via_rcon(op, args)
            except LinkError as e:
                if "can't reach" not in str(e) and "connection closed" not in str(e):
                    raise
                _rcon_until = 0.0
        payload = json.dumps({"op": op, "args": args}, separators=(",", ":")).encode("utf-8")
        resp = _udp(payload, cfg["udp_port"], cfg["udp_timeout"])
        if resp is not None:
            return _unwrap(resp)
        if not has_rcon:
            raise LinkError(NOT_RESPONDING)
        try:
            result = _via_rcon(op, args)
        except LinkError as e:
            if "can't reach" in str(e):
                raise LinkError(NOT_RESPONDING) from e
            raise
        _rcon_until = time.monotonic() + 30  # UDP is down but RCON works: skip the UDP timeout for a while
        return result


def reachable() -> bool:
    try:
        call("ping")
        return True
    except LinkError:
        return False
