#!/usr/bin/env python3
"""Generate a gallery block's own geode-fem site export (issue #958, Epic
#840 Phase 3a) -- `blocks/<slug>/output/<slug>.em-export.json`, field data
for a *real gallery project's* geometry, sitting next to that block's
`layout.json` and `signals/` waveforms as one more artifact of the same
design.

**What is new here vs. `examples/em/interconnect_coupling/`.** That script
(issue #842) proved the GDS->geode-fem seam, but on one hard-coded geometry
family: the two symmetric `met1` power rails of a standard cell, in a
symmetric grounded box with no notion of where the metal sits in the
process stack. A real analog canary has none of those properties, so this
script generalises the seam along three axes and points it at
`blocks/<slug>/`:

1. **Real nets, not two hand-picked rectangles.** The block's routing layer
   is merged into nets (connected geometry), each net named from the GDS's
   own label texts when it carries one. The solved structure is a
   *bundle* -- a contiguous group of nets on one cut line through the
   layout -- selected automatically as the bundle that packs the most real
   nets (at least one of them named) into the smallest cross-section, over
   the longest uniform parallel run. `--cut-position` / `--run-axis` /
   `--conductor` override the search when a human wants a specific
   structure.
2. **Real process stackup.** The conductors sit at their true height above
   a grounded substrate plane, read from the *installed* open PDK's own
   published metal stack (`libs.tech/magic/<variant>.tech`'s `height
   <layer> <bottom> <thickness>` line), falling back to the transcribed
   value in `PDK_PROFILES` below when no PDK install is resolvable.
   Substrate capacitance dominates on-chip routing, so this is what makes
   the extracted matrix comparable to the PDK's own parasitic model.
3. **Real widths and spacings.** The driver's structured grid is
   *breakpoint-aligned*: every conductor edge from the GDS is exactly a
   grid plane, so a 0.24 um drawn space is solved as 0.24 um rather than
   quantised to the mesh step.

**Still not a general GDS-to-mesh pipeline** -- same boundary
`interconnect_coupling` drew, and for the same reason (that is MoM #701 /
FEM #708 epic territory). This is a 2.5-D extrusion of one real
cross-section: honest about what it models, and explicit in
`provenance.mesh_reduction` about what it does not.

Usage (from a sibling geode-fem checkout, per
`docs/design/em-site-export-format.md`):

    git clone https://github.com/rjwalters/geode-fem.git /tmp/geode-fem
    uv run python3 examples/em/block_coupling/generate.py \\
        --block sky130-bandgap --geode-fem-dir /tmp/geode-fem

`--dry-run` prints the geometry the search selected without needing a
geode-fem checkout at all (useful when adding a new block); `--skip-run`
re-converts an already-generated driver result. See this directory's
`README.md` for the recipe for adding the next block.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _DIR.parent.parent.parent
_DRIVER_SRC = _DIR / "geode_driver"

sys.path.insert(0, str(_REPO_ROOT / "src"))
from klayout_tools._provenance import sha256_file  # noqa: E402

SCHEMA_VERSION = 1
BENCHMARK = "block_coupling"

# Typical sky130/gf180mcu inter-metal SiO2 dielectric (approximate, uniform).
DEFAULT_EPS_R = 4.0


class GenerateError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Per-PDK profile: which layer carries the routing, and where it sits.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PdkProfile:
    """Everything that is PDK-specific about one block's solve.

    `metal_index` indexes the PDK's own curated `ParasiticsDeck.metals`
    tuple (`klayout_tools.decks.get_parasitics_deck`), which is this repo's
    in-tree transcription of that open PDK's published area/fringe
    capacitance coefficients -- reused here as an independent sanity oracle
    for the FEM self-capacitance.

    `magic_height_layer` is the layer name in the installed PDK's magic
    tech file whose `height <layer> <bottom_um> <thickness_um>` line gives
    the conductor's real position in the stack; `stackup_fallback` is that
    same line transcribed, used only when no PDK install resolves.
    """

    deck: str
    pdk_variant: str
    routing_layer: tuple[int, int]
    label_layer: tuple[int, int]
    metal_index: int
    magic_height_layer: str
    stackup_fallback: tuple[float, float]


PDK_PROFILES: dict[str, PdkProfile] = {
    "sky130": PdkProfile(
        deck="sky130",
        pdk_variant="sky130A",
        routing_layer=(68, 20),  # met1.drawing
        label_layer=(68, 5),  # met1.label
        metal_index=1,  # PARASITICS.metals = (li1, met1, met2, ...)
        magic_height_layer="allm1",
        # sky130A.tech: `height allm1 1.3761 0.36`.
        stackup_fallback=(1.3761, 0.36),
    ),
    "gf180mcu": PdkProfile(
        deck="gf180mcu",
        pdk_variant="gf180mcuD",
        routing_layer=(34, 0),  # Metal1
        label_layer=(34, 10),  # Metal1 label/pin purpose
        metal_index=0,  # PARASITICS.metals = (Metal1, Metal2, ...)
        magic_height_layer="allm1",
        # gf180mcuD.tech: `height allm1 1.23 0.55`.
        stackup_fallback=(1.23, 0.55),
    ),
}


def profile_for_slug(slug: str) -> PdkProfile:
    """Best-effort PDK guess from a block's slug, the same prefix match
    `scripts/_gallery_common.py`'s `infer_layer_names` uses (canary repos
    carry no explicit `pdk` field)."""
    if slug.startswith("gf180"):
        return PDK_PROFILES["gf180mcu"]
    if slug.startswith("sky130"):
        return PDK_PROFILES["sky130"]
    raise GenerateError(
        f"no PDK profile for block slug {slug!r} -- add one to PDK_PROFILES "
        "(see this directory's README.md)"
    )


def resolve_stackup(profile: PdkProfile) -> dict[str, Any]:
    """Return `{bottom_um, thickness_um, source, source_file}` for the
    routing layer, preferring the *installed* open PDK's own magic tech
    file over the transcribed fallback."""
    try:
        from klayout_tools import pdk as pdk_mod

        found = pdk_mod.find_pdk(profile.pdk_variant)
        magic_dir = found["assets"].get("magic")
        if magic_dir:
            tech = Path(magic_dir) / f"{found['variant']}.tech"
            if tech.is_file():
                pattern = re.compile(
                    rf"^\s*height\s+{re.escape(profile.magic_height_layer)}\s+"
                    r"(-?[\d.]+)\s+([\d.]+)",
                    re.MULTILINE,
                )
                m = pattern.search(tech.read_text(encoding="utf-8", errors="replace"))
                if m:
                    return {
                        "bottom_um": float(m.group(1)),
                        "thickness_um": float(m.group(2)),
                        "source": "installed-pdk",
                        "source_file": (
                            f"{found['variant']}/libs.tech/magic/{found['variant']}.tech"
                            f" (height {profile.magic_height_layer})"
                        ),
                    }
    except Exception:  # noqa: BLE001 -- any resolution failure falls back
        pass
    bottom, thickness = profile.stackup_fallback
    return {
        "bottom_um": bottom,
        "thickness_um": thickness,
        "source": "transcribed-fallback",
        "source_file": (
            f"{profile.pdk_variant}/libs.tech/magic/{profile.pdk_variant}.tech"
            f" (height {profile.magic_height_layer}, transcribed into "
            "examples/em/block_coupling/generate.py)"
        ),
    }


# --------------------------------------------------------------------------
# Real geometry, read from the block's own committed GDS.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Interval:
    """One net's occupancy of the cut line: `[lo, hi]` um on the
    cross-section axis, tagged with the net it belongs to."""

    net: int
    name: str | None
    lo: float
    hi: float


@dataclass
class Nets:
    polys: list[Any]
    names: dict[int, str]
    dbu: float
    bbox: Any


def read_nets(gds_path: Path, profile: PdkProfile) -> Nets:
    """Merge the routing layer into nets (connected geometry) and name each
    one from the GDS's own label texts, when it carries one."""
    import pya

    layout = pya.Layout()
    layout.read(str(gds_path))
    top = layout.top_cell()
    if top is None:
        raise GenerateError(f"{gds_path}: no top cell")

    region = pya.Region(top.begin_shapes_rec(layout.layer(*profile.routing_layer)))
    region.merge()
    polys = list(region.each())
    if not polys:
        raise GenerateError(
            f"{gds_path}: routing layer "
            f"{profile.routing_layer[0]}/{profile.routing_layer[1]} is empty"
        )

    names: dict[int, str] = {}
    label_iter = top.begin_shapes_rec(layout.layer(*profile.label_layer))
    while not label_iter.at_end():
        shape = label_iter.shape()
        if shape.is_text():
            text = shape.text.transformed(label_iter.trans())
            point = pya.Point(text.x, text.y)
            for i, poly in enumerate(polys):
                if poly.inside(point):
                    names.setdefault(i, shape.text_string)
                    break
        label_iter.next()

    return Nets(polys=polys, names=names, dbu=layout.dbu, bbox=region.bbox())


