#!/usr/bin/env bash
# lib/cargo-target-dir.sh — resolve a workspace's actual Cargo target
# directory, plus the per-worktree-scheme predicates (issues #7239, #8458).
#
# ## Why this exists
#
# Cargo's build output is NOT always `<workspace>/target`. `CARGO_TARGET_DIR`
# or `build.target-dir` in any `config.toml` on the lookup path can redirect it
# anywhere — commonly to a large external volume. Callers that need the real
# directory (`spawn-claude.sh` exporting CARGO_TARGET_DIR for a worktree,
# `defaults/hooks/post-worktree.sh` deciding which cache to strip) must resolve
# it the way Cargo does, not assume the default.
#
# The removal-time half of #7239 — reclaiming a redirected target dir with its
# worktree — lived here in bash until every removal surface moved to the Rust
# implementation (`loom-daemon/src/worktree_ops/cargo_target.rs`):
# `worktree.sh remove` in #8471, `merge-pr.sh`'s post-merge cleanup in #9153,
# with `loom-daemon clean` and the periodic reaper already there. The dead bash
# reclaim stack was then deleted (#15075); this library is resolution and
# predicates only.
#
# ## Two parts
#
#   1. `loom_resolve_cargo_target_dir <workspace_root>` — Cargo's resolution
#      order (env → `cargo metadata` → `<root>/target`). This is a LIBRARY
#      twin of the standalone `scripts/cargo-target-dir.sh` (which is part of
#      the Loom repo's own install path and is not shipped to consumer repos).
#      **The two must stay byte-identical in behavior**;
#      `defaults/scripts/tests/test-cargo-target-dir-reclaim.sh` asserts parity
#      between them on every branch of the resolution order, so drift fails CI
#      rather than silently changing what gets deleted.
#
#   2. `loom_is_per_worktree_target_dir` / `loom_read_worktree_target_dir_marker`
#      (issue #8458) — two thin wrappers over `loom-daemon cargo-target-dir`,
#      consulted by `defaults/hooks/post-worktree.sh` so it recognises a target
#      dir the CREATION side (`loom-daemon cargo-target-dir provision`, called
#      by `worktree.sh`) gave this worktree under the otherwise-shared root and
#      recorded in a marker file INSIDE it. See "Per-worktree target dirs"
#      below.

# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------

# Resolve a possibly-relative Cargo path against a workspace root, without
# requiring it to exist (the target dir is created by the build itself).
_loom_ctd_absolutize() {
    local value="$1" root="$2"
    case "$value" in
        /*) printf '%s\n' "$value" ;;
        *)  printf '%s\n' "$root/$value" ;;
    esac
}

# loom_resolve_cargo_target_dir <workspace_root>
#
# Prints Cargo's actual target directory for <workspace_root>. Mirrors
# scripts/cargo-target-dir.sh exactly:
#   1. $CARGO_TARGET_DIR when set and non-empty (env beats config in Cargo).
#   2. `cargo metadata --format-version 1 --no-deps` (applies the full
#      config.toml hierarchy, including build.target-dir).
#   3. `<workspace_root>/target` — Cargo's default.
# Always exits 0 with a path on stdout; a resolution hiccup degrades to the
# historical hardcoded assumption rather than to a hard failure.
#
# `_loom_ctd_metadata_target_dir <root>` is the middle step, factored out so the
# reclaim path can tell "cargo says <root>/target" apart from "cargo could not
# answer" — a distinction `loom_resolve_cargo_target_dir` itself must NOT make,
# because it has to stay behaviorally identical to scripts/cargo-target-dir.sh.
# Prints the absolutized target_directory on stdout, exit 0. Exit 1 means cargo
# is missing, exited non-zero, or emitted no usable field.
_loom_ctd_metadata_target_dir() {
    local root="$1"
    command -v cargo >/dev/null 2>&1 || return 1

    local metadata resolved=""
    metadata="$(cd "$root" 2>/dev/null && cargo metadata --format-version 1 --no-deps 2>/dev/null)" || metadata=""
    [[ -n "$metadata" ]] || return 1

    if command -v jq >/dev/null 2>&1; then
        resolved="$(printf '%s' "$metadata" | jq -r '.target_directory // empty' 2>/dev/null)"
    else
        resolved="$(printf '%s' "$metadata" | sed -n 's/.*"target_directory":"\([^"]*\)".*/\1/p')"
    fi
    [[ -n "$resolved" && "$resolved" != "null" ]] || return 1

    _loom_ctd_absolutize "$resolved" "$root"
}

