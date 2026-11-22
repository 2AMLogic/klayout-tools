"""``klt signoff`` surfaces ``klt pex --engine klayout-runset``'s extraction
disclosure (issue #6559) like it surfaces ``body_bias`` -- in aggregation-
mode ``detail`` and on an item-7 citation -- and, since issue #6606 AC 4,
**refuses** a parasitic-free leg for item 7 (``no_parasitics``): item 7 is
parasitic verification, and a merged leg (``--deck <curated>``, which
attached cards) is the runset route that grades.

Split out of ``tests/test_pex_runset.py`` (file-size ratchet), reusing
``tests/test_signoff.py``'s envelope fixtures.
"""

from __future__ import annotations

from klayout_tools.cli import main
from klayout_tools.signoff import build_signoff, build_tier_report
from test_signoff import PEX_PASS_ENVELOPE, _manifest, _write

PEX_RUNSET_PASS_ENVELOPE = {
    **PEX_PASS_ENVELOPE,
    "extraction": {
        "engine": "klayout-runset",
        "parasitics_carried": False,
        "params_not_carried": {"pnpMPA": ["A", "P"]},
        "device_counts": {"pnpMPA": 10},
    },
    "body_bias": {
        "status": "not_assessed",
        "unbiased_device_count": None,
        "unbiased_nets": None,
        "unbiased_pmos_body_nets": None,
    },
}

#: The same leg with parasitics attached (issue #6606's merged route:
#: ``--deck <curated>``), which is the runset-engine shape item 7 accepts.
PEX_RUNSET_MERGED_PASS_ENVELOPE = {
    **PEX_RUNSET_PASS_ENVELOPE,
    "extraction": {
        **PEX_RUNSET_PASS_ENVELOPE["extraction"],
        "parasitics_carried": True,
        "net_correspondence": {
            "matched": [{"runset": "\\$3", "curated": "VGND"}],
            "unmatched_runset_nets": [],
            "unmatched_curated_nets": [],
            "disagreements": [],
            "probe_count": 6,
            "deck": "sg13cmos5l",
            "attached_card_count": 7,
            "substrate_return": {
                "curated": "vsubs",
                "node": "vsubs",
                "runset": None,
                "tie_resistance_ohm": 1e12,
            },
        },
    },
}

DISCLOSURE = {
    "engine": "klayout-runset",
    "parasitics_carried": False,
    "params_not_carried": {"pnpMPA": ["A", "P"]},
}


def test_signoff_detail_surfaces_the_runset_engine_disclosure(tmp_path):
    path = _write(tmp_path, "pex.json", PEX_RUNSET_PASS_ENVELOPE)

    check = build_signoff([path])["checks"][0]

    assert check["kind"] == "pex"
    assert check["detail"]["pex_extraction"] == DISCLOSURE
    # Not-assessed is quoted as such, never as a clean `biased`.
    assert check["detail"]["body_bias"]["status"] == "not_assessed"
    # `null`, like the count beside it -- never `[]`, which reads as "looked,
    # found no unbiased nets".
    assert check["detail"]["body_bias"]["unbiased_device_count"] is None
    assert check["detail"]["body_bias"]["unbiased_nets"] is None


def test_signoff_citation_surfaces_the_runset_engine_disclosure(tmp_path):
    """A *merged* leg (issue #6606) is the runset-engine citation item 7
    accepts; its disclosure rides the citation exactly as a parasitic-free
    one's did before the grading policy landed."""
    path = _write(tmp_path, "pex.json", PEX_RUNSET_MERGED_PASS_ENVELOPE)

    report = build_tier_report(_manifest(evidence={"7": path}))

    item_7 = next(item for item in report["items"] if item["id"] == 7)
    assert item_7["status"] == "met"
    assert item_7["citation"]["pex_extraction"] == {
        "engine": "klayout-runset",
        "parasitics_carried": True,
        "substrate_return": {
            "curated": "vsubs",
            "node": "vsubs",
            "runset": None,
            "tie_resistance_ohm": 1e12,
        },
        "params_not_carried": {"pnpMPA": ["A", "P"]},
    }


def test_signoff_does_not_grade_on_the_runset_disclosure(tmp_path):
    """The *check* stays pass either way (the gate is item 7's, not the
    check's); the item verdict is what the parasitics disclosure decides."""
    runset = _write(tmp_path, "runset.json", PEX_RUNSET_PASS_ENVELOPE)
    curated = _write(tmp_path, "curated.json", PEX_PASS_ENVELOPE)

    runset_result = build_signoff([runset])
    curated_result = build_signoff([curated])

    assert runset_result["status"] == curated_result["status"] == "pass"
    assert runset_result["checks"][0]["passed"] is True


