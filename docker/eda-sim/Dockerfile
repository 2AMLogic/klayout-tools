# syntax=docker/dockerfile:1

# eda-sim -- the analog/EDA simulation overlay for the Loom fleet's worker
# nodes (issue #509).
#
#   FROM ghcr.io/rjwalters/loom-worker:<version>   (orchestration base, owned
#                                                   by rjwalters/loom)
#   +   ngspice, xschem, ciel, klt                 (domain toolchain, owned
#                                                   here)
#
# HARD RULE: the PDK tree is NOT baked. A single family is 7-9 GB, which would
# blow the ~2 GB ceiling and couple PDK versions to image releases. What ships
# is the *fetch mechanism* -- ciel, the pinned commits in pdk-versions.json,
# and the `eda-sim-fetch-pdk` helper -- and the tree lands in ~/.ciel at first
# use, which callers should back with a volume/cache mount.
#
# NOT baked, deliberately: magic and netgen. Every 2AMLogic gf180-*/sky130-*
# block repo's CI was audited (2026-08-04, spot-checked 2026-09-09) and none of
# them invoke either; this repo's own LVS engine (src/klayout_tools/lvs.py) is
# netlist-vs-netlist and wires up no magic extraction backend.
#
# Every ARG below is declared WITHOUT a default. That is deliberate: the pins
# live in pdk-versions.json and are passed by docker/eda-sim/build.sh, so this
# file cannot drift from the manifest. A bare `docker build` fails loudly at
# the first guarded step rather than baking a stale version.

ARG LOOM_WORKER_IMAGE
ARG LOOM_WORKER_VERSION

# ---------------------------------------------------------------------------
# Stage 1: compile ngspice and xschem into /opt/eda.
#
# Separate stage so the ~400 MB of -dev headers and build tooling they need
# (bison/flex, X11/cairo/Tcl/Tk devel) never reaches the shipped image; only
# the installed prefixes are copied forward.
# ---------------------------------------------------------------------------
FROM ${LOOM_WORKER_IMAGE}:${LOOM_WORKER_VERSION} AS toolchain

USER root
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
ARG DEBIAN_FRONTEND=noninteractive

# build-essential, git and curl already ship in the loom-worker base.
RUN apt-get update -qq \
    && apt-get install -y --no-install-recommends \
        bison \
        flex \
        libcairo2-dev \
        libx11-dev \
        libxcb1-dev \
        libxpm-dev \
        libxrender-dev \
        tcl-dev \
        tk-dev \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# -- ngspice ---------------------------------------------------------------
# Built from source, not apt: Ubuntu's package predates the `ngspice_min_major:
# 46` floor that gf180-sar-adc's and gf180-trng's sim harnesses refuse to run
# below. Version, digest and configure flags come from pdk-versions.json, which
# lifted them from gf180-trng's pdk-nightly.yml (a real, nightly-green build of
# this exact tarball). The `min_major` floor is re-asserted in the same layer
# that builds it, so a build that silently produced an older ngspice fails red
# rather than shipping.
ARG NGSPICE_VERSION
ARG NGSPICE_MIN_MAJOR
ARG NGSPICE_SHA256
ARG NGSPICE_URL
ARG NGSPICE_CONFIGURE_FLAGS
RUN set -euo pipefail; \
    : "${NGSPICE_VERSION:?build with docker/eda-sim/build.sh -- pins come from pdk-versions.json}"; \
    : "${NGSPICE_SHA256:?build with docker/eda-sim/build.sh -- pins come from pdk-versions.json}"; \
    : "${NGSPICE_URL:?build with docker/eda-sim/build.sh -- pins come from pdk-versions.json}"; \
    : "${NGSPICE_MIN_MAJOR:?build with docker/eda-sim/build.sh -- pins come from pdk-versions.json}"; \
    tarball="/tmp/ngspice-${NGSPICE_VERSION}.tar.gz"; \
    curl -fsSL --retry 5 --retry-all-errors --retry-delay 5 -o "${tarball}" "${NGSPICE_URL}"; \
    echo "${NGSPICE_SHA256}  ${tarball}" | sha256sum --check --strict; \
    tar xzf "${tarball}" -C /tmp; \
    cd "/tmp/ngspice-${NGSPICE_VERSION}"; \
    ./configure --prefix=/opt/eda/ngspice ${NGSPICE_CONFIGURE_FLAGS}; \
    make -j"$(nproc)"; \
    make install; \
    cd /; \
    rm -rf "/tmp/ngspice-${NGSPICE_VERSION}" "${tarball}"; \
    out="$(/opt/eda/ngspice/bin/ngspice --version)"; \
    printf '%s\n' "${out}" | head -n 3; \
    major="$(printf '%s\n' "${out}" | sed -n 's/.*ngspice-\([0-9][0-9]*\).*/\1/p' | head -n 1)"; \
    if [[ -z "${major}" || "${major}" -lt "${NGSPICE_MIN_MAJOR}" ]]; then \
        echo "FAIL: ngspice >= ${NGSPICE_MIN_MAJOR} required, found: ${major:-unparseable}" >&2; \
        exit 1; \
    fi

