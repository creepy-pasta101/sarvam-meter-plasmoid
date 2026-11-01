#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
#
# Build a distributable .plasmoid package from the plasmoid source tree.
#
#   ./scripts/build-plasmoid.sh [OUTPUT_DIR]
#
# Output: <OUTPUT_DIR>/sarvam-meter-<version>.plasmoid (default OUTPUT_DIR is
# the repository root). The archive is a plain zip whose *root* is the KPackage
# itself, i.e. metadata.json sits at the top level.
#
# Writes only inside the repository (or the directory you pass in) and a temp
# directory. No root, no network.

set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PLASMOID_ID="io.github.creepy_pasta101.sarvammeter"
PLASMOID_DIR="${REPO_ROOT}/plasmoid/${PLASMOID_ID}"
OUT_DIR="${1:-${REPO_ROOT}}"

log() { printf '[build-plasmoid] %s\n' "$*"; }
die() { printf '[build-plasmoid] ERROR: %s\n' "$*" >&2; exit 1; }

command -v python3 >/dev/null 2>&1 || die "python3 is required but was not found in PATH"
[ -f "${PLASMOID_DIR}/metadata.json" ] || die "no metadata.json in ${PLASMOID_DIR}"

mkdir -p "${OUT_DIR}"
OUT_DIR="$(cd -- "${OUT_DIR}" && pwd)"

# --- validate metadata.json and read the version out of it -----------------
version="$(python3 - "${PLASMOID_DIR}/metadata.json" <<'PY'
import json
import os
import sys

path = sys.argv[1]
try:
    with open(path, encoding="utf-8") as handle:
        doc = json.load(handle)
except (OSError, ValueError) as exc:
    sys.exit("%s: not valid JSON: %s" % (path, exc))

if not isinstance(doc, dict):
    sys.exit("%s: top level must be a JSON object" % path)

kplugin = doc.get("KPlugin")
if not isinstance(kplugin, dict):
    sys.exit("%s: missing the KPlugin object" % path)

for key in ("Id", "Name", "Version"):
    if not isinstance(kplugin.get(key), str) or not kplugin[key].strip():
        sys.exit("%s: KPlugin.%s is missing or empty" % (path, key))

if kplugin["Id"] != "io.github.creepy_pasta101.sarvammeter":
    sys.exit("%s: unexpected KPlugin.Id %r" % (path, kplugin["Id"]))

if doc.get("KPackageStructure") != "Plasma/Applet":
    sys.exit("%s: KPackageStructure must be 'Plasma/Applet'" % path)

package_dir = os.path.dirname(os.path.abspath(path))
for relative in ("contents/config/main.xml", "contents/ui/main.qml"):
    if not os.path.isfile(os.path.join(package_dir, relative)):
        sys.exit("%s: missing %s" % (path, relative))

print(kplugin["Version"])
PY
)"

[ -n "${version}" ] || die "could not determine the plasmoid version from metadata.json"
out="${OUT_DIR}/sarvam-meter-${version}.plasmoid"
rm -f "${out}"

# --- zip the package (root of the archive == the package itself) -----------
if command -v zip >/dev/null 2>&1; then
    ( cd "${PLASMOID_DIR}" && zip -q -r -X "${out}" . )
else
    ( cd "${PLASMOID_DIR}" && python3 -m zipfile -c "${out}" . )
fi

# --- sanity-check the archive ----------------------------------------------
[ -s "${out}" ] || die "the archive ${out} is empty"
python3 -m zipfile -l "${out}" | grep -q 'metadata\.json' \
    || die "${out} does not contain metadata.json at its root"

log "wrote ${out}"
python3 -m zipfile -l "${out}"
