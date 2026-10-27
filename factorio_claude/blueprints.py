"""Blueprint string codec + parametric layout generators (Factorio 2.x format)."""
import base64
import json
import zlib

# Factorio 2.1.23 -> map version number used in blueprint JSON
VERSION = (2 << 48) | (1 << 32) | (23 << 16)

# 16-way directions (2.0+)
DIR = {"north": 0, "east": 4, "south": 8, "west": 12}
OPPOSITE = {"north": "south", "south": "north", "east": "west", "west": "east"}

# Inserter entity direction relative to where it DROPS items.
# Calibrated in-engine (2.1): an inserter with direction D picks up from D and drops opposite,
# so to drop toward X we set direction = OPPOSITE[X].
INSERTER_DIR_FOR_DROP = {d: DIR[OPPOSITE[d]] for d in DIR}


MAX_STRING = 5_000_000      # characters of a blueprint string we accept
MAX_DECODED = 32_000_000    # bytes of JSON one string may inflate to (decompression-bomb guard)


def decode(s: str) -> dict:
    s = s.strip()
    if not s or s[0] != "0":
        raise ValueError("not a Factorio blueprint string (should start with '0')")
    if len(s) > MAX_STRING:
        raise ValueError(f"blueprint string too long ({len(s)} > {MAX_STRING} characters)")
    d = zlib.decompressobj()
    raw = d.decompress(base64.b64decode(s[1:]), MAX_DECODED)
    if d.unconsumed_tail:
        raise ValueError(f"blueprint inflates to more than {MAX_DECODED} bytes")
    return json.loads(raw)


def encode(obj: dict) -> str:
    raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    return "0" + base64.b64encode(zlib.compress(raw, 9)).decode("ascii")


POLE_REACH = {"small-electric-pole": 7.5, "medium-electric-pole": 9, "big-electric-pole": 30, "substation": 18}
POLE_COPPER = 5  # defines.wire_connector_id.pole_copper


def pole_wires(out: list) -> list:
    """Copper wires like in-game auto-connect: each pole links to up to 5 nearest earlier poles in reach.
    Robot-built poles from a blueprint only get the wires the blueprint lists."""
    poles = [e for e in out if e["name"] in POLE_REACH]
    wires = []
    for i, a in enumerate(poles):
        ax, ay = a["position"]["x"], a["position"]["y"]
        cands = []
        for b in poles[:i]:
            d = ((b["position"]["x"] - ax) ** 2 + (b["position"]["y"] - ay) ** 2) ** 0.5
            if d <= min(POLE_REACH[a["name"]], POLE_REACH[b["name"]]):
                cands.append((d, b["entity_number"]))
        for _, num in sorted(cands)[:5]:
            wires.append([num, POLE_COPPER, a["entity_number"], POLE_COPPER])
    return wires


def make_blueprint(entities: list, label: str = "Claude blueprint", icons: list | None = None) -> dict:
    """entities: [{name, x, y, direction?('north'|...|int), recipe?, ...extra keys passed through}].
    Electric poles are wired together automatically."""
    out = []
    for i, e in enumerate(entities, 1):
        ent = {"entity_number": i, "name": e["name"], "position": {"x": e["x"], "y": e["y"]}}
        d = e.get("direction")
        if isinstance(d, str):
            d = DIR[d]
        if d:
            ent["direction"] = d
        for k, v in e.items():
            if k not in ("name", "x", "y", "direction"):
                ent[k] = v
        out.append(ent)
    if not icons:
        counts = {}
        for e in entities:
            counts[e["name"]] = counts.get(e["name"], 0) + 1
        top = sorted(counts, key=counts.get, reverse=True)[:2]
        icons = [{"signal": {"type": "item", "name": n}, "index": i} for i, n in enumerate(top, 1)]
    bp = {"item": "blueprint", "label": label, "icons": icons, "entities": out, "version": VERSION}
    wires = pole_wires(out)
    if wires:
        bp["wires"] = wires
    return {"blueprint": bp}


def _inserter(name, x, y, drop):
    return {"name": name, "x": x, "y": y, "direction": INSERTER_DIR_FOR_DROP[drop]}


# --- generators -----------------------------------------------------------------
# All generators lay out a vertical column. Belts run north (items flow toward y=0).
# Column layout left->right: input belt | inserter | machine | inserter | output belt


