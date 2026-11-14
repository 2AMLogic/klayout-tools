"""ihp-sg13g2 per-instance mismatch gating by the ``mm_ok`` instance
parameter (issue #5487) -- the second of ``monte_carlo.instances``' two
mechanisms. The wrapper mode (sky130/gf180mcu) is covered in
``test_sim_mismatch_gate.py``."""

import pytest

from klayout_tools import sim
from klayout_tools import sim_mismatch_declarations as decl
from klayout_tools import sim_mismatch_gate as gate

# --- sg13g2: instance-parameter gating (issue #5487) -------------------------
#
# sg13g2 has no global mismatch switch to shadow; its `*_mismatch.lib` device
# subcircuits take `mm_ok` as an instance parameter instead, so the gate
# rewrites the unselected instance line rather than wrapping it. The fixtures
# below stand in for the PDK libraries (CI has no PDK): the shapes are copied
# from the IHP-Open-PDK model files -- `sg13_lv_nmos` declares `mm_ok` on its
# `.subckt` continuation line, `cap_cmim` declares it with a body `.param`.

SG13_LIB = (
    "* stand-in for sg13g2_moslv_mod_mismatch.lib / capacitors_mod_mismatch.lib\n"
    ".subckt sg13_lv_nmos d g s b\n"
    "+ w=0.35u l=0.34u ng=1 m=1 mm_ok=1 as=0 ad=0\n"
    "M1 d g s b sg13g2_lv_nmos w=w l=l\n"
    ".ends sg13_lv_nmos\n"
    ".subckt cap_cmim PLUS MINUS\n"
    ".param l=7u\n"
    ".param w=7u\n"
    ".param mm_ok=1\n"
    "C1 PLUS MINUS 1p\n"
    ".ends cap_cmim\n"
    ".subckt pnpmpa c b e\n"
    "Q1 c b e pnp\n"
    ".ends pnpmpa\n"
)

SG13_NETLIST = (
    "Vdd vdd 0 DC 1.5\n"
    "XM1 d g s b sg13_lv_nmos w=1u l=0.13u\n"
    "+ ng=1 m=1\n"
    "XM2 d2 g s b sg13_lv_nmos w=1u l=0.13u mm_ok=1\n"
    "XC1 d 0 cap_cmim\n"
    ".end\n"
)

SG13_DECLARED = decl.scan_declared_params(SG13_LIB, {})


def test_sg13g2_rewrites_the_instance_line_without_a_wrapper():
    text, rows = gate.gate_netlist(
        SG13_NETLIST,
        [],
        ["xm1"],
        instance_param="mm_ok",
        declared_params=SG13_DECLARED,
    )

    assert {r["instance"]: r["randomised"] for r in rows} == {
        "XM1": False,
        "XM2": True,
        "XC1": True,
    }
    # The gated instance keeps its own name, nodes and subcircuit: no wrapper
    # subcircuit is generated, so hierarchical probe names are unchanged.
    assert "XM1 d g s b sg13_lv_nmos w=1u l=0.13u ng=1 m=1 mm_ok=0" in text
    assert "klt_mmoff_" not in text
    assert ".subckt" not in text
    # the untouched instances are byte-identical
    assert "XM2 d2 g s b sg13_lv_nmos w=1u l=0.13u mm_ok=1" in text
    assert "XC1 d 0 cap_cmim" in text
    assert text.rstrip().endswith(".end")


def test_sg13g2_replaces_an_existing_mm_ok_rather_than_duplicating_it():
    text, _rows = gate.gate_netlist(
        SG13_NETLIST,
        [],
        ["xm2"],
        instance_param="mm_ok",
        declared_params=SG13_DECLARED,
    )

    line = next(ln for ln in text.split("\n") if ln.startswith("XM2"))
    assert line == "XM2 d2 g s b sg13_lv_nmos w=1u l=0.13u mm_ok=0"
    assert line.count("mm_ok") == 1


