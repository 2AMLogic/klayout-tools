"""Unit tests for :mod:`klayout_tools.spice_flatten` (issue #2889)."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from klayout_tools.spice_flatten import SpiceFlattenError, flatten_spice

_INV_BUF = (
    "* two-level hierarchy\n"
    ".SUBCKT INV A Y VDD VSS\n"
    "Xn Y A VSS VSS sky130_fd_pr__nfet_01v8 w=1 l=0.15\n"
    "Xp Y A VDD VDD sky130_fd_pr__pfet_01v8 w=2 l=0.15\n"
    ".ENDS INV\n"
    ".SUBCKT BUF A Y VDD VSS\n"
    "Xi1 A M VDD VSS INV\n"
    "Xi2 M Y VDD VSS INV\n"
    ".ENDS BUF\n"
)


def _cards(text: str) -> list[str]:
    return [
        line
        for line in text.splitlines()
        if line and not line.startswith("*") and not line.startswith(".")
    ]


def test_inlines_one_level_with_instance_prefixes():
    result = flatten_spice(_INV_BUF)
    assert result.roots == ["BUF"]
    assert result.inlined_instances == 2
    assert result.inlined_subckts == ["INV"]
    assert result.external_subckts == [
        "sky130_fd_pr__nfet_01v8",
        "sky130_fd_pr__pfet_01v8",
    ]
    assert _cards(result.text) == [
        "X.Xi1.Xn M A VSS VSS sky130_fd_pr__nfet_01v8 w=1 l=0.15",
        "X.Xi1.Xp M A VDD VDD sky130_fd_pr__pfet_01v8 w=2 l=0.15",
        "X.Xi2.Xn Y M VSS VSS sky130_fd_pr__nfet_01v8 w=1 l=0.15",
        "X.Xi2.Xp Y M VDD VDD sky130_fd_pr__pfet_01v8 w=2 l=0.15",
    ]
    # The child definition is gone; only the flattened root remains.
    assert ".SUBCKT INV" not in result.text
    assert ".SUBCKT BUF A Y VDD VSS" in result.text
    assert ".ends BUF" in result.text


def test_two_levels_prefix_internal_nodes_and_devices():
    text = (
        ".subckt leaf a b\n"
        "R1 a mid 100\n"
        "R2 mid b 100\n"
        ".ends leaf\n"
        ".subckt mid_cell p q\n"
        "Xl1 p inner leaf\n"
        "Xl2 inner q leaf\n"
        ".ends mid_cell\n"
        ".subckt top in out\n"
        "Xm in out mid_cell\n"
        ".ends top\n"
    )
    result = flatten_spice(text)
    assert result.roots == ["top"]
    assert result.inlined_instances == 3
    assert result.inlined_subckts == ["leaf", "mid_cell"]
    assert result.external_subckts == []
    assert _cards(result.text) == [
        "R.Xm.Xl1.R1 in Xm.Xl1.mid 100",
        "R.Xm.Xl1.R2 Xm.Xl1.mid Xm.inner 100",
        "R.Xm.Xl2.R1 Xm.inner Xm.Xl2.mid 100",
        "R.Xm.Xl2.R2 Xm.Xl2.mid out 100",
    ]


def test_parameter_defaults_and_instance_overrides():
    text = (
        ".subckt cell a b params: W=1 L=0.15\n"
        "Xn a b b b nfet w={W} l={L} nf=2\n"
        "Rb a b {W*1k}\n"
        ".ends cell\n"
        ".subckt top a b\n"
        "X1 a b cell W=4\n"
        "X2 a b cell\n"
        ".ends top\n"
    )
    cards = _cards(flatten_spice(text).text)
    assert cards == [
        "X.X1.Xn a b b b nfet w=4 l=0.15 nf=2",
        "R.X1.Rb a b {4*1k}",
        "X.X2.Xn a b b b nfet w=1 l=0.15 nf=2",
        "R.X2.Rb a b {1*1k}",
    ]


def test_nested_parameter_override_evaluated_in_caller_scope():
    text = (
        ".subckt leaf a b W=1\n"
        "R1 a b {W}\n"
        ".ends leaf\n"
        ".subckt mid a b Wm=2\n"
        ".param half={Wm/2}\n"
        "X1 a b leaf W={half*3}\n"
        ".ends mid\n"
        ".subckt top a b\n"
        "Xm a b mid Wm=8\n"
        ".ends top\n"
    )
    (card,) = _cards(flatten_spice(text).text)
    assert card == "R.Xm.X1.R1 a b {(8/2)*3}"


def test_nested_subckt_inherits_parameter_set_on_enclosing_instance():
    """ngspice scopes params hierarchically: `leaf` has no `rval` of its own,
    so it sees the `rval=2k` bound on the enclosing `mid` instance."""
    text = (
        ".subckt leaf a b\n"
        "R1 a b {rval}\n"
        ".ends\n"
        ".subckt mid a b rval=1k\n"
        "X1 a b leaf\n"
        ".ends\n"
        ".subckt top a b\n"
        "X1 a b mid rval=2k\n"
        ".ends\n"
    )
    assert _cards(flatten_spice(text, top="top").text) == ["R.X1.X1.R1 a b 2k"]


def test_child_own_parameter_shadows_inherited_one():
    text = (
        ".subckt leaf a b rval=5k\n"
        "R1 a b {rval}\n"
        "R2 a b {w}\n"
        ".ends\n"
        ".subckt mid a b rval=1k w=3\n"
        "X1 a b leaf\n"
        ".ends\n"
        ".subckt top a b\n"
        "X1 a b mid rval=2k\n"
        ".ends\n"
    )
    assert _cards(flatten_spice(text, top="top").text) == [
        "R.X1.X1.R1 a b 5k",
        "R.X1.X1.R2 a b 3",
    ]


def test_dollar_prefixed_include_and_lib_targets_survive():
    """`$` starts an inline comment only with whitespace on both sides
    (ngspice's rule); `$PDK_ROOT/...` is a path, not a comment."""
    include = ".include $PDK_ROOT/sky130A/libs.ref/sky130_fd_pr/spice/models.spice"
    lib = ".lib $SKY130/libs.tech/combined/sky130.lib.spice tt"
    text = (
        f"{include}\n{lib}\n"
        ".subckt cell a b\nR1 a b 1k $ trailing comment\nR2 a b 2k $\n.ends cell\n"
        ".subckt top a b\nX1 a b cell\n.ends top\n"
    )
    result = flatten_spice(text)
    lines = result.text.splitlines()
    assert include in lines
    assert lib in lines
    assert _cards(result.text) == ["R.X1.R1 a b 1k", "R.X1.R2 a b 2k"]


def test_root_parameters_are_left_for_ngspice():
    text = (
        ".subckt leaf a b W=1\n"
        "R1 a b {W}\n"
        ".ends leaf\n"
        ".subckt top a b Wt=5\n"
        "X1 a b leaf W={Wt}\n"
        ".ends top\n"
    )
    result = flatten_spice(text)
    assert ".subckt top a b Wt=5" in result.text
    assert _cards(result.text) == ["R.X1.R1 a b {Wt}"]


def test_instance_multiplier_pushed_down():
    text = (
        ".subckt cell a b\n"
        "Xn a b b b nfet w=1 m=2\n"
        "R1 a b 1k\n"
        ".ends cell\n"
        ".subckt top a b\n"
        "X1 a b cell m=3\n"
        ".ends top\n"
    )
    assert _cards(flatten_spice(text).text) == [
        "X.X1.Xn a b b b nfet w=1 m={(2)*(3)}",
        "R.X1.R1 a b 1k m=3",
    ]


def test_global_and_ground_nets_are_not_renamed():
    text = (
        ".global VSUBS\n"
        ".subckt cell a\n"
        "Xn a a 0 VSUBS nfet\n"
        "R1 a gnd 1k\n"
        "R2 a local 1k\n"
        ".ends cell\n"
        ".subckt top a\n"
        "X1 a cell\n"
        ".ends top\n"
    )
    result = flatten_spice(text)
    assert ".global VSUBS" in result.text
    assert _cards(result.text) == [
        "X.X1.Xn a a 0 VSUBS nfet",
        "R.X1.R1 a gnd 1k",
        "R.X1.R2 a X1.local 1k",
    ]


def test_continuations_comments_and_spaced_assignments():
    text = (
        ".subckt cell a b\n"
        "Xn a b b b nfet $ inline comment\n"
        "+ w = 2 l=0.15\n"
        ".ends cell\n"
        ".subckt top a b\n"
        "* a comment\n"
        "X1 a b\n"
        "+ cell\n"
        ".ends top\n"
    )
    assert _cards(flatten_spice(text).text) == ["X.X1.Xn a b b b nfet w=2 l=0.15"]


def test_model_cards_are_hoisted_and_deduplicated():
    text = (
        ".subckt cell a b\n"
        ".model dm d is=1e-14\n"
        "D1 a b dm\n"
        ".ends cell\n"
        ".subckt top a b\n"
        "X1 a b cell\n"
        "X2 b a cell\n"
        ".ends top\n"
    )
    result = flatten_spice(text)
    assert result.text.count(".model dm d is=1e-14") == 1
    assert _cards(result.text) == ["D.X1.D1 a b dm", "D.X2.D1 b a dm"]


def test_already_flat_netlist_is_unchanged_apart_from_banner():
    text = ".SUBCKT RES RA RB\nR1 RA RB 289.2\n.ENDS RES\n"
    result = flatten_spice(text, banner="flattened")
    assert result.inlined_instances == 0
    assert result.roots == ["RES"]
    assert result.text == "* flattened\n.SUBCKT RES RA RB\nR1 RA RB 289.2\n.ends RES\n"


def test_top_is_kept_even_when_instantiated_elsewhere():
    text = (
        ".subckt core a\nR1 a 0 1k\n.ends core\n.subckt wrap a\nX1 a core\n.ends wrap\n"
    )
    assert flatten_spice(text).roots == ["wrap"]
    assert flatten_spice(text, top="CORE").roots == ["core", "wrap"]


def test_file_level_instance_of_local_subckt_is_inlined():
    text = ".subckt cell a\nR1 a mid 1\nR2 mid 0 1\n.ends cell\nX1 in cell\nV1 in 0 1\n"
    result = flatten_spice(text)
    assert result.roots == []
    assert _cards(result.text) == [
        "R.X1.R1 in X1.mid 1",
        "R.X1.R2 X1.mid 0 1",
        "V1 in 0 1",
    ]


# --------------------------------------------------------------------------- #
# Hard errors -- never a partial flatten
# --------------------------------------------------------------------------- #


def _code(text: str, **kwargs) -> str:
    with pytest.raises(SpiceFlattenError) as excinfo:
        flatten_spice(text, **kwargs)
    return excinfo.value.code


def test_cycle_is_an_error():
    text = (
        ".subckt a p\nX1 p b\n.ends a\n"
        ".subckt b p\nX1 p a\n.ends b\n"
        ".subckt top p\nX1 p a\n.ends top\n"
    )
    with pytest.raises(SpiceFlattenError, match="a -> b -> a") as excinfo:
        flatten_spice(text)
    assert excinfo.value.code == "cycle"


def test_self_recursion_is_an_error():
    assert _code(".subckt a p\nX1 p a\n.ends a\nX0 n a\n") == "cycle"


def test_pin_count_mismatch_is_an_error():
    text = (
        ".subckt cell a b\nR1 a b 1\n.ends cell\n.subckt top a\nX1 a cell\n.ends top\n"
    )
    with pytest.raises(SpiceFlattenError, match="passes 1 node") as excinfo:
        flatten_spice(text)
    assert excinfo.value.code == "pin_count_mismatch"


def test_undefined_subckt_is_an_error_against_a_leaf_set():
    text = ".subckt top a b\nX1 a b mystery\nX2 a b b b nfet\n.ends top\n"
    # Default: undefined targets are leaf devices, reported for audit.
    assert flatten_spice(text).external_subckts == ["mystery", "nfet"]
    assert _code(text, leaf_subckts={"NFET"}) == "undefined_subckt"


def test_behavioural_source_inside_inlined_body_is_an_error():
    text = (
        ".subckt cell a b\nB1 a b V=v(a)*2\n.ends cell\n"
        ".subckt top a b\nX1 a b cell\n.ends top\n"
    )
    assert _code(text) == "unsupported_card"


def test_node_naming_control_card_inside_inlined_body_is_an_error():
    text = (
        ".subckt cell a b\nR1 a b 1\n.ic v(a)=0\n.ends cell\n"
        ".subckt top a b\nX1 a b cell\n.ends top\n"
    )
    assert _code(text) == "unsupported_card"


def test_conflicting_hoisted_models_are_an_error():
    text = (
        ".subckt c1 a\n.model dm d is=1\nD1 a 0 dm\n.ends c1\n"
        ".subckt c2 a\n.model dm d is=2\nD1 a 0 dm\n.ends c2\n"
        ".subckt top a\nX1 a c1\nX2 a c2\n.ends top\n"
    )
    assert _code(text) == "unsupported_card"


def test_multiplier_on_a_source_is_an_error():
    text = (
        ".subckt cell a\nV1 a 0 1\n.ends cell\n"
        ".subckt top a\nX1 a cell m=2\n.ends top\n"
    )
    assert _code(text) == "unsupported_card"


@pytest.mark.parametrize(
    "text",
    [
        ".subckt a p\nR1 p 0 1\n",
        ".ends a\n",
        ".subckt a p\n.subckt b q\n.ends b\n.ends a\n",
        ".subckt a p\n.ends a\n.subckt a p\n.ends a\n",
    ],
)
def test_unbalanced_or_duplicate_definitions_are_errors(text):
    assert _code(text) in ("malformed", "unsupported_card")


# --------------------------------------------------------------------------- #
# The flattened deck simulates identically (one ngspice -b run)
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(shutil.which("ngspice") is None, reason="ngspice not installed")
def test_flattened_deck_simulates_to_the_same_operating_point(tmp_path):
    hierarchical = (
        ".subckt half a b R=1\nR1 a mid {R/2}\nR2 mid b {R/2}\n.ends half\n"
        ".subckt RES RA RB\nX1 RA M half R=144.6\nX2 M RB half R=144.6 m=1\n"
        ".ends RES\n"
    )
    flat = flatten_spice(hierarchical).text
    deck = (
        "* flattened\n"
        f"{flat}"
        "V1 RA 0 DC 1.8\nXr RA RB RES\nRl RB 0 1000\n"
        ".control\nop\nprint v(rb)\n.endc\n.end\n"
    )
    path = tmp_path / "deck.cir"
    path.write_text(deck)
    proc = subprocess.run(
        ["ngspice", "-b", str(path)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    line = next(
        line for line in proc.stdout.splitlines() if line.strip().startswith("v(rb)")
    )
    value = float(line.split("=")[1])
    assert value == pytest.approx(1.8 * 1000 / 1289.2, rel=1e-4)


@pytest.mark.skipif(shutil.which("ngspice") is None, reason="ngspice not installed")
def test_flattened_deck_keeps_parameters_inherited_from_enclosing_instance(tmp_path):
    """A file-level `.param rval=10k` must not leak in: the flattened `leaf`
    resolves `rval` from the enclosing `mid` instance (2k), like ngspice."""
    hierarchical = (
        ".subckt leaf a b\nR1 a b {rval}\n.ends\n"
        ".subckt mid a b rval=1k\nX1 a b leaf\n.ends\n"
        ".subckt top a b\nX1 a b mid rval=2k\n.ends\n"
    )
    flat = flatten_spice(hierarchical, top="top").text
    deck = (
        "* flattened\n"
        ".param rval=10k\n"
        f"{flat}"
        "V1 a 0 DC 1\nXt a 0 top\n"
        ".control\nop\nprint i(v1)\n.endc\n.end\n"
    )
    path = tmp_path / "deck.cir"
    path.write_text(deck)
    proc = subprocess.run(
        ["ngspice", "-b", str(path)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    line = next(
        line for line in proc.stdout.splitlines() if line.strip().startswith("i(v1)")
    )
    assert float(line.split("=")[1]) == pytest.approx(-1 / 2000, rel=1e-4)
