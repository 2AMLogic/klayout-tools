"""List-merge behaviour of ``liberty_merge`` (issue #5625).

Headers must agree field by field: free-text metadata (``date``,
``revision``, the ``comment`` attribute) is ignored, every other header
field conflicts by name with an error naming the field and both values.
Fixtures are minimal cell-group ``.lib`` files under ``tmp_path``;
``KLT_CACHE_DIR`` is pointed at a scratch dir so no test touches the
real user cache.
"""

from __future__ import annotations

import os

import pytest

from klayout_tools import liberty_merge


def _lib(
    tag: str,
    *,
    date: str = "Fri Nov 27 23:17:09 2020",
    revision: str = '"1.0"',
    comment: str = '""',
    max_trans: str = "4000",
    header_extra: str = "",
    body_extra: str = "",
    crlf: bool = False,
) -> str:
    """One minimal cell-group ``.lib``: header attributes, optional extra
    header statements, then one cell."""
    text = (
        f"library (lib_{tag}) {{\n"
        f"  comment : {comment};\n"
        f'  date : "$Date: {date} $";\n'
        f"  revision : {revision};\n"
        f"  delay_model : table_lookup;\n"
        f"  default_max_transition : {max_trans};\n"
        f"{header_extra}"
        f"  cell (X_{tag}) {{\n    area : 1;\n  }}\n"
        f"{body_extra}"
        f"}}\n"
    )
    return text.replace("\n", "\r\n") if crlf else text


def _write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


@pytest.fixture(autouse=True)
def _cache(tmp_path, monkeypatch):
    monkeypatch.setenv(liberty_merge.CACHE_ENV, str(tmp_path / "cache"))


def test_date_only_difference_merges(tmp_path):
    a = _write(tmp_path, "groupA.lib", _lib("A"))
    b = _write(tmp_path, "groupB.lib", _lib("B", date="Mon Dec  7 13:53:54 2020"))
    text = _read(liberty_merge.merge_liberty([a, b]))
    assert "cell (X_A)" in text and "cell (X_B)" in text


def test_revision_only_difference_merges(tmp_path):
    a = _write(tmp_path, "groupA.lib", _lib("A"))
    b = _write(tmp_path, "groupB.lib", _lib("B", revision='"1.1"'))
    text = _read(liberty_merge.merge_liberty([a, b]))
    assert "cell (X_B)" in text


def test_comment_attribute_only_difference_merges(tmp_path):
    a = _write(tmp_path, "groupA.lib", _lib("A"))
    b = _write(tmp_path, "groupB.lib", _lib("B", comment='"vendor note"'))
    text = _read(liberty_merge.merge_liberty([a, b]))
    assert "cell (X_B)" in text


def test_all_ignored_attributes_differ_and_merge(tmp_path):
    a = _write(tmp_path, "groupA.lib", _lib("A"))
    b = _write(
        tmp_path,
        "groupB.lib",
        _lib("B", date="Mon Dec  7 13:53:54 2020", revision='"2.0"', comment='"note"'),
    )
    text = _read(liberty_merge.merge_liberty([a, b]))
    assert "cell (X_A)" in text and "cell (X_B)" in text


def test_merged_header_is_first_file_verbatim(tmp_path):
    a = _write(tmp_path, "groupA.lib", _lib("A"))
    b = _write(tmp_path, "groupB.lib", _lib("B", date="Mon Dec  7 13:53:54 2020"))
    merged = liberty_merge.merge_liberty([a, b])
    header = _read(a).split("cell (X_A)")[0]
    text = _read(merged)
    assert text.startswith(header.rstrip())
    assert "Mon Dec" not in text


def test_max_transition_conflict_names_attribute_and_values(tmp_path):
    a = _write(tmp_path, "groupA.lib", _lib("A", max_trans="320"))
    b = _write(tmp_path, "groupB.lib", _lib("B", max_trans="4000"))
    with pytest.raises(liberty_merge.LibertyMergeError) as exc:
        liberty_merge.merge_liberty([a, b])
    msg = str(exc.value)
    assert "default_max_transition" in msg
    assert "320" in msg and "4000" in msg
    assert "groupA.lib" in msg and "groupB.lib" in msg
    assert "Liberty headers differ" not in msg