def test_sg13g2_handles_a_spaced_equals_spelling():
    text, _rows = gate.gate_netlist(
        "XM1 d g s b sg13_lv_nmos w=1u mm_ok = 1\n.end\n",
        [],
        ["xm1"],
        instance_param="mm_ok",
        declared_params=SG13_DECLARED,
    )

    assert "XM1 d g s b sg13_lv_nmos w=1u mm_ok=0" in text


def test_sg13g2_writes_mm_ok_ahead_of_a_dollar_comment():
    # ngspice discards anything after an end-of-line `$`, so appending
    # `mm_ok=0` past the comment would leave the instance randomised while
    # the report said otherwise -- silently (measured, ngspice-47: an
    # `X1 n 0 rr $ note mm_ok=0` keeps mm_ok at its default).
    text, rows = gate.gate_netlist(
        "XM1 d g s b sg13_lv_nmos w=1u l=0.13u $ mirror ref\n.end\n",
        [],
        ["xm1"],
        instance_param="mm_ok",
        declared_params=SG13_DECLARED,
    )

    line = next(ln for ln in text.split("\n") if ln.startswith("XM1"))
    assert line == "XM1 d g s b sg13_lv_nmos w=1u l=0.13u mm_ok=0 $ mirror ref"
    assert line.index("mm_ok=0") < line.index("$")
    assert rows == [{"instance": "XM1", "model": "sg13_lv_nmos", "randomised": False}]


def test_sg13g2_writes_mm_ok_ahead_of_a_semicolon_comment():
    text, _rows = gate.gate_netlist(
        "XM1 d g s b sg13_lv_nmos w=1u ; mirror ref\n.end\n",
        [],
        ["xm1"],
        instance_param="mm_ok",
        declared_params=SG13_DECLARED,
    )

    line = next(ln for ln in text.split("\n") if ln.startswith("XM1"))
    assert line == "XM1 d g s b sg13_lv_nmos w=1u mm_ok=0 ; mirror ref"
    assert line.index("mm_ok=0") < line.index(";")


def test_sg13g2_handles_a_comment_on_a_continued_line():
    # The comment ends its own *physical* line; the `+` line continues the
    # element. Folding before separating the comment would splice it into the
    # middle of the logical line and swallow the continuation's tokens.
    text, rows = gate.gate_netlist(
        "XM1 d g s b $ nodes\n+ sg13_lv_nmos w=1u ; dims\n.end\n",
        [],
        ["xm1"],
        instance_param="mm_ok",
        declared_params=SG13_DECLARED,
    )

    assert rows == [{"instance": "XM1", "model": "sg13_lv_nmos", "randomised": False}]
    line = next(ln for ln in text.split("\n") if ln.startswith("XM1"))
    code, _, comment = line.partition("$")
    assert code.split() == [
        "XM1",
        "d",
        "g",
        "s",
        "b",
        "sg13_lv_nmos",
        "w=1u",
        "mm_ok=0",
    ]
    assert comment.strip() == "nodes ; dims"


def test_sg13g2_ignores_an_mm_ok_that_only_appears_inside_a_comment():
    # A commented-out assignment is not an assignment: it must not be
    # "replaced" in place, leaving the element itself ungated.
    text, _rows = gate.gate_netlist(
        "XM1 d g s b sg13_lv_nmos w=1u $ was mm_ok=1\n.end\n",
        [],
        ["xm1"],
        instance_param="mm_ok",
        declared_params=SG13_DECLARED,
    )

    line = next(ln for ln in text.split("\n") if ln.startswith("XM1"))
    assert line == "XM1 d g s b sg13_lv_nmos w=1u mm_ok=0 $ was mm_ok=1"


