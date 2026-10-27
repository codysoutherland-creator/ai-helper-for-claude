"""Cross-check validate.py verdicts in-engine (needs a headless test slot; not collected by pytest).

    powershell -NoProfile -ExecutionPolicy Bypass -File testenv/restart.ps1 -Name validator -Slot 8
    .venv\\Scripts\\python.exe tests\\live_validator.py [--slot 8] [case names...]

Every case in validator_cases.LIVE is validated offline (synthetic grid) and built on the slot's map at its own
origin (instant build, manual fuel where the case declares it, loaders/infinity chests as feeders), the game runs,
and the case's works() decides whether the layout really did its job. Verdicts must agree:
validator passes <=> engine works. Also compares engine facts with the validator's model (placement acceptance,
underground pairing, pipe segments, power coverage).
"""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from factorio_claude.planner.validate import _Validator, validate  # noqa: E402
from validator_cases import LIVE  # noqa: E402
from validator_engine import Engine  # noqa: E402
from validator_util import gd_all, make_grid  # noqa: E402


def model_facts(c, gd, info) -> tuple[int, list[str]]:
    """Compare the validator's model of each entity with what the engine reports. Returns (checked, mismatches)."""
    v = _Validator(gd, make_grid(c["resources"], c["water"]), c["expect"])
    v.run(list(c["entities"]))
    by_i = {r.i: r for r in v.recs if not r.existing}
    seg_v, seg_e = {}, {}
    checked, bad = 0, []

    def name_of(t):
        return None if t is None else t.name

    for i, r in by_i.items():
        e = info.get(i + 1) or {}
        if not e or e.get("gone"):
            continue
        where = f"{r.name}@({r.x:g},{r.y:g})"
        if r.type == "inserter":
            checked += 2
            vs, vd = name_of(r.extra["_src"][0]), name_of(r.extra["_dst"][0])
            if vs != e.get("pickup") or vd != e.get("drop"):
                bad.append(f"{where}: validator pickup/drop {vs}/{vd}, engine {e.get('pickup')}/{e.get('drop')}")
        if r.type == "mining-drill" and "_drop" in r.extra and e.get("drop_pos"):
            checked += 1  # drop_target is nil while a drill is idle, so compare the drop tile
            vt = [math.floor(v + 1e-6) for v in r.extra["_drop"]]
            et = [math.floor(v + 1e-6) for v in e["drop_pos"]]
            if vt != et or (e.get("drop") and name_of(r.extra["_dst"][0]) != e["drop"]):
                bad.append(f"{where}: validator drop tile {vt} ({name_of(r.extra['_dst'][0])}), engine {et} "
                           f"({e.get('drop')})")
        if r.type == "underground-belt":
            checked += 1
            n = r.node
            mate = next((m for m, _ in (n.outs if n.kind == "ug_in" else n.ins) if m.r.type == "underground-belt"), None)
            vm = [mate.r.x, mate.r.y] if mate else None
            if vm != e.get("ug_partner"):
                bad.append(f"{where}: validator partner {vm}, engine {e.get('ug_partner')}")
        if r.type == "transport-belt" and "shape" in e:
            checked += 1
            curve = any(mp == "keep" and m.dir != r.dir for m, mp in r.node.ins)
            if curve != (e["shape"] != "straight"):
                bad.append(f"{where}: validator curve={curve}, engine shape {e['shape']}")
        if r.type in ("pipe", "pipe-to-ground") and e.get("seg"):
            seg_v[i] = v.fluid_uf.find((r.i, 1))
            seg_e[i] = e["seg"][0]
        es = r.p.get("energy_source") or {}
        if es.get("type") == "electric" and r.type not in ("generator", "solar-panel"):
            checked += 1
            if hasattr(v, "power") and (e.get("net") is not None) != v._covered(r):
                bad.append(f"{where}: validator covered={v._covered(r)}, engine network {e.get('net')}")
    # same pipe partition: two pipes share a validator system <=> they share an engine fluid segment
    ks = sorted(seg_v)
    for a_i, a in enumerate(ks):
        for b in ks[a_i + 1:]:
            checked += 1
            if (seg_v[a] == seg_v[b]) != (seg_e[a] == seg_e[b]):
                bad.append(f"pipes {a + 1}/{b + 1}: validator same={seg_v[a] == seg_v[b]}, "
                           f"engine segments {seg_e[a]}/{seg_e[b]}")
    return checked, bad


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--slot", type=int, default=8)
    ap.add_argument("names", nargs="*")
    a = ap.parse_args(argv)
    cases = [c for c in LIVE if c["engine"] and (not a.names or c["name"] in a.names)]
    gd = gd_all()
    eng = Engine(a.slot)
    verdicts = {}
    for k, c in enumerate(cases):
        rep = validate(c["entities"], gd, make_grid(c["resources"], c["water"]), c["expect"])
        origin = (3000 + 200 * (k % 10), 3000 + 200 * (k // 10))
        placed = eng.build(c["entities"], origin, c["resources"], c["water"], c["fuel"], c["extras"], key=c["name"])
        refused = [c["entities"][i]["name"] + f"@({c['entities'][i]['x']},{c['entities'][i]['y']})"
                   for i, p in enumerate(placed) if not p.get("can")]
        verdicts[c["name"]] = (rep, refused)
    eng.elapsed = max(c["ticks"] for c in cases)
    eng.run(eng.elapsed)
    bad = facts = bad_facts = 0
    for c in cases:
        rep, refused = verdicts[c["name"]]
        info = eng.inspect(c["name"])
        works, detail = c["works"](info, eng)
        works = works and not refused
        kinds = sorted({i.kind for i in rep.errors})
        agree = rep.ok == works
        bad += not agree
        print(f"{'AGREE' if agree else 'DISAGREE'} {c['name']}: validator {'ok' if rep.ok else kinds}; engine "
              f"{'works' if works else 'fails'} ({detail}{'; refused ' + ', '.join(refused) if refused else ''})")
        for i in rep.errors[:4]:
            print(f"      {i.kind}: {i.msg}")
        n, mism = model_facts(c, gd, info)
        facts += n
        for m in mism:
            print(f"      MODEL MISMATCH {m}")
        bad_facts += len(mism)
    print(f"{len(cases) - bad}/{len(cases)} verdicts agree; model facts {facts - bad_facts}/{facts} match the engine")
    return 1 if bad or bad_facts else 0


if __name__ == "__main__":
    sys.exit(main())