# -- xschem ----------------------------------------------------------------
# The schematic netlister. Built from source at the pinned tag so the image
# matches the block repos' `xschem_tag` pin exactly rather than tracking a
# distro version (a distro bump changes netlist formatting, which those repos'
# schematic-vs-netlist staleness guards compare against committed output).
#
# The resolved commit is asserted against the pin: a re-pointed upstream tag
# fails the build instead of silently changing what is baked.
ARG XSCHEM_REPO
ARG XSCHEM_TAG
ARG XSCHEM_COMMIT
RUN set -euo pipefail; \
    : "${XSCHEM_REPO:?build with docker/eda-sim/build.sh -- pins come from pdk-versions.json}"; \
    : "${XSCHEM_TAG:?build with docker/eda-sim/build.sh -- pins come from pdk-versions.json}"; \
    : "${XSCHEM_COMMIT:?build with docker/eda-sim/build.sh -- pins come from pdk-versions.json}"; \
    git clone --depth 1 --branch "${XSCHEM_TAG}" "${XSCHEM_REPO}" /tmp/xschem; \
    cd /tmp/xschem; \
    resolved="$(git rev-parse HEAD)"; \
    if [[ "${resolved}" != "${XSCHEM_COMMIT}" ]]; then \
        echo "FAIL: xschem tag ${XSCHEM_TAG} resolved to ${resolved}, pinned to ${XSCHEM_COMMIT}" >&2; \
        exit 1; \
    fi; \
    ./configure --prefix=/opt/eda/xschem; \
    make -j"$(nproc)"; \
    make install; \
    cd /; \
    rm -rf /tmp/xschem

# ---------------------------------------------------------------------------
# Stage 2: the shipped image.
# ---------------------------------------------------------------------------
FROM ${LOOM_WORKER_IMAGE}:${LOOM_WORKER_VERSION}

USER root
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
ARG DEBIAN_FRONTEND=noninteractive

# Runtime shared libraries only -- no -dev packages. tcl/tk + the X client
# libraries are xschem's link-time dependencies; it still netlists headlessly
# (`xschem -q -n -x`), no display and no Xvfb required. zstd is what ciel
# unpacks its `.tar.zst` PDK release assets with.
RUN apt-get update -qq \
    && apt-get install -y --no-install-recommends \
        libcairo2 \
        libx11-6 \
        libxcb1 \
        libxpm4 \
        libxrender1 \
        python3-venv \
        tcl \
        tk \
        zstd \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

COPY --from=toolchain /opt/eda /opt/eda

# -- Python tooling --------------------------------------------------------
# A venv rather than `pip install --break-system-packages`: Ubuntu 24.04 is
# PEP 668 externally-managed, and the fleet's sim harnesses legitimately need
# to add packages (scipy, numpy) at runtime. The venv goes FIRST on PATH and is
# chowned to the runtime user, so `pip install <x>` just works in a running
# container without root and without --break-system-packages.
#
#   ciel           the PDK version manager (successor to volare, whose release
#                  feed stopped in Aug 2025). Fetches at RUNTIME; nothing it
#                  downloads is baked here.
#   klayout-tools  this repo's own `klt`, the DRC/LVS driver the block repos'
#                  layout-verification stages invoke. Brings the KLayout Python
#                  module headless -- no GUI, no Qt, no apt package.
ARG CIEL_VERSION
ARG KLAYOUT_TOOLS_VERSION
RUN set -euo pipefail; \
    : "${CIEL_VERSION:?build with docker/eda-sim/build.sh -- pins come from pdk-versions.json}"; \
    : "${KLAYOUT_TOOLS_VERSION:?build with docker/eda-sim/build.sh -- pins come from pdk-versions.json}"; \
    python3 -m venv /opt/eda/venv; \
    /opt/eda/venv/bin/pip install --no-cache-dir --disable-pip-version-check --upgrade pip; \
    /opt/eda/venv/bin/pip install --no-cache-dir --disable-pip-version-check \
        "ciel==${CIEL_VERSION}" \
        "klayout-tools==${KLAYOUT_TOOLS_VERSION}"; \
    chown -R loom: /opt/eda/venv

# -- PDK cache mountpoint --------------------------------------------------
# Pre-created and owned by the runtime user, and that is load-bearing rather
# than tidy: Docker seeds a NEW named volume from whatever the image has at the
# mount path, ownership included. Without this directory the volume is created
# root-owned and ciel dies with `[Errno 13] Permission denied:
# /home/loom/.ciel/ciel` on the first fetch -- observed live while building
# this image, which is why it is an explicit step and not an accident of the
# base image's layout. (A BIND mount still takes the host directory's
# ownership; see docker/eda-sim/README.md.)
RUN mkdir -p /home/loom/.ciel && chown loom: /home/loom/.ciel

COPY pdk-versions.json /opt/eda/pdk-versions.json
COPY eda-sim-fetch-pdk /usr/local/bin/eda-sim-fetch-pdk
COPY smoke.sh /opt/eda/smoke.sh
RUN chmod 0755 /usr/local/bin/eda-sim-fetch-pdk /opt/eda/smoke.sh

ENV PATH="/opt/eda/venv/bin:/opt/eda/ngspice/bin:/opt/eda/xschem/bin:${PATH}"

# PDK_ROOT is deliberately NOT set: ciel installs into ~/.ciel/<variant> when
# it is unset, which is the search root every audited harness already probes.
# Setting it here would silently redirect installs and break that convention.
ENV EDA_SIM_MANIFEST="/opt/eda/pdk-versions.json"

ARG EDA_SIM_VERSION
ARG LOOM_WORKER_IMAGE
ARG LOOM_WORKER_VERSION
LABEL org.opencontainers.image.title="eda-sim" \
      org.opencontainers.image.description="EDA simulation overlay (ngspice, xschem, ciel, klt) on the loom-worker base. PDK fetched at runtime, never baked." \
      org.opencontainers.image.source="https://github.com/2AMLogic/klayout-tools" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${EDA_SIM_VERSION}" \
      org.opencontainers.image.base.name="${LOOM_WORKER_IMAGE}:${LOOM_WORKER_VERSION}"

USER loom
WORKDIR /workspace
CMD ["/bin/bash"]