def test_split_eol_comment_respects_quotes_and_braces():
    assert decl.split_eol_comment("X1 a b m={1;2} $ c") == ("X1 a b m={1;2} ", "$ c")
    assert decl.split_eol_comment("X1 a b v='a$b' $ c") == ("X1 a b v='a$b' ", "$ c")
    # `$` only opens a comment at the start of a line or after whitespace.
    assert decl.split_eol_comment("X1 a b w=1u$2") == ("X1 a b w=1u$2", "")


def test_declaration_scan_ignores_parameters_named_only_in_a_comment():
    # Reading comment prose as a declaration would re-open the hazard from the
    # other end: klt would write mm_ok=0 onto a subcircuit that never declared
    # it and report `randomised: false` for a still-randomised instance.
    header = decl.scan_declared_params(
        ".subckt userblk a b foo=1 $ mm_ok=1 is the default\n.ends\n", {}
    )
    assert header == {"userblk": {"foo"}}
    body = decl.scan_declared_params(
        ".subckt userblk a b\n.param foo=1 ; mm_ok=1 here\n.ends\n", {}
    )
    assert body == {"userblk": {"foo"}}


def test_sg13g2_gates_a_subcircuit_declaring_mm_ok_in_a_body_param():
    text, _rows = gate.gate_netlist(
        SG13_NETLIST,
        [],
        ["xc1"],
        instance_param="mm_ok",
        declared_params=SG13_DECLARED,
    )

    assert "XC1 d 0 cap_cmim mm_ok=0" in text


def test_sg13g2_all_excluded_is_a_negative_control():
    text, rows = gate.gate_netlist(
        SG13_NETLIST,
        [],
        ["x*"],
        instance_param="mm_ok",
        declared_params=SG13_DECLARED,
    )

    assert not any(r["randomised"] for r in rows)
    assert text.count("mm_ok=0") == 3


def test_sg13g2_refuses_a_user_subcircuit_with_no_parameters():
    # ngspice aborts outright here ("0 formal but 1 actual params").
    declared = decl.scan_declared_params(".subckt userdio a c\n.ends\n", {})
    with pytest.raises(gate.GateError, match="declares no such parameter"):
        gate.gate_netlist(
            "XU1 a c userdio\n.end\n",
            [],
            ["xu1"],
            instance_param="mm_ok",
            declared_params=declared,
        )


def test_sg13g2_refuses_a_user_subcircuit_declaring_some_other_parameter():
    # The silent case: ngspice *accepts* `mm_ok=0` on an instance of a
    # subcircuit that declares any other parameter and leaves the devices
    # inside it randomised, so klt would report `randomised: false` for an
    # instance that is still randomised (measured, ngspice-47, 2026-11-13).
    declared = decl.scan_declared_params(".subckt userdio a c foo=1\n.ends\n", {})
    with pytest.raises(gate.GateError, match="declares no such parameter"):
        gate.gate_netlist(
            "XU1 a c userdio\n.end\n",
            [],
            ["xu1"],
            instance_param="mm_ok",
            declared_params=declared,
        )


def test_sg13g2_refuses_a_pdk_device_with_no_mm_ok_gate():
    with pytest.raises(gate.GateError, match="'pnpmpa' declares no such parameter"):
        gate.gate_netlist(
            "XQ1 c b e pnpmpa\n.end\n",
            [],
            ["xq1"],
            instance_param="mm_ok",
            declared_params=SG13_DECLARED,
        )


def test_sg13g2_refuses_an_unknown_subcircuit():
    with pytest.raises(gate.GateError, match="was not found in the request netlist"):
        gate.gate_netlist(
            "XM9 d g s b sg13_hv_nmos\n.end\n",
            [],
            ["xm9"],
            instance_param="mm_ok",
            declared_params=SG13_DECLARED,
        )


def test_gate_netlist_needs_exactly_one_mode():
    with pytest.raises(gate.GateError, match="exactly one of switch"):
        gate.gate_netlist(SG13_NETLIST, [], ["xm1"])
    with pytest.raises(gate.GateError, match="exactly one of switch"):
        gate.gate_netlist(
            SG13_NETLIST, [], ["xm1"], "mc_mm_switch", instance_param="mm_ok"
        )


