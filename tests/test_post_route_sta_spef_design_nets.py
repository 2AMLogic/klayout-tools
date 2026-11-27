"""Run the generated design/SPEF net-correlation check against net connectivity."""

import shutil
import subprocess

import pytest

from klayout_tools import place_and_route_sta as pnr_sta
from klayout_tools import post_route_sta as sta
from klayout_tools._paths import _count_spef_nets_annotated
from klayout_tools._sta_request_diag import SPEF_SKIP_DRIVER_ONLY
from klayout_tools.place_and_route import (
    _SPEF_NET_CHECK_BEGIN,
    _SPEF_NET_CHECK_END,
    _SPEF_NET_CHECK_RE,
)
from test_post_route_sta import (
    _ANNOTATED_PATH,
    _UNANNOTATED_PATH,
    _delay_blocks,
    _listed_stdout,
)

_TCL = """
proc get_nets {quiet pattern} {
    global nets
    if {$pattern eq "*"} {return $nets}
    if {$pattern in $nets} {return [list $pattern]}
    return {}
}
proc get_full_name {net} {return $net}
namespace eval sta {}
proc sta::sta_to_db_net {net} {
    if {$net eq "unknown_db"} {error "net lookup failed"}
    interp alias {} dbnet_$net {} term_count $net
    return dbnet_$net
}
proc term_count {net method} {
    if {$method ne "getTermCount"} {error "unexpected method"}
    return [expr {$net in {physical_only empty_port} ? 1 : 0}]
}
proc get_pins {quiet option net} {
    switch -exact -- $net {
        loaded - missed {return {driver sink}}
        unknown_pins {error "pin lookup failed"}
        sink_only {return sink}
        empty - empty_port - empty_unknown_ports -
        physical_only - unknown_db {return {}}
        unknown_dir {return broken}
        default {return driver}
    }
}
proc get_property {pin prop} {
    if {$pin eq "broken"} {error "unknown direction"}
    if {$pin eq "driver"} {return output}
    return input
}
proc get_ports {quiet option net} {
    if {$net in {port_net empty_port}} {return different_output_port_name}
    if {$net in {unknown_ports empty_unknown_ports}} {error "port lookup failed"}
    return {}
}
"""


@pytest.mark.parametrize(
    ("nets", "spef", "counts", "missing"),
    [
        (["loaded", "u/bus[0]"], ["loaded"], (1, 1), []),
        (["loaded", "empty"], ["loaded"], (1, 1), []),
        (["empty"], [], (0, 0), []),
        (["physical_only", "unknown_db"], [], (0, 2), ["physical_only", "unknown_db"]),
        (
            ["empty_port", "empty_unknown_ports"],
            [],
            (0, 2),
            ["empty_port", "empty_unknown_ports"],
        ),
        (["loaded", "u/bus[0]"], ["loaded", "u/bus[0]"], (1, 1), []),
        (
            ["loaded", "u/bus[0]", "missed", "port_net"],
            ["loaded"],
            (1, 3),
            ["missed", "port_net"],
        ),
        (["u/bus[0]"], ["u/bus[0]"], (0, 0), []),
        (
            [
                "loaded",
                "unknown_pins",
                "unknown_dir",
                "unknown_ports",
                "sink_only",
                "empty",
            ],
            ["loaded"],
            (1, 5),
            ["unknown_pins", "unknown_dir", "unknown_ports", "sink_only"],
        ),
    ],
)
def test_design_counts_and_missing_sample(tmp_path, nets, spef, counts, missing):
    tclsh = shutil.which("tclsh")
    if tclsh is None:
        pytest.skip("needs tclsh")
    script = tmp_path / "correlate.tcl"
    script.write_text(
        _TCL
        + "\nset nets {"
        + " ".join(nets)
        + "}\n"
        + "\n".join(sta._spef_net_check_lines(spef))
    )
    output = subprocess.run(
        [tclsh, str(script)], text=True, capture_output=True, check=True
    )
    stdout = "\n".join(
        [
            output.stdout,
            _delay_blocks(_UNANNOTATED_PATH, _ANNOTATED_PATH),
            _listed_stdout([], [], []),
        ]
    )
    run = sta.OpenRoadResult(returncode=0, stdout=stdout, stderr="", engine_log=None)
    block = sta._spef_annotation_block(run, spef)
    assert (block["design_nets_annotated"], block["design_nets_total"]) == counts
    assert block["design_nets_missing_sample"] == missing
    assert (block["nets_annotated"], block["nets_total"]) == (len(spef), len(spef))
    complete = counts[1] > 0 and counts[0] == counts[1]
    assert block["annotation_complete"] is complete
    assert (block["annotation_warning"] is None) is complete


