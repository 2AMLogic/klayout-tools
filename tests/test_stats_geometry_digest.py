"""`klt stats`' `geometry_digest` field (issue #5551).

Lives beside `test_stats.py` rather than inside it: that module is over the
repository's per-file line limit and may not grow. Fixtures are reused from it,
the way `tests/test_sim_stub_delegation.py` reuses `test_sim`'s.
"""

import json

import jsonschema
import klayout.db as kdb
import pytest

from klayout_tools.cli import main
from klayout_tools.stats import stats_report
from test_stats import _SCHEMA, _make_layout, _make_multi_top_layout


def test_geometry_digest_equals_layout_geometry_digest(tmp_path):
    """The reported field is exactly the digest the signoff grader recomputes."""
    from klayout_tools._provenance import layout_geometry_digest

    path = tmp_path / "design.gds"
    _make_layout().write(str(path))

    report = stats_report(str(path))

    assert report["geometry_digest"] == layout_geometry_digest(str(path))
    assert report["geometry_digest"].startswith("sha256:")
    assert len(report["geometry_digest"]) == len("sha256:") + 64


def test_geometry_digest_ignores_the_embedded_write_timestamp(tmp_path):
    """Two writes of identical geometry whose GDS timestamp records differ are
    byte-different but share one `geometry_digest` -- the whole point of using
    it rather than a raw-byte hash for a regeneration claim.

    The timestamp is toggled explicitly (`gds2_write_timestamps`) rather than
    by writing twice and hoping the wall clock ticked between them, so the
    byte difference is deterministic and comes from the timestamp alone.
    """
    with_stamp = tmp_path / "with-timestamp.gds"
    without_stamp = tmp_path / "without-timestamp.gds"

    stamped = kdb.SaveLayoutOptions()
    stamped.gds2_write_timestamps = True
    unstamped = kdb.SaveLayoutOptions()
    unstamped.gds2_write_timestamps = False
    _make_layout().write(str(with_stamp), stamped)
    _make_layout().write(str(without_stamp), unstamped)

    # Guard the premise: identical bytes would make the assertion below pass
    # for the wrong reason.
    assert with_stamp.read_bytes() != without_stamp.read_bytes()

    assert (
        stats_report(str(with_stamp))["geometry_digest"]
        == stats_report(str(without_stamp))["geometry_digest"]
    )


def test_geometry_digest_is_format_independent(tmp_path):
    """The same geometry hashes identically as GDS and as OASIS, so a
    regeneration claim survives a generator that writes the other format."""
    gds = tmp_path / "design.gds"
    oas = tmp_path / "design.oas"
    _make_layout().write(str(gds))
    _make_layout().write(str(oas))

    assert gds.read_bytes() != oas.read_bytes()
    assert (
        stats_report(str(gds))["geometry_digest"]
        == stats_report(str(oas))["geometry_digest"]
    )


def test_geometry_digest_changes_when_geometry_changes(tmp_path):
    path = tmp_path / "base.gds"
    _make_layout().write(str(path))
    baseline = stats_report(str(path))["geometry_digest"]

    changed_path = tmp_path / "changed.gds"
    changed = _make_layout()
    changed.cell("TOP").shapes(changed.layer(1, 0)).insert(
        kdb.Box(1500, 1500, 1600, 1600)
    )
    changed.write(str(changed_path))

    assert stats_report(str(changed_path))["geometry_digest"] != baseline


def test_geometry_digest_is_whole_stream_not_scoped_by_top(tmp_path):
    """`--top` scopes the area/count figures but not the digest: the signoff
    grader hashes the whole bound stream, so a `--top`-scoped digest would be
    a value that never grades `met`."""
    path = tmp_path / "multi.oas"
    _make_multi_top_layout().write(str(path))

    scoped = stats_report(str(path), top="BLOCK_A")
    other = stats_report(str(path), top="BLOCK_B")

    assert scoped["geometry_digest"] == other["geometry_digest"]


def test_geometry_digest_reported_for_empty_layout(tmp_path):
    path = tmp_path / "empty.gds"
    kdb.Layout().write(str(path))

    report = stats_report(str(path))

    assert report["top_cell"] is None
    assert report["geometry_digest"].startswith("sha256:")


def test_geometry_digest_in_text_output(tmp_path, capsys):
    path = tmp_path / "design.gds"
    _make_layout().write(str(path))
    expected = stats_report(str(path))["geometry_digest"]

    assert main(["stats", str(path)]) == 0

    assert f"geometry_digest: {expected}" in capsys.readouterr().out


def test_schema_rejects_a_non_digest_geometry_digest(tmp_path, capsys):
    """The shipped schema pins the field's shape, so a consumer validating
    against it catches a raw hex `canonical_hash` substituted in."""
    path = tmp_path / "design.gds"
    _make_layout().write(str(path))
    assert main(["stats", str(path), "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)

    jsonschema.validate(data, _SCHEMA, cls=jsonschema.Draft202012Validator)

    # A bare 64-hex digest (what `klt diff` reports as `canonical_hash`) is
    # not the `sha256:`-prefixed shape this field promises.
    data["geometry_digest"] = "2e572c0e" + "0" * 56
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(data, _SCHEMA, cls=jsonschema.Draft202012Validator)
