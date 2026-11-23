"""Repeated Liberty header groups are compared by body and multiplicity."""

import hashlib
from pathlib import Path

import pytest

from klayout_tools import liberty_merge
from test_liberty_merge import _cache as _cache
from test_liberty_merge import _lib, _read, _write


def _input(tmp_path, name, voltages, named=True):
    key = "operating_conditions (keep)" if named else "custom_group"
    groups = "".join(f"  {key} {{ voltage : {voltage}; }}\n" for voltage in voltages)
    return _write(tmp_path, f"{name}.lib", _lib(name, header_extra=groups))


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("named", [False, True])
@pytest.mark.parametrize("left,right", [([1, 2], [2]), ([2, 2], [2]), ([1, 2], [3, 2])])
def test_repeated_header_group_mismatch(tmp_path, left, right, named, reverse):
    paths = [_input(tmp_path, "A", left, named), _input(tmp_path, "B", right, named)]
    if reverse:
        paths.reverse()
    with pytest.raises(liberty_merge.LibertyMergeError) as caught:
        liberty_merge.merge_liberty(paths)
    message = str(caught.value)
    assert all(p in message for p in paths)
    assert ("operating_conditions(keep)" if named else "custom_group") in message


def test_equal_repeated_groups_preserve_the_first_header(tmp_path):
    a = _input(tmp_path, "A", [2, 1, 2])
    b = _input(tmp_path, "B", [2, 2, 1])
    merged = _read(liberty_merge.merge_liberty([a, b]))
    assert merged.count("operating_conditions (keep)") == 3
    assert merged.startswith(_read(a).split("  cell (")[0])
    assert "cell (X_A)" in merged and "cell (X_B)" in merged


def test_old_cached_merge_cannot_hide_a_group_mismatch(tmp_path):
    paths = [_input(tmp_path, "A", [1, 2]), _input(tmp_path, "B", [2])]
    old_digest = hashlib.sha256()
    for path in paths:
        old_digest.update(hashlib.sha256(Path(path).read_bytes()).digest())
    cached = (
        Path(liberty_merge.cache_dir()) / f"merged-{old_digest.hexdigest()[:24]}.lib"
    )
    cached.parent.mkdir(parents=True)
    cached.write_text(_lib("old"))
    with pytest.raises(liberty_merge.LibertyMergeError, match="operating_conditions"):
        liberty_merge.merge_liberty(paths)


def test_group_and_attribute_deltas_follow_source_order(tmp_path):
    paths = [
        _write(
            tmp_path,
            f"{n}.lib",
            _lib(
                n,
                header_extra=(
                    f"  custom_group {{ value : {v}; }}\n  arbitrary_attribute : {v};\n"
                ),
            ),
        )
        for n, v in (("A", 1), ("B", 2))
    ]
    with pytest.raises(liberty_merge.LibertyMergeError) as caught:
        liberty_merge.merge_liberty(paths)
    message = str(caught.value)
    assert message.index("'custom_group'") < message.index("'arbitrary_attribute'")
