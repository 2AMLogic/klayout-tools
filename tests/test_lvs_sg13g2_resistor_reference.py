"""Issue #6313: inline-extracted sg13g2 poly resistors vs a plain-element
reference.

Split out of `tests/test_lvs.py` (which is at its per-file size ceiling).
The issue reported that between klt 0.6.0 and 0.8.0 an inline-extracted
sg13g2 resistor stopped pairing with a plain-element reference, and read
the report's `class: "rppd"` (layout) vs `class: "RPPD"` (reference)
spellings as a case-mismatch cause. These tests are the pinnable form of
the measured refutation plus the real failure signature:

- the comparer pairs device classes whose names differ only in case, for
  every bulk-bearing poly flavour, under the issue's own request shape
  (`reference.device_bulk` + `options.compare_parameters` scoped to
  `R`,`L`,`W`) -- a mixed MOS + resistor layout included;
- what changed across the 0.6.0 -> 0.8.0 boundary is the deck's own
  resistor measurement (issues #2679 and #2652 -- abutting-heads
  recognition plus the IHP rspec end-term/width-offset terms), and a
  reference committed against the pre-0.8.0 extraction reports the exact
  class-wide `device.unmatched` + `net.unmatched` cascade the issue
  measured.

See `docs/cli/lvs.md`, "When every device of one class reports
`device.unmatched`: check scoped parameter values first".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from klayout_tools.lvs import run_lvs

pytestmark = pytest.mark.usefixtures("real_build_identity_git")


def _write(path: Path, text: str) -> str:
    path.write_text(text)
    return str(path)


def _write_request(path: Path, request: dict) -> str:
    import json

    path.write_text(json.dumps(request))
    return str(path)


# --------------------------------------------------------------------------- #
# Fixtures and the deck's own rspec contract
# --------------------------------------------------------------------------- #

#: The three sg13g2 poly-resistor flavours' rspec constants, transcribed
#: from `decks/sg13g2.py` (`end_term_ohm_um` / `width_offset_um` /
#: `sheet_rho_ohm_sq` per flavour -- issue #2679's `topology=
#: "abutting_heads"` recognition plus issue #2652's transcription of
#: IHP-Open-PDK v0.3.0's own xschem symbol `value=` attributes). Kept as
#: literals (not imported) so these tests fail loudly on deck-value drift
#: instead of silently tracking it.
_SG13G2_RES_RSPEC = {
    # flavour: (sheet_rho_ohm_sq, end_term_ohm_um, width_offset_um)
    "rsil": (7.0, 9.0, 0.01),
    "rppd": (260.0, 70.0, 0.006),
    "rhigh": (1360.0, 160.0, -0.04),
}


def _sg13g2_res_rspec_r_ohm(flavor: str, l_um: float, w_um: float) -> float:
    """IHP's own rspec formula the deck implements since 0.8.0:
    ``end_term/W + rho * L / (W + width_offset)`` (ohms, um)."""
    rho, end_term, width_offset = _SG13G2_RES_RSPEC[flavor]
    return end_term / w_um + rho * l_um / (w_um + width_offset)


def _sg13g2_res_pdk_setup(monkeypatch, tmp_path):
    """The no-real-PDK-install environment every sg13g2 generator test
    here uses: only the curated deck, no fetched PDK, no ambient
    PDK_ROOT."""
    from klayout_tools import pdk as pdk_mod

    monkeypatch.delenv("PDK_ROOT", raising=False)
    monkeypatch.delenv("PDK", raising=False)
    monkeypatch.setattr(pdk_mod, "STORE_DIRS", [])
    monkeypatch.setattr(pdk_mod, "CONVENTIONAL_PREFIXES", [])
    pdk_root = tmp_path / "pdk_install"
    (pdk_root / "ihp-sg13g2" / "libs.tech").mkdir(parents=True)
    return pdk_root


def _sg13g2_plain_res_reference(flavor: str, num: int, r_ohm: float) -> str:
    """A schematic-derived plain-element reference for ``num`` unit
    resistors of ``flavor``: 2-terminal `R` cards (model name = flavour,
    exactly as a schematic export writes it), no bulk terminal -- the
    shape `reference.device_bulk` exists to reconcile (issue #506)."""
    cards = "\n".join(
        f"R{i + 1} A{i + 1} B{i + 1} {r_ohm:.12g} {flavor} W=0.5U L=2U"
        for i in range(num)
    )
    return f"""
.SUBCKT res_array_0 SUB
{cards}
.ENDS res_array_0
"""


def _gen_res_array(pdk_root, tmp_path, flavor: str, name: str):
    from klayout_tools.gen import generate

    gds = tmp_path / f"{name}.gds"
    generate(
        {
            "generator": "res_array",
            "pdk": {"variant": "ihp-sg13g2", "root": str(pdk_root)},
            "params": {
                "num": 2,
                "flavor": flavor,
                "width_um": 0.5,
                "length_um": 2.0,
            },
            "options": {"output": str(gds)},
        }
    )
    return gds


# --------------------------------------------------------------------------- #
# The pairing guards (pass-today: the spelling-split refutation)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("flavor", sorted(_SG13G2_RES_RSPEC))
def test_inline_extracted_sg13g2_resistor_pairs_plain_element_reference(
    tmp_path, monkeypatch, flavor
):
    """Issue #6313's headline repro, as a pass-today regression guard:
    the deck-declared lower-case layout class and the
    `NetlistSpiceReader`-upper-cased reference class pair on current
    `main`, for every bulk-bearing poly flavour, with the issue's own
    request shape (`reference.device_bulk` +
    `options.compare_parameters` scoped to `R`,`L`,`W`) and a reference
    whose values match what the deck extracts. The first assert also
    pins the deck side of the contract (extraction implements the
    transcribed rspec formula) so deck drift names itself here instead
    of surfacing as a mystery `mismatch` below."""
    from klayout_tools.extract import run_extract

    pdk_root = _sg13g2_res_pdk_setup(monkeypatch, tmp_path)
    gds = _gen_res_array(pdk_root, tmp_path, flavor, f"res_array_{flavor}")
    # The deck's own extraction realises the transcribed rspec formula on
    # the drawn 2.0x0.5 body (rounded to the report's 6 decimal places).
    extracted = run_extract(str(gds), "sg13g2", output=str(tmp_path / "ext.spice"))
    assert extracted["device_counts"] == {flavor: 2}
    assert extracted["devices"][0]["params"]["r_ohm"] == pytest.approx(
        _sg13g2_res_rspec_r_ohm(flavor, 2.0, 0.5), rel=1e-7
    )

    reference_path = _write(
        tmp_path / "ref.spice",
        _sg13g2_plain_res_reference(
            flavor, 2, _sg13g2_res_rspec_r_ohm(flavor, 2.0, 0.5)
        ),
    )
    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"file": str(gds), "deck": "sg13g2"},
            "reference": {
                "netlist": reference_path,
                "top": "res_array_0",
                "deck": "sg13g2",
                "device_bulk": {flavor: "SUB"},
            },
            "options": {"compare_parameters": {flavor: ["R", "L", "W"]}},
        },
    )
    report = run_lvs(path)

    assert report["status"] == "match"
    assert report["counts"]["devices"] == {"layout": 2, "reference": 2, "matched": 2}
    assert report["counts"]["nets"]["matched"] == report["counts"]["nets"]["layout"]
    assert not [m for m in report["mismatches"] if m["severity"] == "error"]