def cut_intervals(nets: Nets, run_axis: str, position_um: float) -> list[Interval]:
    """Every net interval on the cut line perpendicular to `run_axis`.

    A cut at `run_axis == "x"` is the vertical line `x = position_um`; the
    resulting intervals are `y` ranges, and the conductors run along `x`.
    """
    import pya

    dbu = nets.dbu
    pos = int(round(position_um / dbu))
    bbox = nets.bbox
    if run_axis == "x":
        line_box = pya.Box(pos - 1, bbox.bottom, pos + 1, bbox.top)
    else:
        line_box = pya.Box(bbox.left, pos - 1, bbox.right, pos + 1)
    line = pya.Region(line_box)

    out: list[Interval] = []
    for i, poly in enumerate(nets.polys):
        if not poly.bbox().overlaps(line_box):
            continue
        for piece in (pya.Region(poly) & line).each_merged():
            b = piece.bbox()
            lo, hi = (b.bottom, b.top) if run_axis == "x" else (b.left, b.right)
            out.append(
                Interval(net=i, name=nets.names.get(i), lo=lo * dbu, hi=hi * dbu)
            )
    out.sort(key=lambda iv: (iv.lo, iv.hi))
    return out


def window_signature(window: list[Interval]) -> tuple:
    """Identity of a bundle's cross-section, to nanometre precision -- two
    cut positions with equal signatures see the same structure."""
    return tuple((iv.net, round(iv.lo, 3), round(iv.hi, 3)) for iv in window)


