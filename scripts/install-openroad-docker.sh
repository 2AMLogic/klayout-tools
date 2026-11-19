#!/usr/bin/env bash
# install-openroad-docker.sh -- install an `openroad` wrapper script onto
# $PATH that shells out to the `openroad/orfs` Docker image, per
# docs/cli/place-and-route.md's "Docker: extract the binary onto $PATH"
# recipe. There is no apt/brew/pip package for `openroad` (see that doc
# section's own explanation of why) -- this is the one documented,
# reproducible distribution path, and it is what
# scripts/place-and-route-smoke.sh (issue #1328, Epic #700 Phase 4) uses to
# provision `openroad` in CI.
#
# Usage: scripts/install-openroad-docker.sh [DEST_DIR]
#   DEST_DIR   Directory to write the `openroad` wrapper into (default:
#              /usr/local/bin). Must already be on $PATH, or the caller adds
#              it themself (CI callers typically pass a cache-friendly dir
#              like $HOME/.cache/openroad-docker/bin and append it to
#              $GITHUB_PATH, matching this repo's own pinned-Yosys/Icarus/
#              Verilator/SymbiYosys steps in .github/workflows/ci.yml rather
#              than writing straight into /usr/local/bin).
#
# Requires `docker` on $PATH with a running, reachable daemon. Pulls the
# pinned `openroad/orfs` image digest below -- a large image bundling the
# whole OpenROAD-flow-scripts (ORFS) tree, not just the `openroad` binary --
# so the first pull can take several minutes.
#
# Beyond docs/cli/place-and-route.md's own bare wrapper snippet, the wrapper
# this script writes ALSO mounts $PDK_ROOT (read-only) into the container
# at the identical host path, when set at invocation time. `klt
# place-and-route` resolves the PDK's LEF/tech-LEF/liberty files on the HOST
# and embeds those absolute paths directly into the Tcl script it hands
# `openroad` (`read_liberty <path>`, `read_lef <path>` --
# src/klayout_tools/place_and_route.py's `_stage_script_lines`) -- so unless
# $PDK_ROOT already lives under $PWD, the documented `-v "$PWD":"$PWD"`-only
# mount leaves the container unable to open those files. Mounting $PDK_ROOT
# too (at the same path, so the embedded absolute paths still resolve) is
# what makes the wrapper actually work against a real PDK install rather
# than only against fixtures colocated with the request file.
#
# When $PDK_ROOT is unset at invocation time, the wrapper falls back to
# `klt pdk find --format json | jq -r .root` -- the exact root `klt
# place-and-route` itself resolves in that case (`find_pdk()`'s own search
# order: $PDK_ROOT, then the ciel/volare stores, then the conventional
# prefixes -- `src/klayout_tools/pdk.py`). Without this fallback, a PDK
# discovered via a search root (e.g. `~/.volare`, never `$PDK_ROOT` itself)
# mounts nothing at all, and every stage fails on its first `read_liberty`/
# `read_lef` with an opaque `cannot read file <path>` that gives no hint
# it's a mount-namespace mismatch rather than a missing file (issue #1868).
# The fallback is skipped -- not an error -- when `klt` or `jq` is not on
# $PATH, or when it resolves nothing (e.g. `openroad -version`, which needs
# no PDK at all).
#
# The wrapper also mounts the resolved system temp directory (`$TMPDIR`,
# defaulting to `/tmp`, matching Python's own `tempfile.gettempdir()`
# fallback) -- issue #1353's own `equiv-canary.yml` runs a pytest test whose
# `tmp_path` fixture writes the generated request/`.tcl` files under that
# temp dir, not under `$PWD`; without this mount `openroad` fails with
# `cannot open '<tmp_path>/.../pnr_*.tcl'` the moment a caller's request
# lives outside `$PWD` (verified live against this exact failure while
# building #1353). While `$TMPDIR` is unset (or `/tmp`) that mount is what
# incidentally makes `/tmp` visible as a whole -- but the moment a caller
# exports any other `$TMPDIR`, `/tmp` stops being mounted and an input under
# `/tmp`-but-not-`$TMPDIR` fails inside the container with an opaque
# `[ERROR STA-0340] cannot open '<path>'` for a file the caller can still
# `cat` on the host (measured, issue #5878). So when `$TMPDIR` differs from
# `/tmp` the wrapper keeps `/tmp` mounted as well (skipped when they are
# equal -- docker rejects a duplicate mount point; all trailing slashes
# on `$TMPDIR` are stripped before the comparison so `/tmp/` or `/tmp//`
# does not duplicate it either).
#
# And it mounts the klt cache root (issue #5626): the merged `.lib` that a
# descriptor's LIST-form `liberty` entry produces is written under
# `$KLT_CACHE_DIR` itself when set, else `${XDG_CACHE_HOME:-$HOME/.cache}/klt`
# -- the selection `src/klayout_tools/liberty_merge.py`'s `cache_root()`
# performs -- and that absolute path is embedded in the
# generated Tcl (`read_liberty <path>`), so a container that cannot see it
# fails on its first OpenROAD stage with an opaque `cannot read file` no
# matter how well everything else is mounted. The mount is read-only
# (klt writes the merged file host-side before ever invoking the wrapper;
# OpenROAD only ever `read_liberty`s it), mirroring the `$PDK_ROOT:ro`
# precedent. Two guards the one-liner needs: the directory is `mkdir -p`'d
# by the wrapper BEFORE `docker run` (Docker creates a missing bind-mount
# source as root -- issue #3151's exact hazard, reachable whenever no
# list-form corner has ever created `~/.cache/klt`), and the mount is
# skipped when its path equals a path already mounted after stripping trailing
# slashes (`docker run` rejects a duplicate mount point, so
# `KLT_CACHE_DIR=$TMPDIR/` must not become a second `-v` for it). A leading
# `~` or `~/` in KLT_CACHE_DIR expands to ${HOME:-/tmp} before mkdir or
# comparison; unlike Python's expanduser, `~user` is left literal.
#
# The wrapper forwards a narrow allowlist of the caller's environment into the
# container (issue #3821): `PLATFORM_DIR` (the ORFS idiom, read by Tcl as
# `$::env(PLATFORM_DIR)`) when it is set -- even if empty -- and every variable
# whose name starts with `OPENROAD_`. Each is passed by NAME only (`-e NAME`),
# so Docker copies the host value verbatim and an unset variable stays unset.
# The whole environment is deliberately NOT forwarded (it would leak unrelated
# host state and break reproducibility). Because values are verbatim, a
# host-path `PLATFORM_DIR` only resolves in the container if it lies under a
# mounted path ($PWD, $PDK_ROOT, $TMPDIR, plus /tmp itself when $TMPDIR
# differs from it -- issue #5878 -- or the klt cache root).
#
# Container lifetime (issue #5726): the container is started with `--cidfile`
# and is killed by the wrapper's own EXIT/INT/TERM/HUP handler whenever the
# wrapper is signalled before the `docker run` client completes. Without
# this, a caller that kills `klt` (its timeout, or a harness reaping its
# agent) kills the docker client but NOT the container: OpenROAD keeps
# routing for hours and writes `*.def`/`*.odb`/`*_route_drc.rpt` into the
# caller's repository with no `klt` process and no `invocation.json` to
# attribute them to. Two things make that window close or at least
# attributable:
#
# 1. The client is backgrounded and `wait`ed instead of `exec`ed. `exec`
#    replaces the wrapper's shell with the docker client, so no shell
#    survives to run a `trap`; a signalled client dies with default signal
#    disposition and the container outlives it.
# 2. A background poller emits `klt-openroad-cid <64-hex>` on the wrapper's
#    stderr as soon as the runtime records the cid file.
#    `src/klayout_tools/_openroad_engine.py` scans the retained stderr log
#    for that line and records the id in `invocation.json`, so a container
#    that DOES outlive its caller (the signal was SIGKILL, which no handler
#    can catch) is identifiable -- and precisely reapable -- from the run's
#    own record instead of by guessing.
#
# The trap deliberately does NOT `docker kill` after a normal (unsignalled)
# completion: the client was reaped, `--rm` already removed the container,
# and the handler only cleans up the cid file.

