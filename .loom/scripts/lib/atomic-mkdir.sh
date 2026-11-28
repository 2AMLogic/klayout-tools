#!/usr/bin/env bash
# atomic-mkdir.sh — atomic single-directory creation for `mkdir`-based locks
# (#15287, extracted from filing-lock.sh's resolver added in #15265).
#
# Source this file (do not exec). It defines:
#
#   loom_atomic_mkdir <path>
#       Non-recursive mkdir that is a real mutual-exclusion primitive: of N
#       concurrent callers on the same path exactly one returns 0; the rest
#       return non-zero. Stderr is suppressed (a lost race is the normal case).
#
#   loom_atomic_mkdir_backend
#       Echo the backend name (mkdir | gnumkdir | python3 | perl) the call above
#       would use. Exposed so a caller in a retry loop can resolve once.
#
#   loom_atomic_mkdir_with <backend> <path>
#       Create <path> with an already-resolved backend.
#
# Why: Ubuntu 26.04 ships uutils coreutils 0.10.0 as /usr/bin/mkdir, whose
# create_dir() checks path.exists() before mkdir(2) and then returns Ok(()) on
# EEXIST when path.is_dir() even without -p (src/uu/mkdir/src/mkdir.rs:198,342).
# Under concurrency two callers both pass path.exists() and both exit 0,
# breaking POSIX mkdir mutual exclusion. Measured in ubuntu:26.04, 100 two-racer
# trials: uutils mkdir double-wins 21/100, GNU mkdir 0/100, direct mkdir(2) 0/100.
#
# Resolution order:
#   1. Shell-function `mkdir` (preserves test-seam stubs like #15122/#15157).
#   2. `gnumkdir` when installed (Ubuntu 26.04 ships /usr/bin/gnumkdir from
#      gnu-coreutils alongside rust-coreutils).
#   3. When `mkdir` is uutils and `gnumkdir` is absent, direct mkdir(2) via
#      `python3` or `perl` (Debian Essential `perl-base`).
#   4. Plain `mkdir` (GNU coreutils, BSD/macOS, BusyBox — atomic).

loom_atomic_mkdir_backend() {
    if [[ "$(type -t mkdir 2>/dev/null || true)" == "function" ]]; then
        printf 'mkdir\n'
        return 0
    fi
    if command -v gnumkdir >/dev/null 2>&1; then
        printf 'gnumkdir\n'
        return 0
    fi
    local ver
    ver="$(mkdir --version 2>/dev/null || true)"
    if [[ "$ver" == *[Uu][Uu][Tt][Ii][Ll][Ss]* ]]; then
        if command -v python3 >/dev/null 2>&1; then
            printf 'python3\n'
            return 0
        fi
        if command -v perl >/dev/null 2>&1; then
            printf 'perl\n'
            return 0
        fi
    fi
    printf 'mkdir\n'
}

loom_atomic_mkdir_with() {
    local backend="$1" path="$2"
    case "$backend" in
        gnumkdir) gnumkdir -- "$path" 2>/dev/null ;;
        python3)  python3 -c 'import os, sys; os.mkdir(sys.argv[1])' "$path" 2>/dev/null ;;
        perl)     perl -e 'mkdir($ARGV[0]) or exit 1' -- "$path" 2>/dev/null ;;
        *)        mkdir "$path" 2>/dev/null ;;
    esac
}

loom_atomic_mkdir() {
    loom_atomic_mkdir_with "$(loom_atomic_mkdir_backend)" "$1"
}
