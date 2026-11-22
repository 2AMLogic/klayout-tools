"""Run the generated design/SPEF net-correlation check against net connectivity."""

import shutil
import subprocess

import pytest

from klayout_tools import post_route_sta as sta
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
proc get_pins {quiet option net} {
    switch -exact -- $net {
        loaded - missed {return {driver sink}}
        unknown_pins {error "pin lookup failed"}
        sink_only {return sink}
        empty {return {}}
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
    if {$net eq "port_net"} {return different_output_port_name}
    if {$net eq "unknown_ports"} {error "port lookup failed"}
    return {}
}
"""


@pytest.mark.parametrize(
    ("nets", "spef", "counts", "missing"),
    [
        (["loaded", "u/bus[0]"], ["loaded"], (1, 1), []),
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
            (1, 6),
            ["unknown_pins", "unknown_dir", "unknown_ports", "sink_only", "empty"],
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
    run = sta._OpenRoadResult(returncode=0, stdout=stdout, stderr="", engine_log=None)
    block = sta._spef_annotation_block(run, spef)
    assert (block["design_nets_annotated"], block["design_nets_total"]) == counts
    assert block["design_nets_missing_sample"] == missing
    assert (block["nets_annotated"], block["nets_total"]) == (len(spef), len(spef))
    complete = counts[1] > 0 and counts[0] == counts[1]
    assert block["annotation_complete"] is complete
    assert (block["annotation_warning"] is None) is complete
