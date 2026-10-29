"""Player/helper state for planner v2 (PLANNER-v2.md section 4.1): inventories, chests, machine status, production,
research. Read from the mod's `base_state` op (older mods: assembled from player / agent_status / production /
research), or loaded from a fixture's state.json. A missing state means a fresh freeplay start.

    st = BaseState.from_link(call, cx, cy, radius)
    st = BaseState.load(path); st.save(path)
    st.stock(points=[(x, y)], radius=60)    # items the helper can use: player + helper + chests in range
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import cost

FRESH_INVENTORY = {"iron-plate": 8, "burner-mining-drill": 1, "stone-furnace": 1, "wood": 1}
STOCK_RADIUS = 60       # chests this close to the plan's sites or the player count as stock
MAX_RADIUS = 300


def _pos(v):
    if v is None:
        return None
    if isinstance(v, dict):
        return (float(v.get("x", 0)), float(v.get("y", 0)))
    return (float(v[0]), float(v[1]))


def _inv(v) -> dict:
    """{item: n} from a dict, a list of {name, count} or [[name, n]] rows."""
    out = {}
    if isinstance(v, dict):
        for k, n in v.items():
            if isinstance(n, (int, float)) and n > 0:
                out[str(k)] = out.get(str(k), 0) + n
    elif isinstance(v, list):
        for r in v:
            if isinstance(r, dict) and r.get("name"):
                out[r["name"]] = out.get(r["name"], 0) + (r.get("count") or 0)
            elif isinstance(r, (list, tuple)) and len(r) >= 2:
                out[str(r[0])] = out.get(str(r[0]), 0) + r[1]
    return out


@dataclass
class BaseState:
    tick: int = 0
    surface: str = "nauvis"
    player_pos: tuple | None = None
    player_inv: dict = field(default_factory=dict)
    helper_pos: tuple | None = None
    helper_inv: dict = field(default_factory=dict)
    helper_stats: dict = field(default_factory=dict)
    chests: list = field(default_factory=list)      # [{unit?, name, x, y, contents}]
    machines: list = field(default_factory=list)    # [{unit?, name, type, x, y, status, fuel, input, output, ...}]
    production: dict = field(default_factory=dict)  # items produced per minute (10 min window)
    research_done: list = field(default_factory=list)
    research_current: str | None = None
    fresh: bool = False                             # synthesised "fresh freeplay start"
    source: str = ""
    player_connected: bool = True                   # the helper only takes from a connected player ...
    use_player_items: bool = True                   # ... and only when the mod setting allows it

    # ---- construction --------------------------------------------------------------------------------------
    @classmethod
    def fresh_start(cls, pos=(0.0, 0.0)) -> "BaseState":
        return cls(player_pos=tuple(pos), player_inv=dict(FRESH_INVENTORY), fresh=True, source="fresh")

    @classmethod
    def from_dict(cls, d: dict) -> "BaseState":
        """From a base_state op result or a saved state.json."""
        if not d:
            return cls.fresh_start()
        if "player_inv" in d or "helper_inv" in d:   # saved form
            d = dict(d)
            for k in ("player_pos", "helper_pos"):
                d[k] = _pos(d.get(k))
            known = {f for f in cls.__dataclass_fields__}
            return cls(**{k: v for k, v in d.items() if k in known})
        pl = d.get("player") or {}
        he = d.get("helper") or {}
        res = d.get("research") or {}
        prod = d.get("production") or {}
        sets = d.get("settings") or {}
        return cls(tick=int(d.get("tick") or 0), surface=d.get("surface") or "nauvis",
                   player_connected=bool(pl.get("connected", True)) if pl else False,
                   use_player_items=bool(sets.get("use_player_items", True)),
                   player_pos=_pos(pl.get("pos")), player_inv=_inv(pl.get("main") or pl.get("inventory")),
                   helper_pos=_pos(he.get("pos")), helper_inv=_inv(he.get("main") or he.get("inventory")),
                   helper_stats=he.get("stats") or {}, chests=[dict(c, contents=_inv(c.get("contents")))
                                                             for c in d.get("chests") or []],
                   machines=list(d.get("machines") or []),
                   production=_inv(prod.get("per_min") or prod.get("items_produced") or {}),
                   research_done=list(res.get("done") or []), research_current=res.get("current"),
                   source=d.get("source") or "base_state")

    @classmethod
    def from_link(cls, call, cx, cy, radius=200) -> "BaseState":
        """Ask the game. Uses the `base_state` op when the mod has it, else the older ops (no chests/machines)."""
        r = int(min(MAX_RADIUS, max(16, radius)))
        try:
            return cls.from_dict(dict(call("base_state", x=cx, y=cy, radius=r), source="base_state"))
        except Exception:  # older mod without base_state (LinkError) or a bad answer: fall back
            pass
        out = {"player": {}, "helper": None, "production": {}, "research": {}, "source": "ops"}
        try:
            p = call("player")
            out["player"] = {"pos": p.get("pos"), "main": p.get("inventory") or {}}
            out["surface"] = p.get("surface")
        except Exception:
            out["player"] = {"pos": {"x": cx, "y": cy}, "main": {}}
        try:
            a = call("agent_status")
            if a.get("alive"):
                out["helper"] = {"pos": a.get("pos"), "main": a.get("inventory") or {}}
        except Exception:
            pass
        try:
            out["production"] = {"per_min": call("production", window="10m", limit=40).get("items_produced") or {}}
        except Exception:
            pass
        st = cls.from_dict(out)
        st.source = "ops"
        return st

    @classmethod
    def load(cls, path) -> "BaseState":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    def to_dict(self) -> dict:
        return asdict(self)

    def save(self, path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), indent=1), encoding="utf-8")
        return p

    # ---- queries ---------------------------------------------------------------------------------------------
    @property
    def pos(self):
        """Where the work starts: the helper if it exists, else the player."""
        return self.helper_pos or self.player_pos

    @property
    def player_usable(self) -> bool:
        """The helper can take from the player's inventory (agent.lua take_player: connected + mod setting)."""
        return bool(self.player_inv) and self.player_connected and self.use_player_items

    def stock(self, points=(), radius=STOCK_RADIUS, chests=True, machine_output=True) -> dict:
        """Items the plan can use now: helper inventory, the player's when the helper may take it (connected player,
        mod setting on), chests within `radius` of any of `points` or of the player, and plates in furnace output
        slots in range."""
        out = {}
        for inv in ((self.player_inv if self.player_usable else {}), self.helper_inv):
            for k, v in inv.items():
                cost._add(out, k, v)
        pts = [p for p in list(points) + [self.player_pos, self.helper_pos] if p is not None]

        def near(x, y):
            return any(math.hypot(x - px, y - py) <= radius for px, py in pts) if pts else True
        if chests:
            for c in self.chests:
                if near(c.get("x", 0), c.get("y", 0)):
                    for k, v in _inv(c.get("contents")).items():
                        cost._add(out, k, v)
        if machine_output:
            for m in self.machines:
                if m.get("type") == "furnace" and near(m.get("x", 0), m.get("y", 0)):
                    for k, v in _inv(m.get("output")).items():
                        cost._add(out, k, v)
        return out

    def helper_rate(self) -> float:
        return cost.helper_rate(self.helper_stats)

    def machine_index(self) -> dict:
        """(name, x, y) -> machine row, plus unit -> row."""
        idx = {}
        for m in self.machines:
            idx[(m.get("name"), float(m.get("x", 0)), float(m.get("y", 0)))] = m
            if m.get("unit") is not None:
                idx[("unit", m["unit"])] = m
        return idx

    def brief(self) -> dict:
        """Short block for Claude / world_summary."""
        st = self.stock()
        keys = ("iron-plate", "copper-plate", "stone", "coal", "wood", "iron-ore", "copper-ore", "iron-gear-wheel",
                "burner-mining-drill", "stone-furnace", "transport-belt")
        return {"stock": {k: int(st[k]) for k in keys if st.get(k)}, "production_per_min":
                {k: round(v, 1) for k, v in sorted(self.production.items(), key=lambda kv: -kv[1])[:6]},
                "helper": bool(self.helper_pos), "helper_rate_per_min": self.helper_rate(), "source": self.source,
                "fresh": self.fresh}
