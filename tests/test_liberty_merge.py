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
    msg = str(exc.value)
    # The fail-closed path is the residue key, not some other field.
    assert liberty_merge._RESIDUE_KEY in msg and "Liberty headers differ" not in msg


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


def test_complex_attribute_without_body_merges(tmp_path):
    extra = "  capacitive_load_unit (1, pf);\n  voltage_map (VDD, 1.8);\n"
    a = _write(tmp_path, "groupA.lib", _lib("A", header_extra=extra))
    b = _write(tmp_path, "groupB.lib", _lib("B", header_extra=extra))
    text = _read(liberty_merge.merge_liberty([a, b]))
    assert "capacitive_load_unit (1, pf)" in text
    assert "voltage_map (VDD, 1.8)" in text


def test_complex_attribute_conflict_names_key_and_values(tmp_path):
    a = _write(
        tmp_path, "groupA.lib", _lib("A", header_extra="  voltage_map (VDD, 1.8);\n")
    )
    b = _write(
        tmp_path, "groupB.lib", _lib("B", header_extra="  voltage_map (VDD, 1.7);\n")
    )
    with pytest.raises(liberty_merge.LibertyMergeError) as exc:
        liberty_merge.merge_liberty([a, b])
    msg = str(exc.value)
    # The args are part of the field key, so differing args surface as
    # present-vs-missing: the message names the first file's key (args
    # embedded) and both paths, never the opaque wording.
    assert "voltage_map(VDD, 1.8)" in msg
    assert "groupA.lib" in msg and "groupB.lib" in msg
    assert "missing" in msg and "Liberty headers differ" not in msg


def test_comments_between_header_tokens_merge(tmp_path):
    extra = (
        "  nominal_voltage /* VDD rail */ : 0.7;\n"
        "  voltage_map // VDD at nominal\n"
        "    (VDD, 0.7);\n"
    )
    a = _write(tmp_path, "groupA.lib", _lib("A", header_extra=extra))
    b = _write(tmp_path, "groupB.lib", _lib("B", header_extra=extra))
    text = _read(liberty_merge.merge_liberty([a, b]))
    assert "nominal_voltage /* VDD rail */ : 0.7;" in text
    assert "cell (X_A)" in text and "cell (X_B)" in text


def test_comments_and_nested_group_inside_header_group_merge(tmp_path):
    extra = (
        "  pin_constraints (PC) {\n"
        '    /* nested */ pin_group (PG) { pins : "A, B"; }\n'
        "    // line note\n"
        '    default_pin : "A";\n'
        "  }\n"
    )
    a = _write(tmp_path, "groupA.lib", _lib("A", header_extra=extra))
    b = _write(tmp_path, "groupB.lib", _lib("B", header_extra=extra))
    text = _read(liberty_merge.merge_liberty([a, b]))
    assert "pin_group (PG)" in text
    assert "cell (X_A)" in text and "cell (X_B)" in text


def test_next_significant_skips_comments_to_end_of_text():
    open_block = "a /* never closed"
    assert liberty_merge._next_significant(open_block, 1) == (len(open_block), "")
    open_line = "a // never closed"
    assert liberty_merge._next_significant(open_line, 1) == (len(open_line), "")
    idx, ch = liberty_merge._next_significant("/* c */ b", 0)
    assert (ch, "/* c */ b"[idx:]) == ("b", "b")
    idx, ch = liberty_merge._next_significant("// c\nb", 0)
    assert (ch, "// c\nb"[idx:]) == ("b", "b")
    assert liberty_merge._next_significant("   ", 0) == (3, "")


def test_find_matching_skips_comments_and_nested_braces():
    text = "{ a /* } */ : 1; { b } // tail\n }"
    assert liberty_merge._find_matching(text, 0, "{", "}") == text.rindex("}")
    open_block = "{ /* never closed"
    assert liberty_merge._find_matching(open_block, 0, "{", "}") == len(open_block)
    open_line = "{ // never closed"
    assert liberty_merge._find_matching(open_line, 0, "{", "}") == len(open_line)
    assert liberty_merge._find_matching("{ a", 0, "{", "}") == len("{ a")


