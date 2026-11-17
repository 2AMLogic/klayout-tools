"""`klt pex`'s device-model tally and its "is the reference hierarchical?"
companion, both of which read a device card as *nodes, then one model name,
then ``name=value`` parameters* (issue #5461).

``tests/test_pex.py`` sits at its file-size ceiling, so the tokenizer
regressions live here rather than beside the other ``_model_mismatch``
tests.
"""

import pytest

from klayout_tools.pex import (
    _device_model_counts,
    _model_mismatch,
    _reference_is_hierarchical,
)

MODEL = "sky130_fd_pr__nfet_01v8"


@pytest.mark.parametrize(
    "params",
    [
        "w = 1 l = 0.15",
        "w= 1 l =0.15",
        "w =1 l= 0.15",
        "w=1 l = 0.15",
    ],
)
def test_spaced_parameter_assignment_does_not_become_the_model(params):
    """ngspice accepts a space on either side of ``=``, so a hand-written or
    xschem-netlisted reference may spell ``w = 1`` what `klt extract` writes
    ``w=1``.

    Before the three card tokenizers converged, the spaced spellings left
    bare ``w``/``=``/``1`` tokens, and "the model is the last token without
    ``=``" read the *last parameter value* as the model name -- so the
    reference and extracted sides disagreed about a model neither netlist
    names, and `klt pex` reported a phantom ``model_mismatch``.
    """
    card = f"XM1 d g s b {MODEL} {params}\n"
    assert _device_model_counts(card) == {MODEL: (MODEL, 1)}


def test_spaced_reference_no_longer_mismatches_unspaced_extracted(tmp_path):
    reference = tmp_path / "ref.spice"
    extracted = tmp_path / "ext.spice"
    reference.write_text(f"XM1 d g s b {MODEL} w = 1 l = 0.15\n")
    extracted.write_text(f"XM1 d g s b {MODEL} w=1 l=0.15\n")
    assert (
        _model_mismatch(
            reference_netlist=str(reference), extracted_netlist=str(extracted)
        )
        is None
    )


@pytest.mark.parametrize(
    "params",
    [
        "w=(2 * 1u) l=0.15u",
        "w = (2 * 1u) l = 0.15u",
    ],
)
def test_parenthesised_value_expression_stays_in_its_own_token(params):
    """A parenthesised expression spans spaces exactly as the
    already-handled ``{...}`` (issue #4383) and ``'...'`` (issue #2866)
    forms do; unhandled, its tail (``1u)``) spilled out as the model name.
    """
    assert _device_model_counts(f"M1 d g s b nfet {params}\n") == {"nfet": ("nfet", 1)}


def test_parenthesised_reference_no_longer_mismatches_literal_extracted(tmp_path):
    reference = tmp_path / "ref.spice"
    extracted = tmp_path / "ext.spice"
    reference.write_text("M1 d g s b nfet w=(2 * 1u) l=0.15u\n")
    extracted.write_text("M1 d g s b nfet w=2u l=0.15u\n")
    assert (
        _model_mismatch(
            reference_netlist=str(reference), extracted_netlist=str(extracted)
        )
        is None
    )


def test_hierarchical_reference_detected_through_a_spaced_assignment():
    """The same "last token without ``=``" rule decides whether an X card
    targets a locally-defined ``.subckt``, so a spaced assignment used to
    hide a hierarchical reference from ``model_mismatch``'s
    hierarchy-artifact caveat (issue #1085's shape)."""
    text = (
        ".subckt inv a y vdd vss\n"
        "XM1 y a vdd vdd sky130_fd_pr__pfet_01v8 w=1 l=0.15\n"
        ".ends\n"
        "Xi0 in out vdd vss inv w = 1\n"
    )
    assert _reference_is_hierarchical(text) is True


def test_unspaced_cards_are_unchanged():
    """The spelling `klt extract` itself writes keeps its existing tally."""
    text = (
        f"XM1 d g s b {MODEL} w=1 l=0.15\n"
        f"XM2 d2 g s b {MODEL} w=1 l=0.15\n"
        "M3 d g s b nfet_lv L=0.15u W=1u\n"
    )
    assert _device_model_counts(text) == {MODEL: (MODEL, 2), "nfet_lv": ("nfet_lv", 1)}
