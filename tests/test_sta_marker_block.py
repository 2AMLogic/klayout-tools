"""The one stdout marker-block extractor shared by every ``klt sta`` parser
(issue #6767). Before it lived in ``_sta_request_diag`` there were three
byte-identical ``_extract_block`` copies plus an inline slice in
``parse_spef_missing_nets``; a change to how truncated output is recognised
had four places to review."""

from __future__ import annotations

import pytest

from klayout_tools import (
    _sta_request_diag,
    post_route_sta,
    post_route_sta_electrical,
    post_route_sta_instance_paths,
    post_route_sta_path_groups,
    post_route_sta_stages,
)
from klayout_tools._sta_request_diag import (
    SPEF_MISSING_NETS_BEGIN,
    SPEF_MISSING_NETS_END,
    extract_block,
    parse_spef_missing_nets,
)

BEGIN = "===B==="
END = "===E==="


def test_extract_block_returns_text_between_markers_unstripped():
    assert extract_block(f"pre{BEGIN}\n  a \n{END}post", BEGIN, END) == "\n  a \n"


def test_extract_block_adjacent_markers_is_empty_string_not_none():
    # A present-but-empty block ("nothing to report") is distinct from a
    # missing block ("the script never got that far").
    assert extract_block(f"{BEGIN}{END}", BEGIN, END) == ""


def test_extract_block_missing_end_marker_is_none():
    # A truncated run: the begin marker printed, the end marker never did.
    assert extract_block(f"{BEGIN}\nrow\n", BEGIN, END) is None


def test_extract_block_missing_begin_marker_is_none():
    assert extract_block(f"row\n{END}", BEGIN, END) is None


def test_extract_block_no_markers_is_none():
    assert extract_block("no markers here", BEGIN, END) is None


def test_extract_block_reversed_markers_is_none():
    assert extract_block(f"{END}x{BEGIN}", BEGIN, END) is None


def test_extract_block_repeated_markers_take_first_begin_and_first_end_after_it():
    text = f"{BEGIN}one{END}{BEGIN}two{END}"
    assert extract_block(text, BEGIN, END) == "one"
    # An end marker before the first begin does not close the block.
    assert extract_block(f"{END}{BEGIN}x{END}y{END}", BEGIN, END) == "x"
    # A second begin inside the block is block text, not a restart.
    assert extract_block(f"{BEGIN}a{BEGIN}b{END}", BEGIN, END) == f"a{BEGIN}b"


def _spef_block(body: str) -> str:
    return f"noise\n{SPEF_MISSING_NETS_BEGIN}\n{body}{SPEF_MISSING_NETS_END}\nmore"


def test_parse_spef_missing_nets_strips_lines_and_drops_blanks():
    stdout = _spef_block("  a[10]\n\n\tu_sub/net  \n")
    assert parse_spef_missing_nets(stdout) == ["a[10]", "u_sub/net"]


def test_parse_spef_missing_nets_empty_block_is_empty_list():
    assert parse_spef_missing_nets(_spef_block("")) == []


def test_parse_spef_missing_nets_missing_end_marker_is_empty_list():
    stdout = f"{SPEF_MISSING_NETS_BEGIN}\nnet_a\nnet_b\n"
    assert parse_spef_missing_nets(stdout) == []


def test_parse_spef_missing_nets_no_markers_is_empty_list():
    assert parse_spef_missing_nets("no markers here") == []


@pytest.mark.parametrize(
    "module",
    [
        post_route_sta,
        post_route_sta_electrical,
        post_route_sta_instance_paths,
        post_route_sta_path_groups,
        post_route_sta_stages,
    ],
    ids=lambda m: m.__name__.rsplit(".", 1)[-1],
)
def test_every_sta_parser_module_uses_the_shared_extractor(module):
    # Guards against re-duplication: a module that grows its own copy of the
    # marker slice would rebind this name to a different function.
    assert module._extract_block is _sta_request_diag.extract_block
