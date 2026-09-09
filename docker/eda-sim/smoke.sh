#!/usr/bin/env bash
# smoke.sh -- the eda-sim image's acceptance smoke test, run INSIDE the image.
#
# Baked to /opt/eda/smoke.sh, so it can be run against a published image with
# no checkout:
#
#   docker run --rm ghcr.io/2amlogic/eda-sim /opt/eda/smoke.sh
#   docker run --rm -v eda-pdk-cache:/home/loom/.ciel \
#       ghcr.io/2amlogic/eda-sim /opt/eda/smoke.sh --with-pdk gf180mcu
#
# Two tiers, because they cost three orders of magnitude apart:
#
#   default        every baked tool answers at its pinned version, the KLayout
#                  Python module imports headlessly, no PDK tree is baked (the
#                  hard rule), and ngspice actually SOLVES a circuit -- a real
#                  `.op` whose answer is checked numerically, not a `--version`
#                  banner. Seconds, no network.
#   --with-pdk <f> additionally fetches the pinned PDK with ciel at runtime and
#                  runs a real simulation against its device models (issue
#                  #509's "one real sim runs green inside the container").
#                  Minutes, ~1 GB of download.
#
# Usage: smoke.sh [--with-pdk <family>] [--keep-workdir]
# Exit codes: 0 all checks passed, 1 a check failed.

set -uo pipefail

WITH_PDK=""
KEEP_WORKDIR=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --with-pdk) WITH_PDK="$2"; shift 2 ;;
        --keep-workdir) KEEP_WORKDIR=true; shift ;;
        -h|--help) sed -n '2,25p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 1 ;;
    esac
done

FAILURES=0
WORKDIR="$(mktemp -d)"
if [[ "$KEEP_WORKDIR" != true ]]; then
    trap 'rm -rf "$WORKDIR"' EXIT
fi

pass() { printf '  ok    %s\n' "$*"; }
fail() { printf '  FAIL  %s\n' "$*" >&2; FAILURES=$((FAILURES + 1)); }
step() { printf '\n== %s\n' "$*"; }

# Pull the numeric right-hand side out of an ngspice `print` line, e.g.
#   v(out) = 5.000000e-01     ->  5.000000e-01
#   i(vdd) = -3.245e-03       -> -3.245e-03
# Deliberately tolerant of ngspice's spacing/label decoration: the assertion
# that matters is the number, not the banner formatting.
extract_value() {  # <ngspice-output> <label-substring>
    printf '%s\n' "$1" | grep -F "$2" | grep -F '=' | head -n 1 \
        | sed 's/.*=//' | awk '{print $1}'
}

# Exact installed version of a Python distribution -- more reliable than
# parsing a CLI's `--version` banner, whose formatting is not a contract.
dist_version() {  # <distribution-name>
    python3 -c "import importlib.metadata as m; print(m.version('$1'))" 2>/dev/null
}

MANIFEST="${EDA_SIM_MANIFEST:-/opt/eda/pdk-versions.json}"

# ---------------------------------------------------------------------------
step "baked manifest"
# ---------------------------------------------------------------------------
if [[ -f "$MANIFEST" ]] && jq -e . "$MANIFEST" >/dev/null 2>&1; then
    pass "manifest present and valid JSON: ${MANIFEST}"
else
    echo "FATAL: manifest missing or not valid JSON: ${MANIFEST}" >&2
    exit 1
fi

NGSPICE_MIN_MAJOR="$(jq -r '.tools.ngspice.min_major' "$MANIFEST")"
WANT_NGSPICE="$(jq -r '.tools.ngspice.version' "$MANIFEST")"
WANT_XSCHEM="$(jq -r '.tools.xschem.tag' "$MANIFEST")"
WANT_CIEL="$(jq -r '.tools.ciel.version' "$MANIFEST")"
WANT_KLT="$(jq -r '.tools.klayout_tools.version' "$MANIFEST")"

# ---------------------------------------------------------------------------
step "baked toolchain answers at its pinned version"
# ---------------------------------------------------------------------------
ngspice_out="$(ngspice --version 2>&1)"
ngspice_major="$(printf '%s\n' "$ngspice_out" | sed -n 's/.*ngspice-\([0-9][0-9]*\).*/\1/p' | head -n 1)"
if [[ -n "$ngspice_major" && "$ngspice_major" -ge "$NGSPICE_MIN_MAJOR" ]]; then
    pass "ngspice ${ngspice_major} (floor ${NGSPICE_MIN_MAJOR}, pinned ${WANT_NGSPICE})"
