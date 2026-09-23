"""Cross-validation of `klt lvs`'s **verdict** against a layout netlist
magic extracted -- issue #2316, pairing #1c of tracking issue #2007.

Two adjacent pairings already exist, and this module deliberately re-does
neither:

- `tests/test_lvs.py`'s netgen tier cross-validates LVS *comparison*
  (issue #343) -- netgen against `klt lvs`'s own matcher, with **both**
  comparators fed by KLayout's extraction;
- `tests/test_extract_magic_oracle.py` cross-validates *extraction* itself
  (issue #2014) -- magic's `ext2spice` device list, every device parameter
  and terminal connectivity against `klt extract`'s, device for device.

Neither runs an LVS comparison whose *layout* side came from somewhere
other than KLayout. That is the gap #2007's own analysis names ("netgen
agreement validates comparison, not extraction, since both share KLayout's
front end"), and it is the one this module closes: magic's `ext2spice`
netlist is fed to `klt lvs` as `request.layout.netlist`, compared against
the same schematic reference `klt lvs`'s own `layout.file` run is compared
against, and the two verdicts must agree -- on a clean corpus cell and on
seeded defects, naming the same device and the same nets.

## What agreement means here

`klt lvs` reaches a verdict from a **netlist**, so a verdict comparison
only tests what #2014's device-by-device equality does not if the netlist
actually travels the whole comparer: device-class resolution, port/pin
matching, net pairing, and parameter comparison, on netlist text shaped by
a different extractor's conventions. The two pipelines are therefore run
end to end and compared on:

- `status` (`"match"` / `"mismatch"`) -- the verdict itself;
- for a seeded defect, the *device* the error entries implicate (class and
  reference-side identifier) and the *nets* they implicate, not merely that
  both said "mismatch".

The adapter that renders magic's netlist as SPICE `klt lvs` can read lives
in `tests/helpers/magic_oracle.py`
(:func:`~helpers.magic_oracle.write_magic_lvs_netlist`) -- there is exactly
one magic driver in this repo and this module does not add a second.

## The declared naming differences, and why they are translated

`docs/design/magic-oracle.md` already declares two naming conventions that
differ between the two extractors and mean nothing to a verdict: magic
names a MOSFET's bulk from its own well/substrate node (`VNB`, `SUB`) where
`klt` synthesises `vsubs`, and magic invents position-derived names for
unlabelled internal nets (`w_n86_453#`) where `klt` numbers them (`$5`).
Left untranslated they produce a *false* mismatch, which is why the adapter
translates them -- and
`test_untranslated_bulk_naming_would_have_produced_a_false_mismatch` is the
falsifiability check that the translation is load-bearing rather than
decorative.

`klt`'s promotion of its synthesized substrate net to a top-level pin is
the third instance of the same convention gap, and is handled the same way
(`extra_ports=("vsubs",)`): a pin-count difference is a verdict in every
LVS comparer, and this one is a convention, not a defect.

## Gating

Real-binary-gated exactly like the two modules above: without `magic` or a
magic technology file the magic-driven tests skip cleanly (the adapter's
own fail-closed tests need neither and always run).
`.github/workflows/magic-oracle.yml` is the opt-in job that satisfies the
gate and asserts nothing skipped.
"""

from __future__ import annotations

import json
from pathlib import Path

import klayout.db as kdb
import pytest

from helpers import magic_oracle
from helpers.magic_oracle import (
    MagicDevice,
    MagicExtractResult,
    MagicOracleError,
    run_magic_extract,
    write_magic_lvs_netlist,
)
from klayout_tools.extract import run_extract
from klayout_tools.lvs import run_lvs

CORPUS_DIR = Path(__file__).parent / "corpus"
SKY130_INV = CORPUS_DIR / "sky130" / "sky130_fd_sc_hd__inv_1.gds"
GF180_CLKINV = CORPUS_DIR / "gf180mcu" / "gf180mcu_fd_sc_mcu9t5v0__clkinv_1.gds"

