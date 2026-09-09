#!/usr/bin/env bash
# build.sh -- build the `eda-sim` overlay image from docker/eda-sim/pdk-versions.json.
#
# Every version the image bakes is read from pdk-versions.json and passed to
# `docker build` as a --build-arg. The Dockerfile declares those ARGs WITHOUT
# defaults, so this script is the only way to build it -- which is what makes
# the manifest a real single source of truth rather than a document that
# describes what the Dockerfile happens to contain (issue #509).
#
# Usage:
#   docker/eda-sim/build.sh [--tag <ref>]... [--platform <list>] [--push]
#                           [--manifest <path>] [--print-args] [--dry-run]
#
#   --tag <ref>       extra image ref to tag (repeatable). With no --tag, the
#                     image is tagged <image.name>:<image.version> and
#                     <image.name>:latest from the manifest.
#   --platform <list> comma-separated buildx platform list (e.g.
#                     linux/amd64,linux/arm64). Omitted -> the host platform
#                     via a plain `docker build` that loads into the local
#                     daemon (what CI's build-and-smoke job wants).
#   --push            push instead of loading locally. Requires --platform or
#                     a buildx builder; implies buildx.
#   --print-args      print the resolved build args (KEY=VALUE, one per line)
#                     and exit 0 without building. Used by CI to echo the pins
#                     it is about to bake, and by tests.
#   --dry-run         print the docker command that would run, then exit 0.
#
# Exit codes: 0 success (including --print-args / --dry-run), 2 usage or a
# malformed/missing manifest, other -> whatever docker returned.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MANIFEST="${SCRIPT_DIR}/pdk-versions.json"

TAGS=()
PLATFORM=""
PUSH=false
PRINT_ARGS=false
DRY_RUN=false

log() { echo "[eda-sim/build] $*" >&2; }
die() { local code="$1"; shift; log "error: $*"; exit "$code"; }

usage() { sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --tag) TAGS+=("$2"); shift 2 ;;
        --platform) PLATFORM="$2"; shift 2 ;;
        --push) PUSH=true; shift ;;
        --manifest) MANIFEST="$2"; shift 2 ;;
        --print-args) PRINT_ARGS=true; shift ;;
        --dry-run) DRY_RUN=true; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die 2 "unknown argument: $1 (see --help)" ;;
    esac
done

command -v jq >/dev/null 2>&1 || die 2 "'jq' is required on PATH (manifest parsing)"
[[ -f "$MANIFEST" ]] || die 2 "manifest not found: ${MANIFEST}"

# `jq -e` so a null/missing pin fails here with the field name, rather than
# expanding to an empty --build-arg that only explodes deep inside the build.
m() {  # m <jq-filter>
    jq -er "$1" "$MANIFEST" \
        || die 2 "manifest ${MANIFEST} is missing or null at: $1"
}

IMAGE_NAME="$(m '.image.name')"
IMAGE_VERSION="$(m '.image.version')"
BASE_IMAGE="$(m '.base.image')"
BASE_VERSION="$(m '.base.version')"
NGSPICE_VERSION="$(m '.tools.ngspice.version')"
NGSPICE_MIN_MAJOR="$(m '.tools.ngspice.min_major')"
NGSPICE_SHA256="$(m '.tools.ngspice.sha256')"
NGSPICE_URL="$(m '.tools.ngspice.url_template' | sed "s/{version}/${NGSPICE_VERSION}/g")"
NGSPICE_CONFIGURE_FLAGS="$(m '.tools.ngspice.configure_flags | join(" ")')"
XSCHEM_TAG="$(m '.tools.xschem.tag')"
XSCHEM_COMMIT="$(m '.tools.xschem.commit')"
XSCHEM_REPO="$(m '.tools.xschem.repo')"
CIEL_VERSION="$(m '.tools.ciel.version')"
KLAYOUT_TOOLS_VERSION="$(m '.tools.klayout_tools.version')"

BUILD_ARGS=(
    "LOOM_WORKER_IMAGE=${BASE_IMAGE}"
    "LOOM_WORKER_VERSION=${BASE_VERSION}"
    "EDA_SIM_VERSION=${IMAGE_VERSION}"
    "NGSPICE_VERSION=${NGSPICE_VERSION}"
    "NGSPICE_MIN_MAJOR=${NGSPICE_MIN_MAJOR}"
    "NGSPICE_SHA256=${NGSPICE_SHA256}"
    "NGSPICE_URL=${NGSPICE_URL}"
    "NGSPICE_CONFIGURE_FLAGS=${NGSPICE_CONFIGURE_FLAGS}"
    "XSCHEM_TAG=${XSCHEM_TAG}"
    "XSCHEM_COMMIT=${XSCHEM_COMMIT}"
    "XSCHEM_REPO=${XSCHEM_REPO}"
    "CIEL_VERSION=${CIEL_VERSION}"
    "KLAYOUT_TOOLS_VERSION=${KLAYOUT_TOOLS_VERSION}"
)

if [[ "$PRINT_ARGS" == true ]]; then
    printf '%s\n' "${BUILD_ARGS[@]}"
    exit 0
fi

if [[ ${#TAGS[@]} -eq 0 ]]; then
    TAGS=("${IMAGE_NAME}:${IMAGE_VERSION}" "${IMAGE_NAME}:latest")
fi

CMD=(docker)
if [[ -n "$PLATFORM" || "$PUSH" == true ]]; then
    CMD+=(buildx build)
    [[ -n "$PLATFORM" ]] && CMD+=(--platform "$PLATFORM")
    if [[ "$PUSH" == true ]]; then CMD+=(--push); else CMD+=(--load); fi
else
    CMD+=(build)
fi

for arg in "${BUILD_ARGS[@]}"; do CMD+=(--build-arg "$arg"); done
for tag in "${TAGS[@]}"; do CMD+=(--tag "$tag"); done

# The build context is docker/eda-sim/ itself, not the repo root: nothing from
# the repo tree is copied into the image (klayout-tools is installed from PyPI
# at the pinned version), so a repo-root context would ship a multi-hundred-MB
# context for no reason -- and would make the image depend on the checkout.
CMD+=(--file "${SCRIPT_DIR}/Dockerfile" "${SCRIPT_DIR}")

log "pins: ngspice ${NGSPICE_VERSION} (floor ${NGSPICE_MIN_MAJOR}) | xschem ${XSCHEM_TAG} | ciel ${CIEL_VERSION} | klayout-tools ${KLAYOUT_TOOLS_VERSION}"
log "base: ${BASE_IMAGE}:${BASE_VERSION}"
log "tags: ${TAGS[*]}"

if [[ "$DRY_RUN" == true ]]; then
    printf '%q ' "${CMD[@]}"; printf '\n'
    exit 0
fi

command -v docker >/dev/null 2>&1 || die 2 "'docker' is required on PATH"
exec "${CMD[@]}"
