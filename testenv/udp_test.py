"""UDP transport test for the claude-bridge mod (chunked Q/A protocol, see SPEC-v2.md).
  python testenv/udp_test.py --port 34501 [--rcon-port 27017 --rcon-password testpw] [--n 20]
Also usable as a tiny client:  from udp_test import UdpLink; UdpLink(34501).call("ping")"""
import argparse
import itertools
import json
import socket
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CHUNK = 3900
_ids = itertools.count(1)


def split(data: bytes, size=CHUNK):
    """Pieces of <= size bytes, never cutting inside a UTF-8 sequence."""
    parts, i = [], 0
    while i < len(data):
        j = min(i + size, len(data))
        while i + 1 < j < len(data) and 0x80 <= data[j] < 0xC0:
            j -= 1
        parts.append(data[i:j])
        i = j
    return parts or [b""]


class UdpLink:
    def __init__(self, port, timeout=4.0):
        self.port, self.timeout = port, timeout
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))

    def call(self, op, **args):
        """Returns (response dict, packets sent, packets received)."""
        rid = f"t{next(_ids)}"
        body = json.dumps({"op": op, "args": args}, separators=(",", ":")).encode()
        parts = split(body)
        for n, p in enumerate(parts, 1):
            self.sock.sendto(f"Q|{rid}|{n}|{len(parts)}|".encode() + p, ("127.0.0.1", self.port))
        got, total = {}, None
        deadline = time.monotonic() + self.timeout
        while total is None or len(got) < total:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError(f"no complete reply for {op} ({len(got)}/{total} parts)")
            self.sock.settimeout(left)
            pkt, _ = self.sock.recvfrom(65535)
            kind, pid, seq, tot, chunk = pkt.split(b"|", 4)
            if kind != b"A" or pid.decode() != rid:
                continue  # stale reply from an earlier timed-out call
            got[int(seq)] = chunk
            total = int(tot)
        return json.loads(b"".join(got[i] for i in range(1, total + 1))), len(parts), total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=34500, help="game's --enable-lua-udp port")
    ap.add_argument("--rcon-port", type=int, help="for the inbox test (plain chat via RCON); default port-34500+27016")
    ap.add_argument("--rcon-password", default="testpw")
    ap.add_argument("--n", type=int, default=20, help="pings for the latency measurement")
    a = ap.parse_args()
    link = UdpLink(a.port)
    fails = 0

    def check(name, cond, info=""):
        nonlocal fails
        print(f"{'ok  ' if cond else 'FAIL'} {name} {info}")
        fails += not cond

    r, _, _ = link.call("ping")
    check("ping", r.get("ok") and "tick" in r["result"], f"tick={r.get('result', {}).get('tick')}")

    lat = []
    for _ in range(a.n):
        t0 = time.perf_counter()
        link.call("ping")
        lat.append((time.perf_counter() - t0) * 1000)
    print(f"     latency over {a.n} pings: min {min(lat):.1f} ms, median {statistics.median(lat):.1f} ms, "
          f"max {max(lat):.1f} ms")

    r, _, _ = link.call("nope")
    check("unknown op -> error", r == {"ok": False, "error": "unknown op nope"}, json.dumps(r))

    from factorio_claude import blueprints as bpm
    bp = bpm.encode(bpm.make_blueprint(bpm.assembler_line(count=60) + bpm.smelter_column(count=40)))
    t0 = time.perf_counter()
    r, sent, recv = link.call("blueprint_inspect", blueprint=bp)
    ms = (time.perf_counter() - t0) * 1000
    check("big request (blueprint_inspect)", r.get("ok") and r["result"]["count"] > 500,
          f"req {len(bp)} B in {sent} parts, reply {recv} parts, {ms:.0f} ms, "
          f"count={r.get('result', {}).get('count')} err={r.get('error')}")
    check("big request -> multi-part", sent > 1 and recv > 1)

    t0 = time.perf_counter()
    r, sent, recv = link.call("map", x=0, y=0, radius=40)
    ms = (time.perf_counter() - t0) * 1000
    size = len(json.dumps(r))
    check("big response (map r=40)", r.get("ok") and len(r["result"]["rows"]) == 80 and recv > 1,
          f"{size} B in {recv} parts, {ms:.0f} ms")

    # inbox flow: plain RCON text = server chat
    from factorio_claude import rcon
    rc = rcon.Rcon("127.0.0.1", a.rcon_port or a.port - 34500 + 27016, a.rcon_password)
    link.call("inbox_pop")  # drain
    rc.command("@claude hi from udp_test")
    rc.command("just chatting, not for claude")
    time.sleep(0.2)
    r, _, _ = link.call("inbox_pop")
    msgs = r.get("result") or []
    check("inbox_pop gets @claude chat", len(msgs) == 1 and msgs[0]["text"] == "hi from udp_test"
          and msgs[0]["source"] == "chat", json.dumps(msgs))
    r, _, _ = link.call("inbox_pop")
    check("inbox cleared", r.get("result") in ([], {}), json.dumps(r))
    r, _, _ = link.call("claude_status", status="thinking")
    check("claude_status", r == {"ok": True, "result": True}, json.dumps(r))
    r, _, _ = link.call("claude_reply", text="hello back (ü→ unicode ok)")
    check("claude_reply", r == {"ok": True, "result": True}, json.dumps(r))
    r, _, _ = link.call("claude_reply")
    check("claude_reply without text -> error", r.get("ok") is False, r.get("error", ""))
    link.call("claude_status", status="idle")
    print("FAILED" if fails else "ALL OK", f"({fails} failures)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
