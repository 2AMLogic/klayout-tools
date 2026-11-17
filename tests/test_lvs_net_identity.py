"""Per-report stable net ids on ``klt lvs`` mismatches and correspondence.

Issue #5833: anonymous net names restart per circuit, so thousands of
*distinct* layout nets render under one display string (``\\$0``) in a
hierarchical report, and a consumer holding only the committed report
cannot tell how many distinct nets a ``net.split``/``net.unmatched`` list
actually accuses. Every ``mismatches[].net`` and ``net_correspondence[]``
entry now carries ``layout_id``/``reference_id`` -- 0-based ordinals,
unique per side within one report, assigned from the compare logger's
per-circuit ``begin_circuit`` enumeration (``lvs_net_identity``).

These tests pin the ids' own contract: distinct ids under a shared display
name, the join into ``net_correspondence``, the null-on-absent-side
convention, the unchanged surrounding fields, and run-to-run determinism.
The fixture style follows ``tests/test_lvs.py`` (read-only SPICE decks,
no KLayout GUI) without growing that ratcheted file.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from klayout_tools.lvs import run_lvs
from klayout_tools.lvs_supply import supply_fragmentation_findings

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

#: Two leaf circuits (distinct names, so the comparer visits two scopes)
#: whose internal anonymous node is spelled ``\\$0`` in each leaf -- the
#: committed-report shape from the issue: two distinct layout nets, both
#: displaying ``\\$0``, each one-sided-unmatched in its own circuit pair.
_HIERARCHICAL_LAYOUT_SPICE = """
.subckt LEAF1 A B
R0 A \\$0 1k
R1 \\$0 B 1k
.ends
.subckt LEAF2 A B
R0 A \\$0 1k
R1 \\$0 B 1k
.ends
.subckt TOP IN OUT
X1 IN N1 LEAF1
X2 N1 OUT LEAF2
.ends
"""

#: Same hierarchy, but each leaf connects straight through -- no internal
#: node at all -- so each layout ``\\$0`` has no reference counterpart.
_HIERARCHICAL_REFERENCE_SPICE = """
.subckt LEAF1 A B
R0 A B 2k
.ends
.subckt LEAF2 A B
R0 A B 2k
.ends
.subckt TOP IN OUT
X1 IN N1 LEAF1
X2 N1 OUT LEAF2
.ends
"""

#: The ambiguous-pairing negative control's block D (issue #1533's
#: fixture): an ambiguously-resolvable pairing the comparer resolves into
#: both a ``topology`` warning *and* a ``net_correspondence`` entry -- so
#: every mismatch entry's ids have a correspondence row to join to.
_JOIN_LAYOUT_SPICE = """
.subckt chip D_VDD
R1 D_VDD $1 1k
R2 D_VDD $2 1k
.ends
"""

_JOIN_REFERENCE_SPICE = """
.subckt chip D_VDD
R1 D_VDD D_P1 1k
R2 D_VDD D_P2 1k
.ends
"""


def _write(path: Path, text: str) -> str:
    path.write_text(text)
    return str(path)


def _request(tmp_path: Path, layout: str, reference: str, **extra) -> str:
    path = tmp_path / "request.json"
    path.write_text(
        json.dumps(
            {
                "layout": {
                    "netlist": _write(tmp_path / "layout.spice", layout),
                    "top": _top_of(layout),
                },
                "reference": {
                    "netlist": _write(tmp_path / "ref.spice", reference),
                    "top": _top_of(reference),
                },
                **extra,
            }
        )
    )
    return str(path)


def _top_of(spice: str) -> str:
    """The last ``.subckt`` name in a deck -- the compare's top circuit."""
    return [line.split()[1] for line in spice.splitlines() if ".subckt" in line][-1]


def _net_entries(report: dict) -> list[dict]:
    return [m["net"] for m in report["mismatches"] if m["net"] is not None]


def test_distinct_ids_under_shared_display_name(tmp_path):
    """The issue's headline case: two distinct layout nets that both render
    as ``\\$0`` (one per leaf circuit) get distinct, non-null
    ``layout_id``s -- the report alone can now count the nets it accuses."""
    report = run_lvs(
        _request(tmp_path, _HIERARCHICAL_LAYOUT_SPICE, _HIERARCHICAL_REFERENCE_SPICE)
    )

    zeros = [n for n in _net_entries(report) if n["layout"] == "\\$0"]
    assert len(zeros) == 2, "the fixture must name \\$0 from both leaf scopes"
    ids = [n["layout_id"] for n in zeros]
    assert all(isinstance(n, int) for n in ids)
    assert len(set(ids)) == 2, "same display string, different nets: distinct ids"
    assert all(n["reference"] is None and n["reference_id"] is None for n in zeros)


