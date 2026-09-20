#!/usr/bin/env bash
# Generate the magic technology decks the cross-validation oracle runs
# against -- issue #2014 (sub-issue of #2007).
#
# `tests/test_drc_magic_oracle.py` / `tests/test_extract_magic_oracle.py`
# cross-check `klt drc` / `klt extract` against magic. magic needs a
# technology file per PDK, and the open-PDK ones are *generated* by
# open_pdks from a single source `.tech` per process: the installed
# `libs.tech/magic/sky130A.tech` is `sky130/magic/sky130.tech` run through
# open_pdks' own `common/preproc.py` with the variant's `-D` flags (see
# open_pdks' `sky130/Makefile.in`, `SKY130A_DEFS`).
#
# That is the whole dependency -- three small text files, ~350 KB total --
# so this script reproduces exactly that step from a pinned, checksummed
# open_pdks commit instead of requiring a multi-GB open_pdks/volare PDK
# install just to obtain a deck. (An installed PDK works too: the oracle
# resolves `libs.tech/magic/<variant>.tech` through `klt pdk find` first;
# see `tests/helpers/magic_oracle.py`.) Nothing is vendored into the repo:
# the generated decks land in gitignored `pdks/magic-tech/`, matching
# `pdks/README.md`'s "fetched, never committed" rule.
#
# The defines below are copied verbatim from open_pdks' own Makefiles at
# the pinned commit and must be kept in step with them when the pin moves:
#   sky130/Makefile.in     SKY130A_DEFS   = -DTECHNAME=sky130A  -DMETAL5 -DMIM -DREDISTRIBUTION
#   gf180mcu/Makefile.in   GF180MCUC_DEFS = -DTECHNAME=gf180mcuC -DMETALS5 -DMIM -DTHICKMET0P9 -DHRPOLY1K
# `sky130A` and `gf180mcuC` are the variants this repo's own decks target
# (`klt drc --deck sky130` / `--deck gf180mcu`; see
# `docs/design/magic-oracle.md` for the matched-scope table).
#
# Usage: scripts/fetch-magic-tech.sh [--force]
#   Writes pdks/magic-tech/{sky130A,gf180mcuC}.tech. Idempotent: a prior
#   fetch of the same pinned open_pdks commit is left in place unless
#   --force is given (matches fetch-pdks.sh's own convention).

set -euo pipefail

# shellcheck source=scripts/_install_common.sh
source "$(dirname "${BASH_SOURCE[0]}")/_install_common.sh"

# Pinned open_pdks source -- bump the tag, the commit, and all three
# checksums together in the same change. Fails closed on mismatch.
OPEN_PDKS_TAG="1.0.608"
OPEN_PDKS_COMMIT="1689ac3f2dc763876eaf967227c7dfe831b031ae"
RAW_BASE="https://raw.githubusercontent.com/fossi-foundation/open-pdks/${OPEN_PDKS_COMMIT}"

# Computed once per pinned commit with:
#   curl -fsL "$RAW_BASE/<path>" | shasum -a 256
PREPROC_SHA256="505a4fcbdbf2fef8935ac6811be78f6bf66c63b647adcebfbdb34ef2fd230784"
SKY130_TECH_SHA256="5f96a22bd00169807b2228742e17a27e3624e5fb010c523447bbc4fee30a4f97"
GF180MCU_TECH_SHA256="9ab630331b8711987fa70e8c1db7c521cede2e37c35bc2b289fa7b6836abd525"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="${MAGIC_TECH_DIR:-$REPO_ROOT/pdks/magic-tech}"
MARKER="$DEST/.fetched-version"

FORCE=0
[[ "${1:-}" == "--force" ]] && FORCE=1

check_marker "$MARKER" "$OPEN_PDKS_COMMIT" "$FORCE" "open_pdks magic decks"

if ! command -v python3 &>/dev/null; then
    echo "error: python3 is required (open_pdks' preproc.py is a python3 script)" >&2
    exit 1
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

fetch_and_verify "$RAW_BASE/common/preproc.py" "$PREPROC_SHA256" "$tmp/preproc.py"
fetch_and_verify "$RAW_BASE/sky130/magic/sky130.tech" "$SKY130_TECH_SHA256" "$tmp/sky130.tech"
fetch_and_verify "$RAW_BASE/gf180mcu/magic/gf180mcu.tech" "$GF180MCU_TECH_SHA256" "$tmp/gf180mcu.tech"

mkdir -p "$DEST"

echo "Generating sky130A.tech ..."
python3 "$tmp/preproc.py" "$tmp/sky130.tech" "$DEST/sky130A.tech" \
    -DTECHNAME=sky130A \
    -DREVISION="$OPEN_PDKS_TAG" \
    -DMETAL5 -DMIM -DREDISTRIBUTION \
    -DMAGIC_CURRENT=libs.tech/magic \
    -DSTAGING_PATH="$DEST"

echo "Generating gf180mcuC.tech ..."
python3 "$tmp/preproc.py" "$tmp/gf180mcu.tech" "$DEST/gf180mcuC.tech" \
    -DTECHNAME=gf180mcuC \
    -DREVISION="$OPEN_PDKS_TAG" \
    -DMETALS5 -DMIM -DTHICKMET0P9 -DHRPOLY1K \
    -DMAGIC_CURRENT=libs.tech/magic \
    -DSTAGING_PATH="$DEST"

for deck in sky130A gf180mcuC; do
    if [[ ! -s "$DEST/$deck.tech" ]]; then
        echo "error: $DEST/$deck.tech was not generated" >&2
        exit 1
    fi
done

echo "$OPEN_PDKS_COMMIT" >"$MARKER"
echo "Generated magic decks from open_pdks $OPEN_PDKS_TAG ($OPEN_PDKS_COMMIT):"
echo "  $DEST/sky130A.tech"
echo "  $DEST/gf180mcuC.tech"
