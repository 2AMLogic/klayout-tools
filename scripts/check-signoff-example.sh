#!/usr/bin/env bash
# Fail if `examples/signoff/` is not what its own generator produces (issue #2028).
#
# Usage: scripts/check-signoff-example.sh
#
# Why this exists
# ---------------
# `examples/signoff/drc.json` and `lvs.json` are real `klt drc`/`klt lvs
# --format json` envelopes -- the evidence `examples/signoff/manifest.json`
# cites, and the copy-paste starting point a block author adapts. Nothing
# regenerates them when the verbs that produce them change, so between
# 2026-08 and 2026-09 they silently drifted a long way from reality (issue
# #2028): `drc.json` was still on the pre-#2108 `coverage` schema (a flat
# `rules_skipped` list, `schema_version: 1`), `lvs.json` still recorded
# `provenance.input: null` -- a claim `klt lvs` stopped making in #1969 --
# and both README.md and the generator's own docstring documented that stale
# shape as current behaviour.
#
# A worked example that contradicts the tool is worse than no example, so
# this check regenerates the directory and fails on any diff.
#
# What a failure means
# --------------------
# Not "the generator is broken". It means a `klt drc`/`klt lvs` change moved
# the committed evidence, which is legitimate and expected -- regenerate and
# commit the result *in the same PR* as that change:
#
#     uv run python3 examples/signoff/generate.py
#     git add examples/signoff
#
# Then re-read `examples/signoff/README.md` and the generator's module
# docstring: if the diff changed something either of them describes in prose
# (the coverage rollup's shape, what `provenance.input` carries, the sample
# report output), update that prose too. The whole point of #2028 is that a
# regenerated fixture with stale prose beside it is still a broken example.
#
# Determinism
# -----------
# `generate.py` writes `block.gds` with GDS timestamps suppressed and nulls
# the two host-identity `provenance` fields (see its
# `_NORMALIZED_PROVENANCE_PATHS`), so regeneration is byte-identical from any
# checkout for a given `klt`/KLayout build -- there is no "regenerated on a
# different machine" flake for this check to absorb. A `uv.lock` KLayout bump
# does legitimately move `provenance.klayout_version`; that is a real diff
# belonging to the bumping PR, and this check correctly demands it.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

EXAMPLE_DIR="examples/signoff"

# Refuse to run against an already-dirty example directory: the `git diff`
# below could not tell the operator's own uncommitted edits apart from real
# drift, so it would either cry wolf or (worse) get explained away as "that
# was just my edit". Nothing is written before this gate.
if ! git diff --quiet -- "$EXAMPLE_DIR" || ! git diff --cached --quiet -- "$EXAMPLE_DIR"; then
  echo "error: $EXAMPLE_DIR has uncommitted changes -- commit or stash them first," >&2
  echo "       otherwise this check cannot distinguish them from real drift." >&2
  exit 2
fi

echo "regenerating $EXAMPLE_DIR ..."
uv run python3 "$EXAMPLE_DIR/generate.py"

if git diff --exit-code -- "$EXAMPLE_DIR"; then
  echo "OK: $EXAMPLE_DIR round-trips -- the committed fixtures are what the verbs emit."
  exit 0
fi

cat >&2 <<EOF

error: $EXAMPLE_DIR drifted from what \`klt drc\`/\`klt lvs\` emit today.

The diff above is what regeneration produced. Commit it (and update
$EXAMPLE_DIR/README.md plus generate.py's module docstring if the diff
changed anything they describe in prose):

    uv run python3 $EXAMPLE_DIR/generate.py
    git add $EXAMPLE_DIR

See this script's header comment, and issue #2028, for why.
EOF
exit 1