def test_units_conflict_names_group(tmp_path):
    units = '  units {{\n    time_unit : "{}";\n    voltage_unit : "1V";\n  }}\n'
    a = _write(tmp_path, "groupA.lib", _lib("A", header_extra=units.format("1ns")))
    b = _write(tmp_path, "groupB.lib", _lib("B", header_extra=units.format("1ps")))
    with pytest.raises(liberty_merge.LibertyMergeError) as exc:
        liberty_merge.merge_liberty([a, b])
    assert "'units'" in str(exc.value)


def test_operating_conditions_conflict_names_group(tmp_path):
    oc = (
        "  operating_conditions (TT) {{\n"
        "    process : {};\n"
        "    temperature : 25;\n"
        "    voltage : 0.7;\n"
        "  }}\n"
    )
    a = _write(tmp_path, "groupA.lib", _lib("A", header_extra=oc.format(1)))
    b = _write(tmp_path, "groupB.lib", _lib("B", header_extra=oc.format(1.1)))
    with pytest.raises(liberty_merge.LibertyMergeError) as exc:
        liberty_merge.merge_liberty([a, b])
    assert "operating_conditions(TT)" in str(exc.value)


def test_attribute_missing_in_second_file_is_conflict(tmp_path):
    a = _write(tmp_path, "groupA.lib", _lib("A"))
    b_text = _lib("B").replace("  delay_model : table_lookup;\n", "")
    b = _write(tmp_path, "groupB.lib", b_text)
    with pytest.raises(liberty_merge.LibertyMergeError) as exc:
        liberty_merge.merge_liberty([a, b])
    msg = str(exc.value)
    assert "delay_model" in msg and "missing" in msg and "groupB.lib" in msg


def test_attribute_missing_in_first_file_is_conflict(tmp_path):
    a_text = _lib("A").replace("  delay_model : table_lookup;\n", "")
    a = _write(tmp_path, "groupA.lib", a_text)
    b = _write(tmp_path, "groupB.lib", _lib("B"))
    with pytest.raises(liberty_merge.LibertyMergeError) as exc:
        liberty_merge.merge_liberty([a, b])
    msg = str(exc.value)
    assert "delay_model" in msg and "missing" in msg and "groupA.lib" in msg


def test_unrecognised_residue_still_compared(tmp_path):
    a_text = _lib("A").replace(
        "  delay_model : table_lookup;\n",
        "  delay_model : table_lookup;\n  stray 42;\n",
    )
    b_text = _lib("B").replace(
        "  delay_model : table_lookup;\n",
        "  delay_model : table_lookup;\n  stray 43;\n",
    )
    a = _write(tmp_path, "groupA.lib", a_text)
    b = _write(tmp_path, "groupB.lib", b_text)
    with pytest.raises(liberty_merge.LibertyMergeError) as exc:
        liberty_merge.merge_liberty([a, b])
    assert "Liberty headers differ" not in str(exc.value)


def test_templates_union_by_name(tmp_path):
    tmpl = (
        "  lu_table_template ({}) {{\n"
        '    index_1 ("1, 2");\n'
        "    variable_1 : input_net_transition;\n"
        "  }}\n"
    )
    a = _write(
        tmp_path,
        "groupA.lib",
        _lib("A", header_extra=tmpl.format("shared") + tmpl.format("only_a")),
    )
    b = _write(
        tmp_path,
        "groupB.lib",
        _lib("B", header_extra=tmpl.format("shared") + tmpl.format("only_b")),
    )
    text = _read(liberty_merge.merge_liberty([a, b]))
    assert "lu_table_template (only_a)" in text
    assert "lu_table_template (only_b)" in text
    assert text.count("lu_table_template (shared)") == 1