def score_window(window: list[Interval], prefer: str | None = None) -> tuple:
    """Rank candidate bundles: a bundle containing `--prefer-net` first (the
    escape hatch for "solve *this* net's neighbourhood"), then most *named*
    nets (a bundle whose story can be told), then most nets, then most
    conductors, then tightest."""
    named = {iv.name for iv in window if iv.name}
    nets = {iv.net for iv in window}
    span = window[-1].hi - window[0].lo
    preferred = 1 if prefer is not None and prefer in named else 0
    return (preferred, len(named), len(nets), len(window), -span)


def best_window(
    intervals: list[Interval],
    *,
    max_conductors: int,
    max_span_um: float,
    prefer: str | None = None,
) -> list[Interval] | None:
    """The best contiguous group of conductors on one cut line."""
    best: tuple[tuple, list[Interval]] | None = None
    n = len(intervals)
    for i in range(n):
        for j in range(i + 2, min(n, i + max_conductors) + 1):
            window = intervals[i:j]
            if len({iv.net for iv in window}) < 2:
                continue
            if not any(iv.name for iv in window):
                continue
            if window[-1].hi - window[0].lo > max_span_um:
                continue
            score = score_window(window, prefer)
            if best is None or score > best[0]:
                best = (score, window)
    return None if best is None else best[1]


@dataclass
class Selection:
    run_axis: str
    cut_position_um: float
    window: list[Interval]
    run_start_um: float
    run_end_um: float

    @property
    def run_length_um(self) -> float:
        return self.run_end_um - self.run_start_um


def _restricted_signature(
    nets: Nets, run_axis: str, position_um: float, window: list[Interval]
) -> tuple:
    """The signature of `window`'s own nets, clipped to `window`'s span, at
    another cut position -- the test for "the bundle still looks like this
    here"."""
    lo, hi = window[0].lo, window[-1].hi
    keys = {iv.net for iv in window}
    here = [
        iv
        for iv in cut_intervals(nets, run_axis, position_um)
        if iv.net in keys and iv.lo >= lo - 1e-6 and iv.hi <= hi + 1e-6
    ]
    return window_signature(here)


def _measure_run(
    nets: Nets,
    *,
    run_axis: str,
    window: list[Interval],
    coarse_lo: float,
    coarse_hi: float,
    limits: tuple[float, float],
    run_step_um: float,
) -> tuple[float, float]:
    """Walk outward from a coarse stretch until the bundle's cross-section
    stops looking the same -- the real length over which extruding this one
    cut is a faithful model of the layout."""
    lo_limit, hi_limit = limits
    signature = window_signature(window)
    run_lo = coarse_lo
    probe = coarse_lo - run_step_um
    while probe > lo_limit and (
        _restricted_signature(nets, run_axis, probe, window) == signature
    ):
        run_lo = probe
        probe -= run_step_um
    run_hi = coarse_hi
    probe = coarse_hi + run_step_um
    while probe < hi_limit and (
        _restricted_signature(nets, run_axis, probe, window) == signature
    ):
        run_hi = probe
        probe += run_step_um
    return run_lo, run_hi


