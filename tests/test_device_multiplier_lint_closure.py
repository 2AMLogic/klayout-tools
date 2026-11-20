"""Device-multiplier findings and disclosures across SPICE include files."""

import pytest

from helpers.subprocess_fakes import _stub_subprocess_run
from klayout_tools import device_multiplier_lint as lint
from klayout_tools import sim
from test_device_multiplier_lint import (
    DECLARING_NOT_FORWARDING,
    _findings,
    _report,
    _subckts,
    _write_fixtures,
)

pytestmark = pytest.mark.usefixtures(
    "real_build_identity_git", "bare_name_ngspice_resolution"
)


def test_closure_file_card_finding_carries_file():
    cards = [("XU1 d g s b sky130_fd_pr__nfet_01v8 mult=6", "dut.sp")]
    findings = lint.lint_instances(cards, _subckts(DECLARING_NOT_FORWARDING))
    assert [f["kind"] for f in findings] == [lint.DRIVE_1X]
    assert findings[0]["file"] == "dut.sp"


def test_top_level_card_finding_has_no_file_key():
    findings = _findings(
        "XU1 d g s b sky130_fd_pr__nfet_01v8 mult=6\n", DECLARING_NOT_FORWARDING
    )
    assert findings == [
        {
            "instance": "XU1",
            "subckt": "sky130_fd_pr__nfet_01v8",
            "kind": lint.DRIVE_1X,
            "m": None,
            "mult": "6",
            "note": findings[0]["note"],
        }
    ]


def test_two_closure_files_each_defining_x1_are_distinguished_by_file():
    model = ".subckt dev a b mult=1\nR1 a b 1k\n.ends\n"
    cards = [
        ("X1 a b dev mult=6", "a.sp"),
        ("X1 a b dev m=3", "b.sp"),
    ]
    findings = lint.lint_instances(cards, _subckts(model))
    assert [(f["file"], f["kind"]) for f in findings] == [
        ("a.sp", lint.DRIVE_1X),
        ("b.sp", lint.DRIVE_MISMATCH_DISAGREEMENT),
    ]
    assert all(f["instance"] == "X1" for f in findings)


def test_closure_expression_card_is_not_unevaluated_noise():
    # The sky130 res_xhigh_po shape: a library subckt's own body passes
    # mult={mult} on to a declaring-not-forwarding subckt. That is a
    # parametric library definition, not a user pair to cross-check, so a
    # closure card's uncomparable value must not surface as `unevaluated`.
    cards = [
        ("Xwrap d g s b sky130_fd_pr__nfet_01v8 w={w} l={l} mult={mult}", "models.sp")
    ]
    report = lint.instance_report(cards, _subckts(DECLARING_NOT_FORWARDING))
    assert report == {"findings": []}


def test_closure_unverified_card_is_not_noted():
    cards = [("XU1 a b somewhere_else mult=6", "lib.sp")]
    report = lint.instance_report(cards, _subckts(DECLARING_NOT_FORWARDING))
    assert report == {"findings": []}


def test_top_level_expression_and_unverified_disclosures_are_kept():
    # The suppression above is scoped to closure cards: the netlist's own
    # uncomparable or unresolvable pairs stay disclosed exactly as before.
    report = _report(
        "XU1 d g s b sky130_fd_pr__nfet_01v8 mult={w*2}\nXU2 a b somewhere_else m=2\n",
        DECLARING_NOT_FORWARDING,
    )
    assert report["findings"] == []
    assert "not cross-checked" in report["note"]
    assert [e["instance"] for e in report["unevaluated"]] == ["XU1"]


def test_run_sim_dut_card_in_included_file_is_flagged_with_file(
    tmp_path, monkeypatch, capsys
):
    # The DUT sits in the included file, not the netlist: the finding must
    # still fire, now carrying the closure file's scanned-closure label.
    request = _write_fixtures(
        tmp_path,
        body="R1 d 0 1k\n",
        models=DECLARING_NOT_FORWARDING
        + "XU1 d g s b sky130_fd_pr__nfet_01v8 mult=6\n",
    )
    _stub_subprocess_run(monkeypatch)

    report = sim.run_sim(request)
    entry = report["environment"]["device_multiplier"]

    (finding,) = entry["findings"]
    assert finding["kind"] == lint.DRIVE_1X
    assert finding["instance"] == "XU1"
    assert finding["file"] == "<external>/models.spice"
    assert "device-multiplier finding(s)" in capsys.readouterr().err


def test_run_sim_netlist_in_own_closure_is_not_double_counted(tmp_path, monkeypatch):
    # stage_netlist always lists the netlist itself as the first closure
    # file, so every top-level card is reachable twice; each must yield at
    # most one finding, with today's top-level shape (no `file` key).
    request = _write_fixtures(
        tmp_path,
        body="XU1 d g s b sky130_fd_pr__nfet_01v8 mult=6\nR1 d 0 1k\n",
        models=DECLARING_NOT_FORWARDING,
    )
    _stub_subprocess_run(monkeypatch)

    report = sim.run_sim(request)
    entry = report["environment"]["device_multiplier"]

    assert [f["instance"] for f in entry["findings"]] == ["XU1"]
    assert all("file" not in f for f in entry["findings"])