def test_same_name_template_conflict(tmp_path):
    tmpl = (
        "  lu_table_template (t) {{\n"
        '    index_1 ("{}");\n'
        "    variable_1 : input_net_transition;\n"
        "  }}\n"
    )
    a = _write(tmp_path, "groupA.lib", _lib("A", header_extra=tmpl.format("1, 2")))
    b = _write(tmp_path, "groupB.lib", _lib("B", header_extra=tmpl.format("1, 3")))
    with pytest.raises(liberty_merge.LibertyMergeError) as exc:
        liberty_merge.merge_liberty([a, b])
    msg = str(exc.value)
    assert "template 't'" in msg and "Liberty headers differ" not in msg


def test_duplicate_cell_raises(tmp_path):
    a = _write(tmp_path, "groupA.lib", _lib("A"))
    b = _write(
        tmp_path,
        "groupB.lib",
        _lib("B", body_extra="  cell (X_A) {\n    area : 9;\n  }\n"),
    )
    with pytest.raises(liberty_merge.LibertyMergeError) as exc:
        liberty_merge.merge_liberty([a, b])
    assert "duplicate cell 'X_A'" in str(exc.value)


def test_one_element_list_passthrough(tmp_path):
    a = _write(tmp_path, "groupA.lib", _lib("A"))
    assert liberty_merge.merge_liberty([a]) == a


def test_cache_hit_returns_identical_path(tmp_path):
    a = _write(tmp_path, "groupA.lib", _lib("A"))
    b = _write(tmp_path, "groupB.lib", _lib("B"))
    first = liberty_merge.merge_liberty([a, b])
    second = liberty_merge.merge_liberty([a, b])
    assert first == second
    assert os.path.isfile(first)


def test_date_string_with_brace_and_escaped_quote_still_ignored(tmp_path):
    a = _write(tmp_path, "groupA.lib", _lib("A", date='weird } \\" one'))
    b = _write(tmp_path, "groupB.lib", _lib("B", date="plain"))
    text = _read(liberty_merge.merge_liberty([a, b]))
    assert "cell (X_B)" in text


def test_crlf_line_endings_merge(tmp_path):
    a = _write(tmp_path, "groupA.lib", _lib("A"))
    b = _write(
        tmp_path,
        "groupB.lib",
        _lib("B", date="Mon Dec  7 13:53:54 2020", crlf=True),
    )
    text = _read(liberty_merge.merge_liberty([a, b]))
    assert "cell (X_B)" in text


def test_whitespace_and_lexical_comment_layout_differences_merge(tmp_path):
    a = _write(tmp_path, "groupA.lib", _lib("A"))
    b_text = (
        "library (lib_B) {\n"
        "  /* vendor licence block */\n"
        '  comment   :   "" ;   // inline note\n'
        '  date : "$Date: Mon Dec  7 13:53:54 2020 $";\n'
        '  revision : "1.0";\n'
        "  delay_model : table_lookup;\n"
        "  /* electrical content identical */\n"
        "  default_max_transition : 4000 ;\n"
        "  cell (X_B) {\n    area : 1;\n  }\n"
        "}\n"
    )
    b = _write(tmp_path, "groupB.lib", b_text)
    text = _read(liberty_merge.merge_liberty([a, b]))
    assert "cell (X_B)" in text


def test_trailing_lexical_comment_after_last_attr_merges(tmp_path):
    a = _write(tmp_path, "groupA.lib", _lib("A"))
    b_text = _lib("B").replace(
        "  cell (X_B) {",
        "  /* trailing block */\n  cell (X_B) {",
    )
    b = _write(tmp_path, "groupB.lib", b_text)
    text = _read(liberty_merge.merge_liberty([a, b]))
    assert "cell (X_B)" in text


def test_header_with_only_delay_model_beyond_ignored_set(tmp_path):
    a_text = (
        'library (lib_A) {\n  date : "d1";\n  delay_model : table_lookup;\n'
        "  cell (X_A) {\n    area : 1;\n  }\n}\n"
    )
    b_text = (
        'library (lib_B) {\n  date : "d2";\n  delay_model : table_lookup;\n'
        "  cell (X_B) {\n    area : 2;\n  }\n}\n"
    )
    a = _write(tmp_path, "groupA.lib", a_text)
    b = _write(tmp_path, "groupB.lib", b_text)
    text = _read(liberty_merge.merge_liberty([a, b]))
    assert "cell (X_A)" in text and "cell (X_B)" in text
