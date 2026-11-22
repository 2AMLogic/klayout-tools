"""Refusals for differing header fields during a Liberty list merge."""

import pytest

from klayout_tools import liberty_merge
from klayout_tools.liberty_header_diff import _clip_around_diff
from test_liberty_merge import _cache as _cache
from test_liberty_merge import _lib, _write


def _oc(name: str, body: str) -> str:
    return f"  operating_conditions ({name}) {{\n{body}  }}\n"


def test_all_header_deltas_reported_together(tmp_path):
    """Unit string, one-sided default_max_transition and an in-group order
    change all surface in one refusal, not one per fix."""
    a_extra = '  time_unit : "1ns";\n' + _oc(
        "typ", "    process : 1.0;\n    temperature : 25.0;\n"
    )
    b_extra = '  time_unit : "2ns";\n' + _oc(
        "typ", "    temperature : 25.0;\n    process : 1.0;\n"
    )
    a = _write(tmp_path, "groupA.lib", _lib("A", header_extra=a_extra))
    b_text = _lib("B", header_extra=b_extra).replace(
        "  default_max_transition : 4000;\n", ""
    )
    b = _write(tmp_path, "groupB.lib", b_text)
    with pytest.raises(liberty_merge.LibertyMergeError) as exc:
        liberty_merge.merge_liberty([a, b])
    msg = str(exc.value)
    assert msg.startswith("cannot merge: 3 header fields differ between ")
    assert "groupA.lib" in msg.splitlines()[0] and "groupB.lib" in msg.splitlines()[0]
    assert "  - 'default_max_transition' present in " in msg
    assert "  - 'time_unit' differs: \"1ns\" in " in msg
    assert "  - 'operating_conditions(typ)' differs: " in msg
    assert len(msg.splitlines()) == 4


def test_many_header_deltas_capped_with_remainder(tmp_path):
    a_extra = "".join(f"  attr_{i:02d} : {i};\n" for i in range(12))
    b_extra = "".join(f"  attr_{i:02d} : {i + 100};\n" for i in range(12))
    a = _write(tmp_path, "groupA.lib", _lib("A", header_extra=a_extra))
    b = _write(tmp_path, "groupB.lib", _lib("B", header_extra=b_extra))
    with pytest.raises(liberty_merge.LibertyMergeError) as exc:
        liberty_merge.merge_liberty([a, b])
    lines = str(exc.value).splitlines()
    assert lines[0].startswith("cannot merge: 12 header fields differ")
    assert sum(line.startswith("  - ") for line in lines) == 10
    assert lines[-1] == "  ... and 2 more"


def test_long_group_difference_prints_window_around_divergence(tmp_path):
    common = "".join(f"    attr_{i:02d} : {i};\n" for i in range(12))
    a = _write(
        tmp_path,
        "groupA.lib",
        _lib("A", header_extra=_oc("typ", common + "    voltage : 1.80;\n")),
    )
    b = _write(
        tmp_path,
        "groupB.lib",
        _lib("B", header_extra=_oc("typ", common + "    voltage : 1.62;\n")),
    )
    with pytest.raises(liberty_merge.LibertyMergeError) as exc:
        liberty_merge.merge_liberty([a, b])
    msg = str(exc.value)
    shown_a = msg.split("differs: ", 1)[1].split(" in '", 1)[0]
    shown_b = msg.split("', ", 1)[1].split(" in '", 1)[0]
    assert shown_a != shown_b
    assert shown_a.startswith("...") and "1.80" in shown_a
    assert shown_b.startswith("...") and "1.62" in shown_b


def test_clip_around_diff_short_values_unchanged():
    assert _clip_around_diff("1ns", "2ns") == ("1ns", "2ns")


def test_clip_around_diff_windows_long_values():
    a = "x" * 120 + "AAAA" + "y" * 60
    b = "x" * 120 + "BBBB" + "y" * 60
    shown_a, shown_b = _clip_around_diff(a, b)
    assert shown_a != shown_b and len(shown_a) <= 80 and len(shown_b) <= 80
    assert "AAAA" in shown_a and "BBBB" in shown_b
    assert shown_a.startswith("...") and shown_a.endswith("...")


def test_renamed_group_reported_as_one_paired_entry(tmp_path):
    body = "    process : 1.0;\n    temperature : 25.0;\n"
    a = _write(tmp_path, "groupA.lib", _lib("A", header_extra=_oc("typ", body)))
    b = _write(tmp_path, "groupB.lib", _lib("B", header_extra=_oc("slow", body)))
    with pytest.raises(liberty_merge.LibertyMergeError) as exc:
        liberty_merge.merge_liberty([a, b])
    msg = str(exc.value)
    assert "\n" not in msg  # one entry, single-delta form
    assert "'operating_conditions(typ)' in '" in msg
    assert "is 'operating_conditions(slow)' in '" in msg
    assert "same group, different name" in msg
    assert "groupA.lib" in msg and "groupB.lib" in msg
    assert "missing" not in msg and "body also differs" not in msg


def test_renamed_group_with_different_body_says_so(tmp_path):
    a = _write(
        tmp_path,
        "groupA.lib",
        _lib("A", header_extra=_oc("typ", "    temperature : 25.0;\n")),
    )
    b = _write(
        tmp_path,
        "groupB.lib",
        _lib("B", header_extra=_oc("slow", "    temperature : 100.0;\n")),
    )
    with pytest.raises(liberty_merge.LibertyMergeError) as exc:
        liberty_merge.merge_liberty([a, b])
    msg = str(exc.value)
    assert "same group, different name; body also differs" in msg
    assert "25.0" in msg and "100.0" in msg


def test_single_delta_message_text_unchanged(tmp_path):
    a = _write(tmp_path, "groupA.lib", _lib("A", max_trans="1.0"))
    b = _write(tmp_path, "groupB.lib", _lib("B", max_trans="4.0"))
    with pytest.raises(liberty_merge.LibertyMergeError) as exc:
        liberty_merge.merge_liberty([a, b])
    assert str(exc.value) == (
        f"cannot merge: 'default_max_transition' differs: 1.0 in '{a}', 4.0 in '{b}'"
    )