def test_in_flow_loop_splices_the_shared_driver_only_skip():
    """Issue #7267: the in-flow design-side loop splices the same
    ``SPEF_SKIP_DRIVER_ONLY`` fragment standalone ``klt sta`` uses, defined
    once in ``_sta_request_diag`` -- not a second hand-maintained copy."""
    lines = pnr_sta._spef_sta_script_lines(
        checkpoint_in="/tmp/x.odb",
        liberty_path="/tmp/x.lib",
        clock_port="clk",
        clock_period_ns=1.1,
        spef_path="/tmp/x.spef",
        net_names=["_019_"],
    )
    at = lines.index("foreach klt_design_net [get_nets -quiet *] {")
    end = at + 1 + len(SPEF_SKIP_DRIVER_ONLY)
    assert lines[at + 1 : end] == list(SPEF_SKIP_DRIVER_ONLY)
    assert lines[end] == "    incr klt_design_total"


@pytest.mark.parametrize(
    ("nets", "spef", "counts", "complete"),
    [
        # The #7267 shape: a dangling driver-only net the SPEF omits no
        # longer holds the in-flow verdict at false.
        (["loaded", "dangling"], ["loaded"], (1, 1), True),
        # Driver-only nets leave both design-side counters, named or not.
        (["loaded", "u/bus[0]"], ["loaded", "u/bus[0]"], (1, 1), True),
        (["loaded", "empty"], ["loaded"], (1, 1), True),
        (["empty"], [], (0, 0), False),
        (["physical_only", "unknown_db"], [], (0, 2), False),
        (["empty_port", "empty_unknown_ports"], [], (0, 2), False),
        (["loaded", "missed"], ["loaded"], (1, 2), False),
        (["loaded", "port_net"], ["loaded"], (1, 2), False),
        (["u/bus[0]"], ["u/bus[0]"], (0, 0), False),
        (
            [
                "loaded",
                "unknown_pins",
                "unknown_dir",
                "unknown_ports",
                "sink_only",
                "empty",
            ],
            ["loaded"],
            (1, 5),
            False,
        ),
    ],
)
def test_in_flow_design_counts_skip_driver_only(tmp_path, nets, spef, counts, complete):
    """Issue #7267: the in-flow P&R timing report's own design-side
    correlation lines -- sliced out of ``_spef_sta_script_lines`` (the
    stubs define none of the surrounding OpenSTA session's commands) -- run
    under the same connectivity stubs as the standalone check above, then
    parsed with the markers and verdict rule ``_post_route_spef_metrics``
    itself uses."""
    tclsh = shutil.which("tclsh")
    if tclsh is None:
        pytest.skip("needs tclsh")
    lines = pnr_sta._spef_sta_script_lines(
        checkpoint_in="/tmp/x.odb",
        liberty_path="/tmp/x.lib",
        clock_port="clk",
        clock_period_ns=1.1,
        spef_path="/tmp/x.spef",
        net_names=spef,
    )
    start = next(
        i for i, line in enumerate(lines) if line.startswith("set klt_spef_nets ")
    )
    stop = lines.index(f'puts "{_SPEF_NET_CHECK_END}"')
    script = tmp_path / "in_flow_correlate.tcl"
    script.write_text(
        _TCL
        + "\nset nets {"
        + " ".join(nets)
        + "}\n"
        + "\n".join(lines[start : stop + 1])
    )
    output = subprocess.run(
        [tclsh, str(script)], text=True, capture_output=True, check=True
    )
    parsed = _count_spef_nets_annotated(
        output.stdout,
        begin=_SPEF_NET_CHECK_BEGIN,
        end=_SPEF_NET_CHECK_END,
        pattern=_SPEF_NET_CHECK_RE,
    )
    assert parsed is not None
    nets_annotated, nets_total, design_annotated, design_total = parsed
    assert (design_annotated, design_total) == counts
    assert (nets_annotated, nets_total) == (len(spef), len(spef))
    assert (design_total > 0 and design_annotated == design_total) is complete


def test_discarded_net_count_uses_full_reader_output_and_excludes_instances():
    warnings = ["[WARNING STA-1650] file.spef line 2, net bus[1] not found."] * 6
    stdout = "\n".join([sta._SPEF_READ_BEGIN, *warnings, sta._SPEF_READ_END])
    stderr = (
        "[WARNING STA-1648] file.spef line 3, instance gate:A not found.\n"
        "[WARNING STA-1650] file.spef line 4, net mod|bus[2] not found."
    )
    run = sta.OpenRoadResult(
        returncode=0, stdout=stdout, stderr=stderr, engine_log=None
    )
    block = sta._spef_annotation_block(run, ["bus[1]"])
    assert block["reader_warning_count"] == 8
    assert block["reader_warning_sample"] == warnings[:5]
    assert "2 distinct unresolved net name(s)" in block["annotation_warning"]
    assert "design_nets_annotated counts name matches" in block["annotation_warning"]
    assert block["annotation_complete"] is False