#: Schematic reference for `sky130_fd_sc_hd__inv_1` -- a hand-written
#: netlist of what the cell *is*, deliberately not either extractor's own
#: output: a reference taken from one of the two pipelines would make that
#: pipeline's verdict trivially "match" and the comparison circular. Bulk
#: terminals name the nets `klt`'s extraction resolves them to (`vsubs` for
#: the deck-synthesized substrate, the labelled `VPB` nwell for the PMOS);
#: the magic side reaches the same names through the adapter's declared
#: translation.
SKY130_INV_REFERENCE = """
.subckt sky130_fd_sc_hd__inv_1 A VGND VPB VPWR Y vsubs
M1 Y A VGND vsubs nfet L=0.15U W=0.65U
M2 Y A VPWR VPB pfet L=0.15U W=1.0U
.ends
"""

#: The same for `gf180mcu_fd_sc_mcu9t5v0__clkinv_1`. Its nwell carries no
#: label in the corpus cell, so the PMOS bulk lands on an internal net
#: neither extractor can name meaningfully (`klt`: `$5`; magic:
#: `w_n86_453#`) -- `NWELL` here is a third arbitrary name for it, which is
#: the point: the comparer pairs that net topologically or this whole
#: pairing is not measuring what it claims to.
GF180_CLKINV_REFERENCE = """
.subckt gf180mcu_fd_sc_mcu9t5v0__clkinv_1 I VDD VSS ZN vsubs
M1 ZN I VSS vsubs nfet L=0.6U W=0.73U
M2 ZN I VDD NWELL pfet L=0.5U W=1.83U
.ends
"""

#: `klt` promotes its synthesized substrate net to a top-level pin; magic
#: declares no port for the substrate node. See the module docstring.
SUBSTRATE_PORT = ("vsubs",)


def _require_oracle(deck: str) -> None:
    reason = magic_oracle.oracle_skip_reason(deck)
    if reason:
        pytest.skip(reason)


@pytest.fixture
def sky130_oracle() -> str:
    _require_oracle("sky130")
    return "sky130"


@pytest.fixture
def gf180mcu_oracle() -> str:
    _require_oracle("gf180mcu")
    return "gf180mcu"


def _top_cell(path: Path | str) -> str:
    layout = kdb.Layout()
    layout.read(str(path))
    return layout.top_cell().name


def _bbox_um(path: Path | str) -> tuple[float, float, float, float]:
    layout = kdb.Layout()
    layout.read(str(path))
    box = layout.top_cell().bbox()
    dbu = layout.dbu
    return (
        round(box.left * dbu, 4),
        round(box.bottom * dbu, 4),
        round(box.right * dbu, 4),
        round(box.top * dbu, 4),
    )


def _write(path: Path, text: str) -> str:
    path.write_text(text, encoding="utf-8")
    return str(path)


def _klt_lvs(*, layout: dict, reference: str, top: str) -> dict:
    """`klt lvs`'s own verdict -- the inline-extraction shape for a GDS
    (`{"file", "deck"}`), which is what a caller actually runs."""
    return run_lvs(
        json.dumps(
            {
                "layout": layout,
                "reference": {"netlist": reference, "top": top},
            }
        )
    )


def _magic_fed_lvs(
    gds: Path | str,
    *,
    deck: str,
    cell: str,
    reference: str,
    work_dir: Path,
    aliases: dict[str, str] | None = None,
    extra_ports: tuple[str, ...] = SUBSTRATE_PORT,
) -> tuple[dict, MagicExtractResult, str]:
    """Extract ``gds`` with magic, render its netlist as `klt lvs`'s layout
    side, and return ``(report, magic_result, netlist_path)``."""
    result = run_magic_extract(
        str(gds), deck=deck, cell=cell, work_dir=work_dir / "magic"
    )
    netlist_path = write_magic_lvs_netlist(
        result,
        work_dir / "magic_layout.spice",
        deck=deck,
        aliases=aliases,
        extra_ports=extra_ports,
    )
    report = _klt_lvs(
        layout={"netlist": netlist_path, "top": cell},
        reference=reference,
        top=cell,
    )
    return report, result, netlist_path


def _errors(report: dict) -> list[dict]:
    """Only the entries that carry a verdict. `klt lvs` also emits
    `severity: "warning"` disclosures (`device.body_unverified`,
    `topology`) that are, by construction, asymmetric between the two
    pipelines: they describe `klt`'s *own* inline extraction, which the
    magic-fed side does not run. They are not part of the verdict and are
    not compared."""
    return [entry for entry in report["mismatches"] if entry["severity"] == "error"]