def test_mixed_sg13g2_mos_and_resistor_inline_pairs_plain_element_reference(
    tmp_path, monkeypatch
):
    """The issue's real shape -- a *mixed* MOS + poly-resistor layout --
    as a pass-today regression guard: MOS devices and the resistor pair
    side by side through the same inline-extraction path, the MOS by its
    native `M`-card compare and the resistor through
    `reference.device_bulk` + `options.compare_parameters`, exactly as
    the #6313 request document did.

    The two blocks are composed with `klt gen-compose` (no routing --
    two electrically separate islands sharing one substrate rail is
    enough) so the layout side is one GDS carrying both classes, not two
    separate compares."""
    from klayout_tools.gen import generate
    from klayout_tools.gen_compose import compose

    pdk_root = _sg13g2_res_pdk_setup(monkeypatch, tmp_path)
    mos_report = generate(
        {
            "generator": "mos_array",
            "pdk": {"variant": "ihp-sg13g2", "root": str(pdk_root)},
            "params": {
                "w_um": 1.6,
                "l_um": 0.13,
                "rows": 1,
                "cols": 2,
                "flavor": "nfet",
                "gate_contact": True,
            },
            "options": {"output": str(tmp_path / "mos_array.gds")},
        }
    )
    res_report = generate(
        {
            "generator": "res_array",
            "pdk": {"variant": "ihp-sg13g2", "root": str(pdk_root)},
            "params": {"num": 2, "flavor": "rppd", "width_um": 0.5, "length_um": 2.0},
            "options": {"output": str(tmp_path / "res_array.gds")},
        }
    )
    gds = tmp_path / "mixed.gds"
    compose(
        {
            "pdk": {"variant": "ihp-sg13g2", "root": str(pdk_root)},
            "blocks": [
                {"id": "mos", "generator_report": mos_report},
                {"id": "res", "generator_report": res_report},
            ],
            "placement": {
                "strategy": "row",
                "order": ["mos", "res"],
                "spacing_um": 5.0,
            },
            "options": {"cell_name": "mixed_top", "output": str(gds)},
        }
    )

    r_ohm = _sg13g2_res_rspec_r_ohm("rppd", 2.0, 0.5)
    reference_path = _write(
        tmp_path / "ref.spice",
        f"""
.SUBCKT mixed_top SUB
M1 S1 G1 D1 SUB nfet W=1.6U L=0.13U
M2 S2 G2 D2 SUB nfet W=1.6U L=0.13U
R1 A1 B1 {r_ohm:.12g} rppd W=0.5U L=2U
R2 A2 B2 {r_ohm:.12g} rppd W=0.5U L=2U
.ENDS mixed_top
""",
    )
    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"file": str(gds), "deck": "sg13g2"},
            "reference": {
                "netlist": reference_path,
                "top": "mixed_top",
                "deck": "sg13g2",
                "device_bulk": {"rppd": "SUB"},
            },
            "options": {"compare_parameters": {"rppd": ["R", "L", "W"]}},
        },
    )
    report = run_lvs(path)

    assert report["status"] == "match"
    assert report["counts"]["devices"] == {"layout": 4, "reference": 4, "matched": 4}
    assert report["counts"]["nets"] == {"layout": 11, "reference": 11, "matched": 11}
    assert not [m for m in report["mismatches"] if m["severity"] == "error"]