def test_item_7_refuses_a_parasitic_free_runset_leg(tmp_path):
    """Issue #6606 AC 4: a runset leg that carries no parasitics cannot
    establish item 7 -- its delta rows are device-representation deltas --
    so a passing envelope renders `unmet` with the distinct `no_parasitics`
    remedy ("re-run *this* artifact with `--deck`"), never `check_failed`."""
    path = _write(tmp_path, "pex.json", PEX_RUNSET_PASS_ENVELOPE)

    report = build_tier_report(_manifest(evidence={"2": path, "7": path}))
    item_2 = next(item for item in report["items"] if item["id"] == 2)
    assert item_2["status"] == "met"

    item_7 = next(item for item in report["items"] if item["id"] == 7)
    assert item_7["status"] == "unmet"
    assert item_7["reason"] == "no_parasitics"
    # Refused items carry no citation -- the disclosure stays on the check.
    assert item_7["citation"] is None


def test_item_7_accepts_a_merged_runset_leg(tmp_path):
    """The same envelope with `parasitics_carried: true` (a merged leg whose
    correspondence attached cards) grades exactly as a curated one does."""
    path = _write(tmp_path, "pex.json", PEX_RUNSET_MERGED_PASS_ENVELOPE)

    report = build_tier_report(_manifest(evidence={"7": path}))

    item_7 = next(item for item in report["items"] if item["id"] == 7)
    assert item_7["status"] == "met"
    assert item_7["reason"] is None


def test_signoff_omits_the_disclosure_for_a_curated_pex_envelope(tmp_path):
    path = _write(tmp_path, "pex.json", PEX_PASS_ENVELOPE)

    check = build_signoff([path])["checks"][0]
    report = build_tier_report(_manifest(evidence={"7": path}))
    item_7 = next(item for item in report["items"] if item["id"] == 7)

    assert "pex_extraction" not in check["detail"]
    assert "pex_extraction" not in (item_7["citation"] or {})


def _item_7_text_lines(tmp_path, capsys, envelope):
    pex_path = _write(tmp_path, "pex.json", envelope)
    manifest_path = _write(
        tmp_path, "manifest.json", _manifest(evidence={"7": pex_path})
    )

    main(["signoff", "--manifest", manifest_path, "--format", "text"])

    return capsys.readouterr().out.splitlines()


def test_text_output_states_the_runset_leg_carries_no_parasitics(tmp_path, capsys):
    """The human-readable item 7 is the artifact a reviewer actually reads:
    a *merged* leg's disclosure must be there, not only in the JSON
    citation."""
    lines = _item_7_text_lines(tmp_path, capsys, PEX_RUNSET_MERGED_PASS_ENVELOPE)

    line = next(line for line in lines if "pex extraction:" in line)
    assert "engine=klayout-runset" in line
    assert "parasitics_carried=true" in line
    assert "substrate return vsubs (runset unmatched)" in line
    assert "params_not_carried=[pnpMPA: A, P]" in line


def test_text_output_names_the_no_parasitics_refusal(tmp_path, capsys):
    """A parasitic-free leg's item 7 states its own reason, the same way
    `not_post_layout` does -- the remedy is re-running this artifact with
    `--deck`, not a different kind of artifact."""
    lines = _item_7_text_lines(tmp_path, capsys, PEX_RUNSET_PASS_ENVELOPE)

    assert any("no_parasitics" in line for line in lines)


def test_text_output_reports_no_count_for_a_not_assessed_body_bias(tmp_path, capsys):
    """`not_assessed` carries `null` counts; the text line must not turn that
    back into a measured `0` or hand over the floating-body explanation.
    Rides a *merged* citation since the AC 4 gate (a refused item carries no
    citation lines at all)."""
    lines = _item_7_text_lines(tmp_path, capsys, PEX_RUNSET_MERGED_PASS_ENVELOPE)

    line = next(line for line in lines if "body bias:" in line)
    assert line.split("body bias:")[1].strip().startswith("not_assessed")
    assert "device(s)" not in line
    assert "0 " not in line
    assert "extract.md" not in line
    assert "not comparable to the schematic leg" not in line


def test_text_output_omits_the_extraction_line_for_a_curated_envelope(tmp_path, capsys):
    lines = _item_7_text_lines(tmp_path, capsys, PEX_PASS_ENVELOPE)

    assert any("cite:" in line for line in lines)
    assert not any("pex extraction:" in line for line in lines)
