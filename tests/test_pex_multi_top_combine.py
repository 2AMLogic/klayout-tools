"""Unit tests for the *combined* report a multi-top ``klt pex`` run builds
(issues #5494, #5523).

With two or more ``--top`` values each named cell is extracted on its own and
the results are concatenated into the one ``-o`` netlist. The singular
response fields keep describing that combined file, reduced by
``pex_multi_top_report``'s combine helpers so they **never read cleaner than
the worst top** -- which is what these tests pin. Split out of
``test_pex_multi_top.py`` (its siblings are described there) to keep both
files under the per-file size ratchet.
"""

from __future__ import annotations

from klayout_tools.pex import (
    BODY_BIAS_BIASED,
    BODY_BIAS_UNBIASED,
    _body_bias_report,
)
from klayout_tools.pex_multi_top import _first_firing
from klayout_tools.pex_multi_top_report import (
    _combine_extract_reports,
    _combine_parasitics,
    _extraction_block,
)

# --- the combined report: never cleaner than the worst top ----------------- #


def _extract_report_stub(
    top,
    *,
    devices,
    nets,
    unbiased=(),
    integrity=None,
    nets_=(),
    unsupported=(),
):
    report = {
        "top": top,
        "netlist_path": f"/stage/{top}.spice",
        "netlist_sha256": f"sha256:{top}",
        "device_count": devices,
        "net_count": nets,
        "provenance": {"deck": {"name": "sky130"}, "top": top},
        "unbiased_pmos_body_nets": [
            {"device": f"M_{top}", "net": net} for net in unbiased
        ],
        # `run_extract` always carries this key (issue #5477,
        # `extract_assemble.py`), `[]` when the layout draws no
        # deck-declared unsupported-device marker.
        "unsupported_devices": [
            {
                "kind": kind,
                "marker": "85/44",
                "count": 1,
                "description": f"{kind} in {top}",
            }
            for kind in unsupported
        ],
        "parasitics": {"model": "lumped", "nets": list(nets_)},
    }
    if integrity is not None:
        report["parasitics"]["integrity"] = integrity
    return report


def test_combine_extract_reports_sums_device_and_net_counts():
    combined = _combine_extract_reports(
        [
            _extract_report_stub("RESA", devices=1, nets=2),
            _extract_report_stub("RESB", devices=3, nets=5),
        ],
        netlist_path="/out/combined.spice",
        netlist_sha256="sha256:combined",
    )
    assert combined["device_count"] == 4
    assert combined["net_count"] == 7
    # The combined file is what the extracted leg simulated, so it is what
    # the report's `netlist`/`netlist_sha256` must identify.
    assert combined["netlist_path"] == "/out/combined.spice"
    assert combined["netlist_sha256"] == "sha256:combined"


def test_combine_extract_reports_is_unbiased_when_any_top_is():
    """The worst-top rule for `body_bias` (issue #5494): an unbiased body in
    the *second* top must not be invisible in a report whose `netlist` names
    the file that top's devices are in. `klt signoff` quotes `body_bias`
    verbatim into an item-7 citation, so losing the disclosure there is the
    failure this rule exists to prevent."""
    combined = _combine_extract_reports(
        [
            _extract_report_stub("RESA", devices=1, nets=2),
            _extract_report_stub("RESB", devices=1, nets=2, unbiased=["$3", "$4"]),
        ],
        netlist_path="/out/combined.spice",
        netlist_sha256="sha256:combined",
    )
    report = _body_bias_report(combined)
    assert report["status"] == BODY_BIAS_UNBIASED
    assert report["unbiased_device_count"] == 2
    assert report["unbiased_nets"] == ["$3", "$4"]


def test_combine_extract_reports_keeps_the_clean_verdict_when_every_top_is_biased():
    combined = _combine_extract_reports(
        [
            _extract_report_stub("RESA", devices=1, nets=2),
            _extract_report_stub("RESB", devices=1, nets=2),
        ],
        netlist_path="/out/combined.spice",
        netlist_sha256="sha256:combined",
    )
    assert _body_bias_report(combined)["status"] == BODY_BIAS_BIASED


def test_combine_extract_reports_keeps_a_later_tops_unsupported_devices():
    """The worst-top rule for `unsupported_devices` (issue #5523). It is not
    a flag echo -- `_detect_unsupported_devices` computes it per extracted
    cell -- so a clean first top must not hide a flagged second one: the
    combined netlist really is missing that top's devices, and #5477 added
    the field precisely so a committed envelope carries that limit
    statement."""
    combined = _combine_extract_reports(
        [
            _extract_report_stub("RESA", devices=1, nets=2),
            _extract_report_stub("RESB", devices=1, nets=2, unsupported=["esd_diode"]),
        ],
        netlist_path="/out/combined.spice",
        netlist_sha256="sha256:combined",
    )
    assert [entry["kind"] for entry in combined["unsupported_devices"]] == ["esd_diode"]
    # ... and it reaches the `extraction` block a reader actually quotes.
    block = _extraction_block(
        combined,
        deck_name="sky130",
        netlist_sha256="sha256:combined",
        resistance_graph=False,
        resistance_graph_r_cut_ohm=None,
    )
    assert [entry["kind"] for entry in block["unsupported_devices"]] == ["esd_diode"]


