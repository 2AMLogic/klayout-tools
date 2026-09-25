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
# building #1353).

set -euo pipefail

DEST_DIR="${1:-/usr/local/bin}"

# Pinned image -- bump the digest and CHANGELOG.md together in the same change
# if this is ever refreshed, exactly as install-yosys.sh/install-verilator.sh/
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
# then record the resulting `openroad -version` string below and in CHANGELOG.md.
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
# see this generator's own header comment for why) and a \$TMPDIR mount.
#
# Runs the image by PINNED DIGEST, never by the moving \`:latest\` tag, so this
# wrapper keeps executing the exact OpenROAD build it was installed against
# even after upstream republishes \`:latest\` (issue #2465):
#   $IMAGE
#   openroad -version == 26Q3-1510-g6cb3f2b704
set -euo pipefail
MOUNTS=(-v "\$PWD:\$PWD" -w "\$PWD")
if [[ -n "\${PDK_ROOT:-}" ]]; then
    MOUNTS+=(-v "\$PDK_ROOT:\$PDK_ROOT:ro")
elif command -v klt >/dev/null 2>&1 && command -v jq >/dev/null 2>&1; then
    # \$PDK_ROOT unset -- mirror \`klt place-and-route\`'s own PDK resolution
    # (find_pdk()'s search order) so the mount matches what it will actually
    # reference, rather than mounting nothing and failing later with an
    # opaque "cannot read file" that gives no hint it's a mount gap.
    RESOLVED_PDK_ROOT="\$(klt pdk find --format json 2>/dev/null | jq -r '.root // empty' 2>/dev/null || true)"
    if [[ -n "\$RESOLVED_PDK_ROOT" ]]; then
        MOUNTS+=(-v "\$RESOLVED_PDK_ROOT:\$RESOLVED_PDK_ROOT:ro")
    fi
fi
MOUNTS+=(-v "\${TMPDIR:-/tmp}:\${TMPDIR:-/tmp}")
exec docker run --rm -i \\
    --platform linux/amd64 \\
    "\${MOUNTS[@]}" \\
    $IMAGE \\
    $IN_IMAGE_BINARY "\$@"
WRAPPER_EOF
chmod +x "$WRAPPER"

echo "Installed openroad wrapper at $WRAPPER" >&2
"$WRAPPER" -version
