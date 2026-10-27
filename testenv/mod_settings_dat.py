"""Write a Factorio mod-settings.dat (binary property tree) for the headless TEST harness.

    python testenv/mod_settings_dat.py <out.dat> [name=value ...]   # default: claude-debug-ops=true (startup)

Values: true/false -> bool, numbers -> double, anything else -> string. Settings named in RUNTIME go to runtime-global.
Only used by testenv/restart.ps1 for test instances (their own --mod-directory); never point it at a real mods folder.
"""
import struct
import sys

VERSION = (2, 1, 23, 0)  # game version written in the header; newer games read older files fine
RUNTIME = {"claude-who-can-ask", "claude-foreman-enabled", "claude-foreman-interval", "claude-foreman-chat",
           "claude-helper-use-player-items"}


def _string(s: str) -> bytes:
    b = s.encode("utf-8")
    if not b:
        return b"\x01"  # empty flag
    n = struct.pack("<B", len(b)) if len(b) < 255 else b"\xff" + struct.pack("<I", len(b))
    return b"\x00" + n + b


def _tree(v) -> bytes:
    if isinstance(v, bool):
        return b"\x01\x00" + (b"\x01" if v else b"\x00")
    if isinstance(v, (int, float)):
        return b"\x02\x00" + struct.pack("<d", float(v))
    if isinstance(v, str):
        return b"\x03\x00" + _string(v)
    if isinstance(v, dict):
        out = b"\x05\x00" + struct.pack("<I", len(v))
        for k, x in v.items():
            out += _string(k) + _tree(x)
        return out
    raise TypeError(type(v))


def build(settings: dict) -> bytes:
    tree = {"startup": {}, "runtime-global": {}, "runtime-per-user": {}}
    for name, value in settings.items():
        tree["runtime-global" if name in RUNTIME else "startup"][name] = {"value": value}
    return struct.pack("<HHHH", *VERSION) + b"\x00" + _tree(tree)


def parse_value(s: str):
    if s.lower() in ("true", "false"):
        return s.lower() == "true"
    try:
        return float(s)
    except ValueError:
        return s


def main(argv):
    if not argv:
        print(__doc__)
        return 2
    settings = {"claude-debug-ops": True}
    for kv in argv[1:]:
        k, _, v = kv.partition("=")
        settings[k] = parse_value(v)
    with open(argv[0], "wb") as f:
        f.write(build(settings))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