loom_resolve_cargo_target_dir() {
    local root="$1"

    if [[ -n "${CARGO_TARGET_DIR:-}" ]]; then
        _loom_ctd_absolutize "$CARGO_TARGET_DIR" "$root"
        return 0
    fi

    local resolved
    if resolved="$(_loom_ctd_metadata_target_dir "$root")"; then
        printf '%s\n' "$resolved"
        return 0
    fi

    printf '%s\n' "$root/target"
}


# --------------------------------------------------------------------------
# Per-worktree target dirs — the predicates (issue #8458)
# --------------------------------------------------------------------------
#
# ## The scheme, in one paragraph
#
# Cargo keys a *workspace* crate's artifacts (and its incremental session) by
# the crate's ABSOLUTE SOURCE PATH, so two worktrees never share workspace-crate
# build output even inside one shared target dir. The sharing buys nothing for
# the crates Loom rebuilds, and costs unbounded growth (#8453: 460 GB on one
# host) plus WRONG TEST RESULTS — cargo "uplifts" the final binary to one
# un-hashed `<target>/debug/loom-daemon` that whichever worktree built last
# overwrites, and integration tests execute that path. Giving each worktree
# `<root>/wt/<worktree name>` fixes both.
#
# ## Why only two thin wrappers live here
#
# Every bit of #8458's LOGIC is in the daemon — the opt-in, the derivation, the
# `mkdir`, the marker write (`loom-daemon cargo-target-dir provision|path`), AND
# the two predicates below (`… is-attributable`, `… marker`). That is the
# language policy (`.loom/docs/shell-language-policy.md`): new executable logic
# goes behind a `loom-daemon` subcommand, not into `defaults/scripts/`.
#
# The remaining bash call site is `defaults/hooks/post-worktree.sh`, which
# sources this library to decide whether the worktree's (and the primary
# checkout's) build output lives in a provisioned per-worktree dir before it
# strips the stale copy from the shared cache. Delegating the implementation
# keeps that decision in one place: the functions below are two-line calls into
# `loom-daemon/src/worktree_ops/cargo_target/per_worktree.rs`, which is the one
# place the shape rule and the marker grammar are written.
#
# Degradation is the `requires-daemon: cargo-target-dir optional` contract the
# callers already declare: no resolvable daemon ⇒ both predicates answer "no" ⇒
# no marker is ever read and neither attribution relaxation is ever reached ⇒
# exactly the pre-#8458 behaviour. That is safe by construction, because a host
# with no daemon also never *provisioned* a per-worktree dir to reclaim.
#
# ## Why a marker file and not `.cargo/config.toml`
#
# #7239's attribution rule is that ONLY a redirect derived from the worktree
# itself can prove a directory belongs to it, and it names `build.target-dir` in
# a `.cargo/config.toml` INSIDE the worktree as the way that happens. That
# vehicle is unavailable in a repo that TRACKS `.cargo/config.toml` (Loom itself
# does — it carries the CI-mirroring clippy `rustflags`): the redirect would
# leave every worktree with a modified tracked file, one careless `git add -A`
# from being committed. The marker is inside the worktree, so it is
# worktree-derived evidence in exactly the sense #7239 requires, and it is not a
# Cargo config, so it touches no tracked state. Cargo learns the redirect from
# `CARGO_TARGET_DIR`, exported by the spawn path.
#
# ## The shape IS the attribution
#
# `<root>/wt/<worktree name>` is checked structurally against the basename of
# the worktree being asked about. That predicate is what licenses the two
# attribution relaxations in the Rust reclaim
# (`loom-daemon/src/worktree_ops/cargo_target.rs`: containment stops counting
# and only an exact sharing match does), and it is deliberately un-widenable: a
# machine-global root (`/big/cargo-target`, `$CARGO_HOME`-configured or
# env-exported) cannot satisfy it, and a corrupted or hand-written marker can
# only ever name `<something>/wt/<this worktree's own name>` — never a parent,
# never a sibling's directory, never the shared root.

