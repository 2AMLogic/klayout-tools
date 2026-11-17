"""The one shared SPICE card tokenizer (issue #5461)."""

import pytest

from klayout_tools._spice_tokens import split_spice_tokens

NODES = ["XM1", "d", "g", "s", "b", "sky130_fd_pr__nfet_01v8"]


def test_plain_card_splits_on_whitespace():
    assert split_spice_tokens("M1 d g s b nfet L=0.15u W=1u") == [
        "M1",
        "d",
        "g",
        "s",
        "b",
        "nfet",
        "L=0.15u",
        "W=1u",
    ]


@pytest.mark.parametrize(
    "spelling",
    ["w=1 l=0.15", "w = 1 l = 0.15", "w= 1 l= 0.15", "w =1 l =0.15", "w=1 l = 0.15"],
)
def test_all_four_assignment_spellings_collapse_to_one_token(spelling):
    """ngspice accepts a space on either side of ``=``; every caller here
    goes on to ask "is this token a ``name=value`` parameter?"."""
    assert split_spice_tokens(
        " ".join(["XM1 d g s b sky130_fd_pr__nfet_01v8", spelling])
    ) == [
        *NODES,
        "w=1",
        "l=0.15",
    ]


@pytest.mark.parametrize(
    "value",
    ["(2 * 1u)", "{2 * wn}", "'int((nf+1)/2) * W/nf * 0.18u'", '"2 * 1u"'],
)
def test_expression_values_containing_spaces_stay_whole(value):
    assert split_spice_tokens(f"M1 d g s b nfet w={value}") == [
        "M1",
        "d",
        "g",
        "s",
        "b",
        "nfet",
        f"w={value}",
    ]


def test_spaced_assignment_of_an_expression_value():
    assert split_spice_tokens("M1 d g s b nfet w = (2 * 1u)") == [
        "M1",
        "d",
        "g",
        "s",
        "b",
        "nfet",
        "w=(2 * 1u)",
    ]


def test_nested_groups_do_not_close_early():
    assert split_spice_tokens("M1 a b nfet ad={int((nf+1)/2) * w}") == [
        "M1",
        "a",
        "b",
        "nfet",
        "ad={int((nf+1)/2) * w}",
    ]


def test_quote_shields_an_unbalanced_brace():
    assert split_spice_tokens("M1 a b nfet p='a } b' q=1") == [
        "M1",
        "a",
        "b",
        "nfet",
        "p='a } b'",
        "q=1",
    ]


def test_unbalanced_close_does_not_underflow():
    """A stray ``)`` clamps the depth at zero rather than going negative and
    swallowing the rest of the card."""
    assert split_spice_tokens("M1 a) b nfet w=1") == ["M1", "a)", "b", "nfet", "w=1"]


@pytest.mark.parametrize("line", ["", "   ", "\t\n"])
def test_blank_lines_yield_no_tokens(line):
    assert split_spice_tokens(line) == []


def test_tabs_separate_tokens():
    assert split_spice_tokens("M1\ta\tb\tnfet\tw\t=\t1") == [
        "M1",
        "a",
        "b",
        "nfet",
        "w=1",
    ]


@pytest.mark.parametrize("line", ["=", "= 1", "a =", "w = ", "="])
def test_dangling_equals_is_passed_through_not_dropped(line):
    """A card ending in a bare ``=`` (or starting with one) is malformed
    SPICE; the tokenizer neither raises nor silently drops the ``=``, so a
    caller still sees the text it was given."""
    tokens = split_spice_tokens(line)
    assert "".join(tokens).count("=") == line.count("=")
