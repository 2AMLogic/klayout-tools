#!/usr/bin/env bash
# Fetch real, checksum-verified standard-cell SPICE netlists + primitive
# device models into pdks/cell-netlists/ (gitignored -- see pdks/README.md).
#
# This is the "real ngspice cell netlists" leg of the gallery signals
# pipeline (issue #99): unlike scripts/fetch-pdks.sh, which pins one whole
# release tarball (lambdapdk), this script pins individual files at an exact
# upstream commit SHA -- the full open-PDK primitive-device repos are
# 100+ MB (mostly corners/devices the 7 gallery cells never instantiate),
# so only the files those 7 cells actually need are fetched and verified.
#
# Sources (all Apache-2.0):
#   - google/skywater-pdk-libs-sky130_fd_sc_hd  -- sky130 standard-cell
#     transistor-level netlists (.spice)
#   - google/skywater-pdk-libs-sky130_fd_pr     -- sky130 nfet_01v8 /
#     pfet_01v8_hvt primitive device models (corner .param overrides +
#     binned .model cards), the only two primitives the 4 sky130 gallery
#     cells instantiate
#   - google/globalfoundries-pdk-libs-gf180mcu_fd_sc_mcu9t5v0 -- gf180mcu
#     standard-cell transistor-level netlists (.cdl, SPICE-compatible)
#   - google/globalfoundries-pdk-libs-gf180mcu_fd_pr -- gf180mcu primitive
#     device models (single-file ngspice deck)
#
# See scripts/gallery_signals.py's module docstring for how these are
# assembled into `klt sim` requests, including the documented gf180mcu
# nfet_05v0/pfet_05v0 -> nmos_6p0/pmos_6p0 device-family substitution (the
# mcu9t5v0 5V standard-cell library's transistor references have no
# matching bin in the currently published gf180mcu_fd_pr primitive repo;
# see that module's docstring for the full rationale and prior art).
#
# Usage: scripts/fetch-cell-netlists.sh [--force]

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST_ROOT="$REPO_ROOT/pdks/cell-netlists"
MARKER="$DEST_ROOT/.fetched-manifest-version"
MANIFEST_VERSION="1"

FORCE=0
[[ "${1:-}" == "--force" ]] && FORCE=1

if [[ -f "$MARKER" && $FORCE -eq 0 ]]; then
    have="$(cat "$MARKER")"
    if [[ "$have" == "$MANIFEST_VERSION" ]]; then
        echo "cell netlists already present at $DEST_ROOT (manifest v$MANIFEST_VERSION; use --force to refetch)"
        exit 0
    fi
fi

sha256_of() {
    if command -v shasum &>/dev/null; then
        shasum -a 256 "$1" | awk '{print $1}'
    elif command -v sha256sum &>/dev/null; then
        sha256sum "$1" | awk '{print $1}'
    else
        echo "error: no sha256 command found (need shasum or sha256sum)" >&2
        return 1
    fi
}

# Pinned commit SHAs -- bump alongside the manifest below (URL + sha256) in
# the same change when refreshing; the fetch fails closed on any mismatch.
SKY130_SC_HD_SHA="ac7fb61f06e6470b94e8afdf7c25268f62fbd7b1"
SKY130_FD_PR_SHA="f62031a1be9aefe902d6d54cddd6f59b57627436"
GF180_SC_SHA="3781fd2951da6e3dc4600e52d997398a9463caeb"
GF180_FD_PR_SHA="9f992d5a9186d1f7820c58f039c484ad35b2edea"

SKY130_SC_HD_BASE="https://raw.githubusercontent.com/google/skywater-pdk-libs-sky130_fd_sc_hd/${SKY130_SC_HD_SHA}"
SKY130_FD_PR_BASE="https://raw.githubusercontent.com/google/skywater-pdk-libs-sky130_fd_pr/${SKY130_FD_PR_SHA}"
GF180_SC_BASE="https://raw.githubusercontent.com/google/globalfoundries-pdk-libs-gf180mcu_fd_sc_mcu9t5v0/${GF180_SC_SHA}"
GF180_FD_PR_BASE="https://raw.githubusercontent.com/google/globalfoundries-pdk-libs-gf180mcu_fd_pr/${GF180_FD_PR_SHA}"

