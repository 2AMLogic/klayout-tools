"""Literal Tcl arguments for STA and the place-and-route floorplan (#5708)."""

import shutil
import subprocess
from pathlib import Path

import pytest

from klayout_tools import place_and_route_script, place_and_route_sta, post_route_sta
from klayout_tools._openroad_engine import write_sdf_line
from klayout_tools.place_and_route import run_place_and_route
from klayout_tools.post_route_sta import run_sta
from test_place_and_route import (
    _script_lines,
    _setup_success_env,
    _stage_script,
    _stub_merge_def_to_gds,
    _stub_openroad_success,
)
from test_post_route_sta import (
    _LIVE_ONLY,
    _boundary_constraints,
    _write,
    _write_request,
)

pytestmark = pytest.mark.usefixtures("real_build_identity_git")


@pytest.mark.parametrize(
    ("path", "quoted"),
    [
        ("/design/top.sdf", "/design/top.sdf"),
        ("/my design/.klt/sta/top.sdf", r"/my\ design/.klt/sta/top.sdf"),
        (r"/design/$top[0]\out.sdf", r"/design/\$top\[0\]\\out.sdf"),
        ("/design/top\nname.sdf", r"/design/top\nname.sdf"),
    ],
)
def test_write_sdf_path_is_literal_in_both_flows(path, quoted):
    expected = f"write_sdf -divider . -include_typ {quoted}"
    assert write_sdf_line(path) == expected
    common = dict(
        liberty_path="/pdk/lib.lib",
        clock_port="clk",
        clock_period_ns=2.5,
        spef_path="/design/top.spef",
        sdf_path=path,
    )
    sta = post_route_sta._sta_script_lines(
        tech_lef="/pdk/tech.lef",
        cell_lef="/pdk/cells.lef",
        def_path="/design/top.def",
        spef_net_names=["net_a"],
        **common,
    )
    pnr = place_and_route_sta._spef_sta_script_lines(
        checkpoint_in="/design/top.odb",
        net_names=["net_a"],
        **common,
    )
    for lines in (sta, pnr):
        assert expected in lines
        assert lines.index(expected) > lines.index("read_spef /design/top.spef")


@pytest.mark.parametrize("module", [post_route_sta, place_and_route_script])
def test_literal_newline_is_not_a_tcl_line_continuation(module):
    assert module._tcl_literal("a\nb") == r"a\nb"
    assert module._tcl_literal("") == "{}"


@pytest.mark.skipif(shutil.which("tclsh") is None, reason="requires a Tcl interpreter")
@pytest.mark.parametrize(
    "value",
    ["", "top", "a\nb", "a\rb", "a\tb", "\\\n", r'$a[0]{x};#"\y', "sp ace"],
)
def test_sdf_path_round_trips_through_tcl(value):
    # Execute the generated command with a Tcl argument-capture procedure.
    script = (
        "proc write_sdf {args} {puts [llength $args]; "
        "puts [binary encode hex [encoding convertto utf-8 [lindex $args end]]]}\n"
        + write_sdf_line(value)
        + "\n"
    )
    result = subprocess.run(
        ["tclsh"],
        input=script,
        text=True,
        capture_output=True,
        check=True,
        timeout=5,
    )
    assert result.stderr == ""
    assert result.stdout.splitlines() == ["4", value.encode("utf-8").hex()]


#: The Yosys `$paramod` spelling the issue reported: the module name as it
#: appears in the Verilog escaped identifier (single leading backslash).
_PARAMOD_TOP = r"$paramod\some_module\N=s32'00000000000000000000000000000010"

#: `_tcl_literal`'s spelling of it: every `$` backslash-escaped, every
#: backslash doubled; `'` is not Tcl-special and passes through.
_PARAMOD_TOP_TCL = r"\$paramod\\some_module\\N=s32'00000000000000000000000000000010"


def test_sta_netlist_script_lines_paramod_toplevel_reaches_link_design_literal():
    """Issue #5708: a Yosys `$paramod\\...` escaped-identifier top level is
    one literal Tcl word on the `link_design` line -- `$` escaped, backslash
    doubled -- so Tcl passes the name verbatim to OpenSTA instead of
    failing with ``can't read \"paramod\": no such variable``. The
    ``read_verilog`` path on the preceding line is quoted the same way."""
    lines = post_route_sta._sta_netlist_script_lines(
        tech_lef="/pdk/tech.lef",
        cell_lef="/pdk/cells.lef",
        verilog_path="/design/top.v",
        liberty_path="/pdk/lib.lib",
        hdl_toplevel=_PARAMOD_TOP,
        clock_port="clk",
        clock_period_ns=2.5,
    )

    assert lines[3] == "read_verilog /design/top.v"
    assert lines[4] == f"link_design {_PARAMOD_TOP_TCL}"


