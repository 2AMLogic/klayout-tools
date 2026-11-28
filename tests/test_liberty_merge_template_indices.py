"""Template index overrides in split timing and power libraries."""

from pathlib import Path

import pytest

from klayout_tools import liberty_merge


@pytest.fixture(autouse=True)
def isolate_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("KLT_CACHE_DIR", str(tmp_path / "cache"))


def _member_with_table(
    path,
    cell,
    template_index,
    local,
    *,
    kind="lu_table_template",
    variable="total_output_net_capacitance",
    extra="",
    before="",
):
    table_kind = "rise_power" if kind == "power_lut_template" else "cell_rise"
    path.write_text(f"""library ({cell}) {{
 {before}{kind} (t) {{ variable_1 : input_net_transition;
   variable_2 : {variable}; index_1 ("1, 2"); index_2 ("{template_index}"); }}
 cell ({cell}) {{ pin (Y) {{ timing () {{ {table_kind} (t) {{
   index_1 ("1, 2"); {local}
   values ("0.1, 0.2", "0.3, 0.4"); }} {extra} }} }} }}
}}""")
    return str(path)


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("kind", ["lu_table_template", "power_lut_template"])
def test_local_indices_allow_template_union(tmp_path, reverse, kind):
    local = 'index_2 ("2, 4");'
    a = _member_with_table(tmp_path / "a.lib", "A", "1, 2", local, kind=kind)
    b = _member_with_table(tmp_path / "b.lib", "B", "3, 4", local, kind=kind)
    paths = [b, a] if reverse else [a, b]
    merged = Path(liberty_merge.merge_liberty(paths)).read_text()
    assert merged.count(f"{kind} (t)") == 1
    assert ('index_2 ("3, 4");' if reverse else 'index_2 ("1, 2");') in merged
    assert merged.count(local) == 2
    assert merged.count('values ("0.1, 0.2", "0.3, 0.4");') == 2


@pytest.mark.parametrize("position", [0, 2])
@pytest.mark.parametrize(
    "missing",
    [
        "",
        'index_2 ("");',
        '/* index_2 ("1, 2"); */',
        'note : "index_2 (1, 2);";',
        'nested () { index_2 ("1, 2"); }',
    ],
)
def test_inheriting_member_refuses_anywhere_in_list(tmp_path, position, missing):
    local = 'index_2 ("2, 4");'
    paths = [
        _member_with_table(tmp_path / f"{c}.lib", c, grid, local)
        for c, grid in [("A", "1, 2"), ("B", "3, 4")]
    ]
    relying = _member_with_table(
        tmp_path / "consumer.lib",
        "C",
        "1, 2",
        missing,
        extra='cell_fall (t) { index_1 ("1, 2"); index_2 ("1, 2"); values ("1, 2"); }',
    )
    paths.insert(position, relying)
    with pytest.raises(liberty_merge.LibertyMergeError) as exc:
        liberty_merge.merge_liberty(paths)
    assert "template 't'" in str(exc.value)
    assert "index_2" in str(exc.value) and relying in str(exc.value)


@pytest.mark.parametrize(
    "changed",
    [{"kind": "power_lut_template"}, {"variable": "constrained_pin_transition"}],
)
def test_template_non_index_change_still_refuses(tmp_path, changed):
    local = 'index_2 ("2, 4");'
    a = _member_with_table(tmp_path / "a.lib", "A", "1, 2", local)
    b = _member_with_table(tmp_path / "b.lib", "B", "3, 4", local, **changed)
    with pytest.raises(liberty_merge.LibertyMergeError, match="template 't' differs"):
        liberty_merge.merge_liberty([a, b])


@pytest.mark.parametrize("reverse", [False, True])
def test_complex_attribute_of_a_template_kind_is_a_plain_field(tmp_path, reverse):
    """``lu_table_template (x);`` is a header field compared like any other:
    it is neither a template nor allowed to swallow the template group
    declared after it."""
    local = 'index_2 ("2, 4");'
    a = _member_with_table(
        tmp_path / "a.lib", "A", "1, 2", local, before="lu_table_template (x);\n  "
    )
    b = _member_with_table(tmp_path / "b.lib", "B", "1, 2", local)
    paths = [b, a] if reverse else [a, b]
    with pytest.raises(liberty_merge.LibertyMergeError) as exc:
        liberty_merge.merge_liberty(paths)
    msg = str(exc.value)
    assert "'lu_table_template(x)'" in msg
    assert f"present in '{a}'" in msg and f"missing in '{b}'" in msg
    assert "lu_table_template(t)" not in msg


def test_matching_complex_attribute_of_a_template_kind_merges(tmp_path):
    local = 'index_2 ("2, 4");'
    before = "lu_table_template (x);\n  "
    a = _member_with_table(tmp_path / "a.lib", "A", "1, 2", local, before=before)
    b = _member_with_table(tmp_path / "b.lib", "B", "1, 2", local, before=before)
    merged = Path(liberty_merge.merge_liberty([a, b])).read_text()
    assert merged.count("lu_table_template (x);") == 1
    assert merged.count("lu_table_template (t)") == 1


def test_one_axis_power_table_and_same_name_wire_load(tmp_path):
    def member(cell, index, override):
        path = tmp_path / (cell + ".lib")
        path.write_text(f"""library ({cell}) {{
          wire_load (t) {{ capacitance : 1.0; }}
          power_lut_template (t) {{
            variable_1 : input_transition_time; index_1 ("{index}"); }}
          cell ({cell}) {{ pin (Y) {{ internal_power () {{ rise_power ("t") {{
            {override} values ("3, 4"); }} }} }} }} }}""")
        return str(path)

    a = member("A", "1, 2", 'index_1 ("1, 2");')
    b = member("B", "2, 3", 'index_1 ("0.5, 1.0");')
    merged = Path(liberty_merge.merge_liberty([a, b])).read_text()
    assert merged.count("power_lut_template (t)") == 1
    assert 'index_1 ("0.5, 1.0");' in merged
    b = member("B", "2, 3", "")
    with pytest.raises(liberty_merge.LibertyMergeError, match="inherits index_1"):
        liberty_merge.merge_liberty([b, a])