#: Which *objects* a report implicates is read from every entry, not only
#: the `severity: "error"` ones. `klt lvs` downgrades an unmatched
#: device/net to a warning when a richer `device.property` finding already
#: explains it (`lvs_mismatch.py`'s degraded-pair handling), and whether
#: that downgrade fires differs between the two request shapes these tests
#: compare -- the inline-extraction shape reports the widened NMOS as a
#: plain `device.unmatched`, the pre-extracted shape recognises the pair and
#: reports the parameter difference, downgrading the unmatched entries
#: behind it. The *verdict* is compared through `status` and `_errors`; the
#: identity of what failed is compared across all entries, so this reporting
#: asymmetry cannot make two agreeing pipelines look like disagreeing ones.


def _implicated_device_classes(report: dict) -> set[str]:
    return {
        entry["device"]["class"].lower()
        for entry in report["mismatches"]
        if entry.get("device") and entry["device"].get("class")
    }


def _reference_device_ids(report: dict) -> set[str]:
    """Reference-side device identifiers the report names. The reference
    netlist is byte-identical across both pipelines, so these are directly
    comparable (layout-side identifiers are each extractor's own instance
    names and are not)."""
    return {
        entry["device"]["reference"]
        for entry in report["mismatches"]
        if entry.get("device") and entry["device"].get("reference")
    }


def _error_category_counts(report: dict) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in _errors(report):
        counts[entry["category"]] = counts.get(entry["category"], 0) + 1
    return counts


def _implicated_nets(report: dict, side: str) -> set[str]:
    """Reference-/layout-side net names the report names, upper-cased.

    Case is normalised because it is a *reader* convention, not a verdict:
    KLayout's SPICE reader upper-cases the names it reads (`vsubs` ->
    `VSUBS`), so the pre-extracted layout shape and the inline-extraction
    shape spell the same net differently.
    """
    names = set()
    for entry in report["mismatches"]:
        net = entry.get("net")
        if net and net.get(side):
            names.add(net[side].upper())
    return names


# --------------------------------------------------------------------------- #
# Seeded defects
# --------------------------------------------------------------------------- #


def _seed_wider_nmos(source: Path, dest: Path) -> float:
    """Copy ``source``, stretching the NMOS diffusion island upwards so the
    poly/diff overlap -- the transistor's channel width -- grows from
    0.65 µm to 0.755 µm.

    This is the "parameter mismatch large enough to fail LVS but not
    necessarily DRC" defect #2316 names. It is the sharpest of the three
    shapes it offers, because it leaves the topology completely intact: a
    correct comparer reaches the *same* graph on both sides and disagrees
    about exactly one device, so "both reported a mismatch on the same
    device" is a real claim rather than a whole-circuit cascade in which
    every object is trivially unmatched.

    The new top edge (0.99 µm) stays clear of the poly's own jog at
    0.995 µm and of the input contact at 1.075 µm, so nothing but the
    channel width changes. Returns the new width in µm.
    """
    layout = kdb.Layout()
    layout.read(str(source))
    top = layout.top_cell()
    diff = layout.layer(65, 20)
    boxes = [shape.box.dup() for shape in top.shapes(diff).each()]
    nmos = [box for box in boxes if box.bottom < 1000]
    assert len(nmos) == 1, f"fixture drifted: expected one NMOS diff island, {boxes}"
    top.shapes(diff).clear()
    for box in boxes:
        if box == nmos[0]:
            box = kdb.Box(box.left, box.bottom, box.right, 990)
        top.shapes(diff).insert(box)
    layout.write(str(dest))
    return round((990 - nmos[0].bottom) * layout.dbu, 4)


def _seed_net_short(source: Path, dest: Path) -> None:
    """Copy ``source``, adding an li1 bridge that shorts the inverter's
    input (`A`) to its output (`Y`).

    The connectivity counterpart of the width defect above, and the same
    bridge `tests/test_extract_magic_oracle.py` seeds -- reused here so the
    two pairings react to one fixture, one at the device-list level and one
    at the verdict level.
    """
    layout = kdb.Layout()
    layout.read(str(source))
    top = layout.top_cell()
    li1 = layout.layer(67, 20)
    top.shapes(li1).insert(kdb.Box(640, 1100, 830, 1300))
    layout.write(str(dest))