def test_run_sim_definitions_only_closure_file_stays_quiet(tmp_path, monkeypatch):
    # An included file that only defines subcircuits (no instance cards of
    # its own) must not wake the lint on a clean netlist.
    request = _write_fixtures(
        tmp_path,
        body="XU1 d g s b sky130_fd_pr__nfet_01v8 mult=6 m=6\nR1 d 0 1k\n",
        models=DECLARING_NOT_FORWARDING,
    )
    _stub_subprocess_run(monkeypatch)

    report = sim.run_sim(request)

    assert "device_multiplier" not in report["environment"]


def test_vendored_model_internals_are_quiet_but_dut_cards_are_checked(
    tmp_path, monkeypatch, capsys
):
    # A reduced sky130 combined/continuous deck. The FET library instantiates
    # parasitic devices declared by the diode library. Both are ordinary
    # relative includes outside PDK_ROOT, as they would be in a vendored copy.
    monkeypatch.setenv("PDK_ROOT", str(tmp_path / "installed-pdks"))
    models = tmp_path / "models"
    models.mkdir()
    (models / "models_fet.spice").write_text(
        ".subckt fet d g s b mult=1\n"
        "Xdrain b d diode m = 0.5\n"
        "Xsrc b s diode mult=6\n"
        "M1 d g s b nch\n"
        ".MODEL nch NMOS\n"
        ".ends\n"
        # sky130's nfet_g5v0d16v0 shape: a wrapper that declares mult and
        # carries parasitic cards, but whose `.model` lives in a sibling.
        ".subckt wrap d g s b mult=1\n"
        "Xpar b d diode m = 0.5\n"
        "Xcore d g s b fet\n"
        ".ends\n"
    )
    (models / "models_diodes.spice").write_text(
        ".subckt diode a b mult=1\nD1 a b junction\n.model junction D\n.ends\n"
    )
    includes = (
        '.include "models/models_fet.spice"\n.include "models/models_diodes.spice"\n'
    )
    _stub_subprocess_run(monkeypatch)
    request = _write_fixtures(tmp_path, body=includes + "X1 d g 0 0 fet\n", models=None)
    assert "device_multiplier" not in sim.run_sim(request)["environment"]
    assert "device-multiplier finding(s)" not in capsys.readouterr().err

    (tmp_path / "dut.spice").write_text(
        "* .model in a comment\n.control\n.model ctrl D\n.endc\n"
        ".subckt dut d g\nXD d g 0 0 fet mult=6\n.ends\n"
    )
    request = _write_fixtures(
        tmp_path,
        body=includes + '.include "dut.spice"\n.model local D\n'
        "Xtop d g 0 0 fet m=2\nXuut d g dut\n",
        models=None,
    )
    findings = sim.run_sim(request)["environment"]["device_multiplier"]["findings"]
    assert [(f["instance"], f["kind"], f.get("file")) for f in findings] == [
        ("Xtop", lint.DRIVE_MISMATCH_DISAGREEMENT, None),
        ("XD", lint.DRIVE_1X, "<external>/dut.spice"),
    ]


_LEAF_PLAIN = ".subckt leaf a b mult=1\nRcore a b 1k\n.ends leaf\n"
_LEAF_FORWARD = ".subckt leaf a b mult=1\nRcore a b 1k m={mult}\n.ends leaf\n"


def _dut_with_private_leaf(name, leaf, call):
    # `leaf` is nested in `name`, so it is private to it: ngspice resolves the
    # call inside `name` to this definition whatever the include order is.
    return f".subckt {name} p n\n{leaf}{call}\n.ends {name}\n"


def _closure_findings(tmp_path, monkeypatch, *, body):
    request = _write_fixtures(tmp_path, body=body, models=None)
    _stub_subprocess_run(monkeypatch)
    report = sim.run_sim(request)
    return report["environment"].get("device_multiplier", {}).get("findings", [])


@pytest.mark.parametrize(
    "plain_first", [True, False], ids=["plain-first", "forward-first"]
)
@pytest.mark.parametrize(
    ("call", "kind"),
    [
        ("XU1 p n leaf mult=6", lint.DRIVE_1X),
        ("XU1 p n leaf m=3", lint.DRIVE_MISMATCH_DISAGREEMENT),
    ],
)
def test_private_callee_resolves_in_its_own_scope_in_either_include_order(
    tmp_path, monkeypatch, plain_first, call, kind
):
    # Two DUTs each carry a private `leaf`; only plain.spice's ignores mult.
    # The report must flag plain.spice/XU1 and nothing else, not the first
    # `leaf` read for both calls (or for neither).
    (tmp_path / "plain.spice").write_text(
        _dut_with_private_leaf("dut_plain", _LEAF_PLAIN, call)
    )
    (tmp_path / "forward.spice").write_text(
        _dut_with_private_leaf("dut_forward", _LEAF_FORWARD, call)
    )
    order = ["plain.spice", "forward.spice"]
    if not plain_first:
        order.reverse()
    body = "".join(f'.include "{name}"\n' for name in order)
    body += "Xa a 0 dut_plain\nXb b 0 dut_forward\nV1 a 0 1\nV2 b 0 1\n"

    findings = _closure_findings(tmp_path, monkeypatch, body=body)

    assert [(f["instance"], f["kind"], f["file"]) for f in findings] == [
        ("XU1", kind, "<external>/plain.spice")
    ]