@pytest.mark.parametrize(
    ("name", "tcl"),
    [
        ("data[3]", "data\\[3\\]"),
        ("a$b", "a\\$b"),
        ("x{y}z", "x\\{y\\}z"),
        ('q"r', 'q\\"r'),
        ("sp ace", "sp\\ ace"),
        ("v\\w", "v\\\\w"),
        ("top", "top"),
        ("my_mod_1", "my_mod_1"),
    ],
)
def test_sta_link_design_toplevel_is_one_literal_word(name, tcl):
    """Every Tcl-special character in a `hdl_toplevel` is escaped into one
    literal word (issue #5708); ordinary names pass through unchanged, so
    the emitted line is byte-identical to the pre-#5708 spelling."""
    lines = post_route_sta._sta_netlist_script_lines(
        tech_lef="/pdk/tech.lef",
        cell_lef="/pdk/cells.lef",
        verilog_path="/design/top.v",
        liberty_path="/pdk/lib.lib",
        hdl_toplevel=name,
        clock_port="clk",
        clock_period_ns=2.5,
    )

    assert lines[4] == f"link_design {tcl}"


def test_sta_clock_lines_quote_clock_port():
    """Issue #5708 audit: the caller-supplied clock port reaches every
    generated line that names it as one literal Tcl word -- `create_clock`'s
    `-name` and `[get_ports]`, `set_propagated_clock`'s `[get_clocks]`,
    `set_clock_uncertainty`'s `[get_clocks]`, and the I/O-delay `-clock`
    flags. Ordinary names pass through unchanged."""
    clock_port = "clk$x[0]"

    lines = post_route_sta._clock_lines(clock_port, 2.0)
    assert lines == [
        post_route_sta._UNIT_PIN_LINE,
        "create_clock -name clk\\$x\\[0\\] -period 2.0 [get_ports clk\\$x\\[0\\]]",
    ]

    assert post_route_sta._propagated_clock_lines(clock_port) == [
        "set_propagated_clock [get_clocks clk\\$x\\[0\\]]"
    ]

    assert post_route_sta._clock_uncertainty_lines(clock_port, {"setup_ns": 0.1}) == [
        "set_clock_uncertainty -setup 0.1 [get_clocks clk\\$x\\[0\\]]"
    ]

    assert post_route_sta._io_delay_lines(clock_port, 0.2, 0.3) == [
        "set klt_clock_port [get_ports clk\\$x\\[0\\]]",
        "set klt_non_clock_inputs "
        "[lsearch -inline -all -not -exact [all_inputs] $klt_clock_port]",
        "set_input_delay 0.2 -clock clk\\$x\\[0\\] $klt_non_clock_inputs",
        "set_output_delay 0.3 -clock clk\\$x\\[0\\] [all_outputs]",
    ]


def test_sta_read_paths_are_literal_words():
    """Issue #5708 audit: the `read_lef`/`read_def`/`read_liberty`/`read_spef`
    arguments are paths rooted at the request's own directories, so they are
    quoted like every other caller-supplied string. Ordinary paths (no
    Tcl-special character) are byte-identical -- this is the same assertion
    the ordering tests above make on `/pdk/...` spellings; here a
    space-bearing path proves the quoting actually applies."""
    lines = post_route_sta._sta_script_lines(
        tech_lef="/pdk with space/tech.lef",
        cell_lef="/pdk with space/cells.lef",
        def_path="/design dir/top.def",
        liberty_path="/pdk with space/lib.lib",
        clock_port="clk",
        clock_period_ns=2.5,
        spef_path="/design dir/top.spef",
        spef_net_names=["net_a"],
    )

    assert lines[0] == "read_lef /pdk\\ with\\ space/tech.lef"
    assert lines[2] == "read_def /design\\ dir/top.def"
    assert lines[3] == "read_liberty /pdk\\ with\\ space/lib.lib"
    assert lines.index("read_spef /design\\ dir/top.spef") > lines.index(
        "read_liberty /pdk\\ with\\ space/lib.lib"
    )


def test_sta_netlist_script_lines_read_paths_are_literal_words():
    """Same quoting audit for the netlist-mode `read_*` arguments
    (issue #5708)."""
    lines = post_route_sta._sta_netlist_script_lines(
        tech_lef="/pdk with space/tech.lef",
        cell_lef="/pdk with space/cells.lef",
        verilog_path="/design dir/top.v",
        liberty_path="/pdk with space/lib.lib",
        hdl_toplevel="top",
        clock_port="clk",
        clock_period_ns=2.5,
    )

    assert lines[0] == "read_liberty /pdk\\ with\\ space/lib.lib"
    assert lines[1] == "read_lef /pdk\\ with\\ space/tech.lef"
    assert lines[2] == "read_lef /pdk\\ with\\ space/cells.lef"
    assert lines[3] == "read_verilog /design\\ dir/top.v"
    assert lines[4] == "link_design top"