def select_bundle(
    nets: Nets,
    *,
    run_axis: str,
    cut_step_um: float,
    run_step_um: float,
    max_conductors: int,
    max_span_um: float,
    min_run_um: float,
    min_aspect: float,
    prefer: str | None = None,
    max_refine: int = 8,
) -> Selection | None:
    """Scan cut positions along `run_axis` and return the best *extrudable*
    bundle: the one packing the most real nets into the tightest
    cross-section, subject to actually running that way far enough for a
    2.5-D extrusion to mean anything.

    The length filter is load-bearing, not a nicety. Without it the search
    happily "finds" a dense bundle where a set of perpendicular wires
    *terminate* -- a cross-section that exists for one micrometre and
    describes nothing about the layout when extruded.
    """
    dbu = nets.dbu
    lo_dbu, hi_dbu = (
        (nets.bbox.left, nets.bbox.right)
        if run_axis == "x"
        else (nets.bbox.bottom, nets.bbox.top)
    )
    lo, hi = lo_dbu * dbu, hi_dbu * dbu
    n_steps = max(int(math.floor((hi - lo) / cut_step_um)), 1)
    positions = [lo + cut_step_um * (i + 0.5) for i in range(n_steps)]

    scanned: list[tuple[float, list[Interval], tuple]] = []
    for pos in positions:
        window = best_window(
            cut_intervals(nets, run_axis, pos),
            max_conductors=max_conductors,
            max_span_um=max_span_um,
            prefer=prefer,
        )
        if window is not None:
            scanned.append((pos, window, window_signature(window)))
    if not scanned:
        return None

    # Every distinct bundle the scan saw, with its longest contiguous
    # coarse stretch -- not just the top-scoring one, since a slightly
    # narrower bundle that runs 100x further is the better model.
    stretches: dict[tuple, tuple[float, float, list[Interval]]] = {}
    start = 0
    while start < len(scanned):
        end = start
        while (
            end + 1 < len(scanned)
            and scanned[end + 1][2] == scanned[start][2]
            and scanned[end + 1][0] - scanned[end][0] <= cut_step_um * 1.5
        ):
            end += 1
        signature = scanned[start][2]
        span = scanned[start][0], scanned[end][0], scanned[start][1]
        previous = stretches.get(signature)
        if previous is None or (span[1] - span[0]) > (previous[1] - previous[0]):
            stretches[signature] = span
        start = end + 1

    ranked = sorted(
        stretches.values(),
        key=lambda s: (score_window(s[2], prefer), s[1] - s[0]),
        reverse=True,
    )

    best: Selection | None = None
    best_key: tuple | None = None
    for coarse_lo, coarse_hi, window in ranked[:max_refine]:
        run_lo, run_hi = _measure_run(
            nets,
            run_axis=run_axis,
            window=window,
            coarse_lo=coarse_lo,
            coarse_hi=coarse_hi,
            limits=(lo, hi),
            run_step_um=run_step_um,
        )
        length = run_hi - run_lo
        span = window[-1].hi - window[0].lo
        if length < max(min_run_um, min_aspect * span):
            continue
        key = (score_window(window, prefer), length)
        if best_key is None or key > best_key:
            best_key = key
            best = Selection(
                run_axis=run_axis,
                cut_position_um=0.5 * (run_lo + run_hi),
                window=window,
                run_start_um=run_lo,
                run_end_um=run_hi,
            )
    return best


#: The search inputs recorded into `provenance.geometry.selection`, so the
#: bundle a committed export was solved on can be re-derived from the block's
#: GDS alone (`tests/test_em_block_exports.py` does exactly that).
SEARCH_KEYS = (
    "run_axis",
    "cut_step_um",
    "run_step_um",
    "max_conductors",
    "max_span_um",
    "min_run_um",
    "min_aspect",
    "prefer_net",
)


def search_bundle(nets: Nets, params: dict[str, Any]) -> Selection | None:
    """Run the bundle search described by `params` (a `SEARCH_KEYS` dict) --
    the single entry point both `main()` and the artifact-reproducibility
    test go through, so a committed export's recorded selection replays
    exactly."""
    axes = ("x", "y") if params["run_axis"] == "auto" else (params["run_axis"],)
    prefer = params["prefer_net"]
    selections = []
    for axis in axes:
        selection = select_bundle(
            nets,
            run_axis=axis,
            cut_step_um=params["cut_step_um"],
            run_step_um=params["run_step_um"],
            max_conductors=params["max_conductors"],
            max_span_um=params["max_span_um"],
            min_run_um=params["min_run_um"],
            min_aspect=params["min_aspect"],
            prefer=prefer,
        )
        if selection is not None:
            selections.append(selection)
    if not selections:
        return None
    return max(
        selections, key=lambda s: (score_window(s.window, prefer), s.run_length_um)
    )


def conductors_from_window(window: list[Interval]) -> list[dict[str, Any]]:
    """Group a bundle's intervals into one electrode per *net* (a net that
    crosses the cut twice is one conductor with two intervals, because it is
    one net), preserving cross-section order."""
    order: list[int] = []
    grouped: dict[int, dict[str, Any]] = {}
    for iv in window:
        if iv.net not in grouped:
            order.append(iv.net)
            grouped[iv.net] = {
                "net": iv.net,
                "name": iv.name or f"net_{iv.net}",
                "named": bool(iv.name),
                "intervals": [],
            }
        grouped[iv.net]["intervals"].append([iv.lo, iv.hi])
    for entry in grouped.values():
        entry["width_um"] = sum(hi - lo for lo, hi in entry["intervals"])
    return [grouped[key] for key in order]


def choose_driven(conductors: list[dict[str, Any]]) -> str:
    """Drive the narrowest *named* conductor: on an analog block that is the
    signal net rather than a supply strap or a shield -- exactly the
    high-impedance node whose coupling is worth looking at."""
    named = [c for c in conductors if c["named"]]
    pool = named or conductors
    return min(pool, key=lambda c: (c["width_um"], c["name"]))["name"]


# --------------------------------------------------------------------------
# Driving the geode-fem checkout.
# --------------------------------------------------------------------------


def stage_driver(geode_fem_dir: Path) -> Path:
    dest = geode_fem_dir / "examples" / "block_coupling"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(_DRIVER_SRC, dest)
    return dest