set -euo pipefail

DEST_DIR="${1:-/usr/local/bin}"

# Pinned image -- bump the digest and a changelog fragment
# (`changelog.d/README.md`) together in the same change if this is ever
# refreshed, exactly as install-yosys.sh/install-verilator.sh/
# install-magic.sh/install-icarus-verilog.sh/install-xyce.sh bump a pinned
# version + asset checksum together. `openroad/orfs` publishes ONLY a moving
# `:latest` tag (no versioned tags), so the digest is the only pin available --
# and without it "the same installer" resolves to whatever image the upstream
# tag happened to point at on the day a given host ran it. That is not
# hypothetical: on 2026-09-24 two identically-provisioned fleet workers held
# different images under the same `:latest` tag -- `sha256:bb7f3169...`
# (openroad 26Q3-1510-g6cb3f2b704) vs `sha256:0586b21f...`
# (26Q3-1278-g4421880472) -- so a `place-and-route`/`equiv` result could not be
# attributed to a specific OpenROAD build (issue #2465).
#
# Pulling by digest is resolved deterministically by the registry without
# consulting the `:latest` tag at all. Re-pin with:
#   docker pull --platform linux/amd64 openroad/orfs:latest
#   docker image inspect openroad/orfs:latest --format '{{index .RepoDigests 0}}'
# then record the resulting `openroad -version` string below and in a
# changelog fragment (`changelog.d/README.md`).
ORFS_IMAGE_REPO="openroad/orfs"
# Pinned 2026-09-25: the `:latest` image created 2026-08-24, whose bundled
# binary reports `openroad -version` == 26Q3-1510-g6cb3f2b704. Multi-platform
# index; its linux/amd64 manifest is sha256:bda67bf258143f83893811d0aec0a4a304767d0b033027c90ac350e645a32337.
ORFS_IMAGE_DIGEST="sha256:bb7f31697fb8466ab61852fc796376cc16d9df38f762ccb733218b8a5e55824b"
IMAGE="${ORFS_IMAGE_REPO}@${ORFS_IMAGE_DIGEST}"
IN_IMAGE_BINARY="/OpenROAD-flow-scripts/tools/install/OpenROAD/bin/openroad"