# --------------------------------------------------------------------------- #
# Known-good case: both pipelines must reach the same clean verdict
# --------------------------------------------------------------------------- #


def test_sky130_clean_inverter_lvs_verdicts_agree(sky130_oracle, tmp_path):
    """#2316's clean-agreement criterion: the same schematic reference,
    compared once against `klt`'s own extraction of the corpus cell and
    once against magic's, must reach `status: "match"` both times."""
    cell = _top_cell(SKY130_INV)
    reference = _write(tmp_path / "ref.spice", SKY130_INV_REFERENCE)

    klt_report = _klt_lvs(
        layout={"file": str(SKY130_INV), "deck": sky130_oracle},
        reference=reference,
        top=cell,
    )
    magic_report, result, _ = _magic_fed_lvs(
        SKY130_INV,
        deck=sky130_oracle,
        cell=cell,
        reference=reference,
        work_dir=tmp_path,
    )

    # Evidence the magic half really ran over the same geometry, before any
    # verdict is trusted (#2007 criterion 2).
    assert result.tech_name == "sky130A"
    assert result.cell_bbox_um == _bbox_um(SKY130_INV)
    assert result.device_count == 2

    assert klt_report["status"] == "match"
    assert magic_report["status"] == "match"
    assert _errors(klt_report) == []
    assert _errors(magic_report) == []


def test_magic_fed_layout_netlist_is_magics_own_extraction(sky130_oracle, tmp_path):
    """The layout side really is magic's answer, not a re-spelling of
    `klt`'s: magic writes PDK-model subcircuit calls, `klt` writes deck-class
    `M` cards, and the values the adapter carries across are magic's."""
    cell = _top_cell(SKY130_INV)
    result = run_magic_extract(
        str(SKY130_INV), deck=sky130_oracle, cell=cell, work_dir=tmp_path / "magic"
    )
    path = write_magic_lvs_netlist(
        result,
        tmp_path / "magic_layout.spice",
        deck=sky130_oracle,
        extra_ports=SUBSTRATE_PORT,
    )
    rendered = Path(path).read_text(encoding="utf-8")

    # magic's own output: `X` subcircuit calls naming the full PDK model.
    assert "sky130_fd_pr__nfet_01v8" in result.netlist_text
    assert "\nX0 " in result.netlist_text
    # The adapter's output: `M` cards on the deck's own class names, with
    # magic's own parameter values and magic's own connectivity.
    assert "M0 Y A VGND vsubs nfet L=0.15U W=0.65U" in rendered
    assert "M1 Y A VPWR VPB pfet L=0.15U W=1U" in rendered
    assert rendered.startswith(f"* {cell} as extracted by magic ")


def test_gf180mcu_clean_clkinv_lvs_verdicts_agree(gf180mcu_oracle, tmp_path):
    """The same agreement one PDK over -- and the case that exercises both
    declared naming differences at once: magic calls the substrate `SUB`
    (`klt`: `vsubs`) and the unlabelled nwell `w_<x>_<y>#` (`klt`: `$5`),
    and the reference netlist calls that nwell a third thing again."""
    cell = _top_cell(GF180_CLKINV)
    reference = _write(tmp_path / "ref.spice", GF180_CLKINV_REFERENCE)

    klt_report = _klt_lvs(
        layout={"file": str(GF180_CLKINV), "deck": gf180mcu_oracle},
        reference=reference,
        top=cell,
    )
    magic_report, result, netlist_path = _magic_fed_lvs(
        GF180_CLKINV,
        deck=gf180mcu_oracle,
        cell=cell,
        reference=reference,
        work_dir=tmp_path,
    )

    assert result.tech_name == "gf180mcuC"
    assert result.cell_bbox_um == _bbox_um(GF180_CLKINV)
    # The unlabelled nwell really was renamed, i.e. the translation ran.
    assert any(net.endswith("#") for net in result.nets)
    rendered = Path(netlist_path).read_text(encoding="utf-8")
    assert "#" not in rendered
    assert magic_oracle.GENERATED_NET_PREFIX + "1" in rendered

    assert klt_report["status"] == "match"
    assert magic_report["status"] == "match"
    assert _errors(magic_report) == []