else
    fail "ngspice >= ${NGSPICE_MIN_MAJOR} required, got '${ngspice_major:-unparseable}': ${ngspice_out}"
fi

# xschem's exact pin is asserted at BUILD time (the Dockerfile fails if the
# tag does not resolve to the pinned commit), which is stronger than parsing
# this banner -- so all that is checked here is that the binary runs headlessly
# and says something.
xschem_out="$(xschem --version 2>&1 | head -n 3)"
if [[ -n "$xschem_out" ]]; then
    pass "xschem runs headlessly (pinned tag ${WANT_XSCHEM}): $(printf '%s' "$xschem_out" | head -n 1)"
else
    fail "xschem --version produced no output"
fi

got_ciel="$(dist_version ciel)"
if [[ "$got_ciel" == "$WANT_CIEL" ]] && ciel --version >/dev/null 2>&1; then
    pass "ciel ${got_ciel}"
else
    fail "ciel: pinned ${WANT_CIEL}, installed '${got_ciel:-none}' (or the CLI failed to run)"
fi

got_klt="$(dist_version klayout-tools)"
if [[ "$got_klt" == "$WANT_KLT" ]] && klt --version >/dev/null 2>&1; then
    pass "klayout-tools ${got_klt} (klt --version: $(klt --version 2>&1 | head -n 1))"
else
    fail "klayout-tools: pinned ${WANT_KLT}, installed '${got_klt:-none}' (or the CLI failed to run)"
fi

# `klt` is only useful here if its headless KLayout module actually loads --
# the whole point of baking klayout-tools rather than an apt KLayout (no GUI,
# no Qt). CLAUDE.md: "Headless always."
if python3 -c 'import klayout.db as db; db.Layout()' >/dev/null 2>&1; then
    pass "klayout.db imports and instantiates headlessly"
else
    fail "klayout.db failed to import/instantiate headlessly"
fi

# ---------------------------------------------------------------------------
step "hard rule: the PDK tree is NOT baked"
# ---------------------------------------------------------------------------
if [[ -n "${PDK_ROOT:-}" ]]; then
    fail "PDK_ROOT is set in the image (${PDK_ROOT}); ciel must install into ~/.ciel"
else
    pass "PDK_ROOT is unset (ciel installs into ~/.ciel/<variant>)"
fi

baked_variants="$(find /opt /usr /home -maxdepth 4 -type d \
    \( -name 'sky130?' -o -name 'gf180mcu?' \) 2>/dev/null | head -n 5)"
if [[ -z "$baked_variants" ]]; then
    pass "no PDK variant tree baked into the image"
else
    fail "a PDK variant tree is baked into the image: ${baked_variants}"
fi

plan="$(eda-sim-fetch-pdk gf180mcu --print-plan 2>/dev/null)"
if printf '%s' "$plan" | jq -e '.baked == false and (.open_pdks_commit | length) == 40' >/dev/null 2>&1; then
    pass "eda-sim-fetch-pdk resolves a pinned runtime fetch plan"
else
    fail "eda-sim-fetch-pdk --print-plan did not emit a usable plan: ${plan}"
fi

# ---------------------------------------------------------------------------
step "runtime-writable Python environment"
# ---------------------------------------------------------------------------
# Ubuntu 24.04 is PEP 668 externally-managed; the fleet's harnesses legitimately
# add scipy/numpy at runtime. That has to work as the non-root runtime user
# without --break-system-packages, which is why the venv is chowned to it.
if [[ -w "$(dirname "$(command -v pip)")" ]]; then
    pass "the on-PATH venv is writable by $(id -un) (runtime pip install works)"
else
    fail "the on-PATH venv is not writable by $(id -un); runtime pip install would need root"
fi

# ---------------------------------------------------------------------------
step "ngspice solves a real circuit (no PDK required)"
# ---------------------------------------------------------------------------
# A `--version` banner proves the binary links; this proves it simulates. A
# 1k/1k divider from a 1 V source must put exactly 0.5 V on the midpoint.
cat > "${WORKDIR}/divider.sp" <<'DECK'
* eda-sim smoke: resistive divider, v(out) must be 0.5
v1 in 0 dc 1
r1 in out 1k
r2 out 0 1k
.op
.control
run
print v(out)
.endc
.end
DECK