_CLAMP = (
    ".subckt clamp a b\nD1 a b local_diode\n"
    ".model local_diode D(Is=1e-16)\n.ends clamp\n"
)
_DUT = ".subckt dut a b\nXU1 a b leaf mult=6\n.ends dut\n"


@pytest.mark.parametrize("layout", ["clamp-first", "clamp-last", "clamp-own-include"])
def test_sibling_subckt_with_a_local_model_does_not_hide_the_dut_finding(
    tmp_path, monkeypatch, layout
):
    # Where a sibling subcircuit that declares its own `.model` sits relative
    # to the DUT must not change whether the DUT's call is reported.
    (tmp_path / "leaf.spice").write_text(_LEAF_PLAIN)
    includes = '.include "leaf.spice"\n.include "dut.spice"\n'
    if layout == "clamp-own-include":
        (tmp_path / "clamp.spice").write_text(_CLAMP)
        (tmp_path / "dut.spice").write_text(_DUT)
        includes += '.include "clamp.spice"\n'
    else:
        parts = [_CLAMP, _DUT] if layout == "clamp-first" else [_DUT, _CLAMP]
        (tmp_path / "dut.spice").write_text("".join(parts))

    findings = _closure_findings(tmp_path, monkeypatch, body=includes + "Xd a b dut\n")

    assert [(f["instance"], f["kind"], f["file"]) for f in findings] == [
        ("XU1", lint.DRIVE_1X, "<external>/dut.spice")
    ]


_NESTED_SHADOWS_GLOBAL = (
    ".subckt leaf a b mult=1\nRg a b 1k m={mult}\n.ends leaf\n"
    ".subckt outer p n\n"
    ".subckt leaf a b mult=1\nRn a b 1k\n.ends leaf\n"
    "Xin p n leaf mult=6\n"
    ".ends outer\n"
    "Xout p n leaf mult=6\n"
)


def test_scan_tags_each_card_with_its_enclosing_subckt_chain():
    text = (
        "X0 a b top m=2\n"
        ".subckt Outer p n\nX1 p n leaf m=2\n"
        ".subckt inner p n\nX2 p n leaf m=2\n.ends\n"
        "X3 p n leaf m=2\n.ends\n"
    )
    scanned = [
        (card.split()[0], scope) for card, _, scope in lint.scan_instance_cards(text)
    ]
    assert scanned == [
        ("X0", ()),
        ("X1", ("outer",)),
        ("X2", ("outer", "inner")),
        ("X3", ("outer",)),
    ]
    assert {label for _, label, _ in lint.scan_instance_cards(text, label="f.sp")} == {
        "f.sp"
    }


def test_scan_skips_a_device_definitions_body_wherever_it_sits():
    # A subckt declaring `mult` is a device definition: its own body cards are
    # the library's internals. A sibling that declares a `.model`, or none,
    # is scanned either way -- the decision reads only the enclosing header.
    device = ".subckt dev a b mult=1\nXpar a b diode m=0.5\n.ends\n"
    dut = ".subckt dut a b\nXu a b dev mult=6\n.ends\n"
    clamp = ".subckt clamp a b\nD1 a b d\n.model d D\n.ends\n"
    for text in (device + dut + clamp, clamp + dut + device, dut + clamp + device):
        names = [c.split()[0] for c, _, _ in lint.scan_instance_cards(text)]
        assert sorted(names) == ["Xpar", "Xu"]
        skipped = lint.scan_instance_cards(text, skip_device_internals=True)
        assert [c.split()[0] for c, _, _ in skipped] == ["Xu"]


def test_card_resolves_to_the_callee_its_enclosing_subckt_makes_visible():
    table = lint.collect_subckts([_NESTED_SHADOWS_GLOBAL])
    findings = lint.lint_instances(
        lint.scan_instance_cards(_NESTED_SHADOWS_GLOBAL), table
    )
    # Inside `outer` the nested, non-forwarding `leaf` shadows the global
    # forwarding one; outside it the global one is the callee.
    assert [(f["instance"], f["kind"]) for f in findings] == [("Xin", lint.DRIVE_1X)]


def test_nested_subckt_is_not_visible_outside_its_parent():
    text = ".subckt outer p n\n.subckt inner a b mult=1\nRn a b 1k\n.ends\n.ends\n"
    table = lint.collect_subckts([text])
    card = "Xtop p n inner mult=6"
    # A bare card keeps the flat view (the name is found)...
    assert [f["kind"] for f in lint.lint_instances([card], table)] == [lint.DRIVE_1X]
    # ...a scoped top-level card sees what the simulator sees: no `inner`.
    report = lint.instance_report([(card, None, ())], table)
    assert report["findings"] == []
    assert "not cross-checked" in report["note"]
