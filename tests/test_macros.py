"""Tests for `klayout_tools.macros` -- the shared `request.macros`
hard-macro declaration `klt synthesize`, `klt sta` and `klt
place-and-route` all accept (issue #2635).

This module owns the *shape* tests (accept/reject, path resolution,
per-corner liberty matching, blackbox generation, engine-error hints). The
per-verb wiring -- which Tcl/`.ys` lines each verb emits, and what each
response echoes -- is tested in `tests/test_synthesize.py`,
`tests/test_post_route_sta.py` and `tests/test_place_and_route.py`
respectively, against the same fixtures.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from klayout_tools import macros as macro_spec


class _Err(Exception):
    """Stands in for each verb's own `*Error` class -- `macros.py` is
    deliberately error-class-agnostic (every entry point takes `error_cls`)
    so one validator can serve three verbs without importing any of them."""


def _write_macro_lef(
    path: Path,
    macro_name: str = "sram_8x8",
    *,
    size: str = "SIZE 20.0 BY 10.0 ;",
    pins: str = (
        "  PIN D\n"
        "    DIRECTION INPUT ;\n"
        "    USE SIGNAL ;\n"
        "    PORT\n"
        "      LAYER met2 ;\n"
        "        RECT 1.0 1.0 1.5 1.5 ;\n"
        "    END\n"
        "  END D\n"
        "  PIN Q\n"
        "    DIRECTION OUTPUT ;\n"
        "    USE SIGNAL ;\n"
        "    PORT\n"
        "      LAYER met2 ;\n"
        "        RECT 2.0 1.0 2.5 1.5 ;\n"
        "    END\n"
        "  END Q\n"
    ),
) -> Path:
    path.write_text(
        "VERSION 5.7 ;\n"
        f"MACRO {macro_name}\n"
        "  CLASS BLOCK ;\n"
        f"  {size}\n"
        f"{pins}"
        f"END {macro_name}\n"
        "END LIBRARY\n",
        encoding="utf-8",
    )
    return path


def _write_macro_lib(path: Path, cell: str = "sram_8x8") -> Path:
    path.write_text(
        f"library ({cell}) {{\n"
        f"  cell ({cell}) {{\n"
        "    area : 400.0 ;\n"
        "    pin (D) { direction : input; }\n"
        "    pin (Q) { direction : output; }\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    return path


# --------------------------------------------------------------------------- #
# `validate_macros` -- acceptance
# --------------------------------------------------------------------------- #


def test_macros_field_absent_validates_to_empty_list(tmp_path):
    assert macro_spec.validate_macros(None, str(tmp_path), error_cls=_Err) == []


def test_minimal_macro_derives_cell_name_from_its_lef(tmp_path):
    _write_macro_lef(tmp_path / "m.lef")
    macros = macro_spec.validate_macros(
        [{"lef": "m.lef"}], str(tmp_path), error_cls=_Err
    )
    assert len(macros) == 1
    assert macros[0]["cell"] == "sram_8x8"
    assert macros[0]["lef"] == str(tmp_path / "m.lef")
    assert macros[0]["lib"] is None
    assert macros[0]["gds"] is None
    assert macros[0]["verilog_blackbox"] is None
    assert macros[0]["width_um"] == 20.0
    assert macros[0]["height_um"] == 10.0


def test_macro_paths_resolve_relative_to_the_request_directory(tmp_path):
    (tmp_path / "vendor").mkdir()
    _write_macro_lef(tmp_path / "vendor" / "m.lef")
    _write_macro_lib(tmp_path / "vendor" / "m_tt.lib")
    (tmp_path / "vendor" / "m.gds").write_bytes(b"\x00")
    macros = macro_spec.validate_macros(
        [
            {
                "lef": "vendor/m.lef",
                "lib": {"tt_025C_1v80": "vendor/m_tt.lib"},
                "gds": "vendor/m.gds",
            }
        ],
        str(tmp_path),
        error_cls=_Err,
    )
    assert macros[0]["lef"] == str(tmp_path / "vendor" / "m.lef")
    assert macros[0]["lib"] == {"tt_025C_1v80": str(tmp_path / "vendor" / "m_tt.lib")}
    assert macros[0]["gds"] == str(tmp_path / "vendor" / "m.gds")


def test_explicit_cell_matching_the_lef_is_accepted(tmp_path):
    _write_macro_lef(tmp_path / "m.lef")
    macros = macro_spec.validate_macros(
        [{"cell": "sram_8x8", "lef": "m.lef"}], str(tmp_path), error_cls=_Err
    )
    assert macros[0]["cell"] == "sram_8x8"


def test_liberty_alias_is_accepted_for_lib(tmp_path):
    """`liberty` is an exact alias of `lib` -- two documented spellings of
    one field, so a caller mirroring `klt place-and-route`'s surrounding
    vocabulary is not a request error (issue #2635 AC4 names `liberty`,
    AC1 names `lib`)."""
    _write_macro_lef(tmp_path / "m.lef")
    _write_macro_lib(tmp_path / "m_tt.lib")
    macros = macro_spec.validate_macros(
        [{"lef": "m.lef", "liberty": {"tt_025C_1v80": "m_tt.lib"}}],
        str(tmp_path),
        error_cls=_Err,
    )
    assert macros[0]["lib"] == {"tt_025C_1v80": str(tmp_path / "m_tt.lib")}


# --------------------------------------------------------------------------- #
# `validate_macros` -- rejection
# --------------------------------------------------------------------------- #


def test_macros_field_must_be_a_list(tmp_path):
    with pytest.raises(_Err, match="request.macros must be a list"):
        macro_spec.validate_macros({"not": "a list"}, str(tmp_path), error_cls=_Err)


def test_macro_entry_must_be_an_object(tmp_path):
    with pytest.raises(_Err, match=r"request\.macros\[0\] must be an object"):
        macro_spec.validate_macros(["nope"], str(tmp_path), error_cls=_Err)


def test_macro_lef_is_required(tmp_path):
    with pytest.raises(_Err, match=r"request\.macros\[0\]\.lef is required"):
        macro_spec.validate_macros([{}], str(tmp_path), error_cls=_Err)


def test_macro_lef_must_exist(tmp_path):
    with pytest.raises(_Err, match="lef not found: missing.lef"):
        macro_spec.validate_macros(
            [{"lef": "missing.lef"}], str(tmp_path), error_cls=_Err
        )


def test_macro_lef_must_declare_exactly_one_macro(tmp_path):
    (tmp_path / "empty.lef").write_text("VERSION 5.7 ;\nEND LIBRARY\n")
    with pytest.raises(_Err, match="must declare exactly one MACRO"):
        macro_spec.validate_macros(
            [{"lef": "empty.lef"}], str(tmp_path), error_cls=_Err
        )


def test_macro_cell_disagreeing_with_its_lef_is_rejected(tmp_path):
    _write_macro_lef(tmp_path / "m.lef")
    with pytest.raises(_Err, match="does not match the MACRO name"):
        macro_spec.validate_macros(
            [{"cell": "sram_16x8", "lef": "m.lef"}], str(tmp_path), error_cls=_Err
        )


def test_macro_lib_must_be_an_object(tmp_path):
    _write_macro_lef(tmp_path / "m.lef")
    with pytest.raises(_Err, match="must be an object mapping a corner name"):
        macro_spec.validate_macros(
            [{"lef": "m.lef", "lib": "m_tt.lib"}], str(tmp_path), error_cls=_Err
        )


def test_macro_lib_must_not_be_empty(tmp_path):
    _write_macro_lef(tmp_path / "m.lef")
    with pytest.raises(_Err, match="must not be empty when given"):
        macro_spec.validate_macros(
            [{"lef": "m.lef", "lib": {}}], str(tmp_path), error_cls=_Err
        )


def test_macro_lib_file_must_exist(tmp_path):
    _write_macro_lef(tmp_path / "m.lef")
    with pytest.raises(_Err, match=r"lib\['tt_025C_1v80'\] not found"):
        macro_spec.validate_macros(
            [{"lef": "m.lef", "lib": {"tt_025C_1v80": "nope.lib"}}],
            str(tmp_path),
            error_cls=_Err,
        )


def test_lib_and_liberty_together_are_rejected(tmp_path):
    _write_macro_lef(tmp_path / "m.lef")
    _write_macro_lib(tmp_path / "m_tt.lib")
    with pytest.raises(_Err, match="two spellings of the same per-corner"):
        macro_spec.validate_macros(
            [
                {
                    "lef": "m.lef",
                    "lib": {"tt_025C_1v80": "m_tt.lib"},
                    "liberty": {"tt_025C_1v80": "m_tt.lib"},
                }
            ],
            str(tmp_path),
            error_cls=_Err,
        )


def test_macro_gds_must_exist_when_given(tmp_path):
    _write_macro_lef(tmp_path / "m.lef")
    with pytest.raises(_Err, match="gds not found: missing.gds"):
        macro_spec.validate_macros(
            [{"lef": "m.lef", "gds": "missing.gds"}], str(tmp_path), error_cls=_Err
        )


def test_macro_verilog_blackbox_must_exist_when_given(tmp_path):
    _write_macro_lef(tmp_path / "m.lef")
    with pytest.raises(_Err, match="verilog_blackbox not found"):
        macro_spec.validate_macros(
            [{"lef": "m.lef", "verilog_blackbox": "missing.v"}],
            str(tmp_path),
            error_cls=_Err,
        )


def test_duplicate_macro_cell_names_rejected(tmp_path):
    _write_macro_lef(tmp_path / "m.lef")
    with pytest.raises(_Err, match=r"request\.macros\[\]\.cell values must be unique"):
        macro_spec.validate_macros(
            [{"lef": "m.lef"}, {"lef": "m.lef"}], str(tmp_path), error_cls=_Err
        )


# --------------------------------------------------------------------------- #
# Per-corner liberty resolution
# --------------------------------------------------------------------------- #


def test_macro_lib_for_corner_exact_match(tmp_path):
    _write_macro_lef(tmp_path / "m.lef")
    _write_macro_lib(tmp_path / "m_tt.lib")
    _write_macro_lib(tmp_path / "m_ss.lib")
    macro = macro_spec.validate_macros(
        [
            {
                "lef": "m.lef",
                "lib": {"tt_025C_1v80": "m_tt.lib", "ss_100C_1v60": "m_ss.lib"},
            }
        ],
        str(tmp_path),
        error_cls=_Err,
    )[0]
    assert macro_spec.macro_lib_for_corner(
        macro, "ss_100C_1v60", error_cls=_Err
    ) == str(tmp_path / "m_ss.lib")


def test_macro_lib_for_corner_falls_back_to_the_default_key(tmp_path):
    _write_macro_lef(tmp_path / "m.lef")
    _write_macro_lib(tmp_path / "m_any.lib")
    macro = macro_spec.validate_macros(
        [{"lef": "m.lef", "lib": {"default": "m_any.lib"}}],
        str(tmp_path),
        error_cls=_Err,
    )[0]
    assert macro_spec.macro_lib_for_corner(
        macro, "any_corner_at_all", error_cls=_Err
    ) == str(tmp_path / "m_any.lib")


def test_macro_with_no_lib_at_all_resolves_to_none(tmp_path):
    """A LEF-only macro is legal: the LEF alone is what fixes `ORD-2013`,
    and the engine then treats the instance as an untimed blackbox."""
    _write_macro_lef(tmp_path / "m.lef")
    macro = macro_spec.validate_macros(
        [{"lef": "m.lef"}], str(tmp_path), error_cls=_Err
    )[0]
    assert (
        macro_spec.macro_lib_for_corner(macro, "tt_025C_1v80", error_cls=_Err) is None
    )


def test_macro_lib_with_no_matching_corner_is_a_named_error(tmp_path):
    """Issue #2635's explicit edge case. A macro that *did* declare a
    liberty map must never be silently timed at some other corner's
    liberty -- the error names the macro, the corner being run, and the
    keys that are available."""
    _write_macro_lef(tmp_path / "m.lef")
    _write_macro_lib(tmp_path / "m_tt.lib")
    macro = macro_spec.validate_macros(
        [{"lef": "m.lef", "lib": {"tt_025C_1v80": "m_tt.lib"}}],
        str(tmp_path),
        error_cls=_Err,
    )[0]
    with pytest.raises(_Err) as excinfo:
        macro_spec.macro_lib_for_corner(macro, "ss_100C_1v60", error_cls=_Err)
    message = str(excinfo.value)
    assert "sram_8x8" in message
    assert "tt_025C_1v80" in message
    assert "ss_100C_1v60" in message
    assert "default" in message


# --------------------------------------------------------------------------- #
# Generated blackbox Verilog
# --------------------------------------------------------------------------- #


def test_blackbox_verilog_text_from_lef_pin_directions(tmp_path):
    _write_macro_lef(tmp_path / "m.lef")
    macro = macro_spec.validate_macros(
        [{"lef": "m.lef"}], str(tmp_path), error_cls=_Err
    )[0]
    text = macro_spec.blackbox_verilog_text(macro)
    assert "(* blackbox *)" in text
    assert "module sram_8x8 (D, Q);" in text
    assert "  input D;" in text
    assert "  output Q;" in text
    assert text.rstrip().endswith("endmodule")


def test_blackbox_verilog_text_maps_power_pins_to_inout(tmp_path):
    _write_macro_lef(
        tmp_path / "m.lef",
        pins=(
            "  PIN VPWR\n"
            "    DIRECTION INOUT ;\n"
            "    USE POWER ;\n"
            "  END VPWR\n"
            "  PIN NC\n"
            "    USE SIGNAL ;\n"
            "  END NC\n"
        ),
    )
    macro = macro_spec.validate_macros(
        [{"lef": "m.lef"}], str(tmp_path), error_cls=_Err
    )[0]
    text = macro_spec.blackbox_verilog_text(macro)
    assert "  inout VPWR;" in text
    # A pin whose LEF states no DIRECTION at all degrades to `inout` --
    # never a guessed `input`/`output` that could make Yosys reject a
    # connection it would otherwise accept.
    assert "  inout NC;" in text


def test_blackbox_verilog_text_escapes_a_non_identifier_pin_name(tmp_path):
    _write_macro_lef(
        tmp_path / "m.lef",
        pins=("  PIN a-b\n    DIRECTION INPUT ;\n    USE SIGNAL ;\n  END a-b\n"),
    )
    macro = macro_spec.validate_macros(
        [{"lef": "m.lef"}], str(tmp_path), error_cls=_Err
    )[0]
    text = macro_spec.blackbox_verilog_text(macro)
    assert "module sram_8x8 (\\a-b );" in text
    assert "  input \\a-b ;" in text


def _bus_pin(name, direction="INPUT"):
    return (
        f"  PIN {name}\n    DIRECTION {direction} ;\n    USE SIGNAL ;\n  END {name}\n"
    )


def _bus_macro(tmp_path, pins):
    _write_macro_lef(tmp_path / "m.lef", pins="".join(pins))
    return macro_spec.validate_macros(
        [{"lef": "m.lef"}], str(tmp_path), error_cls=_Err
    )[0]


def test_blackbox_verilog_text_groups_bus_bits_into_vector_ports(tmp_path):
    macro = _bus_macro(
        tmp_path,
        [
            _bus_pin("A[0]"),
            _bus_pin("Q[1]", "OUTPUT"),
            _bus_pin("A[1]"),
            _bus_pin("Q[0]", "OUTPUT"),
            _bus_pin("CLK"),
        ],
    )
    text = macro_spec.blackbox_verilog_text(macro)
    assert "module sram_8x8 (A, CLK, Q);" in text
    assert "  input [1:0] A;" in text
    assert "  output [1:0] Q;" in text
    assert "  input CLK;" in text
    assert "\\A[" not in text


@pytest.mark.parametrize(
    "pins, fragment",
    [
        ([_bus_pin("A[0]"), _bus_pin("A[2]")], "non-contiguous"),
        ([_bus_pin("A[0]"), _bus_pin("A[1]", "OUTPUT")], "mixes pin directions"),
        ([_bus_pin("A"), _bus_pin("A[0]")], "both a scalar pin"),
    ],
)
def test_blackbox_verilog_text_rejects_unrepresentable_buses(tmp_path, pins, fragment):
    macro = _bus_macro(tmp_path, pins)
    with pytest.raises(_Err, match=fragment):
        macro_spec.blackbox_verilog_text(macro, error_cls=_Err)


@pytest.mark.skipif(shutil.which("yosys") is None, reason="yosys is not installed")
def test_blackbox_verilog_text_elaborates_in_yosys_with_vector_ports(tmp_path):
    macro = _bus_macro(
        tmp_path,
        [_bus_pin(f"A[{i}]") for i in range(4)]
        + [_bus_pin(f"Q[{i}]", "OUTPUT") for i in range(8)],
    )
    (tmp_path / "stub.v").write_text(macro_spec.blackbox_verilog_text(macro))
    (tmp_path / "top.v").write_text(
        "module top(input [3:0] address, output [7:0] data);\n"
        "  sram_8x8 u_sram (.A(address), .Q(data));\n"
        "endmodule\n"
    )
    result = subprocess.run(
        [
            "yosys",
            "-q",
            "-p",
            "read_verilog -lib stub.v; read_verilog top.v; hierarchy -check -top top",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


# --------------------------------------------------------------------------- #
# Engine-error hints
# --------------------------------------------------------------------------- #


def test_missing_lef_master_hint_recognizes_ord_2013():
    """The exact OpenROAD 26Q3 diagnostic, captured live from a sky130A
    session whose netlist instantiated a macro with no `read_lef`."""
    hint = macro_spec.missing_lef_master_hint(
        "[ERROR ORD-2013] instance u_sram LEF master sram_8x8 not found."
    )
    assert hint is not None
    assert "u_sram" in hint
    assert "sram_8x8" in hint
    assert "request.macros" in hint


def test_missing_lef_master_hint_ignores_unrelated_failures():
    assert macro_spec.missing_lef_master_hint("[ERROR DRT-0305] something else") is None


def test_undeclared_module_hint_recognizes_the_yosys_error():
    """The exact Yosys 0.69 diagnostic, captured live from a `hierarchy
    -check -top` run whose sources instantiated an unread module."""
    hint = macro_spec.undeclared_module_hint(
        "ERROR: Module `\\sram_8x8' referenced in module `\\top' in cell "
        "`\\u_sram' is not part of the design."
    )
    assert hint is not None
    assert "sram_8x8" in hint
    assert "u_sram" in hint
    assert "request.sources" in hint
    assert "request.macros" in hint


def test_undeclared_module_hint_ignores_unrelated_failures():
    assert macro_spec.undeclared_module_hint("ERROR: syntax error") is None