if divider_out="$(ngspice -b "${WORKDIR}/divider.sp" 2>&1)"; then
    vout="$(extract_value "$divider_out" 'v(out)')"
    if [[ -n "$vout" ]] && awk -v v="$vout" 'BEGIN { exit !(v > 0.4999 && v < 0.5001) }'; then
        pass "ngspice .op solved the divider: v(out) = ${vout}"
    else
        fail "ngspice .op gave v(out) = '${vout:-unparseable}', expected 0.5"
        printf '%s\n' "$divider_out" | sed 's/^/      /' >&2
    fi
else
    fail "ngspice -b exited non-zero on the divider deck"
    printf '%s\n' "$divider_out" | sed 's/^/      /' >&2
fi

# ---------------------------------------------------------------------------
if [[ -n "$WITH_PDK" ]]; then
    step "PDK fetched at runtime via ciel, then simulated (--with-pdk ${WITH_PDK})"
# ---------------------------------------------------------------------------
    if eda-sim-fetch-pdk "$WITH_PDK"; then
        pass "ciel fetched ${WITH_PDK} at the pinned commit"
    else
        fail "eda-sim-fetch-pdk ${WITH_PDK} failed"
    fi

    pdk_path="$(eda-sim-fetch-pdk "$WITH_PDK" --print-path)"
    if [[ -d "$pdk_path" ]]; then
        pass "PDK variant tree present at ${pdk_path}"
    else
        fail "PDK variant tree missing at ${pdk_path}"
    fi

    measure_label=""
    case "$WITH_PDK" in
        gf180mcu)
            # The same deck shape 2AMLogic/gf180-trng's `corner-sanity-nfet-id`
            # testbench uses: design.ngspice first (global switches), then the
            # typical-corner sections of sm141064.ngspice, then a saturated
            # nfet_03v3 whose drain current must come out measurably non-zero.
            cat > "${WORKDIR}/pdk.sp" <<DECK
* eda-sim PDK smoke: gf180mcu nfet_03v3 Id(sat)
.include "${pdk_path}/libs.tech/ngspice/design.ngspice"
.lib "${pdk_path}/libs.tech/ngspice/sm141064.ngspice" typical
.temp 27
vdd vdd 0 dc 3.3
vg  g   0 dc 3.3
xn  d g 0 0 nfet_03v3 w=10u l=0.28u
rload d vdd 1k
.op
.control
run
print i(vdd)
.endc
.end
DECK
            measure_label='i(vdd)'
            ;;
        sky130)
            # sky130's combined library is one .lib file with corner sections
            # (the `ngspice_lib` path 2AMLogic/sky130-bandgap's sim/pdk.json
            # names). Same shape as the gf180mcu deck above: a saturated
            # nfet_01v8 whose drain current must come out measurably non-zero.
            cat > "${WORKDIR}/pdk.sp" <<DECK
* eda-sim PDK smoke: sky130 nfet_01v8 Id(sat)
.lib "${pdk_path}/libs.tech/combined/sky130.lib.spice" tt
.temp 27
vdd vdd 0 dc 1.8
vg  g   0 dc 1.8
xn  d g 0 0 sky130_fd_pr__nfet_01v8 w=10 l=0.15
rload d vdd 1k
.op
.control
run
print i(vdd)
.endc
.end
DECK
            measure_label='i(vdd)'
            ;;
        *)
            fail "no PDK smoke deck defined for family '${WITH_PDK}'"
            ;;
    esac

    if [[ -n "$measure_label" ]]; then
        if pdk_out="$(ngspice -b "${WORKDIR}/pdk.sp" 2>&1)"; then
            value="$(extract_value "$pdk_out" "$measure_label")"
            # Magnitude, not sign: ngspice reports source current with the
            # passive-sign convention, so Id shows up negative through vdd.
            if [[ -n "$value" ]] && awk -v v="$value" 'BEGIN { v = (v < 0 ? -v : v); exit !(v > 1e-12) }'; then
                pass "PDK-backed ngspice run solved: ${measure_label} = ${value}"
            else
                fail "PDK-backed ngspice run gave ${measure_label} = '${value:-unparseable}', expected a non-zero magnitude"
                printf '%s\n' "$pdk_out" | tail -n 40 | sed 's/^/      /' >&2
            fi
        else
            fail "PDK-backed ngspice run exited non-zero"
            printf '%s\n' "$pdk_out" | tail -n 40 | sed 's/^/      /' >&2
        fi
    fi
fi

printf '\n'
if [[ "$FAILURES" -eq 0 ]]; then
    echo "eda-sim smoke: all checks passed"
    exit 0
fi
echo "eda-sim smoke: ${FAILURES} check(s) failed" >&2
exit 1
