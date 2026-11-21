#!/bin/bash
# Builds the plugin package for OpenDeck ("Install from file") / Stream Deck:
#   dist/io.github.gawson.warpipe.streamDeckPlugin
# = a zip with the io.github.gawson.warpipe.sdPlugin/ directory at the top.
# Python standard library + system PySide6 (for the icons) only.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
NAME=io.github.gawson.warpipe
SRC="$HERE/$NAME.sdPlugin"
DIST="$HERE/dist"
OUT="$DIST/$NAME.streamDeckPlugin"

python3 -I "$HERE/make_icons.py"
mkdir -p "$DIST"
rm -f "$OUT"

python3 -I - "$SRC" "$OUT" <<'PY'
import json, os, stat, sys, zipfile

src, out = sys.argv[1], sys.argv[2]
top = os.path.basename(src)
with open(os.path.join(src, "manifest.json"), encoding="utf-8") as f:
    manifest = json.load(f)                       # bad JSON = build error
code = manifest["CodePathLin"]
for k in ("Name", "Author", "Version", "Icon", "Actions", "OS"):
    assert k in manifest, f"manifest: missing {k}"
assert {"Platform": "linux"} in manifest["OS"], "manifest: missing OS linux"
for a in manifest["Actions"]:
    for k in ("UUID", "Name", "States"):
        assert k in a, f"action without {k}"
    pi = a.get("PropertyInspectorPath")
    assert not pi or os.path.isfile(os.path.join(src, pi)), f"missing {pi}"
    for icon in [a.get("Icon")] + [s.get("Image") for s in a["States"]]:
        assert os.path.isfile(os.path.join(src, icon + ".png")), f"missing icon {icon}.png"
assert os.access(os.path.join(src, code), os.X_OK), f"{code} is not executable"

def skip(name):
    return name == "__pycache__" or name.endswith((".pyc", ".pyo", "~")) or name.startswith(".")

files = 0
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for root, dirs, names in os.walk(src):
        dirs[:] = sorted(d for d in dirs if not skip(d))
        rel_root = os.path.relpath(root, os.path.dirname(src))
        zi = zipfile.ZipInfo(rel_root.rstrip("/") + "/")
        zi.external_attr = (stat.S_IFDIR | 0o755) << 16
        z.writestr(zi, b"")
        for n in sorted(names):
            if skip(n):
                continue
            path = os.path.join(root, n)
            arc = os.path.join(rel_root, n)
            mode = 0o755 if os.access(path, os.X_OK) else 0o644
            zi = zipfile.ZipInfo.from_file(path, arc)
            zi.external_attr = (stat.S_IFREG | mode) << 16
            zi.compress_type = zipfile.ZIP_DEFLATED
            with open(path, "rb") as f:
                z.writestr(zi, f.read())
            files += 1
with zipfile.ZipFile(out) as z:
    names = z.namelist()
    assert all(n.startswith(top + "/") for n in names), "package contains something outside the .sdPlugin directory"
    assert f"{top}/manifest.json" in names
    exe = z.getinfo(f"{top}/{code}")
    assert (exe.external_attr >> 16) & 0o111, "plugin executable lost its exec bit in the package"
print(f"{out}: {files} files, version {manifest['Version']}")
PY
