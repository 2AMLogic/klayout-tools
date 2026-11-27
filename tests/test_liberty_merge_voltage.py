"""One-sided ``input_voltage`` / ``output_voltage`` groups in a merge (issue #7809).

A public ASAP7 release splits a corner over five cell-group files; four declare
the optional library-level ``input_voltage(default_VDD_VSS_input)`` and
``output_voltage(default_VDD_VSS_output)`` groups and the later re-released
SIMPLE file declares neither. The merge unions such a group by name instead of
refusing it, as long as same-named groups agree and no member that lacks a
group references its name.

Split from ``test_liberty_merge.py`` (near the 500-line file-size ratchet).
Fixtures are minimal synthetic cell-group ``.lib`` files under ``tmp_path``;
``KLT_CACHE_DIR`` is pointed at a scratch dir so no test touches the real user
cache.
"""

from __future__ import annotations

import pytest

from klayout_tools import liberty_merge

_INPUT = (
    "input_voltage (default_VDD_VSS_input) {\n"
    "    vil : 0.0;\n    vih : 0.77;\n    vimin : 0.0;\n    vimax : 0.77;\n"
    "  }"
)
_OUTPUT = (
    "output_voltage (default_VDD_VSS_output) {\n"
    "    vol : 0.0;\n    voh : 0.77;\n    vomin : 0.0;\n    vomax : 0.77;\n"
    "  }"
)
_BOTH = (_INPUT, _OUTPUT)


def _lib(
    tag: str,
    *,
    groups: tuple[str, ...] = (),
    condition: str | None = None,
    templates: str = "",
    header_extra: str = "",
    pin: str = "",
) -> str:
    """One minimal cell-group ``.lib``; ``groups`` are raw library-level groups."""
    text = (
        f"library (synth_{tag}) {{\n"
        "  technology (cmos);\n"
        "  delay_model : table_lookup;\n"
        '  time_unit : "1ps";\n'
        "  nom_voltage : 0.77;\n"
        "  default_max_transition : 320;\n"
    )
    if condition is not None:
        text += (
            f"  operating_conditions ({condition}) {{\n"
            "    process : 1;\n    temperature : 0;\n    voltage : 0.77;\n"
            "  }\n"
            f"  default_operating_conditions : {condition};\n"
        )
    for group in groups:
        text += f"  {group}\n"
    text += templates + header_extra
    return text + f"  cell (CELL_{tag}) {{\n    area : 1.0;\n{pin}  }}\n}}\n"


def _pin(kind: str, name: str) -> str:
    return f"    pin (A) {{\n      direction : input;\n      {kind} : {name};\n    }}\n"


def _write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def _merge(tmp_path, *libs: str) -> str:
    paths = [_write(tmp_path, f"group{i}.lib", text) for i, text in enumerate(libs)]
    with open(liberty_merge.merge_liberty(paths), encoding="utf-8") as handle:
        return handle.read()


def _refusal(tmp_path, *libs: str) -> str:
    with pytest.raises(liberty_merge.LibertyMergeError) as exc:
        _merge(tmp_path, *libs)
    return str(exc.value)


@pytest.fixture(autouse=True)
def _cache(tmp_path, monkeypatch):
    monkeypatch.setenv(liberty_merge.CACHE_ENV, str(tmp_path / "cache"))


def _assert_each_group_once(text: str) -> None:
    for group in _BOTH:
        assert text.count(group) == 1
    assert text.count("{") == text.count("}")


@pytest.mark.parametrize(
    "with_groups_first", [True, False], ids=["groups-first", "groups-second"]
)
def test_one_sided_groups_merge_in_both_orders(tmp_path, with_groups_first):
    libs = [_lib("A", groups=_BOTH), _lib("B")]
    text = _merge(tmp_path, *(libs if with_groups_first else libs[::-1]))
    _assert_each_group_once(text)
    first, second = ("A", "B") if with_groups_first else ("B", "A")
    assert text.index(f"cell (CELL_{first})") < text.index(f"cell (CELL_{second})")