command -v docker >/dev/null 2>&1 || {
    echo "error: docker is required on \$PATH to install the openroad wrapper" >&2
    exit 1
}

echo "Pulling $IMAGE (large image -- first pull can take several minutes)..." >&2
docker pull --platform linux/amd64 "$IMAGE"

mkdir -p "$DEST_DIR"
WRAPPER="$DEST_DIR/openroad"

cat >"$WRAPPER" <<WRAPPER_EOF
#!/usr/bin/env bash
# Auto-generated by scripts/install-openroad-docker.sh -- do not edit by
# hand, re-run that script instead. Mirrors docs/cli/place-and-route.md's
# "Docker: extract the binary onto \$PATH" recipe, plus a \$PDK_ROOT mount
# (falling back to \`klt pdk find\` when \$PDK_ROOT is unset -- issue #1868,
# see this generator's own header comment for why), a \$TMPDIR mount, and
# /tmp itself kept mounted whenever \$TMPDIR differs from it (issue #5878),
# and the klt cache root that holds the merged Liberty file a list-form
# descriptor's \`liberty\` entry produces (issue #5626).
#
# Runs the image by PINNED DIGEST, never by the moving \`:latest\` tag, so this
# wrapper keeps executing the exact OpenROAD build it was installed against
# even after upstream republishes \`:latest\` (issue #2465):
#   $IMAGE
#   openroad -version == 26Q3-1510-g6cb3f2b704
#
# Runs the container as the CALLER's uid:gid (\`--user\`), not the image's
# default root, so files OpenROAD writes into the read-write \$PWD / \$TMPDIR
# mounts are owned by the caller on Linux (a root-owned directory there blocks
# worktree/scratch cleanup -- issue #3151). \$HOME is pointed at \$TMPDIR
# because that uid usually has no passwd entry (hence no home) in the image.
#
# The container is RECORDED and KILLED when this wrapper dies before the
# client completes (issue #5726): \`--cidfile\` names it, and the EXIT/INT/
# TERM/HUP handler below \`docker kill\`s whatever the cid file recorded.
# The client is backgrounded and waited (not exec'd) precisely so this
# shell survives to run the handler. After a normal completion the handler
# only removes the cid file: the client was reaped and \`--rm\` already
# removed the container. A SIGKILL to this wrapper is uncatchable -- the
# container then survives, but the stderr marker line
# (\`klt-openroad-cid <id>\`, emitted by the poller below) leaves it
# identifiable and precisely reapable from the run's own logs.
set -euo pipefail
MOUNTS=(-v "\$PWD:\$PWD" -w "\$PWD")
MOUNTED_PATHS=("\$PWD")
if [[ -n "\${PDK_ROOT:-}" ]]; then
    MOUNTS+=(-v "\$PDK_ROOT:\$PDK_ROOT:ro")
    MOUNTED_PATHS+=("\$PDK_ROOT")