# Manifest: "<dest relpath>|<sha256>|<url>". One row per vendored file.
MANIFEST=(
    # -- sky130 standard-cell netlists --
    "sky130/cells/sky130_fd_sc_hd__inv_1.spice|1099e3641c46d241852db47974540b3ad161e20e01b74f2fd5cc152627011909|${SKY130_SC_HD_BASE}/cells/inv/sky130_fd_sc_hd__inv_1.spice"
    "sky130/cells/sky130_fd_sc_hd__nand2_2.spice|90624697a8d4d7f5864eb3b1bdb776a791b3c745d961beb95e166819bce1490e|${SKY130_SC_HD_BASE}/cells/nand2/sky130_fd_sc_hd__nand2_2.spice"
    "sky130/cells/sky130_fd_sc_hd__buf_4.spice|ea462df085563296d3ee44d60cc9bbb257892a0506c7cb86c13533d20c40d682|${SKY130_SC_HD_BASE}/cells/buf/sky130_fd_sc_hd__buf_4.spice"
    "sky130/cells/sky130_fd_sc_hd__dfxtp_2.spice|5ea5d93ac94789ce478690a06ce3f3462cc4cb81003c050cb6b96e549db13aef|${SKY130_SC_HD_BASE}/cells/dfxtp/sky130_fd_sc_hd__dfxtp_2.spice"
    # -- sky130 primitive device models: nfet_01v8 --
    "sky130/models/sky130_fd_pr__nfet_01v8__mismatch.corner.spice|24a3b29d7d7f26f99098811c31eb5497950a3aed520ab83768a96f35467fe818|${SKY130_FD_PR_BASE}/cells/nfet_01v8/sky130_fd_pr__nfet_01v8__mismatch.corner.spice"
    "sky130/models/sky130_fd_pr__nfet_01v8__tt.corner.spice|0c03b2e5727c414942f1d2b27e19a68724fb84be90b2c73b361ad41779311130|${SKY130_FD_PR_BASE}/cells/nfet_01v8/sky130_fd_pr__nfet_01v8__tt.corner.spice"
    "sky130/models/sky130_fd_pr__nfet_01v8__tt.pm3.spice|3d706d50cf2e57175aea78bcf27e4ee07d9f750b154a4b06e6090531364a831b|${SKY130_FD_PR_BASE}/cells/nfet_01v8/sky130_fd_pr__nfet_01v8__tt.pm3.spice"
    "sky130/models/sky130_fd_pr__nfet_01v8__ss.corner.spice|896cd2d75e86826556819d92048c99a14bab6b9e4ae9d9295519a1bd64a3210f|${SKY130_FD_PR_BASE}/cells/nfet_01v8/sky130_fd_pr__nfet_01v8__ss.corner.spice"
    "sky130/models/sky130_fd_pr__nfet_01v8__ss.pm3.spice|5280e1a07ed54547a4393008dccc4c3c1640920a5741f507ad7db9467b809114|${SKY130_FD_PR_BASE}/cells/nfet_01v8/sky130_fd_pr__nfet_01v8__ss.pm3.spice"
    "sky130/models/sky130_fd_pr__nfet_01v8__ff.corner.spice|440a5b78e0aa92f9cae53a7fc750493211c9e08dbdf0169de8396de0a53e7c55|${SKY130_FD_PR_BASE}/cells/nfet_01v8/sky130_fd_pr__nfet_01v8__ff.corner.spice"
    "sky130/models/sky130_fd_pr__nfet_01v8__ff.pm3.spice|6b7859f53968eb8bd82a8febb6ab252bb98c550b6f2c3f743db9d9728c304405|${SKY130_FD_PR_BASE}/cells/nfet_01v8/sky130_fd_pr__nfet_01v8__ff.pm3.spice"
    # -- sky130 primitive device models: pfet_01v8_hvt --
    "sky130/models/sky130_fd_pr__pfet_01v8_hvt__mismatch.corner.spice|05e258ab6724c8276cc058f6dba732e8024c5f0bb79ff3131ca219143491d631|${SKY130_FD_PR_BASE}/cells/pfet_01v8_hvt/sky130_fd_pr__pfet_01v8_hvt__mismatch.corner.spice"
    "sky130/models/sky130_fd_pr__pfet_01v8_hvt__tt.corner.spice|69401f9b657f9ebb20d201d39e3fbf36b68628307d6fa982168fa51db3e77b03|${SKY130_FD_PR_BASE}/cells/pfet_01v8_hvt/sky130_fd_pr__pfet_01v8_hvt__tt.corner.spice"
    "sky130/models/sky130_fd_pr__pfet_01v8_hvt__tt.pm3.spice|f575eef5caea7f9638fa7e1b0078aedcf6d8513f5499b5bb79399186557881f2|${SKY130_FD_PR_BASE}/cells/pfet_01v8_hvt/sky130_fd_pr__pfet_01v8_hvt__tt.pm3.spice"
    "sky130/models/sky130_fd_pr__pfet_01v8_hvt__ss.corner.spice|514f2133d1a022a7956374c69bb66fe549ab5977669ae284159916d75e1324dc|${SKY130_FD_PR_BASE}/cells/pfet_01v8_hvt/sky130_fd_pr__pfet_01v8_hvt__ss.corner.spice"
    "sky130/models/sky130_fd_pr__pfet_01v8_hvt__ss.pm3.spice|5954e8e5e14dee0153f39fb2c9ae74aa3a69621ec8098e18266b2fa049e4841c|${SKY130_FD_PR_BASE}/cells/pfet_01v8_hvt/sky130_fd_pr__pfet_01v8_hvt__ss.pm3.spice"
    "sky130/models/sky130_fd_pr__pfet_01v8_hvt__ff.corner.spice|209003cb03ec551cd1dfe51259a1e795fef8d6ef1bde697f74d62db28c51ae79|${SKY130_FD_PR_BASE}/cells/pfet_01v8_hvt/sky130_fd_pr__pfet_01v8_hvt__ff.corner.spice"
    "sky130/models/sky130_fd_pr__pfet_01v8_hvt__ff.pm3.spice|6bb3e467fd1ea6eebe0831a8844b381e507165a6c7bab4affb4fb5b87e39e9d0|${SKY130_FD_PR_BASE}/cells/pfet_01v8_hvt/sky130_fd_pr__pfet_01v8_hvt__ff.pm3.spice"
    # -- gf180mcu standard-cell netlists (CDL, SPICE-compatible) --
    "gf180mcu/cells/gf180mcu_fd_sc_mcu9t5v0__and2_1.cdl|c4a48feea59b4fa4b00f62b93a11dbd7631895505ccf953510cd9edc12436d25|${GF180_SC_BASE}/cells/and2/gf180mcu_fd_sc_mcu9t5v0__and2_1.cdl"
    "gf180mcu/cells/gf180mcu_fd_sc_mcu9t5v0__dffnq_1.cdl|bff973c552b4ba2e365c3392b7fcb98ce6830ed00780ec9ed7b6e977fd6259c9|${GF180_SC_BASE}/cells/dffnq/gf180mcu_fd_sc_mcu9t5v0__dffnq_1.cdl"
    "gf180mcu/cells/gf180mcu_fd_sc_mcu9t5v0__clkinv_1.cdl|1e798a8f0c4f58ceb9e1d386f0dc0a3b931579fa81d1836a6ed3b36ef5d7745a|${GF180_SC_BASE}/cells/clkinv/gf180mcu_fd_sc_mcu9t5v0__clkinv_1.cdl"
    # -- gf180mcu primitive device models --
    "gf180mcu/models/design.ngspice|8d9721a5bf8f079d3fddbd03339af9a0c84d4feb06db8e06465fbd02c7500508|${GF180_FD_PR_BASE}/models/ngspice/design.ngspice"
    "gf180mcu/models/sm141064.ngspice|73fc67d38747d95ce03f3c2ba5f0a25c98f56a293363a9df4c971a3a28a3dcda|${GF180_FD_PR_BASE}/models/ngspice/sm141064.ngspice"
)