def test_ids_unique_per_side_across_the_report(tmp_path):
    """Ids are a per-report, per-side identity: every net id a report emits
    on one side is distinct, even though the *display names* repeat across
    circuits (both leaves name their nets ``A``/``B``/``\\$0``)."""
    report = run_lvs(
        _request(tmp_path, _HIERARCHICAL_LAYOUT_SPICE, _HIERARCHICAL_REFERENCE_SPICE)
    )

    nets = _net_entries(report)
    layout_ids = [n["layout_id"] for n in nets if n["layout_id"] is not None]
    reference_ids = [n["reference_id"] for n in nets if n["reference_id"] is not None]
    assert len(set(layout_ids)) == len(layout_ids)
    assert len(set(reference_ids)) == len(reference_ids)


def test_ids_deterministic_across_runs(tmp_path):
    """Same inputs, same ids -- the same repeated-runs-diff-clean guarantee
    ``mismatches[]`` and ``net_correspondence[]`` already make."""
    request = _request(
        tmp_path, _HIERARCHICAL_LAYOUT_SPICE, _HIERARCHICAL_REFERENCE_SPICE
    )
    first = run_lvs(request)
    second = run_lvs(request)

    assert _net_entries(first) == _net_entries(second)
    assert first["net_correspondence"] == second["net_correspondence"]


def test_ids_join_to_net_correspondence(tmp_path):
    """Every ``mismatches[].net`` id joins to the matching
    ``net_correspondence[]`` entry where one exists: the ambiguous pairing
    the comparer resolved appears in *both* arrays, and the ids -- not just
    the display names -- identify it as the same net."""
    report = run_lvs(_request(tmp_path, _JOIN_LAYOUT_SPICE, _JOIN_REFERENCE_SPICE))

    correspondence = report["net_correspondence"]
    assert correspondence, "the fixture must leave matched nets to join against"
    for entry in correspondence:
        assert set(entry) == {
            "layout",
            "layout_id",
            "reference",
            "reference_id",
            "pin",
        }
    layout_ids = [e["layout_id"] for e in correspondence]
    reference_ids = [e["reference_id"] for e in correspondence]
    assert len(set(layout_ids)) == len(layout_ids) == len(correspondence)
    assert len(set(reference_ids)) == len(reference_ids) == len(correspondence)

    joined = 0
    for entry in correspondence:
        by_layout_id = [
            m["net"]
            for m in report["mismatches"]
            if m["net"] is not None
            and m["net"]["layout"] == entry["layout"]
            and m["net"]["layout_id"] == entry["layout_id"]
        ]
        if not by_layout_id:
            continue  # a cleanly matched net has no mismatch entry to join
        joined += 1
        assert all(
            (n["reference"], n["reference_id"])
            == (entry["reference"], entry["reference_id"])
            for n in by_layout_id
        ), "a paired net's mismatch entry joins the same correspondence row"
    assert joined >= 2, (
        "the fixture's ambiguously-paired nets must appear in both arrays"
    )


def test_null_id_on_absent_side_and_key_set_unchanged(tmp_path):
    """One-sided findings carry the id keys with ``null`` on the absent
    side (never omitted), and every pre-existing field is byte-unchanged:
    the entry key set stays the documented eleven, and the ``net`` object
    gains exactly the two id keys around the untouched name strings."""
    report = run_lvs(
        _request(tmp_path, _HIERARCHICAL_LAYOUT_SPICE, _HIERARCHICAL_REFERENCE_SPICE)
    )

    entry_keys = {
        "category",
        "severity",
        "description",
        "side",
        "net",
        "device",
        "property",
        "details",
        "circuit",
        "instance",
        "subcircuit",
    }
    for mismatch in report["mismatches"]:
        assert set(mismatch) == entry_keys
    for net in _net_entries(report):
        assert set(net) == {"layout", "layout_id", "reference", "reference_id"}

    layout_side = next(n for n in _net_entries(report) if n["layout"] == "\\$0")
    assert isinstance(layout_side["layout_id"], int)
    assert layout_side["reference"] is None
    assert layout_side["reference_id"] is None

    reference_side = next(n for n in _net_entries(report) if n["reference"] is not None)
    assert isinstance(reference_side["reference_id"], int)
    assert reference_side["layout"] is None
    assert reference_side["layout_id"] is None


def test_supply_fragmented_names_a_group_so_ids_stay_null():
    """A ``net.supply_fragmented`` entry names a supply *group* (its
    ``details.fragment_count`` nets), not one net -- so its ``net`` object
    carries the keys with ``null`` ids, keeping the every-entry-carries-the
    -keys convention intact for consumers."""

    class _FragmentNet:
        def __init__(self, name: str) -> None:
            self.name = name
            self.cluster_id = 0

    class _Circuit:
        name = "supply_tile"

        def each_net(self):
            return iter([_FragmentNet("VDD"), _FragmentNet("VDD")])

        def each_subcircuit(self):
            return iter([])

    (finding,) = supply_fragmentation_findings(_Circuit(), ["VDD"])
    assert finding["net"] == {
        "layout": "VDD",
        "layout_id": None,
        "reference": None,
        "reference_id": None,
    }