elif command -v klt >/dev/null 2>&1 && command -v jq >/dev/null 2>&1; then
    # \$PDK_ROOT unset -- mirror \`klt place-and-route\`'s own PDK resolution
    # (find_pdk()'s search order) so the mount matches what it will actually
    # reference, rather than mounting nothing and failing later with an
    # opaque "cannot read file" that gives no hint it's a mount gap.
    RESOLVED_PDK_ROOT="\$(klt pdk find --format json 2>/dev/null | jq -r '.root // empty' 2>/dev/null || true)"
    if [[ -n "\$RESOLVED_PDK_ROOT" ]]; then
        MOUNTS+=(-v "\$RESOLVED_PDK_ROOT:\$RESOLVED_PDK_ROOT:ro")
        MOUNTED_PATHS+=("\$RESOLVED_PDK_ROOT")
    fi
fi
TMPDIR_MOUNT="\${TMPDIR:-/tmp}"
MOUNTS+=(-v "\$TMPDIR_MOUNT:\$TMPDIR_MOUNT")
MOUNTED_PATHS+=("\$TMPDIR_MOUNT")
# A \$TMPDIR other than /tmp stops /tmp itself being visible inside the
# container, but callers legitimately pass /tmp-resident paths (issue #5878:
# with any other TMPDIR exported, an input under /tmp failed with an opaque
# "[ERROR STA-0340] cannot open" for a file the caller could read on the
# host). Keep /tmp mounted too unless TMPDIR already resolves to it --
# docker rejects a duplicate mount point, and all trailing slashes are
# stripped from the comparison so TMPDIR=/tmp/ or /tmp// does not
# produce one. /tmp is recorded in MOUNTED_PATHS so the klt cache-root
# dedupe below also sees it (KLT_CACHE_DIR=/tmp must not become a second
# -v for it).
strip_trailing_slashes() {
    local path="\$1"
    while [[ "\$path" == */ && "\$path" != / ]]; do
        path="\${path%/}"
    done
    printf '%s' "\$path"
}
TMPDIR_CMP="\$(strip_trailing_slashes "\$TMPDIR_MOUNT")"
if [[ "\$TMPDIR_CMP" != "/tmp" ]]; then
    MOUNTS+=(-v "/tmp:/tmp")
    MOUNTED_PATHS+=("/tmp")
