"""Text rendering of a PEX citation's device-body bias."""

import pytest

from klayout_tools.signoff_details import _format_body_bias


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