# The marker file itself is `.loom-cargo-target-dir` inside the worktree —
# gitignored via the loom-managed block (loom-daemon/src/init/post_init.rs) and
# filtered out of the dirty-worktree guards in worktree.sh / merge-pr.sh, like
# every other marker in its family. Its name is spelled out at the two places
# that need it rather than held in a shell variable: the grammar lives in
# `per_worktree::MARKER_FILE`, and a second shell-side "source of truth" for a
# constant is how the two drift.

# _loom_ctd_daemon <verb> [arg...] — run `loom-daemon cargo-target-dir <verb>`,
# or exit 1 when no daemon binary resolves (the `optional` half of the callers'
# `requires-daemon: cargo-target-dir optional` declaration: both predicates then
# answer "no", which is the pre-#8458 behaviour).
#
# `loom_locate_daemon_bin` comes from the sibling `lib/locate-daemon-bin.sh`,
# which every consumer of this library already sources. It is called through a
# `|| true` command substitution with stderr discarded, so a partial checkout
# where it is NOT defined degrades to "no daemon" rather than erroring — the
# same failure direction as a host with no binary. Quiet, because this resolves
# once per predicate call inside loops and the #4997 resolution trace would
# otherwise dominate a removal's stderr.
#
# The result is memoised per shell. Calls made from inside a command
# substitution (`loom_read_worktree_target_dir_marker` is one) cannot write the
# memo back to the parent, so they re-resolve; that is a PATH/`-x` probe, not a
# build, and it is not in the per-sibling hot path.
_loom_ctd_daemon() {
    [[ -n "${_LOOM_CTD_DAEMON_BIN+x}" ]] || _LOOM_CTD_DAEMON_BIN="$(LOOM_LOCATE_DAEMON_BIN_QUIET=1 loom_locate_daemon_bin "$PWD" 2>/dev/null || true)"
    [[ -n "$_LOOM_CTD_DAEMON_BIN" ]] && "$_LOOM_CTD_DAEMON_BIN" cargo-target-dir "$@" 2>/dev/null
}

# loom_is_per_worktree_target_dir <worktree_path> <candidate> [<candidate>...]
#   Exit 0 when ANY <candidate> has the Loom per-worktree shape FOR
#   <worktree_path>. Purely structural — no disk access — so the same rule in
#   the Rust reclaim stays answerable after the worktree has been removed.
#   Several candidates are accepted so a caller can ask about a path and its
#   `realpath` together; one subprocess answers both. The rule itself is
#   `per_worktree::is_attributable`, not a copy of it.
#
# loom_read_worktree_target_dir_marker <worktree_path>
#   Print the target dir recorded for this worktree, or exit 1 when there is no
#   usable marker. MUST be called while the worktree is still on disk. Every
#   failure mode — absent, empty, a value without the per-worktree shape, a tree
#   with no Cargo manifest — exits 1, so a corrupt marker degrades to "no
#   per-worktree redirect" (pre-#8458 behavior) rather than to a path the
#   caller would then act on. That validation chain is
#   `per_worktree::marker_value`, not a copy of it.
#
# Both are one line because there is nothing left for them to do: the whole
# predicate is the Rust it names. They keep their own names because
# `.loom/hooks/post-worktree.sh` overrides exactly these two on a partial
# checkout that has no lib at all.
loom_is_per_worktree_target_dir() { _loom_ctd_daemon is-attributable "$@"; }
loom_read_worktree_target_dir_marker() { _loom_ctd_daemon marker "$1"; }
