"""Tests for ``nets[].structural_id`` (issue #5735): the per-net stable
handle emitted by ``assemble_extract_report``.

Every anonymous net carries the `$anon<...>` structural identity
``_anonymous_net_name_map`` already derives for ``--check --rerun`` and
``netlist_normalized_sha256`` (issue #3407); named nets and zero-terminal
anonymous nets carry ``null``. The field is stripped alongside ``net_id``
by ``_strip_net_id_and_rewrite_names`` so both the digest and
``--check --rerun`` stay byte-identical across the field's introduction.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from klayout_tools.decks import get_extraction_deck
from klayout_tools.extract import run_extract
from klayout_tools.extract_assemble import assemble_extract_report
from test_extract import (
    _DIGEST_FIXTURE_SHA256,
    _anon_connectivity,
    _digest_fixture,
    _make_series_nmos_layout,
    _write_gds,
    _write_report,
    rerun_extract_report,
)


@pytest.fixture
def tmp_path(tmp_path: Path) -> Path:
    """Same shadow as ``test_extract.py``'s (issue #2659): seed a ``.git``
    marker so ``tmp_path`` resolves as its own repo root, making the committed
    report's ``file`` field ``{path: ..., scope: "repo"}`` -- the one shape
    ``--check``/``--rerun`` can resolve."""
    (tmp_path / ".git").mkdir()
    return tmp_path


def _annotate_structural_ids(devices: list[Any], nets: list[Any]) -> None:
    """Annotate ``nets`` entries with ``structural_id`` exactly the way
    ``assemble_extract_report`` does (issue #5735) -- the synthetic-test
    twin of the production loop; the real-run tests and
    :func:`_assemble_report` below exercise the assembler itself."""
    from klayout_tools.extract import _anonymous_net_name_map

    name_map = _anonymous_net_name_map({"devices": devices, "nets": nets})
    for entry in nets:
        if isinstance(entry, dict):
            name = entry.get("name")
            entry["structural_id"] = (
                name_map.get(name) if isinstance(name, str) else None
            )


def _assemble_report(
    devices: list[Any], nets: list[Any], tmp_path: Any
) -> dict[str, Any]:
    """Run the real ``assemble_extract_report`` over a synthetic
    ``devices``/``nets`` pair (issue #5735): every stage input the assembler
    consumes is filled with its flag-less-run equivalent so the call isolates
    the ``structural_id`` annotation loop. ``nets`` entries gain the ``pin``
    key the assembler's ``pin_count`` sum reads."""
    input_path = tmp_path / "synthetic.gds"
    input_path.write_bytes(b"")
    return assemble_extract_report(
        abstracted_cells=[],
        black_box_regions=[],
        dbu_um=1000,
        dead_metal=[],
        dead_metal_omitted=False,
        dead_metal_summary={},
        deck=get_extraction_deck("sky130"),
        deck_name="sky130",
        deck_options={},
        declared_pins=None,
        def_pin_promotion=None,
        def_pins=None,
        device_counts={},
        device_models=None,
        device_recognition_only_layers=[],
        devices=devices,
        dummy_devices_dropped=0,
        hierarchy_report=None,
        ignored_layers=[],
        label_layers=None,
        matched_device_groups_report=None,
        merged_net_labels=[],
        missing_flavour_markers=[],
        netlist_sha256=None,
        nets=[
            {**entry, "pin": False} if isinstance(entry, dict) else entry
            for entry in nets
        ],
        parasitics_report=None,
        report_parasitics_shape=None,
        path=str(input_path),
        pdk_info=None,
        pin_renames=None,
        pin_ties=[],
        probe_results=[],
        rename_declared_pins=False,
        reported_netlist_path=None,
        single_terminal_nets=[],
        spef_path=None,
        split_net_labels=[],
        subcircuit_report=None,
        tie_declared_pins=False,
        top_cell_name="TOP",
        unbiased_pmos_body_nets=[],
        unmodelled_poly=[],
        unsupported_devices=[],
        voltage_domain_warnings=[],
        warnings=[],
    )


def test_extract_report_nets_carry_structural_id(tmp_path):
    """Every `nets[]` entry carries `structural_id` (issue #5735): the
    anonymous internal node of a series pair gets its `$anon<...>` identity,
    named nets get `null` (their `name` is the label a consumer already
    joins on)."""
    path = _write_gds(_make_series_nmos_layout(), tmp_path / "series.gds")
    report = run_extract(path, "sky130", output=str(tmp_path / "series.spice"))

    assert report["nets"], "fixture must extract nets"
    for entry in report["nets"]:
        assert "structural_id" in entry, entry
    named = {entry["name"]: entry for entry in report["nets"] if entry["pin"] or True}
    for name, entry in named.items():
        if not name.startswith("\\$"):
            assert entry["structural_id"] is None, name
    anonymous = [
        entry
        for entry in report["nets"]
        if entry["name"].startswith("\\$") and entry["device_count"] == 2
    ]
    assert len(anonymous) == 1, [entry["name"] for entry in report["nets"]]
    (internal,) = anonymous
    structural_id = internal["structural_id"]
    assert isinstance(structural_id, str)
    assert structural_id.startswith("$anon<") and structural_id.endswith(">")
    # The identity is the net's sorted `<device>.<terminal>` attachment list
    # -- here T1's drain and T2's source (`$1.d`, `$2.s`: KLayout-synthesized
    # device instance names, lower-cased terminal keys), the two terminals
    # the issue's testbench probe wants to measure across.
    assert ".d," in structural_id or ".d>" in structural_id
    assert ".s," in structural_id or ".s>" in structural_id


def test_structural_id_stable_across_runs(tmp_path):
    """Two extractions of one GDS report equal `structural_id` per net, so a
    consumer joins an older report to the current one on the field alone --
    no `devices[]` parsing (issue #5735 acceptance criterion 2)."""
    path = _write_gds(_make_series_nmos_layout(), tmp_path / "series.gds")
    a = run_extract(path, "sky130", output=str(tmp_path / "a.spice"))
    b = run_extract(path, "sky130", output=str(tmp_path / "b.spice"))
    by_name_a = {entry["name"]: entry["structural_id"] for entry in a["nets"]}
    by_name_b = {entry["name"]: entry["structural_id"] for entry in b["nets"]}
    assert by_name_a == by_name_b
    anonymous_ids = [sid for sid in by_name_a.values() if sid is not None]
    assert anonymous_ids, "fixture must carry an anonymous net with an identity"


def test_structural_id_ignores_anonymous_net_numbering(tmp_path):
    """Synthetic renumbering (the same shape the `netlist_normalized_sha256`
    tests use, issue #5735 acceptance criterion 1): reports that differ only
    in `$N` numbering report equal `structural_id` sets -- a consumer cannot
    tell a renumbering happened -- while a genuine rewiring, routed through
    the real ``assemble_extract_report``, changes them."""
    first: list[Any] = []
    second: list[Any] = []
    for numbers, sink in (((3, 7), first), ((81, 12), second)):
        devices, nets = _anon_connectivity(*numbers)
        _annotate_structural_ids(devices, nets)
        sink.extend(entry["structural_id"] for entry in nets)
    assert sorted(sid for sid in first if sid is not None) == sorted(
        sid for sid in second if sid is not None
    )
    assert len(first) == len(second) == 4  # nulls line up too
    assert first[1] == "$anon<M1.D,M2.G>"  # `\\$3`'s own attachments, verbatim
    assert first[2] == "$anon<M2.D>"

    devices, nets = _anon_connectivity(3, 7)
    base_ids = [
        entry["structural_id"]
        for entry in _assemble_report(devices, nets, tmp_path)["nets"]
    ]
    devices[1]["nets"]["G"] = "IN"  # rewire M2's gate off the anonymous net
    rewired_ids = [
        entry["structural_id"]
        for entry in _assemble_report(devices, nets, tmp_path)["nets"]
    ]
    assert rewired_ids != base_ids


def test_structural_id_null_when_no_identity():
    """`structural_id` is `null` exactly when the net has no structural
    identity (issue #5735 acceptance criterion 3): a named net, or an
    anonymous net with zero device terminals -- the case
    `_net_attachment_key` returns `""` for and
    `_anonymous_net_name_map` skips."""
    devices, nets = _anon_connectivity(3, 7)
    nets.append({"name": "\\$999", "net_id": 999, "device_count": 0})
    _annotate_structural_ids(devices, nets)
    by_name = {entry["name"]: entry for entry in nets if isinstance(entry, dict)}
    assert by_name["IN"]["structural_id"] is None
    assert by_name["VSS"]["structural_id"] is None
    assert by_name["\\$999"]["structural_id"] is None
    assert by_name["\\$3"]["structural_id"] == "$anon<M1.D,M2.G>"
    assert by_name["\\$7"]["structural_id"] == "$anon<M2.D>"


def test_structural_id_present_on_every_net_with_no_netlist(tmp_path):
    """`--no-netlist` still emits the field (issue #5735): the identity
    depends only on `devices[]`/`nets[]`, which are always present -- gating
    it on the written netlist would withhold it from exactly the report a
    consumer is reading."""
    path = _write_gds(_make_series_nmos_layout(), tmp_path / "series.gds")
    report = run_extract(path, "sky130", no_netlist=True)
    assert report["netlist_normalized_sha256"] is None
    assert report["netlist_sha256"] is None
    assert report["nets"]
    for entry in report["nets"]:
        assert "structural_id" in entry, entry
    anonymous = [
        entry for entry in report["nets"] if entry["structural_id"] is not None
    ]
    assert anonymous, "the anonymous internal net must still carry its identity"


def test_normalized_netlist_hash_ignores_structural_id_field():
    """Issue #5735 acceptance criterion 4: annotating the hash payload's own
    `nets[]` entries with `structural_id` leaves
    `netlist_normalized_sha256` byte-identical -- the field is stripped
    alongside `net_id` before the digest is serialized."""
    from klayout_tools.extract import _normalized_netlist_sha256

    devices, nets = _anon_connectivity(3, 7)
    base = _normalized_netlist_sha256(devices, nets)
    _annotate_structural_ids(devices, nets)
    assert _normalized_netlist_sha256(devices, nets) == base

    # Golden-digest pin across the field's introduction: the annotated
    # `_digest_fixture` (which carries floating nets, non-dict entries and
    # one-device-two-terminals shapes) must still hash to the pre-change
    # constant computed with the pre-#3568 implementation.
    fixture_devices, fixture_nets = _digest_fixture()
    _annotate_structural_ids(fixture_devices, fixture_nets)
    assert (
        _normalized_netlist_sha256(fixture_devices, fixture_nets)
        == _DIGEST_FIXTURE_SHA256
    )


def test_canonicalization_strips_structural_id():
    """The mechanism behind criteria 4-5: `_canonicalize_extract_report_for_rerun_diff`
    drops every `structural_id` key (the identity is redundant with the
    anonymous-name rewrite it performs anyway, and dropping it keeps
    pre-field committed reports comparable), so a committed report
    predating the field can never diff against a fresh one on the new key."""
    from klayout_tools.extract import _canonicalize_extract_report_for_rerun_diff

    devices, nets = _anon_connectivity(3, 7)
    _annotate_structural_ids(devices, nets)
    canonical = _canonicalize_extract_report_for_rerun_diff(
        {"devices": devices, "nets": nets}
    )
    for entry in canonical["nets"]:
        assert "structural_id" not in entry, entry
    # ...and the anonymous names still rewrite to the same `$anon<...>`
    # spelling the stripped field carried.
    by_name = {entry["name"] for entry in canonical["nets"]}
    assert {"$anon<M1.D,M2.G>", "$anon<M2.D>"} <= by_name


def test_rerun_matches_report_committed_without_structural_id(tmp_path):
    """Issue #5735 acceptance criterion 5: a report committed *before* the
    field existed -- here simulated by deleting it from a fresh report --
    still verifies `status: "match"` against a current `--rerun`; the new
    field alone never registers as drift (and would otherwise have
    invalidated every committed #3407 evidence pin)."""
    path = _write_gds(_make_series_nmos_layout(), tmp_path / "series.gds")
    report = run_extract(path, "sky130", output=str(tmp_path / "series.spice"))
    assert any(entry["structural_id"] is not None for entry in report["nets"])
    for entry in report["nets"]:
        del entry["structural_id"]
    report_path = tmp_path / "series.extract.json"
    _write_report(report_path, report)

    result = rerun_extract_report(str(report_path))

    assert result["status"] == "match", result["drift"]


def test_structural_id_keeps_duplicate_attachments():
    """A net both of whose terminals belong to one device keeps the
    duplicate in its identity: `_net_attachment_index` deliberately stores
    one `<device>.<terminal>` entry per terminal, so the emitted token
    carries both (issue #5735 edge case)."""
    devices = [{"name": "M1", "nets": {"D": "\\$5", "S": "\\$5"}}]
    nets = [{"name": "\\$5", "net_id": 5}]
    _annotate_structural_ids(devices, nets)
    assert nets[0]["structural_id"] == "$anon<M1.D,M1.S>"


def test_parasitics_nets_gain_no_structural_id(tmp_path):
    """Scope guard (issue #5735): the field lands on top-level `nets[]` only
    -- `parasitics.nets[]` entries are untouched (its rerun sort already
    derives the analogous key internally; extending the emitted field there
    is separate work)."""
    path = _write_gds(_make_series_nmos_layout(), tmp_path / "series.gds")
    report = run_extract(
        path, "sky130", output=str(tmp_path / "series.spice"), parasitics=True
    )
    assert report["nets"]
    assert all("structural_id" in entry for entry in report["nets"])
    assert report["parasitics"] is not None
    assert report["parasitics"]["nets"]
    for entry in report["parasitics"]["nets"]:
        assert "structural_id" not in entry, entry
