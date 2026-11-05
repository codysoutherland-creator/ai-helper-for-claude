"""Check doctrine.json's game facts against the installed game's prototypes (developer tool; CI uses the fixtures).

    python tools/verify_doctrine.py --data "<factorio>/data"      # base (+ space-age if present) technology/recipe.lua
    python tools/verify_doctrine.py --gamedata path/to/data.json   # an export_data dump of a running game
    python tools/verify_doctrine.py                                 # default: testenv/fixtures/seed_1001/data.json

Exit 0 when every checked fact matches. --data reads only the technology and recipe prototypes (a tolerant Lua table
reader: anything that is not a literal is skipped), so entity facts (speeds, belt rates) are checked by --gamedata.
Space Age overrides from data/space-age are applied on top of base when that folder exists.
"""
import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from factorio_claude import knowledge as K  # noqa: E402

TOKEN = re.compile(r"""
    (?P<ws>\s+)|(?P<lcomment>--\[(?P<eq1>=*)\[.*?\](?P=eq1)\])|(?P<comment>--[^\n]*)|
    (?P<lstr>\[(?P<eq2>=*)\[.*?\](?P=eq2)\])|(?P<str>"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')|
    (?P<num>0x[0-9a-fA-F]+|\d+\.?\d*(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?)|
    (?P<name>[A-Za-z_][A-Za-z0-9_]*)|(?P<op>\.\.\.|\.\.|==|~=|<=|>=|[{}()\[\],;=+\-*/%^#<>.:])""",
                   re.S | re.X)


def tokens(src: str):
    i, out = 0, []
    while i < len(src):
        m = TOKEN.match(src, i)
        if not m:
            i += 1
            continue
        i = m.end()
        k = m.lastgroup
        if k in ("ws", "comment", "lcomment", "eq1"):
            continue
        if k == "lstr" or k == "eq2":
            out.append(("str", m.group("lstr")))
        elif k == "str":
            out.append(("str", m.group("str")[1:-1]))
        elif k == "num":
            out.append(("num", float(int(m.group("num"), 16)) if m.group("num").startswith("0x")
                        else float(m.group("num"))))
        else:
            out.append((k, m.group(k)))
    return out


class Reader:
    """Literal Lua tables; any other expression (names, calls, arithmetic) reads as None."""

    def __init__(self, toks):
        self.t, self.i = toks, 0

    def peek(self, k=0):
        return self.t[self.i + k] if self.i + k < len(self.t) else ("eof", None)

    def skip_expr(self):
        depth = 0
        while self.i < len(self.t):
            k, v = self.peek()
            if v in ("{", "(", "["):
                depth += 1
            elif v in ("}", ")", "]"):
                if depth == 0:
                    return
                depth -= 1
            elif v in (",", ";") and depth == 0:
                return
            self.i += 1

    def value(self):
        k, v = self.peek()
        start = self.i
        if v == "{":
            val = self.table()
        elif k in ("str", "num"):
            self.i += 1
            val = v
        elif k == "name" and v in ("true", "false", "nil"):
            self.i += 1
            val = {"true": True, "false": False, "nil": None}[v]
        else:
            val = None
        nk, nv = self.peek()
        if nv not in (",", ";", "}", ")") or (val is None and self.i == start):
            self.i = start
            self.skip_expr()
            return None
        return val

    def table(self):
        self.i += 1   # {
        arr, d = [], {}
        while self.peek()[1] != "}" and self.peek()[0] != "eof":
            k, v = self.peek()
            if k == "name" and self.peek(1)[1] == "=" and self.peek(1)[1] != "==":
                self.i += 2
                d[v] = self.value()
            elif v == "[":
                self.i += 1
                key = self.value()
                if self.peek()[1] == "]":
                    self.i += 1
                if self.peek()[1] == "=":
                    self.i += 1
                d[key] = self.value()
            else:
                arr.append(self.value())
            if self.peek()[1] in (",", ";"):
                self.i += 1
        self.i += 1   # }
        return d if d or not arr else arr


def prototypes(path: Path) -> list:
    """Every table literal passed to data:extend in a prototype file."""
    toks = tokens(path.read_text(encoding="utf-8", errors="replace"))
    out = []
    for i in range(len(toks) - 3):
        if toks[i] == ("name", "data") and toks[i + 1][1] == ":" and toks[i + 2] == ("name", "extend"):
            r = Reader(toks)
            r.i = i + 3
            if r.peek()[1] == "(":
                r.i += 1
            if r.peek()[1] == "{":
                v = r.table()
                out += [p for p in (v if isinstance(v, list) else [v]) if isinstance(p, dict)]
    return out


def gd_from_lua(data_dir: Path):
    """A minimal GameData-shaped dict (technologies + recipes) from base/ and space-age/ prototype files."""
    techs, recipes = {}, {}
    files = []
    for mod in ("base", "space-age"):
        d = data_dir / mod / "prototypes"
        files += [d / "technology.lua", d / "recipe.lua"]
    for f in files:
        if not f.exists():
            continue
        for p in prototypes(f):
            if p.get("type") == "technology" and p.get("name"):
                unit = p.get("unit") or {}
                ings = [[x[0], x[1]] for x in (unit.get("ingredients") or []) if isinstance(x, list) and x]
                techs[p["name"]] = {"unit_count": unit.get("count") or 1, "unit_energy": (unit.get("time") or 0) * 60,
                                    "ingredients": ings, "prerequisites": p.get("prerequisites") or [],
                                    "effects": p.get("effects") or [], "enabled": True,
                                    "research_trigger": p.get("research_trigger")}
            elif p.get("type") == "recipe" and p.get("name"):
                res = p.get("results") or []
                recipes[p["name"]] = {"energy": p.get("energy_required") or 0.5,
                                      "ingredients": [{"name": x.get("name"), "amount": x.get("amount")}
                                                      for x in (p.get("ingredients") or []) if isinstance(x, dict)],
                                      "products": [{"name": x.get("name"), "amount": x.get("amount")}
                                                   for x in res if isinstance(x, dict)]}
    # entity speeds are not read from Lua: the doctrine's speeds stand in, so recipe times and amounts get checked
    ents = {m: {"crafting_speed": v} for m, v in K.facts()["crafting_speed"].items()}
    return {"technologies": techs, "recipes": recipes, "entities": ents}


class _GD:
    def __init__(self, raw):
        self.raw = raw


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, help="the game's data folder (holds base/ and space-age/)")
    ap.add_argument("--gamedata", type=Path, help="an export_data dump (claude/data.json)")
    a = ap.parse_args()
    if a.data:
        raw = gd_from_lua(a.data)
        if not raw["technologies"]:
            print("no technology prototypes found under", a.data)
            return 2
        issues = K.check(_GD(raw))
        # entity facts need the full export: only the tech/recipe lines count here
        issues = [i for i in issues if i.startswith(("tech ", "trigger tech", "research_order", "recipe ",
                                                     "mall item", "assembling-machine-", "stone-furnace ",
                                                     "steel-furnace ")) or " not unlocked by " in i]
        print(f"checked {len(raw['technologies'])} techs and {len(raw['recipes'])} recipes from {a.data}")
    else:
        from factorio_claude.planner.gamedata import GameData
        path = a.gamedata or ROOT / "testenv" / "fixtures" / "seed_1001" / "data.json"
        issues = K.check(GameData.load(path))
        print("checked", path)
    for i in issues:
        print("MISMATCH", i)
    print("ok" if not issues else f"{len(issues)} mismatch(es)")
    return 1 if issues else 0


if __name__ == "__main__":
    sys.exit(main())