echo "Fetching ${#MANIFEST[@]} pinned cell-netlist/model files into $DEST_ROOT ..."

fail=0
for row in "${MANIFEST[@]}"; do
    relpath="${row%%|*}"
    rest="${row#*|}"
    expected_sha256="${rest%%|*}"
    url="${rest#*|}"

    dest="$DEST_ROOT/$relpath"
    mkdir -p "$(dirname "$dest")"

    tmp="$(mktemp)"
    if ! curl -fsL --retry 3 -o "$tmp" "$url"; then
        echo "error: failed to fetch $url" >&2
        rm -f "$tmp"
        fail=1
        continue
    fi

    actual_sha256="$(sha256_of "$tmp")"
    if [[ "$actual_sha256" != "$expected_sha256" ]]; then
        echo "error: checksum mismatch for $relpath" >&2
        echo "  url:      $url" >&2
        echo "  expected: $expected_sha256" >&2
        echo "  actual:   $actual_sha256" >&2
        rm -f "$tmp"
        fail=1
        continue
    fi

    mv "$tmp" "$dest"
    echo "  ok: $relpath"
done

if [[ $fail -ne 0 ]]; then
    echo "error: one or more files failed to fetch/verify -- see above" >&2
    exit 1
fi

echo "$MANIFEST_VERSION" > "$MARKER"
echo "Done: $DEST_ROOT"
du -sh "$DEST_ROOT"