def _colon_date_lib(
    tag: str,
    *,
    date: str = "$Date: N $",
    time_unit: str = "1ns",
    max_trans: str = "320",
) -> str:
    """A cell-group ``.lib`` whose ``date`` value is *unquoted* and holds a
    second ':' (an RCS keyword, or a bare ``hh:mm:ss`` stamp)."""
    return (
        f"library (lib_{tag}) {{\n"
        f"  date : {date};\n"
        "  delay_model : table_lookup;\n"
        f'  time_unit : "{time_unit}";\n'
        f"  default_max_transition : {max_trans};\n"
        f"  cell (X_{tag}) {{ area : 1; }}\n"
        "}\n"
    )


def test_unquoted_colon_in_date_keeps_following_fields(tmp_path):
    """A second ':' in an ignored attribute's value must not make the
    statement scan run on and swallow the rest of the header."""
    path = _write(tmp_path, "colon.lib", _colon_date_lib("E"))
    header = _read(path).split("cell (")[0]
    assert liberty_merge._header_fields(path, header) == {
        "delay_model": "table_lookup",
        "time_unit": '"1ns"',
        "default_max_transition": "320",
    }


def test_unquoted_colon_in_date_does_not_hide_conflicts(tmp_path):
    """Same-quirk files (every file of one vendor release shares the writer)
    with genuinely conflicting electrical headers are still refused, as
    ``main`` refuses them."""
    both = {"time_unit": "1ps", "max_trans": "4000"}
    a = _write(tmp_path, "groupA.lib", _colon_date_lib("A"))
    b = _write(tmp_path, "groupB.lib", _colon_date_lib("B", **both))
    with pytest.raises(liberty_merge.LibertyMergeError) as exc:
        liberty_merge.merge_liberty([a, b])
    assert "time_unit" in str(exc.value)

    c = _write(tmp_path, "groupC.lib", _colon_date_lib("C", max_trans="4000"))
    with pytest.raises(liberty_merge.LibertyMergeError) as exc:
        liberty_merge.merge_liberty([a, c])
    msg = str(exc.value)
    assert "default_max_transition" in msg and "320" in msg and "4000" in msg


def test_unquoted_colon_dates_only_difference_merges(tmp_path):
    a = _write(
        tmp_path, "groupA.lib", _colon_date_lib("A", date="Fri Nov 27 23:17:09 2020")
    )
    b = _write(
        tmp_path, "groupB.lib", _colon_date_lib("B", date="Mon Dec  7 13:53:54 2020")
    )
    text = _read(liberty_merge.merge_liberty([a, b]))
    assert "cell (X_A)" in text and "cell (X_B)" in text
    assert "13:53:54" not in text


def test_header_of_only_ignored_attributes_is_refused(tmp_path):
    """Statements present but nothing comparable parsed: refuse rather than
    compare an empty field map against another empty one."""
    only_ignored = (
        'library (lib_{0}) {{\n  date : "d{0}";\n  revision : "1";\n'
        "  cell (X_{0}) {{ area : 1; }}\n}}\n"
    )
    a = _write(tmp_path, "groupA.lib", only_ignored.format("A"))
    b = _write(tmp_path, "groupB.lib", only_ignored.format("B"))
    with pytest.raises(liberty_merge.LibertyMergeError) as exc:
        liberty_merge.merge_liberty([a, b])
    msg = str(exc.value)
    assert "no comparable header field" in msg and "groupA.lib" in msg
    assert "comment, date, revision" in msg


def test_header_without_statements_still_merges(tmp_path):
    """The empty-field-map guard needs statements to have been consumed: a
    bare ``library (x) {`` opener has nothing to compare and is not an error."""
    bare = "library (lib_{0}) {{\n  cell (X_{0}) {{ area : 1; }}\n}}\n"
    a = _write(tmp_path, "groupA.lib", bare.format("A"))
    b = _write(tmp_path, "groupB.lib", bare.format("B"))
    text = _read(liberty_merge.merge_liberty([a, b]))
    assert "cell (X_A)" in text and "cell (X_B)" in text


def test_find_terminator_ignores_colons_strings_and_comments():
    text = 'x : a: b "q;" /* ; */ // ;\n c ; tail'
    assert liberty_merge._find_terminator(text, 0) == text.index("c ;") + 2
    assert liberty_merge._find_terminator('"a\\";" ;', 0) == len('"a\\";" ;') - 1
    assert liberty_merge._find_terminator("a ; b ;", 0) == 2
    assert liberty_merge._find_terminator("a ; b ;", 3) == 6


def test_find_terminator_unterminated_runs_to_end_of_text():
    for text in ("a b", '"never closed ;', "a /* never closed ;", "a // never ;"):
        assert liberty_merge._find_terminator(text, 0) == len(text)
