"""Stable terminal identities for top-level anonymous nets in extraction reports."""

from __future__ import annotations

from pathlib import Path

import klayout.db as kdb
import pytest

from klayout_tools.extract import run_extract
from klayout_tools.extract_report import _describe_devices, _describe_nets
from test_extract import (
    _DIGEST_FIXTURE_SHA256,
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


def _circuit_report(first=3, second=7, *, rewire=False, tie_terminals=False):
    netlist = kdb.Netlist()
    circuit = kdb.Circuit()
    circuit.name = "TOP"
    netlist.add(circuit)
    mos = kdb.DeviceClassMOS4Transistor()
    mos.name = "MOS"
    netlist.add(mos)
    source = circuit.create_net("VSS")
    gate = circuit.create_net("IN")
    internal = circuit.create_net()
    internal.cluster_id = first
    drain = circuit.create_net()
    drain.cluster_id = second
    circuit.create_net().cluster_id = 999
    m1 = circuit.create_device(mos, "M1")
    m1.connect_terminal("D", internal)
    m1.connect_terminal("G", gate)
    m1.connect_terminal("S", internal if tie_terminals else source)
    m2 = circuit.create_device(mos, "M2")
    m2.connect_terminal("D", drain)
    m2.connect_terminal("G", gate if rewire else internal)
    m2.connect_terminal("S", source)
    devices, _ = _describe_devices(circuit)
    return {"devices": devices, "nets": _describe_nets(circuit)}


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
    for entry in report["nets"]:
        name = entry["name"]
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


def test_structural_id_ignores_anonymous_net_numbering():
    original = _circuit_report()
    renumbered = _circuit_report(81, 12)
    first = {n["name"]: n["structural_id"] for n in original["nets"]}
    second = {n["name"]: n["structural_id"] for n in renumbered["nets"]}
    assert first[r"\$3"] == second[r"\$81"] == "$anon<M1.d,M2.g>"
    assert first[r"\$7"] == second[r"\$12"] == "$anon<M2.d>"

    rewired = _circuit_report(rewire=True)
    assert (
        next(n for n in rewired["nets"] if n["name"] == r"\$3")["structural_id"]
        == "$anon<M1.d>"
    )


def test_structural_id_null_when_no_identity():
    ids = {n["name"]: n["structural_id"] for n in _circuit_report()["nets"]}
    assert ids["IN"] is None
    assert ids["VSS"] is None
    assert ids[r"\$999"] is None


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


def test_normalized_netlist_hash_ignores_structural_id_field(tmp_path):
    """Issue #5735 acceptance criterion 4: annotating the hash payload's own
    `nets[]` entries with `structural_id` leaves
    `netlist_normalized_sha256` byte-identical -- the field is stripped
    alongside `net_id` before the digest is serialized."""
    from klayout_tools.extract import _normalized_netlist_sha256

    report = _circuit_report()
    legacy = [
        {k: v for k, v in n.items() if k != "structural_id"} for n in report["nets"]
    ]
    assert _normalized_netlist_sha256(
        report["devices"], report["nets"]
    ) == _normalized_netlist_sha256(report["devices"], legacy)

    # Golden-digest pin across the field's introduction: the annotated
    # `_digest_fixture` (which carries floating nets, non-dict entries and
    # one-device-two-terminals shapes) must still hash to the pre-change
    # constant computed with the pre-#3568 implementation.
    fixture_devices, fixture_nets = _digest_fixture()
    for entry in fixture_nets:
        if isinstance(entry, dict):
            entry["structural_id"] = "$anon<ignored.by.digest>"
    assert (
        _normalized_netlist_sha256(fixture_devices, fixture_nets)
        == _DIGEST_FIXTURE_SHA256
    )


def test_canonicalization_strips_structural_id(tmp_path):
    """The mechanism behind criteria 4-5: `_canonicalize_extract_report_for_rerun_diff`
    drops each top-level net row’s `structural_id` (the identity is redundant with the
    anonymous-name rewrite it performs anyway, and dropping it keeps
    pre-field committed reports comparable), so a committed report
    predating the field can never diff against a fresh one on the new key."""
    from klayout_tools.extract import _canonicalize_extract_report_for_rerun_diff

    report = _circuit_report()
    canonical = _canonicalize_extract_report_for_rerun_diff(report)
    for entry in canonical["nets"]:
        assert "structural_id" not in entry, entry
    # ...and the anonymous names still rewrite to the same `$anon<...>`
    # spelling the stripped field carried.
    by_name = {entry["name"] for entry in canonical["nets"]}
    assert {"$anon<M1.d,M2.g>", "$anon<M2.d>"} <= by_name


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


def test_structural_id_keeps_duplicate_attachments(tmp_path):
    """A net both of whose terminals belong to one device keeps the
    duplicate in its identity: `_net_attachment_index` deliberately stores
    one `<device>.<terminal>` entry per terminal, so the emitted token
    carries both (issue #5735 edge case)."""
    nets = _circuit_report(rewire=True, tie_terminals=True)["nets"]
    assert (
        next(n for n in nets if n["name"] == r"\$3")["structural_id"]
        == "$anon<M1.d,M1.s>"
    )


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


@pytest.mark.parametrize("label", ["$2", "$123"])
def test_drawn_dollar_label_does_not_alias_an_anonymous_net(tmp_path, label):
    layout = _make_series_nmos_layout()
    (output,) = (
        shape
        for shape in layout.top_cell().shapes(layout.layer(67, 5)).each()
        if shape.is_text() and shape.text.string == "Y"
    )
    output.text_string = label
    gds = _write_gds(layout, tmp_path / "series.gds")
    report = run_extract(gds, "sky130", no_netlist=True)
    named = next(
        net
        for net in report["nets"]
        if any(p["text"] == label for p in net["label_positions_um"])
    )
    assert named["structural_id"] is None
    (internal,) = [net for net in report["nets"] if net["structural_id"] is not None]
    assert internal["device_count"] == 2
    assert internal["structural_id"].count(",") == 1


def test_canonicalize_keeps_same_spelling_outside_top_level_nets():
    from klayout_tools.extract import _canonicalize_extract_report_for_rerun_diff

    def report(value):
        return {
            "devices": [{"name": "M", "nets": {"structural_id": value}}],
            "nets": [{"name": "A", "structural_id": None}],
            "provenance": {"runset": {"deck_vars": {"structural_id": value}}},
        }

    normalized = _canonicalize_extract_report_for_rerun_diff(report("A"))
    assert normalized["nets"] == [{"name": "A"}]
    assert normalized["devices"][0]["nets"] == {"structural_id": "A"}
    assert normalized["provenance"]["runset"]["deck_vars"] == {"structural_id": "A"}
    assert normalized != _canonicalize_extract_report_for_rerun_diff(report("B"))