def test_scan_declared_params_skips_lib_section_definitions():
    text = (
        ".subckt visible a b p=1\n.ends\n"
        ".lib some_section\n.subckt hidden a b q=1\n.ends\n.endl some_section\n"
    )

    assert decl.scan_declared_params(text, {}) == {"visible": {"p"}}


def test_scan_library_declared_params_reads_only_the_named_section(tmp_path):
    (tmp_path / "devices_mm.lib").write_text(
        ".subckt sg13_lv_nmos d g s b\n+ w=1u mm_ok=1\n.ends\n"
    )
    (tmp_path / "devices.lib").write_text(
        ".subckt sg13_lv_nmos d g s b\n+ w=1u\n.ends\n"
    )
    (tmp_path / "corner.lib").write_text(
        ".LIB mos_tt\n  .include devices.lib\n.ENDL mos_tt\n"
        ".LIB mos_tt_mismatch\n  .include devices_mm.lib\n.ENDL mos_tt_mismatch\n"
    )

    lib = str(tmp_path / "corner.lib")
    assert (
        "mm_ok"
        in decl.scan_library_declared_params(lib, "mos_tt_mismatch", {})["sg13_lv_nmos"]
    )
    assert (
        "mm_ok"
        not in decl.scan_library_declared_params(lib, "mos_tt", {})["sg13_lv_nmos"]
    )


def test_scan_library_declared_params_follows_a_file_bearing_lib_reference(tmp_path):
    (tmp_path / "inner.lib").write_text(
        ".LIB res_mm\n.subckt rsil a b bn\n.param w=1u mm_ok=1\n.ends\n.ENDL res_mm\n"
    )
    (tmp_path / "outer.lib").write_text(".LIB top\n.lib inner.lib res_mm\n.ENDL top\n")

    declared = decl.scan_library_declared_params(str(tmp_path / "outer.lib"), "top", {})
    assert "mm_ok" in declared["rsil"]


def test_scan_library_declared_params_ignores_a_missing_file(tmp_path):
    assert decl.scan_library_declared_params(str(tmp_path / "nope.lib"), "tt", {}) == {}


def _sg13g2_pdk(tmp_path):
    """A minimal ihp-sg13g2 install: a corner library whose
    ``mos_tt_mismatch`` section includes the mm_ok-declaring device models,
    and whose plain ``mos_tt`` section includes the ones that do not."""
    models = tmp_path / "pdk" / "ihp-sg13g2" / "libs.tech" / "ngspice" / "models"
    models.mkdir(parents=True)
    (tmp_path / "pdk" / "ihp-sg13g2" / "libs.tech" / "klayout").mkdir()
    (models / "devices_mismatch.lib").write_text(SG13_LIB)
    (models / "devices.lib").write_text(
        ".subckt sg13_lv_nmos d g s b\n+ w=0.35u l=0.34u\n.ends\n"
    )
    (models / "cornerMOSlv.lib").write_text(
        ".LIB mos_tt\n  .include devices.lib\n.ENDL mos_tt\n"
        ".LIB mos_tt_mismatch\n  .include devices_mismatch.lib\n"
        ".ENDL mos_tt_mismatch\n"
    )
    return tmp_path / "pdk"


def _sg13g2_request(tmp_path, netlist=SG13_NETLIST, **mc_extra):
    import json

    root = _sg13g2_pdk(tmp_path)
    (tmp_path / "body.spice").write_text(netlist)
    path = tmp_path / "request.json"
    path.write_text(
        json.dumps(
            {
                "netlist": "body.spice",
                "models": {
                    "pdk": "ihp-sg13g2",
                    "pdk_root": str(root),
                    "lib": "libs.tech/ngspice/models/cornerMOSlv.lib",
                },
                "corners": {"process": ["mos_tt_mismatch"]},
                "monte_carlo": {"n": 2, "seed": 1, "vary": "mismatch", **mc_extra},
                "analysis": {"kind": "op"},
            }
        )
    )
    return path