def test_declared_bulk_naming_difference_cannot_decide_the_verdict(
    sky130_oracle, tmp_path
):
    """The naming criterion, stated so it can fail.

    The difference is real and is asserted here rather than assumed: magic
    names the NMOS bulk `VNB`, `klt` names the same node `vsubs`. Two
    independent things then stop it producing a false mismatch, and both
    are checked:

    1. the adapter *translates* it, so none of magic's naming conventions
       reach the comparer at all -- the rendered netlist says `vsubs`;
    2. the `klayout` engine pairs those nets **topologically** anyway --
       verified by comparing the deliberately untranslated netlist too,
       which also reaches `"match"`.

    Recording (2) is what keeps (1) honest. Were the untranslated form to
    start mismatching, this test fails and the translation's status changes
    from a normalisation to the only thing standing between this pairing
    and a false verdict -- which is a fact about the oracle a reader must
    not have to rediscover.
    """
    cell = _top_cell(SKY130_INV)
    reference = _write(tmp_path / "ref.spice", SKY130_INV_REFERENCE)

    # The difference itself.
    result = run_magic_extract(
        str(SKY130_INV), deck=sky130_oracle, cell=cell, work_dir=tmp_path / "magic"
    )
    klt_extracted = run_extract(
        str(SKY130_INV), sky130_oracle, output=str(tmp_path / "klt.spice")
    )
    assert "VNB" in result.nets and "vsubs" not in result.nets
    assert [
        device["nets"]["b"]
        for device in klt_extracted["devices"]
        if device["class"] == "nfet"
    ] == ["vsubs"]

    # (1) the adapter translates it.
    translated = magic_oracle.magic_lvs_netlist_text(
        result, deck=sky130_oracle, extra_ports=SUBSTRATE_PORT
    )
    assert "VNB" not in translated
    assert " vsubs nfet " in translated

    # (2) and the comparer does not need it to.
    untranslated = _write(
        tmp_path / "untranslated.spice",
        magic_oracle.magic_lvs_netlist_text(
            result, deck=sky130_oracle, aliases={}, extra_ports=()
        ),
    )
    report = _klt_lvs(
        layout={"netlist": untranslated, "top": cell},
        reference=reference,
        top=cell,
    )
    assert report["status"] == "match"


# --------------------------------------------------------------------------- #
# Seeded defects: both pipelines must fail on the same device / the same nets
# --------------------------------------------------------------------------- #


def test_seeded_width_defect_is_a_mismatch_on_the_same_device_in_both_pipelines(
    sky130_oracle, tmp_path
):
    """#2316's seeded-defect criterion. A widened NMOS channel leaves the
    topology untouched, so both pipelines must (a) return `"mismatch"`, and
    (b) implicate the *same* device and the *same* nets -- not merely both
    say "mismatch"."""
    broken = tmp_path / "inv_wide_nmos.gds"
    new_width = _seed_wider_nmos(SKY130_INV, broken)
    assert new_width == 0.755, "fixture drifted: seeded width is not 0.755 um"
    cell = _top_cell(broken)
    reference = _write(tmp_path / "ref.spice", SKY130_INV_REFERENCE)

    # Both extractors must first *see* the widened channel, or the compare
    # below would be agreeing about the wrong thing.
    klt_extracted = run_extract(
        str(broken), sky130_oracle, output=str(tmp_path / "klt.spice")
    )
    assert [
        device["params"]["w_um"]
        for device in klt_extracted["devices"]
        if device["class"] == "nfet"
    ] == [new_width]

    klt_report = _klt_lvs(
        layout={"file": str(broken), "deck": sky130_oracle},
        reference=reference,
        top=cell,
    )
    magic_report, result, _ = _magic_fed_lvs(
        broken,
        deck=sky130_oracle,
        cell=cell,
        reference=reference,
        work_dir=tmp_path,
    )
    assert [
        device.params["w"] for device in result.devices if device.kind == "nfet"
    ] == [new_width]

    assert klt_report["status"] == "mismatch"
    assert magic_report["status"] == "mismatch"

    # The same device: only the NFET is implicated, on both sides.
    assert _implicated_device_classes(klt_report) == {"nfet"}
    assert _implicated_device_classes(magic_report) == {"nfet"}
    assert _reference_device_ids(klt_report) == _reference_device_ids(magic_report)

    # The same nets: the NMOS's own source and bulk nets, on both sides.
    assert _implicated_nets(klt_report, "reference") == {"VGND", "VSUBS"}
    assert _implicated_nets(magic_report, "reference") == {"VGND", "VSUBS"}
    assert _implicated_nets(klt_report, "layout") == {"VGND", "VSUBS"}
    assert _implicated_nets(magic_report, "layout") == {"VGND", "VSUBS"}

    # magic's netlist additionally carries the width difference all the way
    # into a structured `device.property` finding, which is the strongest
    # possible statement of "the same defect": name, and both values.
    (width_entry,) = [
        entry
        for entry in _errors(magic_report)
        if entry["category"] == "device.property"
        and entry["property"]["name"] == "w_um"
    ]
    assert width_entry["property"]["layout"] == pytest.approx(new_width)
    assert width_entry["property"]["reference"] == pytest.approx(0.65)
    assert width_entry["device"]["class"].lower() == "nfet"


