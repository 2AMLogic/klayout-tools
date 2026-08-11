#!/usr/bin/env bash
# Shared fetch/checksum/build boilerplate for scripts/install-*.sh (Icarus
# Verilog, Verilator, Yosys) -- issue #687.
#
# Diffing the three CI toolchain installers (added for Epic #391, issues
# #417/#423) found the "pin a release, fetch it, verify its checksum,
# build, install idempotently" recipe byte-identical or structurally
# identical across all three, differing only in per-tool variable names.
# This file centralizes that recipe so each install-*.sh keeps only its
# pinned version constants and its actual build step (configure/make,
# autoconf+configure/make, or cmake).
#
# Meant to be sourced, never executed directly:
#   source "$(dirname "${BASH_SOURCE[0]}")/_install_common.sh"
#
# Each helper below takes its inputs as explicit arguments -- sourcing this
# file alone does not require any variable to already be set, except for
# `nproc_val`, which is computed as a plain variable at source time (same
# as it was inlined in each script before this split).

set -euo pipefail

# sha256_of <file>
# Print the SHA-256 hex digest of <file>, preferring `shasum` (macOS
# default) and falling back to `sha256sum` (Linux/CI default).
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

# check_marker <marker-file> <tag> <force> <tool-name>
# Idempotency check shared by all three installers: if <marker-file>
# exists, <force> is 0, and it already records <tag>, print the "already
# installed" message and exit the calling script with status 0. Otherwise
# returns normally so the caller proceeds with a fresh build.
check_marker() {
    local marker="$1" tag="$2" force="$3" tool_name="$4"
    if [[ -f "$marker" && "$force" -eq 0 ]]; then
        local have
        have="$(cat "$marker")"
        if [[ "$have" == "$tag" ]]; then
            echo "$tool_name $tag already installed at $(dirname "$marker") (use --force to rebuild)"
            exit 0
        fi
    fi
}

# fetch_and_verify <url> <expected-sha256> <dest-file>
# Download <url> to <dest-file> with retry, then verify its SHA-256 matches
# <expected-sha256>. Fails closed (non-zero exit, matching wording) on
# fetch failure or checksum mismatch.
fetch_and_verify() {
    local url="$1" expected_sha256="$2" dest="$3"
    echo "Fetching $url ..."
    if ! curl -fsL --retry 3 -o "$dest" "$url"; then
        echo "error: failed to fetch $url" >&2
        exit 1
    fi

    local actual_sha256
    actual_sha256="$(sha256_of "$dest")"
    if [[ "$actual_sha256" != "$expected_sha256" ]]; then
        echo "error: checksum mismatch for $url" >&2
        echo "  expected: $expected_sha256" >&2
        echo "  actual:   $actual_sha256" >&2
        exit 1
    fi
}

# finish_install <marker-file> <tag> <tool-name> <prefix>
# Record the installed tag in <marker-file> and print the standard
# "Installed .../ Add to PATH" summary shared by all three scripts.
finish_install() {
    local marker="$1" tag="$2" tool_name="$3" prefix="$4"
    echo "$tag" >"$marker"
    echo "Installed $tool_name $tag into $prefix"
    echo "  Add to PATH: $prefix/bin"
}

# nproc_val: parallelism to pass to `make -j`/`cmake --build --parallel`.
# Prefers GNU `nproc` (Linux/CI), falls back to `sysctl -n hw.ncpu` (macOS),
# then 2 if neither is available. Computed once at source time so every
# caller sees the same value without re-detecting it.
# shellcheck disable=SC2034  # used by every script that sources this file
nproc_val="$( (command -v nproc &>/dev/null && nproc) || sysctl -n hw.ncpu 2>/dev/null || echo 2)"