def _capture_dispatched_netlists(monkeypatch):
    """Record the netlist each generated deck ``.include``s, stubbing ngspice."""
    import re
    import subprocess

    included: list[str] = []
    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if "-o" not in cmd:
            return real_run(cmd, *args, **kwargs)
        with open(cmd[cmd.index("-o") + 1], "w", encoding="utf-8") as handle:
            handle.write("")
        deck = next(c for c in cmd if str(c).endswith(".cir"))
        match = re.search(r"^\.include (\S+)", open(deck).read(), re.MULTILINE)
        included.append(open(match.group(1)).read())
        return subprocess.CompletedProcess(cmd, 0, "** ngspice-99\n", "")

    monkeypatch.setattr(sim.subprocess, "run", fake_run)
    return included


def test_run_sim_sg13g2_gates_with_mm_ok_on_the_instance_line(tmp_path, monkeypatch):
    included = _capture_dispatched_netlists(monkeypatch)
    request = _sg13g2_request(tmp_path, instances={"exclude": ["xm1"]})

    report = sim.run_sim(str(request))

    gating = report["environment"]["monte_carlo"]["instance_mismatch"]
    assert gating["switch"] == "mm_ok"
    assert gating["negative_control"] is False
    assert {r["instance"]: r["randomised"] for r in gating["instances"]} == {
        "XM1": False,
        "XM2": True,
        "XC1": True,
    }
    assert included
    for text in included:
        assert "XM1 d g s b sg13_lv_nmos w=1u l=0.13u ng=1 m=1 mm_ok=0" in text
        # no wrapper subcircuit, so hierarchical probe names are unchanged
        assert "klt_mmoff_" not in text
        assert "XM2 d2 g s b sg13_lv_nmos w=1u l=0.13u mm_ok=1" in text


def test_run_sim_sg13g2_negative_control_gates_every_instance(tmp_path, monkeypatch):
    included = _capture_dispatched_netlists(monkeypatch)
    request = _sg13g2_request(tmp_path, instances={"exclude": ["x*"]})

    report = sim.run_sim(str(request))

    gating = report["environment"]["monte_carlo"]["instance_mismatch"]
    assert gating["negative_control"] is True
    assert included and all(t.count("mm_ok=0") == 3 for t in included)


def test_run_sim_sg13g2_refuses_an_instance_whose_subckt_lacks_mm_ok(tmp_path):
    netlist = SG13_NETLIST.replace(
        ".end\n", "XU1 d 0 userblk\n.subckt userblk a b foo=1\nR1 a b 1k\n.ends\n.end\n"
    )
    request = _sg13g2_request(tmp_path, netlist=netlist, instances={"include": ["xm1"]})

    with pytest.raises(sim.SimError, match="'userblk' declares no such parameter"):
        sim.run_sim(str(request))


def test_run_sim_sg13g2_still_refuses_process_only_vary(tmp_path):
    request = _sg13g2_request(tmp_path, vary="process", instances={"exclude": ["xm1"]})

    with pytest.raises(sim.SimError, match="requires monte_carlo.vary"):
        sim.run_sim(str(request))


def test_run_sim_sg13g2_still_refuses_offhost_backends(tmp_path):
    request = _sg13g2_request(tmp_path, instances={"exclude": ["xm1"]})

    with pytest.raises(sim.SimError, match="only supported on local backends"):
        sim.run_sim(str(request), backend="batch")


def test_run_sim_sg13g2_still_refuses_a_glob_matching_nothing(tmp_path):
    request = _sg13g2_request(tmp_path, instances={"exclude": ["xq*"]})

    with pytest.raises(sim.SimError, match="matches no top-level X instance"):
        sim.run_sim(str(request))
