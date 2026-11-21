"""Operating-condition rename merges of ``liberty_merge`` (issue #6940).

Split from ``test_liberty_merge.py``, which sits near the 500-line file-size
ratchet: these cases extend its header-comparison coverage with the local
``operating_conditions`` rename exception -- one condition per file, each
selected by its own default, group bodies equal, only the name differs.
Fixtures are minimal cell-group ``.lib`` files under ``tmp_path``;
``KLT_CACHE_DIR`` is pointed at a scratch dir so no test touches the real
user cache.
"""

from __future__ import annotations

import pytest

from klayout_tools import liberty_merge


def _lib(
    tag: str,
    *,
    condition: str = "typ_1_25",
    default: str | None = None,
    process: str = "1.0",
    temperature: str = "25.0",
    voltage: str = "1.0",
    condition_extra: str = "",
    max_trans: str = "1.0",
    quoted: bool = True,
    second_condition: tuple[str, str] | None = None,
    body_extra: str = "",
) -> str:
    """One minimal cell-group ``.lib`` whose header declares an operating
    condition (optionally two) selected as its default -- issue #6940's
    reproduction shape."""
    name = f'"{condition}"' if quoted else condition
    selected = condition if default is None else default
    default_name = f'"{selected}"' if quoted else selected
    text = (
        f"library (synth_{tag}) {{\n"
        "  technology (cmos);\n"
        "  delay_model : table_lookup;\n"
        f"  default_max_transition : {max_trans};\n"
        f"  operating_conditions ({name}) {{\n"
        f"    process : {process};\n"
        f"    temperature : {temperature};\n"
        f"    voltage : {voltage};\n"
        f"{condition_extra}"
        "  }\n"
        f"  default_operating_conditions : {default_name};\n"
    )
    if second_condition is not None:
        second = f'"{second_condition[0]}"' if quoted else second_condition[0]
        text += (
            f"  operating_conditions ({second}) {{\n"
            f"    process : {second_condition[1]};\n"
            "    temperature : 25.0;\n"
            "    voltage : 1.0;\n"
            "  }\n"
        )
    return text + f"  cell (INV_{tag}) {{\n    area : 1.0;\n  }}\n" + body_extra + "}\n"


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


def test_renamed_condition_merges_keeping_first_spelling(tmp_path):
    a = _write(tmp_path, "groupA.lib", _lib("A"))
    b = _write(tmp_path, "groupB.lib", _lib("B", condition="typ25"))
    text = _read(liberty_merge.merge_liberty([a, b]))
    assert 'operating_conditions ("typ_1_25")' in text
    assert 'default_operating_conditions : "typ_1_25";' in text
    assert "typ25" not in text
    assert "cell (INV_A)" in text and "cell (INV_B)" in text
    assert text.index("cell (INV_A)") < text.index("cell (INV_B)")


def test_reversed_inputs_keep_new_first_spelling(tmp_path):
    a = _write(tmp_path, "groupA.lib", _lib("A"))
    b = _write(tmp_path, "groupB.lib", _lib("B", condition="typ25"))
    text = _read(liberty_merge.merge_liberty([b, a]))
    assert 'operating_conditions ("typ25")' in text
    assert 'default_operating_conditions : "typ25";' in text
    assert "typ_1_25" not in text
    assert text.index("cell (INV_B)") < text.index("cell (INV_A)")


def test_unquoted_condition_names_merge(tmp_path):
    a = _write(tmp_path, "groupA.lib", _lib("A", quoted=False))
    b = _write(tmp_path, "groupB.lib", _lib("B", condition="tt_1p0v_25c", quoted=False))
    text = _read(liberty_merge.merge_liberty([a, b]))
    assert "operating_conditions (typ_1_25)" in text
    assert "default_operating_conditions : typ_1_25;" in text
    assert "tt_1p0v_25c" not in text
    assert "cell (INV_B)" in text


@pytest.mark.parametrize(
    "kwargs",
    [
        {"process": "1.1"},
        {"voltage": "0.77"},
        {"temperature": "0.0"},
        {"condition_extra": "    tree_type : buffered;\n"},
    ],
    ids=["process", "voltage", "temperature", "extra-attribute"],
)
def test_condition_content_difference_refuses(tmp_path, kwargs):
    a = _write(tmp_path, "groupA.lib", _lib("A"))
    b = _write(tmp_path, "groupB.lib", _lib("B", condition="typ25", **kwargs))
    with pytest.raises(liberty_merge.LibertyMergeError) as exc:
        liberty_merge.merge_liberty([a, b])
    msg = str(exc.value)
    assert "operating_conditions" in msg
    assert "groupA.lib" in msg and "groupB.lib" in msg


