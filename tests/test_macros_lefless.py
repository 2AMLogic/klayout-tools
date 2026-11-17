"""Abstract-less (LEF-less) macro validation -- `klt synthesize` only
(issue #5612). Split out of `test_macros.py` to keep that governed file
under its per-file size ceiling; the identity code under test lives in
`klayout_tools.macro_blackbox`, the shared validator in
`klayout_tools.macros.validate_macros` (opt-in via `allow_missing_lef`).
Reuses `test_macros`' error stand-in and LEF fixture so both files assert
the same shapes."""

from __future__ import annotations

import pytest

from klayout_tools import macros as macro_spec
from test_macros import _Err, _write_macro_lef

# --------------------------------------------------------------------------- #
# Abstract-less (LEF-less) macros -- klt synthesize only (issue #5612)
# --------------------------------------------------------------------------- #


def _lefless(tmp_path, text, **entry):
    (tmp_path / "bb.v").write_text(text, encoding="utf-8")
    return [{"verilog_blackbox": "bb.v", **entry}]


def _validate_lefless(request, tmp_path):
    return macro_spec.validate_macros(
        request, str(tmp_path), error_cls=_Err, allow_missing_lef=True
    )


def test_lefless_macro_takes_identity_from_the_blackbox_module(tmp_path):
    request = _lefless(tmp_path, "(* blackbox *)\nmodule foo (D);\nendmodule\n")
    (macro,) = _validate_lefless(request, tmp_path)
    assert macro["cell"] == "foo"
    assert macro["lef"] is None
    assert macro["pins"] == []
    assert macro["width_um"] is None


def test_lefless_macro_honours_an_explicit_matching_cell(tmp_path):
    request = _lefless(
        tmp_path,
        "module helper (D);\nendmodule\nmodule foo (D);\nendmodule\n",
        cell="foo",
    )
    assert _validate_lefless(request, tmp_path)[0]["cell"] == "foo"


def test_lefless_macro_needs_a_blackbox(tmp_path):
    with pytest.raises(_Err, match=r"macros\[0\].*verilog_blackbox is required"):
        _validate_lefless([{}], tmp_path)


def test_lefless_multi_module_blackbox_without_cell_lists_modules(tmp_path):
    request = _lefless(tmp_path, "module a ();\nendmodule\nmodule b ();\nendmodule\n")
    with pytest.raises(_Err, match=r"\['a', 'b'\]"):
        _validate_lefless(request, tmp_path)


def test_lefless_cell_must_be_a_declared_module(tmp_path):
    request = _lefless(tmp_path, "module a ();\nendmodule\n", cell="zzz")
    with pytest.raises(_Err, match=r"'zzz'.*\['a'\]"):
        _validate_lefless(request, tmp_path)


@pytest.mark.parametrize("lef", ["", "missing.lef"])
def test_lefless_relaxation_does_not_cover_an_unusable_lef(tmp_path, lef):
    request = _lefless(tmp_path, "module a ();\nendmodule\n", lef=lef)
    with pytest.raises(_Err, match=r"\.lef"):
        _validate_lefless(request, tmp_path)


def test_lefless_duplicate_cell_with_lef_backed_entry_rejected(tmp_path):
    _write_macro_lef(tmp_path / "m.lef")
    request = _lefless(tmp_path, "module sram_8x8 ();\nendmodule\n")
    request.append({"lef": "m.lef"})
    with pytest.raises(_Err, match="must be unique"):
        _validate_lefless(request, tmp_path)


def test_lefless_entry_is_rejected_by_default_validator(tmp_path):
    """The relaxation is opt-in: `klt sta` / `klt place-and-route` keep it."""
    request = _lefless(tmp_path, "module a ();\nendmodule\n")
    with pytest.raises(_Err, match=r"macros\[0\]\.lef is required"):
        macro_spec.validate_macros(request, str(tmp_path), error_cls=_Err)


def test_sta_and_place_and_route_still_reject_a_lefless_entry(tmp_path):
    from klayout_tools import post_route_sta
    from klayout_tools.place_and_route_request import (
        _validate_macros as pnr_validate_macros,
    )

    request = _lefless(tmp_path, "module a ();\nendmodule\n")
    with pytest.raises(Exception, match=r"macros\[0\]\.lef is required"):
        post_route_sta._validate_macros(request, str(tmp_path))
    with pytest.raises(Exception, match=r"macros\[0\]\.lef is required"):
        pnr_validate_macros([{**request[0], "instance": "u_a"}], str(tmp_path))