def test_sta_script_lines_paramod_toplevel_quoted_def_mode_untouched():
    """A `$paramod`-class name can only arrive as `hdl_toplevel` (netlist
    mode); the def-mode script has no `link_design` at all -- asserting the
    boundary the issue describes."""
    lines = post_route_sta._sta_script_lines(
        tech_lef="/pdk/tech.lef",
        cell_lef="/pdk/cells.lef",
        def_path="/design/top.def",
        liberty_path="/pdk/lib.lib",
        clock_port="clk",
        clock_period_ns=2.5,
    )

    assert not any("link_design" in line for line in lines)


def test_stubbed_paramod_toplevel_reaches_link_design_literal(tmp_path, monkeypatch):
    """Issue #5708: a Yosys `$paramod\\...` escaped-identifier `hdl_toplevel`
    must reach the floorplan stage's `link_design` as one literal Tcl word,
    and every generated path that embeds the name (`read_verilog` netlist is
    ordinary, but `write_db`'s checkpoint filename is
    `{hdl_toplevel}_{stage}.odb`) must be quoted the same way -- Tcl would
    otherwise substitute `$paramod` before OpenROAD saw it."""
    paramod_top = r"$paramod\some_module\N=s32'00000000000000000000000000000010"
    paramod_tcl = r"\$paramod\\some_module\\N=s32'00000000000000000000000000000010"

    request_path = _setup_success_env(tmp_path, monkeypatch, hdl_toplevel=paramod_top)
    _stub_openroad_success(monkeypatch)
    _stub_merge_def_to_gds(monkeypatch)

    report = run_place_and_route(request_path)

    assert report["stage_reached"] == "route"
    floorplan_script = _stage_script(request_path, "floorplan", paramod_top)
    lines = _script_lines(floorplan_script)
    link_design_line = f"link_design {paramod_tcl}"
    assert link_design_line in lines
    # The checkpoint written at the end of the floorplan stage embeds the
    # same name in its filename -- one literal word too.
    write_db_line = next(ln for ln in lines if ln.startswith("write_db "))
    assert write_db_line.startswith("write_db ")
    assert f"{paramod_tcl}_floorplan.odb" in write_db_line


@_LIVE_ONLY
def test_live_engine_paramod_toplevel(tmp_path):
    """Issue #5708, real engine: a netlist whose top module is a Yosys
    `$paramod\\\\...` escaped identifier links and times when the request's
    `hdl_toplevel` names it. Two live-verified facts shape this test:

    - Pre-#5708 NO spelling of such a name survived the generated script:
      Tcl mangled every form before `link_design` saw it (the issue's
      observed table). The generator now emits one literal Tcl word, so
      the caller's spelling reaches OpenSTA verbatim.
    - OpenSTA's Verilog reader registers an escaped identifier with its
      internal backslashes DOUBLED (`\\foo\\bar` is stored as `foo\\\\bar`;
      live-probed on openroad 26Q3), so the caller addresses the module by
      that doubled spelling -- exactly the "pre-escaped backslashes" form
      the issue's table row 3 tried and could not express.

    The fixture netlist (`tests/fixtures/sta_paramod_toplevel/paramod_top.v`)
    is the captured boundary design with its top renamed to the `$paramod`
    spelling, so the worst paths must be the same identities the plain-name
    live run (`test_live_engine_boundary_paths`) asserts."""
    single = r"$paramod\some_module\N=s32'00000000000000000000000000000010"
    doubled = single.replace("\\", "\\\\")
    netlist = (
        Path(__file__).parent / "fixtures" / "sta_paramod_toplevel" / "paramod_top.v"
    ).read_text(encoding="utf-8")
    assert netlist.count(f"module \\{single}") == 1
    _write(tmp_path / "paramod_top.v", netlist)
    request = {
        "verilog": "paramod_top.v",
        "hdl_toplevel": doubled,
        "pdk": {"cell_library": "sky130_fd_sc_hd", "corner": "tt_025C_1v80"},
        "constraints": _boundary_constraints(),
    }
    request_path = _write_request(tmp_path / "request.json", request)

    report = run_sta(request_path, pdk_variant="sky130A")

    hold = report["worst_hold_path"]
    setup = report["worst_setup_path"]
    assert (hold["check_type"], hold["startpoint"], hold["endpoint"]) == (
        "removal",
        "rst_n",
        "ff_a/RESET_B",
    )
    assert (setup["check_type"], setup["startpoint"], setup["endpoint"]) == (
        "setup",
        "b_in",
        "ff_b/D",
    )