def test_seeded_short_is_a_mismatch_with_the_same_findings_in_both_pipelines(
    sky130_oracle, tmp_path
):
    """The connectivity-defect half: an input-to-output short must be a
    mismatch in both pipelines, with the same error categories in the same
    counts and the same reference-side nets left unpaired.

    Unlike the width defect, a short collapses the whole graph, so *which*
    objects are unmatched is a weaker claim -- what is load-bearing here is
    that two independently extracted netlists put the comparer in exactly
    the same state.
    """
    shorted = tmp_path / "inv_short.gds"
    _seed_net_short(SKY130_INV, shorted)
    cell = _top_cell(shorted)
    reference = _write(tmp_path / "ref.spice", SKY130_INV_REFERENCE)

    klt_report = _klt_lvs(
        layout={"file": str(shorted), "deck": sky130_oracle},
        reference=reference,
        top=cell,
    )
    magic_report, result, _ = _magic_fed_lvs(
        shorted,
        deck=sky130_oracle,
        cell=cell,
        reference=reference,
        work_dir=tmp_path,
    )

    # The short is real on the magic side too: gate and drain share a net.
    for device in result.devices:
        by_role = dict(
            zip(
                magic_oracle.MAGIC_MOSFET_TERMINAL_ORDER,
                device.terminals,
                strict=True,
            )
        )
        assert by_role["g"] == by_role["d"], device

    assert klt_report["status"] == "mismatch"
    assert magic_report["status"] == "mismatch"
    assert _error_category_counts(klt_report) == _error_category_counts(magic_report)
    assert _implicated_nets(klt_report, "reference") == _implicated_nets(
        magic_report, "reference"
    )
    assert _implicated_device_classes(klt_report) == _implicated_device_classes(
        magic_report
    )


# --------------------------------------------------------------------------- #
# Provenance (#2007 criterion 4)
# --------------------------------------------------------------------------- #


def test_lvs_oracle_provenance_records_the_same_input_bytes(sky130_oracle, tmp_path):
    """A disagreement is only attributable if both halves provably read the
    same stream. `klt lvs`'s own `provenance.input.content_hash` and the
    oracle's hash of the same file must agree."""
    cell = _top_cell(SKY130_INV)
    reference = _write(tmp_path / "ref.spice", SKY130_INV_REFERENCE)
    klt_report = _klt_lvs(
        layout={"file": str(SKY130_INV), "deck": sky130_oracle},
        reference=reference,
        top=cell,
    )
    _magic_report, result, _ = _magic_fed_lvs(
        SKY130_INV,
        deck=sky130_oracle,
        cell=cell,
        reference=reference,
        work_dir=tmp_path,
    )

    provenance = magic_oracle.oracle_provenance(
        deck=sky130_oracle,
        gds_path=str(SKY130_INV),
        klt_report=klt_report,
        tech_version=result.tech_version,
    )

    assert provenance["oracle"]["tool"] == "magic"
    assert provenance["oracle"]["version"] == magic_oracle.magic_version()
    assert provenance["oracle"]["deck"]["content_hash"].startswith("sha256:")
    assert (
        provenance["klt"]["input"]["content_hash"]
        == provenance["input"]["content_hash"]
    )