def test_combine_extract_reports_concatenates_unsupported_devices_in_top_order():
    combined = _combine_extract_reports(
        [
            _extract_report_stub("RESA", devices=1, nets=2, unsupported=["pnp"]),
            _extract_report_stub("RESB", devices=1, nets=2, unsupported=["esd_diode"]),
        ],
        netlist_path="/out/combined.spice",
        netlist_sha256="sha256:combined",
    )
    assert [entry["kind"] for entry in combined["unsupported_devices"]] == [
        "pnp",
        "esd_diode",
    ]
    # Attribution is not lost: each entry still names the top it came from.
    assert [entry["description"] for entry in combined["unsupported_devices"]] == [
        "pnp in RESA",
        "esd_diode in RESB",
    ]


def test_combine_extract_reports_stays_clean_when_no_top_draws_a_marker():
    combined = _combine_extract_reports(
        [
            _extract_report_stub("RESA", devices=1, nets=2),
            _extract_report_stub("RESB", devices=1, nets=2),
        ],
        netlist_path="/out/combined.spice",
        netlist_sha256="sha256:combined",
    )
    assert combined["unsupported_devices"] == []


def test_combine_parasitics_unions_critical_nets_resolved_per_top():
    """`critical_nets` is only usually a flag echo (issue #5523): with
    `--critical-net-pattern`/`--critical-net-all`,
    `_adopt_resolved_net_selectors` replaces the request with the set that
    matched *that* top, so the first top's resolution must not stand for the
    combined netlist."""
    merged = _combine_parasitics(
        [
            {"critical_nets": ["in_a", "shared"]},
            {"critical_nets": ["shared", "in_b"]},
        ]
    )
    assert merged["critical_nets"] == ["in_a", "shared", "in_b"]


def test_combine_parasitics_keeps_an_identical_critical_net_echo_verbatim():
    """A plain `--critical-net` list is this run's own flag, echoed back by
    every top "as given, not sorted/deduplicated" -- so an identical echo is
    returned verbatim, repeats included, and the field stays byte-identical
    to a single-top run's."""
    merged = _combine_parasitics(
        [{"critical_nets": ["b", "a", "a"]}, {"critical_nets": ["b", "a", "a"]}]
    )
    assert merged["critical_nets"] == ["b", "a", "a"]


def test_combine_parasitics_omits_critical_nets_when_no_top_reports_it():
    """Without `--parasitics` there is no `critical_nets` key to combine, and
    the combined block must not invent one."""
    assert "critical_nets" not in _combine_parasitics([{"model": "lumped"}, {}])


def test_combine_extract_reports_takes_provenance_from_the_first_top():
    """`provenance` and the flag echoes are this run's own inputs -- the same
    layout stream, deck, PDK and flags for every top -- so the first top's
    are the run's. Per-top detail lives in `tops[]`."""
    combined = _combine_extract_reports(
        [
            _extract_report_stub("RESA", devices=1, nets=2),
            _extract_report_stub("RESB", devices=1, nets=2),
        ],
        netlist_path="/out/combined.spice",
        netlist_sha256="sha256:combined",
    )
    assert combined["provenance"]["top"] == "RESA"


def test_combine_parasitics_sums_integrity_counters_and_concatenates_nets():
    """An integrity finding on the second top is summed into the top-level
    `extraction.parasitic_integrity`, never dropped -- the same worst-top
    rule `body_bias` follows. Thresholds are the caller's own flags,
    identical across tops, so the first top's are the run's."""
    base = {
        "short_threshold_ohm": 1.0,
        "cap_tolerance": {"absolute_ff": 0.1, "relative": 0.01},
        "max_detail_items": 20,
        "nets_checked": 0,
        "nets_with_findings": 0,
        "open_nets": 0,
        "island_components": 0,
        "orphan_nodes": 0,
        "suspicious_resistors": 0,
        "short_candidates": 0,
        "capacitance_mismatches": 0,
    }
    merged = _combine_parasitics(
        [
            {
                "integrity": {**base, "nets_checked": 2},
                "nets": [{"net": "RA", "integrity": {"ok": True}}],
            },
            {
                "integrity": {
                    **base,
                    "nets_checked": 3,
                    "nets_with_findings": 1,
                    "open_nets": 1,
                },
                "nets": [{"net": "RC", "integrity": {"ok": False}}],
            },
        ]
    )
    assert merged["integrity"]["nets_checked"] == 5
    assert merged["integrity"]["nets_with_findings"] == 1
    assert merged["integrity"]["open_nets"] == 1
    assert merged["integrity"]["short_threshold_ohm"] == 1.0
    assert [entry["net"] for entry in merged["nets"]] == ["RA", "RC"]


def test_first_firing_returns_the_first_tops_block():
    entries = [
        {"top": "RESA", "pin_count_mismatch": None},
        {"top": "RESB", "pin_count_mismatch": {"subcircuit": "RESB"}},
        {"top": "RESC", "pin_count_mismatch": {"subcircuit": "RESC"}},
    ]
    assert _first_firing(entries, "pin_count_mismatch") == {"subcircuit": "RESB"}


def test_first_firing_is_none_when_no_top_fires():
    assert _first_firing([{"pin_count_mismatch": None}], "pin_count_mismatch") is None