def test_unioned_groups_land_in_the_header_before_the_first_cell(tmp_path):
    text = _merge(tmp_path, _lib("A"), _lib("B", groups=_BOTH))
    assert text.index(_INPUT) < text.index(_OUTPUT) < text.index("cell (CELL_A)")


@pytest.mark.parametrize(
    "pattern",
    ["GGN", "NGG", "GNG", "GNN", "NNG", "NGN"],
)
def test_three_members_merge_with_cells_in_listed_order(tmp_path, pattern):
    libs = [
        _lib(str(i), groups=_BOTH if flag == "G" else ())
        for i, flag in enumerate(pattern)
    ]
    text = _merge(tmp_path, *libs)
    _assert_each_group_once(text)
    positions = [text.index(f"cell (CELL_{i})") for i in range(3)]
    assert positions == sorted(positions)


def test_only_one_group_kind_missing_merges(tmp_path):
    text = _merge(
        tmp_path,
        _lib("A", groups=(_INPUT,)),
        _lib("B", groups=(_OUTPUT,)),
        _lib("C"),
    )
    _assert_each_group_once(text)


def test_same_name_group_with_differing_body_refuses_naming_the_field(tmp_path):
    other = _INPUT.replace("vih : 0.77;", "vih : 0.7;")
    msg = _refusal(tmp_path, _lib("A", groups=(_INPUT,)), _lib("B", groups=(other,)))
    assert "'input_voltage(default_VDD_VSS_input)' differs" in msg
    assert "group0.lib" in msg and "group1.lib" in msg


def test_differing_bodies_between_later_members_refuse(tmp_path):
    other = _OUTPUT.replace("voh : 0.77;", "voh : 0.7;")
    msg = _refusal(
        tmp_path,
        _lib("A"),
        _lib("B", groups=(_OUTPUT,)),
        _lib("C", groups=(other,)),
    )
    assert "'output_voltage(default_VDD_VSS_output)' differs" in msg
    assert "group1.lib" in msg and "group2.lib" in msg


def test_group_with_different_name_is_not_unioned(tmp_path):
    renamed = _INPUT.replace("default_VDD_VSS_input", "other_input")
    msg = _refusal(tmp_path, _lib("A", groups=(_INPUT,)), _lib("B", groups=(renamed,)))
    assert "input_voltage(default_VDD_VSS_input)" in msg
    assert "different name" in msg


def test_different_names_between_later_members_refuse(tmp_path):
    renamed = _INPUT.replace("default_VDD_VSS_input", "other_input")
    msg = _refusal(
        tmp_path,
        _lib("A"),
        _lib("B", groups=(_INPUT,)),
        _lib("C", groups=(renamed,)),
    )
    assert "different name" in msg
    assert "group1.lib" in msg and "group2.lib" in msg


@pytest.mark.parametrize("quoted", [False, True], ids=["bare", "quoted"])
@pytest.mark.parametrize(
    "lacking_first", [False, True], ids=["lacking-second", "lacking-first"]
)
def test_member_without_the_group_referencing_it_refuses(
    tmp_path, quoted, lacking_first
):
    name = '"default_VDD_VSS_input"' if quoted else "default_VDD_VSS_input"
    with_group = _lib("A", groups=_BOTH)
    lacking = _lib("B", pin=_pin("input_voltage", name))
    libs = [lacking, with_group] if lacking_first else [with_group, lacking]
    msg = _refusal(tmp_path, *libs)
    lacking_file = "group0.lib" if lacking_first else "group1.lib"
    assert lacking_file in msg
    assert "input_voltage" in msg and "default_VDD_VSS_input" in msg


def test_reference_by_a_member_between_the_declaring_ones_refuses(tmp_path):
    msg = _refusal(
        tmp_path,
        _lib("A"),
        _lib("B", pin=_pin("output_voltage", "default_VDD_VSS_output")),
        _lib("C", groups=_BOTH),
    )
    assert "group1.lib" in msg and "default_VDD_VSS_output" in msg