# --------------------------------------------------------------------------- #
# The drift signature (the issue's actual failure mode, pinned)
# --------------------------------------------------------------------------- #


def test_sg13g2_resistor_value_drift_reports_the_class_cascade(tmp_path, monkeypatch):
    """The failure signature behind issue #6313's report, pinned so the
    next reader of that signature finds the cause named in a test
    instead of re-deriving it: one *stale* reference value under a
    `options.compare_parameters` scope that names `R` fails the pairing
    of every instance of the class -- both sides report their devices as
    `device.unmatched` (the report names each side's own class spelling:
    deck-declared `rppd` vs SPICE-reader-upper-cased `RPPD` -- spelling
    is not the mechanism, pairing is case-insensitive) -- and the
    resistor nets cascade into `net.unmatched`, while every device of an
    unaffected class still pairs. This is exactly the 0.8.0 report the
    issue measured: 257 `rppd` + 257 `RPPD` unmatched with MOS intact,
    because #2679/#2652 changed the deck's extracted resistance under a
    reference committed to the 0.6.0 values."""
    pdk_root = _sg13g2_res_pdk_setup(monkeypatch, tmp_path)
    gds = _gen_res_array(pdk_root, tmp_path, "rppd", "res_array_rppd")
    # The pre-0.8.0 plain sheet-rho value (260 * 2.0 / 0.5 = 1040 ohm) --
    # the kind of value a reference committed against the 0.6.0
    # extraction carries, ~11% below the 0.8.0+ rspec value with its end
    # term and width offset.
    stale_r_ohm = 260.0 * 2.0 / 0.5
    reference_path = _write(
        tmp_path / "ref.spice", _sg13g2_plain_res_reference("rppd", 2, stale_r_ohm)
    )
    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"file": str(gds), "deck": "sg13g2"},
            "reference": {
                "netlist": reference_path,
                "top": "res_array_0",
                "deck": "sg13g2",
                "device_bulk": {"rppd": "SUB"},
            },
            "options": {"compare_parameters": {"rppd": ["R", "L", "W"]}},
        },
    )
    report = run_lvs(path)

    assert report["status"] == "mismatch"
    assert report["counts"]["devices"] == {"layout": 2, "reference": 2, "matched": 0}
    unmatched_classes = {
        m["device"]["class"]
        for m in report["mismatches"]
        if m["category"] == "device.unmatched"
    }
    assert unmatched_classes == {"rppd", "RPPD"}
