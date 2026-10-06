"""Test-only prototype for GF180MCU DF.12 / DF.13 / DF.14 (issue #2371 spike).

NOTHING in ``src/klayout_tools`` imports this module. It exists to answer, with
executable evidence, whether the issue's original "subtract a D-expanded tap
from every active point" algorithm is equivalent to the pinned upstream rule
deck, and to prototype the bounded descriptor/evaluator the design note
(``docs/design/gf180mcu-comp-checks-spike.md``) recommends instead.

Three implementations are compared:

* **upstream (reference)** -- the *verbatim* upstream Ruby DRC expressions,
  executed by a real ``klayout -b`` (``run_upstream``). This is the
  independent oracle: no code in this module computes its expected geometry.
* **port** -- ``evaluate`` below, a Python ``klayout.db.Region`` translation
  of the upstream operation sequence (the recommended bounded evaluator).
* **original** -- ``original_proposal``, the issue's first algorithm
  (``active - tap.sized(D)``), kept only so its disagreement is measurable.

Attribution: ``UPSTREAM_RUBY_*`` below are excerpts of
``efabless/globalfoundries-pdk-libs-gf180mcu_fd_pv`` at commit
``05e7b6adf19edf942969c1c9625f02fd87874f06`` (``klayout/drc/rule_decks/
main.drc`` lines 185-202 and ``comp.drc`` lines 294-364), Copyright 2022-2023
GlobalFoundries PDK Authors, Apache License 2.0
(https://www.apache.org/licenses/LICENSE-2.0). Only ``output(...)`` calls are
redirected (see ``_RUBY_PREAMBLE``); expressions are unmodified, but comment,
``logger.info`` and ``.forget`` (memory-release) lines are omitted. Numeric
values are cross-checked against ``google/gf180mcu-pdk`` @ ``de3240d``
``tables_clear/14_COMP33_1.csv`` rows DF.12-DF.14 (20 um 3.3 V, 15 um 5/6 V).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import klayout.db as kdb

UPSTREAM_REPO = "efabless/globalfoundries-pdk-libs-gf180mcu_fd_pv"
UPSTREAM_COMMIT = "05e7b6adf19edf942969c1c9625f02fd87874f06"
DRM_REPO = "google/gf180mcu-pdk"
DRM_COMMIT = "de3240d"

# GDS layer/datatype per upstream layers_def.drc get_polygons(...) calls.
LAYER_MAP: dict[str, tuple[int, int]] = {
    "comp": (22, 0),
    "dnwell": (12, 0),
    "nwell": (21, 0),
    "lvpwell": (204, 0),
    "dualgate": (55, 0),
    "nplus": (32, 0),
    "pplus": (31, 0),
    "schottky_diode": (241, 0),
    "res_mk": (110, 5),
    "v5_xtor": (112, 1),
}

STEP_UM = 0.5  # upstream DF.13 sizing step (comp.drc)

# Result layer per upstream rule id in the Ruby harness output.
RESULT_LAYERS = {
    "DF.12": (1012, 0),
    "DF.13_LV": (1013, 0),
    "DF.13_MV": (1113, 0),
    "DF.14_LV": (1014, 0),
    "DF.14_MV": (1114, 0),
}

# --- upstream excerpts (verbatim expressions; Apache-2.0, see module doc) ---

UPSTREAM_RUBY_MAIN = """\
dnwell_n        = dnwell.not(lvpwell)
dnwell_p        = dnwell.and(lvpwell)
all_nwell       = dnwell_n.join(nwell)
ncomp           = comp.and(nplus)
pcomp           = comp.and(pplus)
nactive         = ncomp.not(all_nwell)
ptap            = pcomp.not(all_nwell).not(res_mk)
pactive         = pcomp.and(all_nwell)
ntap            = ncomp.and(all_nwell).not(res_mk)
"""

UPSTREAM_RUBY_COMP = """\
  df12_l1 = comp.not_interacting(schottky_diode).not(nplus).not(pplus)
  df12_l1.output('DF.12',
                 'DF.12 : COMP not covered by Nplus or Pplus is forbidden (except those COMP under marking).')
  df12_l1.forget
  df13_ntap_sized = ntap
  sz = 0.0
  while sz < 15.0
    df13_ntap_sized = df13_ntap_sized.sized(0.5.um, octagon_limit).and(nwell)
    sz += 0.5
  end
  df13_ntap_sized_by15 = df13_ntap_sized
  while sz < 20.0
    df13_ntap_sized = df13_ntap_sized.sized(0.5.um, octagon_limit).and(nwell)
    sz += 0.5
  end
  df13_ntap_sized_by20 = df13_ntap_sized
  pactive_3p3v = pactive.not_interacting(v5_xtor).not_interacting(dualgate)
  df13_l1 = pactive_3p3v.not_interacting(df13_ntap_sized_by20)
  df13_l1.output('DF.13_LV',
                 'DF.13_LV : Max distance of Nwell tap (NCOMP inside Nwell) from (PCOMP inside Nwell): 20um')
  df13_l1.forget
  pactive_56v = pactive.overlapping(dualgate)
  df13_l1 = pactive_56v.not_interacting(df13_ntap_sized_by15)
  df13_l1.output('DF.13_MV',
                 'DF.13_MV : Max distance of Nwell tap (NCOMP inside Nwell) from (PCOMP inside Nwell): 15um')
  df13_l1.forget
  nactive_3p3v = nactive.not_interacting(v5_xtor).not_interacting(dualgate)
  df14_poss_bad_active = nactive_3p3v.not_interacting(ptap.sized(20.0.um, diamond_limit))
  df14_good_active = df14_poss_bad_active.sep(ptap, 20.0.um).polygons
  df14_l1 = df14_poss_bad_active.not_interacting(df14_good_active)
  df14_l1.output('DF.14_LV',
                 'DF.14_LV : Max distance of substrate tap (PCOMP outside Nwell) from (NCOMP outside Nwell): 20um')
  df14_l1.forget
  nactive_56v = nactive.overlapping(dualgate)
  df14_poss_bad_active = nactive_56v.not_interacting(ptap.sized(15.0.um, diamond_limit))
  df14_good_active = df14_poss_bad_active.sep(ptap, 15.0.um).polygons
  df14_l1 = df14_poss_bad_active.not_interacting(df14_good_active)
  df14_l1.output('DF.14_MV',
                 'DF.14_MV : Max distance of substrate tap (PCOMP outside Nwell) from (NCOMP outside Nwell): 15um')
  df14_l1.forget
