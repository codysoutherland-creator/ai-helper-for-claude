"""Build the Factorio mod zip: dist/claude-bridge_<version>.zip with one top folder claude-bridge_<version>/.

Factorio requires the zip to be named {mod-name}_{version}.zip (name + version from info.json); the folder inside
"does not have any naming restrictions" (lua-api.factorio.com/latest/auxiliary/mod-structure.html), so it uses the
same conventional name. Checks first that info.json, the top changelog.txt entry and (with --tag) the git tag agree.

    python tools/build_mod_zip.py [--tag v0.4.0] [--out dist]
"""
import argparse
import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MOD = ROOT / "mod" / "claude-bridge"
SKIP_DIRS = {"__pycache__", ".git"}
SKIP_FILES = {".aihelper-copy", ".DS_Store", "Thumbs.db"}
DASHES = "-" * 99


def check_changelog(text: str, version: str) -> list:
    """Problems with changelog.txt in Factorio's strict format (empty list = fine)."""
    problems = []
    lines = text.splitlines()
    if not lines or lines[0] != DASHES:
        problems.append("changelog.txt must start with a line of exactly 99 dashes")
    versions = re.findall(r"^Version: (\S+)$", text, re.M)
    if not versions:
        problems.append("changelog.txt has no 'Version: x.y.z' line")
    elif versions[0] != version:
        problems.append(f"top changelog entry is {versions[0]}, info.json says {version}")
    for i, line in enumerate(lines, 1):
        if line.startswith("-") and line != DASHES:
            problems.append(f"changelog.txt line {i}: separator must be exactly 99 dashes")
        if "\t" in line:
            problems.append(f"changelog.txt line {i}: tabs are not allowed")
        if line.rstrip() != line:
            problems.append(f"changelog.txt line {i}: trailing whitespace")
    return problems


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", help="release tag (vX.Y.Z) that must match info.json")
    ap.add_argument("--out", default=str(ROOT / "dist"))
    a = ap.parse_args(argv)
    info = json.loads((MOD / "info.json").read_text(encoding="utf-8"))
    name, version = info["name"], info["version"]
    problems = []
    if name != "claude-bridge":
        problems.append(f"info.json name is {name!r}; it must stay 'claude-bridge' (save compatibility)")
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        problems.append(f"info.json version {version!r} is not x.y.z")
    if a.tag and a.tag.lstrip("v") != version:
        problems.append(f"tag {a.tag} does not match info.json version {version}")
    changelog = MOD / "changelog.txt"
    if changelog.exists():
        problems += check_changelog(changelog.read_text(encoding="utf-8"), version)
    if problems:
        print("\n".join("ERROR: " + p for p in problems), file=sys.stderr)
        return 1
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    zpath = out / f"{name}_{version}.zip"
    top = f"{name}_{version}"
    files = sorted(p for p in MOD.rglob("*") if p.is_file() and p.name not in SKIP_FILES
                   and not SKIP_DIRS.intersection(p.relative_to(MOD).parts))
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for p in files:
            rel = p.relative_to(MOD).as_posix()
            zi = zipfile.ZipInfo(f"{top}/{rel}", date_time=(2020, 1, 1, 0, 0, 0))  # reproducible
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = 0o644 << 16
            z.writestr(zi, p.read_bytes())
    digest = hashlib.sha256(zpath.read_bytes()).hexdigest()
    (out / "SHA256SUMS").write_text(f"{digest}  {zpath.name}\n", encoding="utf-8")
    print(f"{zpath} ({len(files)} files, sha256 {digest})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