def test_missing_condition_group_in_second_file_refuses(tmp_path):
    a = _write(tmp_path, "groupA.lib", _lib("A"))
    plain = (
        "library (synth_B) {\n"
        "  technology (cmos);\n"
        "  delay_model : table_lookup;\n"
        "  default_max_transition : 1.0;\n"
        "  cell (INV_B) {\n    area : 1.0;\n  }\n}\n"
    )
    b = _write(tmp_path, "groupB.lib", plain)
    with pytest.raises(liberty_merge.LibertyMergeError) as exc:
        liberty_merge.merge_liberty([a, b])
    msg = str(exc.value)
    assert "operating_conditions" in msg and "missing" in msg
    assert "groupA.lib" in msg and "groupB.lib" in msg


def test_mismatched_default_refuses(tmp_path):
    # File B's default points at file A's condition name: it neither selects
    # its local group nor resolves locally (a dangling default).
    a = _write(tmp_path, "groupA.lib", _lib("A"))
    b = _write(tmp_path, "groupB.lib", _lib("B", condition="typ25", default="typ_1_25"))
    with pytest.raises(liberty_merge.LibertyMergeError) as exc:
        liberty_merge.merge_liberty([a, b])
    msg = str(exc.value)
    assert "operating_conditions" in msg
    assert "groupA.lib" in msg and "groupB.lib" in msg


def test_ambiguous_rename_with_second_group_refuses(tmp_path):
    a = _write(tmp_path, "groupA.lib", _lib("A", second_condition=("typ_a2", "2.0")))
    b = _write(
        tmp_path,
        "groupB.lib",
        _lib("B", condition="typ_b1", second_condition=("typ_a2", "2.0")),
    )
    with pytest.raises(liberty_merge.LibertyMergeError) as exc:
        liberty_merge.merge_liberty([a, b])
    msg = str(exc.value)
    assert "operating_conditions" in msg
    assert "groupA.lib" in msg and "groupB.lib" in msg


def test_identical_multi_condition_headers_merge(tmp_path):
    a = _write(tmp_path, "groupA.lib", _lib("A", second_condition=("typ_a2", "2.0")))
    b = _write(tmp_path, "groupB.lib", _lib("B", second_condition=("typ_a2", "2.0")))
    text = _read(liberty_merge.merge_liberty([a, b]))
    assert text.count("operating_conditions (") == 2
    assert text.count("default_operating_conditions") == 1
    assert "cell (INV_A)" in text and "cell (INV_B)" in text


def test_max_transition_conflict_still_refuses_with_rename(tmp_path):
    a = _write(tmp_path, "groupA.lib", _lib("A", max_trans="1.0"))
    b = _write(tmp_path, "groupB.lib", _lib("B", condition="typ25", max_trans="4.0"))
    with pytest.raises(liberty_merge.LibertyMergeError) as exc:
        liberty_merge.merge_liberty([a, b])
    msg = str(exc.value)
    assert "default_max_transition" in msg
    assert "1.0" in msg and "4.0" in msg
    assert "groupA.lib" in msg and "groupB.lib" in msg


def test_body_reference_to_renamed_condition_refuses(tmp_path):
    a = _write(tmp_path, "groupA.lib", _lib("A"))
    b = _write(
        tmp_path,
        "groupB.lib",
        _lib(
            "B",
            condition="typ25",
            body_extra="  cell (AUX_B) {\n    area : 1.0;\n"
            '    related_condition : "typ25";\n  }\n',
        ),
    )
    with pytest.raises(liberty_merge.LibertyMergeError) as exc:
        liberty_merge.merge_liberty([a, b])
    msg = str(exc.value)
    assert "typ25" in msg and "groupB.lib" in msg


def test_renamed_condition_with_default_max_transition_equal_merges(tmp_path):
    a = _write(tmp_path, "groupA.lib", _lib("A", max_trans="320"))
    b = _write(tmp_path, "groupB.lib", _lib("B", condition="typ25", max_trans="320"))
    text = _read(liberty_merge.merge_liberty([a, b]))
    assert "cell (INV_A)" in text and "cell (INV_B)" in text
    assert "typ25" not in text
