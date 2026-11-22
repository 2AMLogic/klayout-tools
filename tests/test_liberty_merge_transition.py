"""Diagnostics for conflicting output transition defaults in Liberty members."""

import pytest

from klayout_tools import liberty_merge
from test_liberty_merge import _write


def _group_lib(
    tag: str,
    *,
    max_trans: str,
    output_limit: str | None = None,
    decoy: str = "",
) -> str:
    """One cell-group ``.lib`` around a two-pin cell: the input carries an
    explicit ``max_transition``, the output carries ``output_limit`` (absent
    when ``None`` -- it then relies on the library default, issue #6933)."""
    limit = f"      max_transition : {output_limit};\n" if output_limit else ""
    return (
        f"library (lib_{tag}) {{\n"
        '  date : "$Date: fixed $";\n'
        "  delay_model : table_lookup;\n"
        f"  default_max_transition : {max_trans};\n"
        f"  cell (X_{tag}) {{\n"
        "    area : 1;\n"
        "    pin (A) {\n"
        '      direction : "input";\n'
        "      max_transition : 1;\n"
        "    }\n"
        "    pin (Y) {\n"
        '      direction : "output";\n'
        f"{decoy}{limit}"
        "      max_capacitance : 2;\n"
        "    }\n"
        "  }\n"
        "}\n"
    )


def test_default_transition_conflict_names_uncovered_output(tmp_path):
    """Issue #6933: the 320/4000 refusal explains that an output without
    its own max_transition uses its source library's default, names one
    such cell/output from the conflicting members, and gives the recovery."""
    a = _write(tmp_path, "groupA.lib", _group_lib("A", max_trans="320"))
    b = _write(tmp_path, "groupB.lib", _group_lib("B", max_trans="4000"))
    with pytest.raises(liberty_merge.LibertyMergeError) as exc:
        liberty_merge.merge_liberty([a, b])
    msg = str(exc.value)
    assert "'default_max_transition' differs" in msg
    assert "320" in msg and "4000" in msg
    assert "groupA.lib" in msg and "groupB.lib" in msg
    assert "source library's default_max_transition" in msg
    # The witness: groupB's uncovered output Y, tied to groupB's 4000
    # (paths are absolute; groupB.lib appears in both the value diff and
    # the witness).
    assert "cell 'X_B' output 'Y'" in msg
    assert msg.count("groupB.lib") == 2
    assert "limited by 4000" in msg
    assert "liberty.<cell_library>.<corner>" in msg


def test_default_transition_conflict_reversed_order_still_explains(tmp_path):
    a = _write(tmp_path, "groupA.lib", _group_lib("A", max_trans="320"))
    b = _write(tmp_path, "groupB.lib", _group_lib("B", max_trans="4000"))
    with pytest.raises(liberty_merge.LibertyMergeError) as exc:
        liberty_merge.merge_liberty([b, a])
    msg = str(exc.value)
    assert "'default_max_transition' differs" in msg
    assert "320" in msg and "4000" in msg
    assert "groupA.lib" in msg and "groupB.lib" in msg
    assert "cell 'X_A' output 'Y'" in msg
    assert "limited by 320" in msg


def test_default_transition_conflict_witness_from_first_file(tmp_path):
    """Both conflicting members are scanned: an uncovered output in the
    first file is named when the second carries explicit limits everywhere."""
    a = _write(tmp_path, "groupA.lib", _group_lib("A", max_trans="320"))
    b = _write(
        tmp_path, "groupB.lib", _group_lib("B", max_trans="4000", output_limit="4000")
    )
    with pytest.raises(liberty_merge.LibertyMergeError) as exc:
        liberty_merge.merge_liberty([a, b])
    msg = str(exc.value)
    assert "cell 'X_A' output 'Y'" in msg
    assert "limited by 320" in msg


@pytest.mark.parametrize(
    "decoy",
    [
        pytest.param("      /* max_transition : 9; vendor note */\n", id="comment"),
        pytest.param(
            '      timing () { related_pin : "A"; max_transition : 9; }\n',
            id="nested-group",
        ),
    ],
)
def test_max_transition_decoys_do_not_count_as_overrides(tmp_path, decoy):
    """A comment mentioning max_transition, or one buried in a nested group,
    is not an override: the output still counts as uncovered."""
    a = _write(tmp_path, "groupA.lib", _group_lib("A", max_trans="320"))
    b = _write(tmp_path, "groupB.lib", _group_lib("B", max_trans="4000", decoy=decoy))
    with pytest.raises(liberty_merge.LibertyMergeError) as exc:
        liberty_merge.merge_liberty([a, b])
    assert "cell 'X_B' output 'Y'" in str(exc.value)


def test_default_transition_conflict_without_uncovered_output_names_no_pin(tmp_path):
    """Every output explicit: the refusal still fires and gives the recovery
    path, but makes no claim about a particular pin relying on a default."""
    a = _write(
        tmp_path, "groupA.lib", _group_lib("A", max_trans="320", output_limit="320")
    )
    b = _write(
        tmp_path, "groupB.lib", _group_lib("B", max_trans="4000", output_limit="4000")
    )
    with pytest.raises(liberty_merge.LibertyMergeError) as exc:
        liberty_merge.merge_liberty([a, b])
    msg = str(exc.value)
    assert "'default_max_transition' differs" in msg
    assert "320" in msg and "4000" in msg
    assert "groupA.lib" in msg and "groupB.lib" in msg
    assert "output 'Y'" not in msg
    assert "No output pin inheriting the default was identified" in msg
    assert "liberty.<cell_library>.<corner>" in msg


@pytest.mark.parametrize("reverse", [False, True])
def test_only_one_member_inherits_the_default(tmp_path, reverse):
    covered = _write(
        tmp_path, "covered.lib", _group_lib("A", max_trans="320", output_limit="150")
    )
    uncovered = _write(tmp_path, "uncovered.lib", _group_lib("B", max_trans="4000"))
    paths = [uncovered, covered] if reverse else [covered, uncovered]

    with pytest.raises(liberty_merge.LibertyMergeError) as exc:
        liberty_merge.merge_liberty(paths)

    message = str(exc.value)
    assert "cell 'X_B' output 'Y'" in message
    assert "is limited by 4000" in message
    assert "no single shared header" not in message
