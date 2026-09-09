# `eda-sim` — the EDA simulation overlay image

A container image for fleet worker nodes that need the analog/EDA toolchain:

```
ghcr.io/rjwalters/loom-worker:<pin>     orchestration base (owned by rjwalters/loom)
  + ngspice, xschem, ciel, klt          domain toolchain (owned here)
  = ghcr.io/2amlogic/eda-sim:<version>
```

Two layers on purpose: orchestration is generic and belongs to Loom; the
analog toolchain is domain tooling and belongs to this repo (issue #509).

## The contract

| | Baked into the image | Fetched at runtime |
|---|---|---|
| ngspice | yes — built from source at the pinned version | |
| xschem | yes — built from source at the pinned tag | |
| ciel (PDK version manager) | yes | |
| `klt` (this repo's CLI, + headless KLayout) | yes | |
| **the PDK tree** | **never** | yes — `eda-sim-fetch-pdk`, into `~/.ciel` |

**The PDK tree is never baked.** A single family is 7–9 GB; baking it would
blow the ~2 GB size ceiling and weld PDK versions to image releases. The image
carries the *fetch mechanism* plus pinned commits; the tree lands in
`~/.ciel/<variant>` at first use. CI asserts both halves of this: an image-size
check on every build, and a scheduled job that fetches a PDK at runtime and
simulates against it.

**`magic` and `netgen` are deliberately absent.** Every 2AMLogic
`gf180-*`/`sky130-*` block repo's CI was audited (2026-08-04, spot-checked
2026-09-09) and none of them invoke either; this repo's own LVS engine
(`src/klayout_tools/lvs.py`) is netlist-vs-netlist and wires up no `magic`
extraction backend. Add them only when a real repo's CI is found to need them.

## Using it

```bash
# One-off, no PDK
docker run --rm ghcr.io/2amlogic/eda-sim ngspice -v

# With a persistent PDK cache (strongly recommended — the fetch is ~1 GB)
docker volume create eda-pdk-cache
docker run --rm -v eda-pdk-cache:/home/loom/.ciel \
    ghcr.io/2amlogic/eda-sim eda-sim-fetch-pdk gf180mcu

# Then run a real job against it
docker run --rm -v eda-pdk-cache:/home/loom/.ciel -v "$PWD:/workspace" \
    ghcr.io/2amlogic/eda-sim ngspice -b sim/deck.sp
```

`eda-sim-fetch-pdk` reads the baked manifest at `/opt/eda/pdk-versions.json`:

```
eda-sim-fetch-pdk [sky130|gf180mcu] [--commit <sha>] [--library <name>]...
                  [--all-libraries] [--print-plan] [--print-path]
```

`--print-plan` emits the resolved fetch as JSON (this repo's "JSON is the
contract" rule) without downloading anything, so a caller can assert on the
pin it is about to install.

### Things the image deliberately does *not* set

- **`PDK_ROOT` is unset.** `ciel` installs into `~/.ciel/<variant>` when it is
  unset — the search root every audited fleet harness already probes. Setting
  it redirects the install somewhere those harnesses will not look, so
  `eda-sim-fetch-pdk` refuses to run when it is set.
- **No `ENTRYPOINT`.** Same shape as the base image: `CMD ["/bin/bash"]`, so
  any command can be passed through.

### Mounting the PDK cache

`/home/loom/.ciel` exists in the image, owned by the runtime user. That is
load-bearing: Docker seeds a **new named volume** from whatever the image has
at the mount path, ownership included, so `-v eda-pdk-cache:/home/loom/.ciel`
lands writable. Without it the volume is created root-owned and `ciel` dies
with `[Errno 13] Permission denied: /home/loom/.ciel/ciel` on the first fetch
(observed live while building this image).

A **bind** mount takes the *host* directory's ownership instead, so if you
bind-mount a host path, `chown 1000:1000` it first (or pass `--user`).

### Runtime Python environment

`/opt/eda/venv` is first on `PATH` and is owned by the runtime user (`loom`),
so `pip install scipy` inside a running container works without root and
without `--break-system-packages` — which matters because Ubuntu 24.04 is
PEP 668 externally-managed and the fleet's sim harnesses legitimately add
packages at run time. `python3`, `pip`, `klt` and `ciel` all resolve to that
venv.

## The pins

`pdk-versions.json` is the **single source of truth**. Nothing in the
`Dockerfile` or the publish workflow duplicates a version literal: `build.sh`
reads the manifest and passes each pin as a `--build-arg`, and the Dockerfile's
`ARG`s are declared *without defaults* so a build that bypasses `build.sh`
fails loudly instead of baking a stale version.
`tests/test_eda_sim_image.py` enforces that no-duplicate-literals property, so
a future edit cannot quietly reintroduce drift.

| Pin | Value | Where it came from |
|---|---|---|
| base image | `ghcr.io/rjwalters/loom-worker:0.19.0` | multi-arch (amd64 + arm64) OCI index; verified live against the GHCR registry API 2026-09-09 |
| ngspice | `47` (floor `min_major: 46`) | floor + configure flags from `gf180-sar-adc`/`gf180-trng`; **version and sha256 re-derived here**, see below |
| xschem | tag `3.4.7` → commit `92dd8fe5…` | `gf180-sar-adc`'s `sim/toolchain.json`; tag→commit re-verified against the upstream GitHub API 2026-09-09 |
| ciel | `2.6.1` | `gf180-trng`'s `CIEL_VERSION`; latest on PyPI 2026-09-09 |
| klayout-tools | `0.4.0` | latest PyPI release of this repo 2026-09-09 |
| open_pdks (both families) | `f6eeac7dad085ffcc829ccfd721f7b4ce39edcf7` | `gf180-trng`'s `PDK_VERSION` |

**Bumping a pin = editing `pdk-versions.json`** (and bumping `image.version` in
the same commit). Bumping ngspice means updating its `version` *and* `sha256`
together.

### Why ngspice is built from source

Ubuntu's `ngspice` package is older than the `ngspice_min_major: 46` floor that
`gf180-sar-adc`'s and `gf180-trng`'s harnesses refuse to run below. Several
fleet repos still `apt-get install ngspice`; building the pinned release here
satisfies both groups from one image. Configure flags match `gf180-trng`'s
verified build: batch-mode only (`--with-x=no --with-readline=no`), with XSPICE
enabled because open_pdks device models may use its extensions.

**Why 47 and not `gf180-trng`'s 46.** The obvious move was to copy that repo's
`NGSPICE_VERSION: 46` and its sha256 verbatim. Re-verifying before baking them
in showed the pin is stale: upstream has moved 46 into
`ng-spice-rework/old-releases/46/`, so the URL that workflow pins
(`ng-spice-rework/46/ngspice-46.tar.gz`) **now 404s** — confirmed live
2026-09-09, and reproduced as a real `docker build` failure before the pin was
changed. That nightly is presumably surviving on an `actions/cache` hit and
would fail on a cold cache. 47 is the current release at the stable path and
clears the same `min_major: 46` floor, so the image pins it, with a sha256
computed from the actual 13,105,136-byte tarball rather than copied.

If a future bump 404s the same way, the tarball has almost certainly moved to
`ng-spice-rework/old-releases/<version>/` — adjust `url_template`.

### Why `ciel` and not `volare`

`volare`'s PDK release feed stopped in August 2025; `ciel` is the maintained
successor (FOSSi Foundation) with the same open_pdks build format and CLI
shape. `gf180-trng` has already migrated; the other block repos still pin
`volare`.

**This is not a free swap, and the manifest records why.** Verified live
2026-09-09 against the `fossi-foundation/ciel-releases` API: the older
`c6d73a35f524070e85faff4a6a9eef49553ebc2b` commit that the still-on-volare
repos pin has **no ciel release** — both `sky130-c6d73a35…` and
`gf180mcu-c6d73a35…` 404. `f6eeac7d…` resolves for *both* families. So a repo
moving from volare to ciel also moves its open_pdks pin, exactly as
`gf180-trng` did. A caller that must have the old commit can override
per-invocation (`eda-sim-fetch-pdk --commit <sha>`) or `pip install volare`
into the image's writable venv.

### Library selection

`pdks.families.<f>.libraries` is the minimum set the audited harnesses actually
need: the primitive library (device models — every ngspice run needs it) plus
the one digital standard-cell library `klt synthesize` / `klt place-and-route`
map onto. `ciel` always fetches the `common` tarball as well, which is where
the ngspice model decks and the xschem symbol library live. Fetching the full
default set instead pulls ~5 GB of unused IO/SRAM/alt-cell libraries;
`--all-libraries` opts into that.

## Building and testing locally

```bash
docker/eda-sim/build.sh                    # host arch, tags from the manifest
docker/eda-sim/build.sh --print-args       # just show the resolved pins
docker/eda-sim/build.sh --dry-run          # show the docker command

docker run --rm ghcr.io/2amlogic/eda-sim:latest /opt/eda/smoke.sh
docker run --rm -v eda-pdk-cache:/home/loom/.ciel \
    ghcr.io/2amlogic/eda-sim:latest /opt/eda/smoke.sh --with-pdk gf180mcu
```

`smoke.sh` is baked into the image, so a fleet node can re-run exactly what CI
ran with no checkout. Its default tier is seconds and needs no network: every
tool answers at its pinned version, `klayout.db` imports headlessly, no PDK
tree is present, the venv is user-writable, and ngspice **solves** a resistive
divider (the answer is checked numerically — a `--version` banner only proves
the binary links). `--with-pdk <family>` adds the runtime `ciel` fetch and a
saturated-NMOS `.op` against the fetched device models, asserting a non-zero
drain current.

### What was verified locally (amd64, 2026-09-09)

The image was built and all three tiers run on a real Docker daemon while this
was written:

```
image size: 369 MB (ceiling 2048 MB)
smoke.sh                       all checks passed
smoke.sh --with-pdk gf180mcu   i(vdd) = -2.74699e-03   (nfet_03v3 @ 3.3 V)
smoke.sh --with-pdk sky130     i(vdd) = -1.66032e-03   (nfet_01v8 @ 1.8 V)
```

Only `linux/amd64` was exercised locally; `linux/arm64` is built and smoke-
tested by CI on a native arm64 runner.

## CI

`.github/workflows/publish-eda-sim-image.yml`:

| Trigger | What runs |
|---|---|
| `pull_request` touching `docker/eda-sim/**` | build + size assert + `smoke.sh`, both arches, no push |
| `push` of a `v*` tag | the above, then push per-arch tags and join them into a multi-arch manifest |
| weekly schedule (Mon 05:11 UTC) | the above without publishing, **plus** the PDK-backed simulation stage |
| `workflow_dispatch` | same, with `publish` / `run-pdk-sim` as explicit inputs |

Published tags: `<image.version>`, `latest`, `sha-<12-char git sha>`, plus the
intermediate `<image.version>-amd64` / `-arm64` per-arch tags the manifest is
assembled from.

Each architecture is built and smoke-tested on **native** hardware
(`ubuntu-latest` / `ubuntu-24.04-arm`) rather than one job with QEMU: stage 1
compiles ngspice and xschem from source, which under arm64 emulation is a
30-minutes-plus proposition that routinely trips job timeouts.

## Known gap

Issue #509's acceptance criteria include *"one real sim from a gf180 repo runs
green inside the container."* A real gf180mcu device-model simulation does run
green (verified locally, and by the weekly `pdk-sim` job): the deck is the same
shape as `2AMLogic/gf180-trng`'s `corner-sanity-nfet-id` testbench, and the PDK
under it is fetched at runtime with `ciel`.

What is *not* covered is running a **block repo's own harness** end-to-end
inside the image (`sim/selftest.sh --require-pdk`, `sim/bin/corner-run.py`, …).
That is a cross-repo change — it needs those repos' CI to switch to
`container: ghcr.io/2amlogic/eda-sim:<version>` — and cannot be done from here.
See the follow-up issue linked from #509.