def smelter_column(count=8, furnace="stone-furnace", belt="transport-belt", inserter=None, pole="small-electric-pole"):
    """Furnace column. Stone/steel furnaces (2x2) use burner inserters by default and need no power;
    put ore on one lane and coal on the other lane of the input belt. Electric furnaces (3x3) get
    electric inserters + poles."""
    ents = []
    size = 3 if furnace == "electric-furnace" else 2
    if inserter is None:
        inserter = "inserter" if furnace == "electric-furnace" else "burner-inserter"
    electric = inserter != "burner-inserter"
    out_x = 2 + size + 1  # belt column on the right
    for i in range(count):
        y0 = i * size
        for dy in range(size):
            ents.append({"name": belt, "x": 0.5, "y": y0 + dy + 0.5, "direction": "north"})
            ents.append({"name": belt, "x": out_x + 0.5, "y": y0 + dy + 0.5, "direction": "north"})
        ents.append(_inserter(inserter, 1.5, y0 + 0.5, "east"))
        ents.append({"name": furnace, "x": 2 + size / 2, "y": y0 + size / 2})
        ents.append(_inserter(inserter, 2 + size + 0.5, y0 + 0.5, "east"))
        if electric:
            ents.append({"name": pole, "x": 1.5, "y": y0 + size - 0.5})
            ents.append({"name": pole, "x": 2 + size + 0.5, "y": y0 + size - 0.5})
    return ents


def assembler_line(count=6, recipe="iron-gear-wheel", machine="assembling-machine-1", belt="transport-belt",
                   inserter="inserter", pole="small-electric-pole", second_input=False):
    """Row of assemblers (3x3) fed from a belt on the left, outputting to a belt on the right.
    second_input=True adds a second input belt + long-handed inserter on the far left for 3+ ingredients."""
    ents = []
    off = 1 if second_input else 0
    for i in range(count):
        y0 = i * 3
        for dy in range(3):
            ents.append({"name": belt, "x": off + 0.5, "y": y0 + dy + 0.5, "direction": "north"})
            ents.append({"name": belt, "x": off + 6.5, "y": y0 + dy + 0.5, "direction": "north"})
            if second_input:
                ents.append({"name": belt, "x": -0.5, "y": y0 + dy + 0.5, "direction": "north"})
        ents.append(_inserter(inserter, off + 1.5, y0 + 1.5, "east"))
        if second_input:
            ents.append(_inserter("long-handed-inserter", off + 1.5, y0 + 2.5, "east"))
        ents.append({"name": machine, "x": off + 3.5, "y": y0 + 1.5, "recipe": recipe})
        ents.append(_inserter(inserter, off + 5.5, y0 + 1.5, "east"))
        ents.append({"name": pole, "x": off + 1.5, "y": y0 + 0.5})
        ents.append({"name": pole, "x": off + 5.5, "y": y0 + 0.5})
    return ents


def mining_outpost(rows=4, drill="electric-mining-drill", belt="transport-belt", pole="small-electric-pole"):
    """Two columns of 3x3 drills facing a shared belt in the middle, poles between pairs."""
    ents = []
    for i in range(rows):
        y = i * 3 + 1.5
        ents.append({"name": drill, "x": 1.5, "y": y, "direction": "east"})
        ents.append({"name": drill, "x": 5.5, "y": y, "direction": "west"})
        for dy in range(3):
            ents.append({"name": belt, "x": 3.5, "y": i * 3 + dy + 0.5, "direction": "north"})
        if i % 2 == 0:
            ents.append({"name": pole, "x": 7.5, "y": y})
    return ents


def solar_block(pairs=2, panel_rows=4, accumulator_rows=0, pole="medium-electric-pole"):
    """Solar field built from 7-wide strips: panel | pole gap | panel. Each strip has a medium-pole
    column in its 1-tile gap (one pole per 6 tiles) that powers both sides. Optional rows of 2x2
    accumulators (two per row per strip) below the panels. Ratio hint: 25 panels : 21 accumulators."""
    ents = []
    height = panel_rows * 3 + accumulator_rows * 2
    for p in range(pairs):
        bx = p * 7
        for r in range(panel_rows):
            ents.append({"name": "solar-panel", "x": bx + 1.5, "y": r * 3 + 1.5})
            ents.append({"name": "solar-panel", "x": bx + 5.5, "y": r * 3 + 1.5})
        for r in range(accumulator_rows):
            y = panel_rows * 3 + r * 2 + 1
            ents.append({"name": "accumulator", "x": bx + 1, "y": y})
            ents.append({"name": "accumulator", "x": bx + 6, "y": y})
        y = 3.5
        while y - 3.5 < height:
            ents.append({"name": pole, "x": bx + 3.5, "y": min(y, height - 0.5)})
            y += 6
    return ents


GENERATORS = {
    "smelter_column": smelter_column,
    "assembler_line": assembler_line,
    "mining_outpost": mining_outpost,
    "solar_block": solar_block,
}
