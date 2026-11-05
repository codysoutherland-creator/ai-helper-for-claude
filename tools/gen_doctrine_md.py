"""Generate factorio_claude/knowledge/doctrine.md (and the skill's copy) from doctrine.json + roadmap.json.

    python tools/gen_doctrine_md.py           # write both files
    python tools/gen_doctrine_md.py --check   # exit 1 when either file is out of date (tests/test_knowledge.py)
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from factorio_claude import knowledge as K  # noqa: E402

OUTS = [ROOT / "factorio_claude" / "knowledge" / "doctrine.md", ROOT / "skill" / "factorio" / "doctrine.md"]
GROUP_TITLES = {"AUTO": "Automation first", "ACQ": "Getting materials", "MALL": "Mall", "RES": "Research",
                "ORG": "Organization", "SCALE": "Scale", "PWR": "Power", "SAFE": "Safety"}


def render() -> str:
    d = K.load()
    f, c = d["facts"], d["conventions"]
    out = ["# Factorio 2.x doctrine",
           "",
           "Generated from `factorio_claude/knowledge/doctrine.json` and `roadmap.json` by `tools/gen_doctrine_md.py`;"
           " do not edit by hand. Game facts are checked against the game's own data (`knowledge.check`, "
           "`tools/verify_doctrine.py`). Rules marked (c) are conventions: rules of thumb, not game facts.",
           ""]
    for g in K.GROUPS:
        rs = [r for r in d["rules"] if r["group"] == g]
        if not rs:
            continue
        out += [f"## {GROUP_TITLES[g]}", ""]
        for r in rs:
            out.append(f"- **{r['id']}**{' (c)' if r['kind'] == 'convention' else ''}: {r['text']}")
        out.append("")
    out += ["## Numbers (game facts)", "", "| machine | item | per min |", "|---|---|---|"]
    for m, t in f["machine_out_per_min"].items():
        for item, v in t.items():
            out.append(f"| {m} | {item} | {v:g} |")
    out += ["", "| drill | ore per min |", "|---|---|"]
    out += [f"| {k} | {v:g} |" for k, v in f["mining_out_per_min"].items()]
    out += ["", "Belts (items/s): " + ", ".join(f"{k} {v}" for k, v in f["belt_items_per_s"].items()) + ".",
            "Underground reach (tiles): " + ", ".join(f"{k} {v}" for k, v in f["underground_max_distance"].items()) + ".",
            "Per yellow lane: " + ", ".join(f"{v} {k}" for k, v in f["column_cap_per_yellow_lane"].items()) + ".",
            f"Steam: 1 offshore pump : {f['steam']['boilers_per_offshore_pump']} boilers : "
            f"{f['steam']['boilers_per_offshore_pump'] * f['steam']['engines_per_boiler']} engines; engine "
            f"{f['steam']['engine_kw']} kW; boiler {f['steam']['boiler_coal_per_min']} coal/min.",
            "Inserters (items/s, " + f["inserter_items_per_s"]["source"] + "): " +
            ", ".join(f"{k} {v}" for k, v in f["inserter_items_per_s"].items() if k != "source") + ".", "",
            "## Research", "", "| tech | units | s/unit | packs | unlocks |", "|---|---|---|---|---|"]
    for name, t in f["techs"].items():
        out.append(f"| {name} | {t['count']} | {t['unit_s']:g} | {', '.join(t['packs'])} | "
                   f"{', '.join(t.get('unlocks', [])) or '-'} |")
    out += ["", "Trigger techs: " + "; ".join(f"{k}: {v['what']}" for k, v in f["trigger_techs"].items()) + ".",
            "", "Default order (c): " + ", ".join(c["research_order"]) + ".", "",
            "## Scale (c)", "", "| SPM each | red AM1 | green AM1 | iron/min | copper/min | iron furnaces | "
            "copper furnaces |", "|---|---|---|---|---|---|---|"]
    for spm in (10, 30, 60):
        s = K.sizing(spm)
        out.append(f"| {spm} | {s['machines']['automation-science-pack']:g} | "
                   f"{s['machines']['logistic-science-pack']:g} | {s['iron-plate_per_min']:g} | "
                   f"{s['copper-plate_per_min']:g} | {s['furnaces']['iron']:g} | {s['furnaces']['copper']:g} |")
    h = c["headroom"]
    out += ["", f"Build for {h['build']:g}x the target, reserve room for {h['reserve_min']:g}-{h['reserve_max']:g}x; "
                f"grow power at {int(h['power_grow_at'] * 100)}% of capacity.",
            "Labs: " + c["labs_per_spm"] + ".", "",
            "| when | then |", "|---|---|"]
    out += [f"| {a} | {b} |" for a, b in c["expand_vs_rebuild"]]
    sp = c["spacing"]
    out += ["", "## Layout (c)", "",
            f"- Grid {sp['grid']} tiles; {sp['between_blocks_min']} tiles between blocks at least "
            f"({sp['between_blocks_pref'][0]}-{sp['between_blocks_pref'][1]} preferred); a {sp['bus_gap']}-tile gap "
            f"after every {sp['bus_group']} bus belts.",
            "- Bus lanes: " + ", ".join(f"{n} x{k}" for n, k in c["bus_lane_plan"]) + ".",
            f"- Smelting column: {c['footprints']['smelting_column']['width']} tiles wide, "
            f"{c['footprints']['smelting_column']['tiles_per_furnace_pair']} tiles per furnace pair, at most "
            f"{c['footprints']['smelting_column']['max_pairs_per_lane']} pairs per yellow ore lane.",
            f"- Mall v1 zone {c['footprints']['mall_v1']['zone'][0]} x {c['footprints']['mall_v1']['zone'][1]}: "
            + ", ".join(c["mall_v1"]) + f"; output chests limited to {c['mall_chest_limit_stacks']} stacks. Later: "
            + ", ".join(f"{k} (after {v})" for k, v in c["mall_v1_after"].items()) + ". Not in v1: "
            + "; ".join(f"{k} ({v})" for k, v in c["not_in_mall_v1"].items()) + ".",
            "", "## Getting materials (helper)", "",
            "Order: " + " > ".join(c["acquisition_order"]) + ".",
            f"Hand-craft: at most {c['handcraft']['max_job_s']} s per job and "
            f"{c['handcraft']['loop_alarm_s_per_10min']} s per 10 game minutes once a mall exists; target hand-craft "
            f"share under {int(c['handcraft']['target_share_after_mall'] * 100)}% of built items after the mall. "
            "Bootstrap items: " + ", ".join(c["handcraft"]["bootstrap_items"]) + ".",
            "", "## Autopilot roadmap", "", "| goal | what | needs | ships in |", "|---|---|---|---|"]
    for g in K.roadmap():
        out.append(f"| {g['id']} | {g['title']} | {', '.join(g['needs']) or '-'} | round {g['round']} |")
    out.append("")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    text = render()
    stale = [p for p in OUTS if not p.exists() or p.read_text(encoding="utf-8") != text]
    if a.check:
        for p in stale:
            print(f"out of date: {p.relative_to(ROOT)} (run tools/gen_doctrine_md.py)")
        return 1 if stale else 0
    for p in OUTS:
        p.write_text(text, encoding="utf-8", newline="\n")
        print("wrote", p.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