# --------------------------------------------------------------------------- #
# The adapter itself: fail closed, never render a comparable-looking netlist
# out of something magic did not actually produce. No magic binary needed.
# --------------------------------------------------------------------------- #


def _fake_result(
    *,
    devices: tuple[MagicDevice, ...],
    ports: tuple[str, ...],
    nets: frozenset[str],
    top: str = "cell",
) -> MagicExtractResult:
    return MagicExtractResult(
        top=top,
        ports=ports,
        nets=nets,
        devices=devices,
        netlist_path="/nonexistent/cell.magic.spice",
        netlist_text="",
        tech_name="sky130A",
        tech_version="1.0.608",
        cell_bbox_um=(0.0, 0.0, 1.0, 1.0),
        log="",
    )


def _fake_mosfet(
    kind: str = "nfet",
    terminals: tuple[str, ...] = ("Y", "A", "VGND", "VNB"),
    **params: float,
) -> MagicDevice:
    values = {"l": 0.15, "w": 0.65, "as": 0.169, "ad": 0.169, "ps": 1.82, "pd": 1.82}
    values.update(params)
    return MagicDevice(
        model=f"sky130_fd_pr__{kind}_01v8",
        kind=kind,
        terminals=terminals,
        params=values,
    )


def test_adapter_refuses_a_device_less_netlist(tmp_path):
    """An empty device list would compare clean against an equally empty
    reference -- the exact false-clean `magic_oracle`'s fail-closed rules
    exist to prevent."""
    result = _fake_result(devices=(), ports=("A",), nets=frozenset({"A", "VNB"}))
    with pytest.raises(MagicOracleError, match="no devices"):
        write_magic_lvs_netlist(result, tmp_path / "out.spice", deck="sky130")


def test_adapter_refuses_an_unknown_deck_without_explicit_aliases(tmp_path):
    result = _fake_result(
        devices=(_fake_mosfet(),),
        ports=("A", "Y", "VGND", "VNB"),
        nets=frozenset({"A", "Y", "VGND", "VNB"}),
    )
    with pytest.raises(MagicOracleError, match="no magic bulk-net alias table"):
        write_magic_lvs_netlist(result, tmp_path / "out.spice", deck="sg13g2")


def test_adapter_refuses_a_stale_bulk_alias(tmp_path):
    """An alias whose source net this cell does not have would translate
    nothing at all, silently, and the untranslated name would then read as a
    real mismatch."""
    result = _fake_result(
        devices=(_fake_mosfet(terminals=("Y", "A", "VGND", "SUB")),),
        ports=("A", "Y", "VGND", "SUB"),
        nets=frozenset({"A", "Y", "VGND", "SUB"}),
    )
    with pytest.raises(MagicOracleError, match="stale for this deck/cell"):
        write_magic_lvs_netlist(result, tmp_path / "out.spice", deck="sky130")


def test_adapter_refuses_a_translation_that_would_merge_two_nets(tmp_path):
    """Renaming one net onto another's name is a connectivity change, and
    an LVS comparer cannot tell the difference afterwards."""
    result = _fake_result(
        devices=(_fake_mosfet(),),
        ports=("A", "Y", "VGND", "VNB"),
        nets=frozenset({"A", "Y", "VGND", "VNB"}),
    )
    with pytest.raises(MagicOracleError, match="merge distinct magic nets"):
        write_magic_lvs_netlist(
            result, tmp_path / "out.spice", deck="sky130", aliases={"VNB": "VGND"}
        )


def test_adapter_refuses_a_device_it_cannot_express(tmp_path):
    """Silently dropping a device magic found is how a broken layout
    reports a clean match."""
    result = _fake_result(
        devices=(_fake_mosfet(kind="sky130_fd_pr__res_high_po"),),
        ports=("A", "Y", "VGND", "VNB"),
        nets=frozenset({"A", "Y", "VGND", "VNB"}),
    )
    with pytest.raises(MagicOracleError, match="refusing to drop it"):
        write_magic_lvs_netlist(result, tmp_path / "out.spice", deck="sky130")


