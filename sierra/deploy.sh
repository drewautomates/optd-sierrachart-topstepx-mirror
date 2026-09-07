#!/usr/bin/env bash
# Copy Manual_Mirror.cpp and tsx_manual_emit.h into a Sierra Chart ACS_Source
# folder. No default target on purpose - pass it, or set SC_ACS_SOURCE.
#
#   bash sierra/deploy.sh "/c/SierraChart/ACS_Source"
#
# ACS_Source sits inside the Sierra Chart install folder, next to Data/. After
# deploying, build inside Sierra Chart: Analysis > Build Custom Studies DLL.
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="${1:-${SC_ACS_SOURCE:-}}"

if [ -z "$TARGET" ]; then
    echo "ERROR: no target ACS_Source folder specified." >&2
    echo "  bash sierra/deploy.sh \"/path/to/SierraChart/ACS_Source\"   (or set SC_ACS_SOURCE)" >&2
    exit 1
fi
if [ ! -d "$TARGET" ]; then
    echo "ERROR: target ACS_Source not found: $TARGET" >&2
    exit 1
fi

for name in Manual_Mirror.cpp tsx_manual_emit.h; do
    cp "$SRC_DIR/$name" "$TARGET/$name"
    echo "  deployed    $name -> $TARGET"
done

echo
echo "Next: Sierra Chart -> Analysis > Build Custom Studies DLL > Build > Manual_Mirror"
echo "      Then on ONE chart -> Analysis > Studies > Add Custom Study -> TopstepX Manual Mirror."