def test_reference_to_an_undefined_name_is_left_alone(tmp_path):
    lacking = _lib("B", pin=_pin("input_voltage", "somewhere_else"))
    text = _merge(tmp_path, _lib("A", groups=_BOTH), lacking)
    _assert_each_group_once(text)
    assert "somewhere_else" in text


def test_members_that_declare_the_group_may_reference_it(tmp_path):
    pin = _pin("input_voltage", "default_VDD_VSS_input")
    text = _merge(
        tmp_path, _lib("A", groups=_BOTH, pin=pin), _lib("B", groups=_BOTH, pin=pin)
    )
    _assert_each_group_once(text)


def test_combined_with_condition_rename_and_template_union(tmp_path):
    """The real ASAP7 set needs the #6940 rename and the template union too."""
    tmpl_a = "  lu_table_template (tmpl_a) {\n    variable_1 : x;\n  }\n"
    tmpl_b = "  lu_table_template (tmpl_b) {\n    variable_1 : y;\n  }\n"
    text = _merge(
        tmp_path,
        _lib("A", groups=_BOTH, condition="PVT_0P77V_0C", templates=tmpl_a),
        _lib("B", condition="FF_0.77_0", templates=tmpl_b),
    )
    _assert_each_group_once(text)
    assert "operating_conditions (PVT_0P77V_0C)" in text
    assert "FF_0.77_0" not in text
    assert "lu_table_template (tmpl_a)" in text and "lu_table_template (tmpl_b)" in text


def test_combined_rename_when_the_renamed_member_declares_the_groups(tmp_path):
    text = _merge(
        tmp_path,
        _lib("A", condition="FF_0.77_0"),
        _lib("B", groups=_BOTH, condition="PVT_0P77V_0C"),
    )
    _assert_each_group_once(text)
    assert "operating_conditions (FF_0.77_0)" in text
    assert "PVT_0P77V_0C" not in text


def test_unrelated_missing_field_still_refuses_alongside_the_groups(tmp_path):
    without = _lib("B").replace("  nom_voltage : 0.77;\n", "")
    msg = _refusal(tmp_path, _lib("A", groups=_BOTH), without)
    assert "'nom_voltage' present in" in msg and "missing in" in msg
    assert "input_voltage" not in msg and "output_voltage" not in msg


def test_other_header_conflicts_are_still_reported_in_one_error(tmp_path):
    other = _lib("B").replace(
        "default_max_transition : 320;", "default_max_transition : 100;"
    )
    msg = _refusal(tmp_path, _lib("A", groups=_BOTH), other)
    assert "'default_max_transition' differs" in msg
    assert "input_voltage" not in msg


def test_group_declared_twice_in_one_header_is_carried_verbatim(tmp_path):
    dup = _lib("B", groups=(_INPUT, _INPUT))
    text = _merge(tmp_path, _lib("A"), dup)
    assert text.count(_INPUT) == 2
    text = _merge(tmp_path, dup, _lib("C"))
    assert text.count(_INPUT) == 2


def test_duplicate_declarations_must_match_across_members(tmp_path):
    msg = _refusal(
        tmp_path,
        _lib("A"),
        _lib("B", groups=(_INPUT, _INPUT)),
        _lib("C", groups=(_INPUT,)),
    )
    assert "occurrence 2" in msg


def test_complex_attribute_of_the_same_name_is_not_a_group(tmp_path):
    """Only ``name (arg) { ... }`` groups are unioned; anything else fails closed."""
    msg = _refusal(tmp_path, _lib("A", groups=("input_voltage (x);",)), _lib("B"))
    assert "'input_voltage(x)' present in" in msg and "missing in" in msg


def test_merge_is_cached_by_content(tmp_path):
    paths = [
        _write(tmp_path, "a.lib", _lib("A", groups=_BOTH)),
        _write(tmp_path, "b.lib", _lib("B")),
    ]
    first = liberty_merge.merge_liberty(paths)
    assert liberty_merge.merge_liberty(paths) == first