def run_driver(
    geode_fem_dir: Path,
    *,
    conductors: list[dict[str, Any]],
    stackup: dict[str, Any],
    length_um: float,
    eps_r: float,
    side_margin_um: float,
    top_margin_um: float,
    step_um: float,
    n_x: int,
    driven: str,
    result_path: Path,
) -> None:
    args = [
        "cargo",
        "run",
        "-p",
        "block_coupling",
        "--release",
        "--",
        "--metal-bottom-um",
        repr(stackup["bottom_um"]),
        "--thickness-um",
        repr(stackup["thickness_um"]),
        "--length-um",
        repr(length_um),
        "--eps-r",
        repr(eps_r),
        "--side-margin-um",
        repr(side_margin_um),
        "--top-margin-um",
        repr(top_margin_um),
        "--step-um",
        repr(step_um),
        "--n-x",
        str(n_x),
        "--drive",
        driven,
        "--out",
        str(result_path),
    ]
    for c in conductors:
        spec = ",".join(f"{lo!r}:{hi!r}" for lo, hi in c["intervals"])
        args += ["--conductor", f"{c['name']}:{spec}"]
    print(f"[geode-fem] {geode_fem_dir}: {' '.join(args)}")
    subprocess.run(args, cwd=geode_fem_dir, check=True)


def geode_fem_commit(geode_fem_dir: Path) -> str:
    out = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=geode_fem_dir,
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