"""  # noqa: E501 -- verbatim upstream lines

# Harness (ours, not upstream): reads the fixture GDS flat, mirrors upstream's
# get_polygons (merged), and redirects the string-named ``output`` calls to
# result layers so no report database / full runset is required.
_RUBY_PREAMBLE = """\
$run_mode = 'flat'
FEOL = true
class SpikeLogger; def info(m); end; end
logger = SpikeLogger.new
def get_polygons(layer, data_type)
  ps = polygons(layer, data_type)
  $run_mode == 'deep' ? ps : ps.merged
end
$spike_result_layers = {%(result_map)s}
class DRC::DRCLayer
  alias_method :spike_orig_output, :output
  def output(*args)
    if args[0].is_a?(String)
      l, d = $spike_result_layers.fetch(args[0])
      spike_orig_output(l, d)
    else
      spike_orig_output(*args)
    end
  end
end
$spike_jobs = [%(jobs)s]
dbu(%(dbu)r)
$spike_jobs.each do |gin, gout|
source(gin, "TOP")
target(gout, "RESULT")
%(layer_defs)s
"""

_RUBY_EPILOGUE = "end\n"


# --- geometry fixture model ---------------------------------------------------


@dataclass
class Fixture:
    """Geometry in integer DBU, per layer name, plus an optional hierarchy.

    ``layers`` shapes go into TOP. ``sub_layers`` shapes (if any) go into a
    child cell placed at every offset in ``sub_offsets`` -- the hierarchical
    twin of a flat fixture (``flatten_twin``).
    """

    name: str
    dbu: float
    layers: dict[str, list[tuple[int, int, int, int]]]
    sub_layers: dict[str, list[tuple[int, int, int, int]]] | None = None
    sub_offsets: tuple[tuple[int, int], ...] = ()

    def build(self) -> kdb.Layout:
        ly = kdb.Layout()
        ly.dbu = self.dbu
        top = ly.create_cell("TOP")
        idx = {n: ly.layer(*ln) for n, ln in LAYER_MAP.items()}
        for lname, boxes in self.layers.items():
            for x1, y1, x2, y2 in boxes:
                top.shapes(idx[lname]).insert(kdb.Box(x1, y1, x2, y2))
        if self.sub_layers:
            sub = ly.create_cell("SUB")
            for lname, boxes in self.sub_layers.items():
                for x1, y1, x2, y2 in boxes:
                    sub.shapes(idx[lname]).insert(kdb.Box(x1, y1, x2, y2))
            for dx, dy in self.sub_offsets:
                top.insert(kdb.CellInstArray(sub.cell_index(), kdb.Trans(dx, dy)))
        return ly

    def write(self, path: Path) -> None:
        self.build().write(str(path))

    def regions(self) -> dict[str, kdb.Region]:
        ly = self.build()
        top = ly.top_cell()
        out: dict[str, kdb.Region] = {}
        for n, ln in LAYER_MAP.items():
            out[n] = kdb.Region(top.begin_shapes_rec(ly.layer(*ln))).merged()
        return out


# --- bounded descriptor + evaluator (the recommended design, prototyped) -------


@dataclass(frozen=True)
class CompCoverageRule:
    """DF.12-style: ``comp`` minus implants, whole polygons exempted by marker."""

    rule_id: str
    checked: str = "comp"
    covered_by_any: tuple[str, ...] = ("nplus", "pplus")
    exempt_markers: tuple[str, ...] = ("schottky_diode",)

    def __post_init__(self) -> None:
        if not self.covered_by_any:
            raise ValueError("covered_by_any must name at least one layer")
        for n in (self.checked, *self.covered_by_any, *self.exempt_markers):
            if n not in LAYER_MAP:
                raise ValueError(f"unknown input layer {n!r}")


@dataclass(frozen=True)
class TapDistanceRule:
    """DF.13 / DF.14-style max-distance-to-tap rule.

    ``well_domain`` "inside" -> DF.13 (active = pcomp & all_nwell, tap = ntap,
    sizing stays inside nwell in ``step_um`` octagon steps); "outside" ->
    DF.14 (active = ncomp - all_nwell, tap = ptap, diamond candidate filter +
    Euclidean separation refinement). ``voltage``: "lv" (not interacting
    v5_xtor/dualgate) or "mv" (overlapping dualgate).
    """

    rule_id: str
    well_domain: str
    voltage: str
    max_distance_um: float
    step_um: float = STEP_UM

    def __post_init__(self) -> None:
        if self.well_domain not in ("inside", "outside"):
            raise ValueError("well_domain must be 'inside' or 'outside'")
        if self.voltage not in ("lv", "mv"):
            raise ValueError("voltage must be 'lv' or 'mv'")
        if self.max_distance_um <= 0:
            raise ValueError("max_distance_um must be positive")
        if self.step_um <= 0:
            raise ValueError("step_um must be positive")
        n = self.max_distance_um / self.step_um
        if abs(n - round(n)) > 1e-9:
            raise ValueError("max_distance_um must be a multiple of step_um")


RULES: dict[str, CompCoverageRule | TapDistanceRule] = {
    "DF.12": CompCoverageRule("DF.12"),
    "DF.13_LV": TapDistanceRule("DF.13_LV", "inside", "lv", 20.0),
    "DF.13_MV": TapDistanceRule("DF.13_MV", "inside", "mv", 15.0),
    "DF.14_LV": TapDistanceRule("DF.14_LV", "outside", "lv", 20.0),
    "DF.14_MV": TapDistanceRule("DF.14_MV", "outside", "mv", 15.0),
}


def derive(r: dict[str, kdb.Region]) -> dict[str, kdb.Region]:
    """Port of upstream main.drc 185-202 (absent layers are empty Regions)."""
    d = dict(r)
    dnwell_n = r["dnwell"] - r["lvpwell"]
    all_nwell = (dnwell_n + r["nwell"]).merged()
    ncomp = r["comp"] & r["nplus"]
    pcomp = r["comp"] & r["pplus"]
    d["all_nwell"] = all_nwell
    d["nactive"] = ncomp - all_nwell
    d["ptap"] = (pcomp - all_nwell) - r["res_mk"]
    d["pactive"] = pcomp & all_nwell
    d["ntap"] = (ncomp & all_nwell) - r["res_mk"]
    return d


def evaluate(rule, regions: dict[str, kdb.Region], dbu: float) -> kdb.Region:
    """Evaluate one rule on raw drawn-layer Regions (no checked-layer skip:
    empty taps/implants yield violations whenever checked active exists)."""
    d = derive(regions)
    um = lambda x: int(round(x / dbu))  # noqa: E731
    if isinstance(rule, CompCoverageRule):
        c = regions[rule.checked]
        for m in rule.exempt_markers:
            c = c.not_interacting(regions[m])
        for cov in rule.covered_by_any:
            c = c - regions[cov]
        return c
    active = d["pactive"] if rule.well_domain == "inside" else d["nactive"]
    tap = d["ntap"] if rule.well_domain == "inside" else d["ptap"]
    if rule.voltage == "lv":
        active = active.not_interacting(d["v5_xtor"]).not_interacting(d["dualgate"])
    else:
        active = active.overlapping(d["dualgate"])
    dist = um(rule.max_distance_um)
    if rule.well_domain == "inside":
        sized = tap
        step = um(rule.step_um)
        for _ in range(int(round(rule.max_distance_um / rule.step_um))):
            sized = sized.sized(step, 1).merged() & d["all_nwell"]  # octagon
        # NOTE: upstream clips with `nwell`, not `all_nwell` (see design note
        # section "discrepancies"); fixtures with dnwell-only wells expose it.
        return active.not_interacting(sized)
    cand = active.not_interacting(tap.sized(dist, 0))  # diamond
    good = cand.separation_check(tap, dist).polygons()
    return cand.not_interacting(good)


def evaluate_port_upstream_clip(rule, regions, dbu):
    """Like ``evaluate`` but DF.13 clips with raw ``nwell`` exactly as upstream."""
    if not (isinstance(rule, TapDistanceRule) and rule.well_domain == "inside"):
        return evaluate(rule, regions, dbu)
    d = derive(regions)
    um = lambda x: int(round(x / dbu))  # noqa: E731
    active = d["pactive"]
    if rule.voltage == "lv":
        active = active.not_interacting(d["v5_xtor"]).not_interacting(d["dualgate"])
    else:
        active = active.overlapping(d["dualgate"])
    sized = d["ntap"]
    step = um(rule.step_um)
    for _ in range(int(round(rule.max_distance_um / rule.step_um))):
        sized = sized.sized(step, 1).merged() & regions["nwell"]
    return active.not_interacting(sized)


def original_proposal(rule, regions, dbu) -> kdb.Region:
    """The issue's first algorithm: violation = active - tap.sized(D), applied
    to every active point, no well clipping, polygon pieces (not selection)."""
    d = derive(regions)
    dist = int(round(rule.max_distance_um / dbu))
    active = d["pactive"] if rule.well_domain == "inside" else d["nactive"]
    tap = d["ntap"] if rule.well_domain == "inside" else d["ptap"]
    if rule.voltage == "lv":
        active = active.not_interacting(d["v5_xtor"]).not_interacting(d["dualgate"])
    else:
        active = active.overlapping(d["dualgate"])
    return active - tap.sized(dist, 1)


def original_df12(regions) -> kdb.Region:
    """Original DF.12 reading: area subtraction of the marker, not whole-polygon."""
    return (
        regions["comp"]
        - regions["nplus"]
        - regions["pplus"]
        - regions["schottky_diode"]
    )


# --- real-KLayout Ruby oracle --------------------------------------------------

_MAC_APP = "/Applications/KLayout/klayout.app/Contents/MacOS/klayout"


def find_klayout_binary() -> str | None:
    env = os.environ.get("KLT_KLAYOUT_BIN")
    if env and Path(env).exists():
        return env
    found = shutil.which("klayout")
    if found:
        return found
    return _MAC_APP if Path(_MAC_APP).exists() else None


def klayout_version(binary: str) -> str:
    p = subprocess.run([binary, "-v"], capture_output=True, text=True, timeout=60)
    return p.stdout.strip()


def run_upstream_batch(
    fixtures: dict[str, Fixture], binary: str
) -> dict[str, dict[str, kdb.Region]]:
    """Execute the verbatim upstream expressions on every fixture in ONE
    ``klayout -b`` launch (the app's start-up dominates; see design note).

    ``fixtures`` maps a unique key to a Fixture. Returns key -> rule id -> Region.
    """
    layer_defs = "\n".join(
        f"{n} = get_polygons({ln[0]}, {ln[1]})" for n, ln in LAYER_MAP.items()
    )
    result_map = ", ".join(
        f'"{k}" => [{v[0]}, {v[1]}]' for k, v in RESULT_LAYERS.items()
    )
    with tempfile.TemporaryDirectory(prefix="klt-spike-") as td:
        tdp = Path(td)
        keys = list(fixtures)
        dbus = {fx.dbu for fx in fixtures.values()}
        if len(dbus) != 1:
            raise ValueError(
                "one klayout launch supports one DBU (dbu cannot change after source)"
            )
        (batch_dbu,) = dbus
        jobs = []
        for i, key in enumerate(keys):
            gin, gout = tdp / f"in{i}.gds", tdp / f"out{i}.gds"
            fixtures[key].write(gin)
            jobs.append(f'["{gin}", "{gout}"]')
        script = (
            _RUBY_PREAMBLE
            % {
                "result_map": result_map,
                "layer_defs": layer_defs,
                "jobs": ", ".join(jobs),
                "dbu": batch_dbu,
            }
            + UPSTREAM_RUBY_MAIN
            + "if FEOL\n"
            + UPSTREAM_RUBY_COMP
            + "end\n"
            + _RUBY_EPILOGUE
        )
        drc = tdp / "run.drc"
        drc.write_text(script)
        p = subprocess.run(
            [binary, "-b", "-r", str(drc)], capture_output=True, text=True, timeout=1800
        )
        results: dict[str, dict[str, kdb.Region]] = {}
        for i, key in enumerate(keys):
            gout = tdp / f"out{i}.gds"
            if p.returncode != 0 or not gout.exists():
                raise RuntimeError(
                    f"klayout upstream run failed ({p.returncode}) at {key}: "
                    f"{p.stderr[-2000:]}"
                )
            ly = kdb.Layout()
            ly.read(str(gout))
            top = ly.top_cell()
            out = {}
            for rid, ln in RESULT_LAYERS.items():
                li = ly.find_layer(*ln)
                out[rid] = (
                    kdb.Region(top.begin_shapes_rec(li)).merged()
                    if li is not None
                    else kdb.Region()
                )
            results[key] = out
        return results


def run_upstream_all(
    fixtures: dict[str, Fixture], binary: str
) -> dict[str, dict[str, kdb.Region]]:
    """One ``klayout -b`` launch per DBU scale."""
    out: dict[str, dict[str, kdb.Region]] = {}
    for dbu in sorted({fx.dbu for fx in fixtures.values()}):
        out.update(
            run_upstream_batch(
                {k: f for k, f in fixtures.items() if f.dbu == dbu}, binary
            )
        )
    return out


def all_fixtures() -> dict[str, Fixture]:
    """Every fixture at every DBU scale, keyed ``"<dbu>/<name>"``."""
    return {
        f"{dbu:g}/{fx.name}": fx for dbu in DBU_SCALES for fx in build_fixtures(dbu)
    }


# --- comparison helpers ---------------------------------------------------------


def same(a: kdb.Region, b: kdb.Region) -> bool:
    return (a.merged() ^ b.merged()).is_empty()


def area_um2(r: kdb.Region, dbu: float) -> float:
    return r.area() * dbu * dbu


def summarize(r: kdb.Region, dbu: float) -> str:
    bb = r.bbox()
    if r.is_empty():
        return "empty"
    box = (bb.left * dbu, bb.bottom * dbu, bb.right * dbu, bb.top * dbu)
    return (
        f"{r.count()}poly {area_um2(r, dbu):.3f}um2 "
        f"bbox=({box[0]:g},{box[1]:g};{box[2]:g},{box[3]:g})"
    )


# --- fixture library -------------------------------------------------------------


def _um(dbu: float) -> Callable[[float], int]:
    return lambda x: int(round(x / dbu))


def _active(kind: str, u, x1, y1, x2, y2) -> dict[str, list]:
    """comp + implant (implant covers comp with 0.1um margin)."""
    m = u(0.1)
    return {
        "comp": [(x1, y1, x2, y2)],
        ("nplus" if kind == "n" else "pplus"): [(x1 - m, y1 - m, x2 + m, y2 + m)],
    }


def _merge(*parts: dict[str, list]) -> dict[str, list]:
    out: dict[str, list] = {}
    for p in parts:
        for k, v in p.items():
            out.setdefault(k, []).extend(v)
    return out


def _tap_gap_fixture(
    name: str,
    dbu: float,
    *,
    well_domain: str,
    gap_dbu_offset: int,
    d_um: float,
    mv: bool,
    geometry: str = "axial",
    diag_pct: int = 100,
    extras: dict[str, list] | None = None,
) -> Fixture:
    """Tap box at x in [0,2um]; checked active ``d_um + offset`` DBU away.

    axial: separated along +x; diagonal: 3-4-5 corner offset (0.6 D, 0.8 D)
    plus the offset on y (Euclidean distance = D + offset along the hypotenuse
    direction for the corner-to-corner case up to second order).
    """
    u = _um(dbu)
    tap_kind, act_kind = ("n", "p") if well_domain == "inside" else ("p", "n")
    tx1, ty1, tx2, ty2 = 0, 0, u(2), u(2)
    D = u(d_um)
    if geometry == "axial":
        ax1 = tx2 + D + gap_dbu_offset
        ay1 = 0
    else:  # diagonal 3-4-5
        ax1 = tx2 + (D * 3 * diag_pct) // 500
        ay1 = ty2 + (D * 4 * diag_pct) // 500 + gap_dbu_offset
    act = (ax1, ay1, ax1 + u(2), ay1 + u(2))
    parts = [_active(tap_kind, u, tx1, ty1, tx2, ty2), _active(act_kind, u, *act)]
    layers = _merge(*parts)
    if well_domain == "inside":
        layers["nwell"] = [(-u(2), -u(2), act[2] + u(2), act[3] + u(2))]
    if mv:
        layers.setdefault("dualgate", []).append(
            (act[0] - u(1), act[1] - u(1), act[2] + u(1), act[3] + u(1))
        )
    if extras:
        layers = _merge(layers, extras)
    return Fixture(name, dbu, layers)


def build_fixtures(dbu: float) -> list[Fixture]:
    """The fixture matrix (see design note). All coordinates derived from ``dbu``."""
    u = _um(dbu)
    fxs: list[Fixture] = []

    # ---- DF.12 ----
    big = (0, 0, u(4), u(2))
    fxs.append(Fixture("df12_absent_implants", dbu, {"comp": [big]}))
    fxs.append(
        Fixture(
            "df12_full_nplus",
            dbu,
            _merge({"comp": [big]}, {"nplus": [(-u(1), -u(1), u(5), u(3))]}),
        )
    )
    fxs.append(
        Fixture(
            "df12_partial_nplus",
            dbu,
            _merge({"comp": [big]}, {"nplus": [(-u(1), -u(1), u(2), u(3))]}),
        )
    )
    fxs.append(
        Fixture(
            "df12_joint_n_p",
            dbu,
            {
                "comp": [big],
                "nplus": [(-u(1), -u(1), u(2), u(3))],
                "pplus": [(u(2), -u(1), u(5), u(3))],
            },
        )
    )
    fxs.append(
        Fixture(
            "df12_partial_marker",
            dbu,
            {
                "comp": [big],
                "nplus": [(-u(1), -u(1), u(1), u(3))],
                "schottky_diode": [(u(3), -u(1), u(5), u(3))],
            },
        )
    )
    fxs.append(
        Fixture(
            "df12_marker_edge_touch",
            dbu,
            {"comp": [big], "schottky_diode": [(u(4), -u(1), u(6), u(3))]},
        )
    )

    # ---- DF.13 / DF.14 qualitative ----
    for dom, tag in (("inside", "13"), ("outside", "14")):
        tk, ak = ("n", "p") if dom == "inside" else ("p", "n")

        def wrap(layers, x2, dom=dom):
            if dom == "inside":
                layers["nwell"] = [(-u(2), -u(2), x2 + u(2), u(10))]
            return layers

        # zero taps: active exists, tap layer absent entirely
        fxs.append(
            Fixture(
                f"df{tag}_zero_taps",
                dbu,
                wrap(_active(ak, u, u(5), 0, u(7), u(2)), u(7)),
            )
        )
        # valid nearby tap (5 um)
        fxs.append(
            Fixture(
                f"df{tag}_near_tap",
                dbu,
                wrap(
                    _merge(
                        _active(tk, u, 0, 0, u(2), u(2)),
                        _active(ak, u, u(7), 0, u(9), u(2)),
                    ),
                    u(9),
                ),
            )
        )
        # remote tap (30 um)
        fxs.append(
            Fixture(
                f"df{tag}_remote_tap",
                dbu,
                wrap(
                    _merge(
                        _active(tk, u, 0, 0, u(2), u(2)),
                        _active(ak, u, u(32), 0, u(34), u(2)),
                    ),
                    u(34),
                ),
            )
        )
        # long active, only the left end within 20 um of the tap
        fxs.append(
            Fixture(
                f"df{tag}_long_active_one_end",
                dbu,
                wrap(
                    _merge(
                        _active(tk, u, 0, 0, u(2), u(2)),
                        _active(ak, u, u(5), 0, u(45), u(2)),
                    ),
                    u(45),
                ),
            )
        )
        # absent checked active: nothing to check even though a tap exists
        fxs.append(
            Fixture(
                f"df{tag}_no_active", dbu, wrap(_active(tk, u, 0, 0, u(2), u(2)), u(2))
            )
        )

    # disconnected wells with the minimum legal 0.6 um gap: tap in A, pactive in B
    fxs.append(
        Fixture(
            "df13_island_gap_0p6",
            dbu,
            {
                "nwell": [(-u(2), -u(2), u(10), u(6)), (u(10.6), -u(2), u(24), u(6))],
                **_merge(
                    _active("n", u, 0, 0, u(2), u(2)),
                    _active("p", u, u(12), 0, u(14), u(2)),
                ),
            },
        )
    )
    # same but one well (no gap): clean control
    fxs.append(
        Fixture(
            "df13_island_control_joined",
            dbu,
            {
                "nwell": [(-u(2), -u(2), u(24), u(6))],
                **_merge(
                    _active("n", u, 0, 0, u(2), u(2)),
                    _active("p", u, u(12), 0, u(14), u(2)),
                ),
            },
        )
    )
    # U-shaped well: Euclid-near but well-path-far (notch 1 um wide, 12 um deep)
    u_well = [
        (-u(2), -u(2), u(7), u(1)),
        (-u(2), u(1), u(1), u(16)),
        (u(4), u(1), u(7), u(16)),
    ]
    fxs.append(
        Fixture(
            "df13_u_well_notch",
            dbu,
            {
                "nwell": u_well,
                **_merge(
                    _active("n", u, -u(1), u(14), 0, u(15)),
                    _active("p", u, u(4.5), u(14), u(5.5), u(15)),
                ),
            },
        )
    )
    # resistor marker over the only tap: tap removed -> flagged
    for dom, tag in (("inside", "13"), ("outside", "14")):
        tk, ak = ("n", "p") if dom == "inside" else ("p", "n")
        lay = _merge(
            _active(tk, u, 0, 0, u(2), u(2)), _active(ak, u, u(7), 0, u(9), u(2))
        )
        lay["res_mk"] = [(-u(1), -u(1), u(3), u(3))]
        if dom == "inside":
            lay["nwell"] = [(-u(2), -u(2), u(11), u(4))]
        fxs.append(Fixture(f"df{tag}_res_mk_over_tap", dbu, lay))
    # deep-well context: well = dnwell only (no nwell); DF.13 clips with `nwell`
    lay = _merge(
        _active("n", u, 0, 0, u(2), u(2)), _active("p", u, u(7), 0, u(9), u(2))
    )
    lay["dnwell"] = [(-u(2), -u(2), u(11), u(4))]
    fxs.append(Fixture("df13_dnwell_only", dbu, lay))
    lay = _merge(
        _active("n", u, 0, 0, u(2), u(2)), _active("p", u, u(7), 0, u(9), u(2))
    )
    lay["dnwell"] = [(-u(2), -u(2), u(11), u(4))]
    lay["nwell"] = [(-u(2), -u(2), u(11), u(4))]
    fxs.append(Fixture("df13_dnwell_plus_nwell", dbu, lay))
    # dnwell fully under lvpwell: not a well -> pcomp is a substrate tap
    lay = _merge(
        _active("p", u, 0, 0, u(2), u(2)), _active("n", u, u(7), 0, u(9), u(2))
    )
    lay["dnwell"] = [(-u(2), -u(2), u(11), u(4))]
    lay["lvpwell"] = [(-u(2), -u(2), u(11), u(4))]
    fxs.append(Fixture("df14_dnwell_under_lvpwell", dbu, lay))

    # voltage selection: active 25 um from tap, so a violation only if checked.
    def v_fx(name, dg_kind):
        f = _tap_gap_fixture(
            name, dbu, well_domain="inside", gap_dbu_offset=u(5), d_um=20.0, mv=False
        )
        act = f.layers["comp"][1]
        box = (act[0] - u(1), act[1] - u(1), act[2] + u(1), act[3] + u(1))
        if dg_kind == "dualgate":
            f.layers["dualgate"] = [box]
        elif dg_kind == "v5_only":
            f.layers["v5_xtor"] = [box]
        elif dg_kind == "both":
            f.layers["dualgate"] = [box]
            f.layers["v5_xtor"] = [box]
        elif dg_kind == "dg_touch":
            f.layers["dualgate"] = [
                (act[2], act[1], act[2] + u(1), act[3])
            ]  # edge touch only
        return f

    fxs.append(v_fx("df13_v_none", "none"))
    fxs.append(v_fx("df13_v_dualgate", "dualgate"))
    fxs.append(v_fx("df13_v_v5_only", "v5_only"))
    fxs.append(v_fx("df13_v_dualgate_and_v5", "both"))
    fxs.append(v_fx("df13_v_dualgate_edge_touch", "dg_touch"))

    # boundary probes: D-1, D, D+1 DBU; axial + diagonal; LV/MV; DF.13/DF.14
    for dom, tag in (("inside", "13"), ("outside", "14")):
        for mv, dval, vt in ((False, 20.0, "lv"), (True, 15.0, "mv")):
            for geom in ("axial", "diag"):
                for k, kn in ((-1, "dm1"), (0, "d0"), (1, "dp1")):
                    fxs.append(
                        _tap_gap_fixture(
                            f"df{tag}_{vt}_{geom}_{kn}",
                            dbu,
                            well_domain=dom,
                            gap_dbu_offset=k,
                            d_um=dval,
                            mv=mv,
                            geometry="axial" if geom == "axial" else "diagonal",
                        )
                    )

    # diagonal over-reach: corner-to-corner Euclidean distance = pct% of D
    for dom, tag in (("inside", "13"), ("outside", "14")):
        for mv, dval, vt in ((False, 20.0, "lv"), (True, 15.0, "mv")):
            for pct in (104, 110):
                fxs.append(
                    _tap_gap_fixture(
                        f"df{tag}_{vt}_diag_x{pct}",
                        dbu,
                        well_domain=dom,
                        gap_dbu_offset=0,
                        d_um=dval,
                        mv=mv,
                        geometry="diagonal",
                        diag_pct=pct,
                    )
                )

    # hierarchy: tap and active in a child instantiated twice vs flat twin
    flat_layers = _merge(
        _active("n", u, 0, 0, u(2), u(2)),
        _active("p", u, u(7), 0, u(9), u(2)),
        _active("n", u, u(100), 0, u(102), u(2)),
        _active("p", u, u(137), 0, u(139), u(2)),  # 35 um away -> violation in 2nd copy
        {"nwell": [(-u(2), -u(2), u(11), u(4)), (u(98), -u(2), u(141), u(4))]},
    )
    fxs.append(Fixture("df13_hier_flat", dbu, flat_layers))
    top_extra = _merge(
        _active("p", u, u(7), 0, u(9), u(2)), _active("p", u, u(137), 0, u(139), u(2))
    )
    sub_f = Fixture(
        "df13_hier_hier",
        dbu,
        _merge(top_extra, {"nwell": [(u(98), -u(2), u(141), u(4))]}),
        sub_layers=_merge(
            _active("n", u, 0, 0, u(2), u(2)), {"nwell": [(-u(2), -u(2), u(11), u(4))]}
        ),
        sub_offsets=((0, 0), (u(100), 0)),
    )
    fxs.append(sub_f)
    return fxs


DBU_SCALES = (0.001, 0.0005)


def matrix(binary: str) -> list[str]:
    """Human-readable result matrix (used to populate the design note)."""
    fixtures = all_fixtures()
    ups = run_upstream_all(fixtures, binary)
    rows = []
    for key, fx in fixtures.items():
        dbu = fx.dbu
        regs = fx.regions()
        for rid, rule in RULES.items():
            up = ups[key][rid]
            port = evaluate_port_upstream_clip(rule, regs, dbu)
            orig = (
                original_df12(regs)
                if rid == "DF.12"
                else original_proposal(rule, regs, dbu)
            )
            if up.is_empty() and port.is_empty() and orig.is_empty():
                continue
            rows.append(
                f"{key:42s} {rid:9s} upstream={summarize(up, dbu):50s}"
                f" port={'==' if same(up, port) else '!='}"
                f" orig={'==' if same(up, orig) else '!='}"
                + ("" if same(up, orig) else f" orig={summarize(orig, dbu)}")
            )
    return rows


if __name__ == "__main__":  # python tests/helpers/gf180mcu_comp_spike.py
    b = find_klayout_binary()
    if not b:
        raise SystemExit("klayout binary not found (set KLT_KLAYOUT_BIN)")
    print("klayout:", klayout_version(b))
    print("pya:", getattr(kdb, "__version__", "?"), kdb.Region.__module__)
    for line in matrix(b):
        print(line)