def test_adapter_refuses_a_mosfet_with_an_unexpected_terminal_count(tmp_path):
    result = _fake_result(
        devices=(_fake_mosfet(terminals=("Y", "A", "VGND")),),
        ports=("A", "Y", "VGND", "VNB"),
        nets=frozenset({"A", "Y", "VGND", "VNB"}),
    )
    with pytest.raises(MagicOracleError, match="not the"):
        write_magic_lvs_netlist(result, tmp_path / "out.spice", deck="sky130")


def test_adapter_refuses_a_mosfet_missing_a_parameter(tmp_path):
    device = MagicDevice(
        model="sky130_fd_pr__nfet_01v8",
        kind="nfet",
        terminals=("Y", "A", "VGND", "VNB"),
        params={"l": 0.15},
    )
    result = _fake_result(
        devices=(device,),
        ports=("A", "Y", "VGND", "VNB"),
        nets=frozenset({"A", "Y", "VGND", "VNB"}),
    )
    with pytest.raises(MagicOracleError, match="missing parameter"):
        write_magic_lvs_netlist(result, tmp_path / "out.spice", deck="sky130")


def test_adapter_refuses_an_invented_extra_port(tmp_path):
    """`extra_ports` promotes an existing net; inventing one would be a
    connectivity claim, not a rename."""
    result = _fake_result(
        devices=(_fake_mosfet(),),
        ports=("A", "Y", "VGND", "VNB"),
        nets=frozenset({"A", "Y", "VGND", "VNB"}),
    )
    with pytest.raises(MagicOracleError, match="not a net of magic's netlist"):
        write_magic_lvs_netlist(
            result, tmp_path / "out.spice", deck="sky130", extra_ports=("VPWR",)
        )


def test_adapter_renames_magic_generated_nets_stably(tmp_path):
    """magic's position-derived names for unlabelled nets are renamed to
    stable, collision-free ones -- in sorted order, so the same netlist
    always renders identically."""
    device = _fake_mosfet(terminals=("w_n86_453#", "A", "a_74_47#", "VNB"))
    result = _fake_result(
        devices=(device,),
        ports=("A", "VNB"),
        nets=frozenset({"A", "VNB", "a_74_47#", "w_n86_453#"}),
    )
    translation = magic_oracle.magic_net_translation(result, deck="sky130")

    assert translation == {
        "A": "A",
        "VNB": "vsubs",
        "a_74_47#": f"{magic_oracle.GENERATED_NET_PREFIX}1",
        "w_n86_453#": f"{magic_oracle.GENERATED_NET_PREFIX}2",
    }
    path = write_magic_lvs_netlist(result, tmp_path / "out.spice", deck="sky130")
    rendered = Path(path).read_text(encoding="utf-8")
    assert "#" not in rendered
    assert (
        f"M0 {magic_oracle.GENERATED_NET_PREFIX}2 A "
        f"{magic_oracle.GENERATED_NET_PREFIX}1 vsubs nfet" in rendered
    )


def test_adapter_carries_every_extracted_parameter(tmp_path):
    """The adapter is a format translation, not a filter: every parameter
    magic reported reaches the netlist `klt lvs` reads."""
    result = _fake_result(
        devices=(_fake_mosfet(),),
        ports=("A", "Y", "VGND", "VNB"),
        nets=frozenset({"A", "Y", "VGND", "VNB"}),
    )
    path = write_magic_lvs_netlist(
        result, tmp_path / "out.spice", deck="sky130", extra_ports=SUBSTRATE_PORT
    )
    rendered = Path(path).read_text(encoding="utf-8")

    assert (
        "M0 Y A VGND vsubs nfet L=0.15U W=0.65U AS=0.169P AD=0.169P "
        "PS=1.82U PD=1.82U" in rendered
    )
    # `vsubs` is magic's translated `VNB`, promoted to a port.
    assert ".SUBCKT cell A Y VGND vsubs" in rendered
