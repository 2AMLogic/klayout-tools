"""Text rendering of a PEX citation's device-body bias."""

import pytest

from klayout_tools.signoff_details import (
    _format_body_bias,
    _format_pex_extraction,
    _pex_citation_lines,
)


def test_biased_devices_report_a_dc_bias_path():
    assert (
        _format_body_bias({"status": "biased"})
        == "biased (every device body has a DC bias path)"
    )


@pytest.mark.parametrize(
    ("nets", "preview"),
    [
        (["n1", "n2", "n3", "n4"], "n1, n2, n3, n4"),
        (["n1", "n2", "n3", "n4", "n5", "n6"], "n1, n2, n3, n4, +2 more"),
    ],
)
def test_unbiased_net_preview_shows_four_names_and_counts_the_rest(nets, preview):
    line = _format_body_bias(
        {"status": "unbiased", "unbiased_device_count": 8, "unbiased_nets": nets}
    )
    assert line.startswith(f"unbiased (8 device(s) with no DC bias path on {preview})")


def test_not_assessed_body_bias_states_what_it_is_and_prints_no_count():
    line = _format_body_bias(
        {
            "status": "not_assessed",
            "unbiased_device_count": None,
            "unbiased_nets": None,
        }
    )
    assert line.startswith("not_assessed (no body-bias check ran for this leg")
    assert "device(s)" not in line
    assert "0 " not in line
    # The floating-body gloss and its pointer are the *measured* case's.
    assert "not comparable" not in line
    assert "extract.md" not in line


def test_pex_extraction_line_names_the_engine_and_that_no_parasitics_ride_along():
    line = _format_pex_extraction(
        {
            "engine": "klayout-runset",
            "parasitics_carried": False,
            "params_not_carried": {"pnpMPA": ["A", "P"]},
        }
    )
    assert line.startswith(
        "engine=klayout-runset, parasitics_carried=false, "
        "params_not_carried=[pnpMPA: A, P] -- "
    )
    assert "device-representation deltas, not parasitic ones" in line


def test_pex_extraction_line_omits_an_empty_params_not_carried():
    line = _format_pex_extraction(
        {
            "engine": "klayout-runset",
            "parasitics_carried": False,
            "params_not_carried": {},
        }
    )
    assert "params_not_carried" not in line


def test_pex_extraction_line_does_not_claim_no_parasitics_when_unstated():
    line = _format_pex_extraction({"engine": "klayout-runset"})
    assert line == "engine=klayout-runset, parasitics_carried=unknown"


def test_citation_lines_put_the_extraction_disclosure_before_body_bias():
    lines = _pex_citation_lines(
        {
            "pex_extraction": {"engine": "klayout-runset", "parasitics_carried": False},
            "body_bias": {"status": "biased"},
        }
    )
    assert [line.split(":")[0] for line in lines] == ["pex extraction", "body bias"]


def test_citation_lines_are_empty_without_either_disclosure():
    assert _pex_citation_lines({"kind": "pex"}) == []