fi
# klt cache root (issue #5626): the merged .lib a list-form \`liberty\`
# descriptor produces is written under it and embedded as an absolute path
# in the generated Tcl, so the container must see it. Uses the same selection
# as src/klayout_tools/liberty_merge.py's cache_root(), expanding only ~ and
# ~/ in KLT_CACHE_DIR (not ~user), with HOME defaulting to /tmp. mkdir -p
# BEFORE docker run -- Docker would otherwise create a missing bind-mount
# source as root (issue #3151). The mount is skipped when the path already
# equals one mounted above after stripping trailing slashes from both paths
# (keeping /) -- docker run rejects a duplicate mount point. Only comparisons
# are normalized; existing mount arguments and permissions stay intact.
# \$HOME is defaulted (\${HOME:-/tmp}) and a failed mkdir degrades to no
# cache mount instead of killing the run: this block executes on EVERY
# invocation under \`set -euo pipefail\`, and an unset or unwritable HOME
# must not stop \`openroad -version\` et al. A missing cache root only
# matters for list-form corners, which produce their own diagnostic.
KLT_CACHE_ROOT="\${KLT_CACHE_DIR:-\${XDG_CACHE_HOME:-\${HOME:-/tmp}/.cache}/klt}"
if [[ "\${KLT_CACHE_DIR:-}" == \~ || "\${KLT_CACHE_DIR:-}" == \~/* ]]; then
    KLT_CACHE_ROOT="\${HOME:-/tmp}\${KLT_CACHE_DIR:1}"
fi
mkdir -p "\$KLT_CACHE_ROOT" 2>/dev/null || KLT_CACHE_ROOT=""
KLT_CACHE_CMP="\$(strip_trailing_slashes "\$KLT_CACHE_ROOT")"
CACHE_DUPE=0
for m in "\${MOUNTED_PATHS[@]}"; do
    if [[ "\$KLT_CACHE_CMP" == "\$(strip_trailing_slashes "\$m")" ]]; then
        CACHE_DUPE=1
        break
    fi
done
if [[ -n "\$KLT_CACHE_ROOT" && "\$CACHE_DUPE" -eq 0 ]]; then
    MOUNTS+=(-v "\$KLT_CACHE_ROOT:\$KLT_CACHE_ROOT:ro")
fi
# Forward PLATFORM_DIR and OPENROAD_* by name (issue #3821).
ENV_ARGS=()
if [[ -n "\${PLATFORM_DIR+x}" ]]; then
    ENV_ARGS+=(-e PLATFORM_DIR)
fi
while IFS= read -r v; do
    [[ -n "\$v" ]] && ENV_ARGS+=(-e "\$v")
done < <(compgen -e OPENROAD_ || true)
# mktemp guarantees a unique --cidfile path that cannot collide with a
# concurrent invocation; the file itself is removed again because the
# runtime refuses to start a container whose cid file already exists.
CID_FILE="\$(mktemp "\${TMPDIR:-/tmp}/klt-openroad-cid.XXXXXX")"
rm -f "\$CID_FILE"
CLIENT_PID=
FINISHED=0
MARKER_PID=
cleanup() {
    # Only kill on an unfinished run: after \`wait\` reaped the client the
    # container is already gone (--rm); the cid file is all that is left.
    if [[ "\$FINISHED" == 0 ]]; then
        CID="\$(cat "\$CID_FILE" 2>/dev/null || true)"
        if [[ -n "\$CID" ]]; then
            # Async on purpose: the runtime daemon performs the kill, and a
            # slow or hung daemon must not delay this wrapper's exit -- the
            # shell is inside its EXIT trap here.
            docker kill "\$CID" >/dev/null 2>&1 &
        fi
        if [[ -n "\$CLIENT_PID" ]]; then
            kill "\$CLIENT_PID" >/dev/null 2>&1 || true
        fi
    fi
    if [[ -n "\$MARKER_PID" ]]; then
        kill "\$MARKER_PID" >/dev/null 2>&1 || true
    fi
    rm -f "\$CID_FILE" >/dev/null 2>&1 || true
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP
docker run --rm -i \\
    --platform linux/amd64 \\
    --cidfile "\$CID_FILE" \\
    --user "\$(id -u):\$(id -g)" \\
    -e HOME="\${TMPDIR:-/tmp}" \\
    \${ENV_ARGS[@]+"\${ENV_ARGS[@]}"} \\
    "\${MOUNTS[@]}" \\
    $IMAGE \\
    $IN_IMAGE_BINARY "\$@" &
CLIENT_PID=\$!
(
    # Emit the container id on stderr as soon as the runtime records it,
    # and stop once the client is gone. klayout_tools'
    # _openroad_engine scans the retained stderr log for this line and
    # records the id in invocation.json (issue #5726) -- including after a
    # SIGKILL, where the poller itself is the only surviving witness.
    while kill -0 "\$CLIENT_PID" 2>/dev/null; do
        if [[ -s "\$CID_FILE" ]]; then
            printf 'klt-openroad-cid %s\\n' "\$(cat "\$CID_FILE")" >&2
            exit 0
        fi
        sleep 0.1
    done
) &
MARKER_PID=\$!
STATUS=0
wait "\$CLIENT_PID" || STATUS=\$?
FINISHED=1
exit "\$STATUS"
WRAPPER_EOF
chmod +x "$WRAPPER"

echo "Installed openroad wrapper at $WRAPPER" >&2
"$WRAPPER" -version