def geode_fem_version(geode_fem_dir: Path) -> str | None:
    text = (geode_fem_dir / "Cargo.toml").read_text(encoding="utf-8")
    m = re.search(r'^\s*version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return m.group(1) if m else None


# --------------------------------------------------------------------------
# PDK oracle cross-check (this repo's own curated open-PDK parasitics deck).
# --------------------------------------------------------------------------


def pdk_self_capacitance_oracle(
    profile: PdkProfile, conductor: dict[str, Any], length_um: float
) -> dict[str, Any] | None:
    """Independent sanity cross-check for the driven conductor's FEM
    self-capacitance: this repo's *own* curated open-PDK parasitics table
    (`klayout_tools.decks.get_parasitics_deck`, transcribed from that PDK's
    public magic `.tech` `defaultareacap`/`defaultperimeter` entries and
    already shipping as `klt extract --parasitics`'s coefficients), applied
    to the conductor's real drawn area and perimeter.

    That model is a first-order area+fringe capacitance *to the substrate*,
    while the FEM run additionally sees a grounded box top and side walls at
    a finite, modeled distance and the neighbouring bundle conductors held
    at 0 V -- all of which add capacitance. So this is an honest
    order-of-magnitude cross-check, not a strict oracle, the same band
    convention `benchmarks/electrostatic/results.toml`'s own
    `two_sphere_box` oracle documents for its own box truncation.
    """
    from klayout_tools.decks import get_parasitics_deck

    deck = get_parasitics_deck(profile.deck)
    metals = deck.metals
    if profile.metal_index >= len(metals):
        return None
    rc = metals[profile.metal_index]
    area_um2 = sum((hi - lo) * length_um for lo, hi in conductor["intervals"])
    perimeter_um = sum(
        2.0 * ((hi - lo) + length_um) for lo, hi in conductor["intervals"]
    )
    farad = 1e-15 * (rc.cap_area_ff_um2 * area_um2 + rc.cap_perim_ff_um * perimeter_um)
    return {
        "predicted_self_capacitance_farad": farad,
        "area_um2": area_um2,
        "perimeter_um": perimeter_um,
        "cap_area_ff_um2": rc.cap_area_ff_um2,
        "cap_perim_ff_um": rc.cap_perim_ff_um,
    }


# --------------------------------------------------------------------------
# Assembling the export.
# --------------------------------------------------------------------------


def build_export(
    *,
    geode_fem_dir: Path,
    driver_result: dict[str, Any],
    slug: str,
    profile: PdkProfile,
    selection: Selection,
    conductors: list[dict[str, Any]],
    search: dict[str, Any],
    stackup: dict[str, Any],
    driven: str,
    eps_r: float,
    side_margin_um: float,
    top_margin_um: float,
    step_um: float,
    gds_path: Path,
    source_commit: str,
) -> dict[str, Any]:
    slice_ = driver_result["slice"]
    # Round to a compact-but-lossless-for-visualization precision, the same
    # convention `examples/em/patch_antenna/generate.py`'s `_compact_mesh`
    # and `interconnect_coupling`'s own export use, to keep the committed
    # JSON a reasonable size.
    vertices = [[round(v[0], 4), round(v[1], 4), 0.0] for v in slice_["vertices_yz_um"]]
    phi_v = [float(f"{v:.6g}") for v in slice_["phi_v"]]
    e_vec_v = [[float(f"{c:.6g}") for c in v] for v in slice_["e_vec_v_per_um"]]

    cap = driver_result["capacitance"]
    names = cap["names"]
    matrix = cap["c_farad"]
    fem_self = {names[i]: matrix[i][i] for i in range(len(names))}

    driven_conductor = next(c for c in conductors if c["name"] == driven)
    oracle_terms = pdk_self_capacitance_oracle(
        profile, driven_conductor, selection.run_length_um
    )
    oracle: dict[str, Any] | None = None
    if oracle_terms is not None:
        predicted = oracle_terms["predicted_self_capacitance_farad"]
        fem = fem_self.get(driven)
        oracle = {
            "description": (
                "This repo's own curated open-PDK parasitics table for "
                f"{profile.deck} (klayout_tools.decks.get_parasitics_deck -- "
                "area + fringe coefficients transcribed from that PDK's public "
                "magic .tech `defaultareacap`/`defaultperimeter` entries, the "
                "same coefficients `klt extract --parasitics` ships), applied "
                f"to the `{driven}` conductor's real drawn area and perimeter. "
                "An honest order-of-magnitude cross-check, not a strict "
                "physical oracle: that model is capacitance to the substrate "
                "alone, while this solve additionally sees a grounded box top "
                "and side walls at a finite modeled distance plus the "
                "neighbouring bundle conductors held at 0 V, all of which add "
                "capacitance (same box-truncation caveat "
                "`benchmarks/electrostatic/results.toml`'s `two_sphere_box` "
                "oracle documents)."
            ),
            "source": (
                f"klayout_tools.decks.{profile.deck}.PARASITICS.metals"
                f"[{profile.metal_index}] (public, open-PDK-derived)"
            ),
            "conductor": driven,
            "predicted_self_capacitance_farad": predicted,
            "fem_self_capacitance_farad": fem,
            "rel_err": (
                abs(fem - predicted) / predicted
                if fem is not None and predicted
                else None
            ),
            "terms": {
                "area_um2": oracle_terms["area_um2"],
                "perimeter_um": oracle_terms["perimeter_um"],
                "cap_area_ff_um2": oracle_terms["cap_area_ff_um2"],
                "cap_perim_ff_um": oracle_terms["cap_perim_ff_um"],
            },
        }

    cross_axis = "y" if selection.run_axis == "x" else "x"
    layer_label = layer_name(profile)
    named_nets = sorted({c["name"] for c in conductors if c["named"]})

    return {
        "schema_version": SCHEMA_VERSION,
        "benchmark": BENCHMARK,
        "block": slug,
        "mesh": {"vertices": vertices, "cells": slice_["cells"]},
        "frames": [
            {
                "label": (
                    f"{driven} driven 1V, all other bundle conductors 0V "
                    f"(cross-section at {selection.run_axis} = "
                    f"{selection.cut_position_um:.2f} um)"
                ),
                "frequency_hz": None,
                "scalar": phi_v,
                "vector": e_vec_v,
            }
        ],
        "capacitance": {
            "conductors": names,
            "matrix_farad": matrix,
            "matrix_flux_diag_farad": cap["c_flux_diag_farad"],
            "max_rel_asymmetry": cap["max_rel_asymmetry"],
            "oracle": oracle,
        },
        "provenance": {
            "generator": {
                "repo": "https://github.com/rjwalters/geode-fem",
                "license": "MIT",
                "commit": geode_fem_commit(geode_fem_dir),
                "version": geode_fem_version(geode_fem_dir),
                "backend": (
                    "burn::backend::NdArray<f64, i32> (CPU; no GPU feature "
                    "compiled -- see geode-core's testing::TestBackend cfg "
                    "fallback)"
                ),
                "build_profile": "release",
                "driver": "examples/em/block_coupling/geode_driver",
            },
            "geometry": {
                "fixture": str(gds_path.relative_to(_REPO_ROOT)),
                "fixture_sha256": sha256_file(str(gds_path)),
                "description": (
                    f"A real routed bundle of the `{slug}` gallery block: the "
                    f"{len(conductors)} {layer_label} nets "
                    f"({', '.join(c['name'] for c in conductors)}) that run "
                    f"parallel along {selection.run_axis} for "
                    f"{selection.run_length_um:.2f} um "
                    f"({selection.run_axis} = {selection.run_start_um:.2f} .. "
                    f"{selection.run_end_um:.2f} um), read out of the block's "
                    "own committed GDS -- the same layout the gallery renders, "
                    "not a benchmark shape. Widths and spacings are the drawn "
                    "ones (the mesh is breakpoint-aligned to every conductor "
                    "edge); net names are the GDS's own label texts."
                ),
                "source_repo": "https://github.com/2AMLogic/klayout-tools",
                "source_commit": source_commit,
                "source_block": slug,
                "source_layer": (
                    f"{layer_label} ({profile.routing_layer[0]}/"
                    f"{profile.routing_layer[1]})"
                ),
                "named_nets": named_nets,
                "selection": search,
                "conductors": [
                    {
                        "name": c["name"],
                        "gds_labeled": c["named"],
                        "intervals_um": c["intervals"],
                        "width_um": c["width_um"],
                    }
                    for c in conductors
                ],
                "driven_conductor": driven,
            },
            "solve_parameters": {
                "run_axis": selection.run_axis,
                "cross_section_axis": cross_axis,
                "cut_position_um": selection.cut_position_um,
                "run_start_um": selection.run_start_um,
                "run_end_um": selection.run_end_um,
                "length_um": selection.run_length_um,
                "metal_bottom_um": stackup["bottom_um"],
                "thickness_um": stackup["thickness_um"],
                "stackup_source": stackup["source"],
                "stackup_source_file": stackup["source_file"],
                "eps_r": eps_r,
                "eps_r_note": (
                    "Typical inter-metal SiO2 dielectric (approximate, "
                    "uniform -- a single permittivity everywhere, not the "
                    "PDK's layered stack)."
                ),
                "side_margin_um": side_margin_um,
                "top_margin_um": top_margin_um,
                "step_um": step_um,
                "n_nodes": driver_result["mesh_stats"]["n_nodes"],
                "n_tets": driver_result["mesh_stats"]["n_tets"],
                "boundary": (
                    "Substrate plane (z = 0), box top and both side walls are "
                    "the grounded return conductor; the run-axis ends are "
                    "natural (Neumann), so the finite-length result "
                    "approximates a per-length quantity away from the ends."
                ),
            },
            "mesh_reduction": {
                "method": (
                    "2.5-D extrusion: the GDS-derived cross-section is meshed "
                    "on a breakpoint-aligned structured grid (every conductor "
                    "edge is exactly a grid plane) and extruded along the "
                    "measured uniform run, then split into tets "
                    "(geode_driver/src/main.rs). The exported 2-D slice is the "
                    "already-grid-aligned mid-run node layer, not a "
                    "marching-tetrahedra cut. This is one cross-section of the "
                    "block, not a general GDS-to-mesh conversion of the whole "
                    "layout."
                ),
                "slice_position_um": slice_["x_um"] + selection.run_start_um,
                "vertex_axes_note": (
                    f"mesh.vertices are exported as (x={cross_axis}_um, "
                    "y=z_um, z=0.0): the cross-section's real in-plane "
                    f"({cross_axis}) and vertical (z) coordinates, flattened "
                    "onto a single Z=0 plane so the export renders as a flat "
                    "2-D slice; not the original 3-D (x,y,z) axis labels. z=0 "
                    "is the grounded substrate plane."
                ),
                "source_volumetric_mesh": {
                    "n_nodes": driver_result["mesh_stats"]["n_nodes"],
                    "n_tets": driver_result["mesh_stats"]["n_tets"],
                },
            },
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    }


def layer_name(profile: PdkProfile) -> str:
    from klayout_tools.decks import get_layer_names

    label = get_layer_names(profile.deck).get(profile.routing_layer)
    if not label:
        return f"layer {profile.routing_layer[0]}/{profile.routing_layer[1]}"
    return label.split(".")[0]


def find_block_gds(blocks_dir: Path, slug: str) -> Path:
    """The block's committed layout GDS -- `layout.json`'s own
    `layout_file` when present (canary blocks keep their upstream file
    name), else `<slug>.gds`."""
    output = blocks_dir / slug / "output"
    layout_json = output / "layout.json"
    if layout_json.is_file():
        data = json.loads(layout_json.read_text(encoding="utf-8"))
        name = data.get("layout_file")
        if name and (output / name).is_file():
            return output / name
    fallback = output / f"{slug}.gds"
    if fallback.is_file():
        return fallback
    raise GenerateError(
        f"no committed GDS found for block {slug!r} under {output} "
        "(layout.json has no resolvable `layout_file`)"
    )


def describe(
    selection: Selection, conductors: list[dict[str, Any]], driven: str
) -> str:
    lines = [
        f"run axis          : {selection.run_axis}",
        f"cut position      : {selection.cut_position_um:.3f} um",
        f"uniform run       : {selection.run_start_um:.3f} .. "
        f"{selection.run_end_um:.3f} um "
        f"({selection.run_length_um:.3f} um)",
        f"driven conductor  : {driven}",
        "conductors        :",
    ]
    for c in conductors:
        intervals = ", ".join(f"[{lo:.3f}, {hi:.3f}]" for lo, hi in c["intervals"])
        flag = "labeled" if c["named"] else "unlabeled"
        lines.append(f"  - {c['name']:<12s} ({flag:<9s}) {intervals}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--block", required=True, help="Gallery block slug under blocks/."
    )
    parser.add_argument("--geode-fem-dir", type=Path, default=None)
    parser.add_argument(
        "--blocks-dir",
        type=Path,
        default=_REPO_ROOT / "blocks",
        help="Root of the gallery's blocks/ tree (default: repo blocks/).",
    )
    parser.add_argument(
        "--run-axis",
        choices=("auto", "x", "y"),
        default="auto",
        help="Axis the bundle runs along (default: try both, keep the best).",
    )
    parser.add_argument("--cut-step-um", type=float, default=2.0)
    parser.add_argument("--run-step-um", type=float, default=0.5)
    parser.add_argument("--max-conductors", type=int, default=6)
    parser.add_argument("--max-span-um", type=float, default=20.0)
    parser.add_argument(
        "--min-run-um",
        type=float,
        default=10.0,
        help="Reject bundles that do not run uniformly at least this far.",
    )
    parser.add_argument(
        "--min-aspect",
        type=float,
        default=10.0,
        help="Reject bundles whose uniform run is shorter than this many "
        "cross-section spans (a 2.5-D extrusion of a stubby cut is fiction).",
    )
    parser.add_argument("--eps-r", type=float, default=DEFAULT_EPS_R)
    parser.add_argument("--side-margin-um", type=float, default=2.0)
    parser.add_argument("--top-margin-um", type=float, default=1.5)
    parser.add_argument("--step-um", type=float, default=0.12)
    parser.add_argument("--n-x", type=int, default=6)
    parser.add_argument(
        "--prefer-net",
        default=None,
        help="Prefer bundles containing this GDS-labeled net (e.g. the "
        "block's own output node), before every other ranking criterion.",
    )
    parser.add_argument(
        "--drive", default=None, help="Conductor to hold at 1V (default: auto)."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the selected geometry and exit (no geode-fem needed).",
    )
    parser.add_argument(
        "--skip-run",
        action="store_true",
        help="Reuse an already-generated driver result instead of re-running cargo.",
    )
    parser.add_argument("--result-json", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    slug = args.block
    try:
        profile = profile_for_slug(slug)
        gds_path = find_block_gds(args.blocks_dir, slug)
        nets = read_nets(gds_path, profile)
    except GenerateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    search = {key: getattr(args, key) for key in SEARCH_KEYS}
    selection = search_bundle(nets, search)
    if selection is None:
        print(
            f"error: no extrudable multi-net bundle found in {gds_path} "
            f"(--max-span-um {args.max_span_um}, --min-run-um "
            f"{args.min_run_um}, --min-aspect {args.min_aspect})",
            file=sys.stderr,
        )
        return 1

    conductors = conductors_from_window(selection.window)
    driven = args.drive or choose_driven(conductors)
    if driven not in {c["name"] for c in conductors}:
        print(
            f"error: --drive {driven!r} is not one of the selected conductors "
            f"({', '.join(c['name'] for c in conductors)})",
            file=sys.stderr,
        )
        return 1

    print(f"block {slug}: {gds_path.relative_to(_REPO_ROOT)}")
    print(describe(selection, conductors, driven))
    if args.dry_run:
        return 0

    if args.geode_fem_dir is None:
        print("error: --geode-fem-dir is required (or pass --dry-run)", file=sys.stderr)
        return 1
    geode_fem_dir = args.geode_fem_dir.resolve()
    if not (geode_fem_dir / "Cargo.toml").is_file():
        print(
            f"error: {geode_fem_dir} does not look like a geode-fem checkout "
            "(no Cargo.toml)",
            file=sys.stderr,
        )
        return 1

    stackup = resolve_stackup(profile)
    result_path = args.result_json or (
        geode_fem_dir / "target" / f"block_coupling_{slug}_result.json"
    )
    out_path = args.out or (
        args.blocks_dir / slug / "output" / f"{slug}.em-export.json"
    )

    try:
        if not args.skip_run:
            stage_driver(geode_fem_dir)
            # The driver writes here directly; a checkout built with a custom
            # CARGO_TARGET_DIR has no `target/` of its own yet.
            result_path.parent.mkdir(parents=True, exist_ok=True)
            run_driver(
                geode_fem_dir,
                conductors=conductors,
                stackup=stackup,
                length_um=selection.run_length_um,
                eps_r=args.eps_r,
                side_margin_um=args.side_margin_um,
                top_margin_um=args.top_margin_um,
                step_um=args.step_um,
                n_x=args.n_x,
                driven=driven,
                result_path=result_path,
            )
        if not result_path.is_file():
            print(
                f"error: expected driver output not found: {result_path}",
                file=sys.stderr,
            )
            return 1
        driver_result = json.loads(result_path.read_text(encoding="utf-8"))

        source_commit = subprocess.run(
            [
                "git",
                "log",
                "-1",
                "--format=%H",
                "--",
                str(gds_path.relative_to(_REPO_ROOT)),
            ],
            cwd=_REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        export = build_export(
            geode_fem_dir=geode_fem_dir,
            driver_result=driver_result,
            slug=slug,
            profile=profile,
            selection=selection,
            conductors=conductors,
            search=search,
            stackup=stackup,
            driven=driven,
            eps_r=args.eps_r,
            side_margin_um=args.side_margin_um,
            top_margin_um=args.top_margin_um,
            step_um=args.step_um,
            gds_path=gds_path,
            source_commit=source_commit,
        )
    except subprocess.CalledProcessError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(export, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out_path} ({out_path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
