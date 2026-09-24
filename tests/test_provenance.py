"""Unit tests for the shared reproducibility provenance helper
(`klayout_tools._provenance`) and the deck-source resolver it relies on
(`klayout_tools.decks.deck_source_path`).

The per-verb wiring is exercised in each verb's own test module
(`test_drc.py`, `test_lvs.py`, `test_extract.py`, `test_sim.py`,
`test_precheck.py`); this module covers the shared building blocks and their
documented edge cases (missing files -> `null`, content hash tracks content).
"""

from __future__ import annotations

import hashlib
import json

import pytest

from klayout_tools import _provenance
from klayout_tools.decks import deck_source_path

# --------------------------------------------------------------------------- #
# sha256_file
# --------------------------------------------------------------------------- #


def test_sha256_file_matches_hashlib(tmp_path):
    path = tmp_path / "deck.txt"
    path.write_bytes(b"rule set v1\n")

    expected = hashlib.sha256(b"rule set v1\n").hexdigest()
    assert _provenance.sha256_file(str(path)) == expected


def test_sha256_file_none_for_missing_or_empty_path(tmp_path):
    # Edge case from the acceptance test plan: an absent path must surface as
    # `None`, not raise -- the defensive shape sim.py already relied on.
    assert _provenance.sha256_file(None) is None
    assert _provenance.sha256_file("") is None
    assert _provenance.sha256_file(str(tmp_path / "nope.txt")) is None


def test_sha256_file_changes_with_content(tmp_path):
    path = tmp_path / "deck.txt"
    path.write_bytes(b"rule set v1\n")
    first = _provenance.sha256_file(str(path))
    path.write_bytes(b"rule set v2\n")
    second = _provenance.sha256_file(str(path))

    assert first is not None and second is not None
    assert first != second


# --------------------------------------------------------------------------- #
# build_provenance
# --------------------------------------------------------------------------- #


def test_build_provenance_always_reports_versions():
    prov = _provenance.build_provenance()
    assert set(prov.keys()) == {
        "klt_version",
        "klayout_version",
        "pdk",
        "deck",
        "input",
    }
    assert prov["klt_version"] == _provenance.build_identity.build_version()


def test_build_provenance_no_deck_no_pdk_no_input_are_null():
    prov = _provenance.build_provenance()
    assert prov["deck"] is None
    assert prov["pdk"] is None
    assert prov["input"] is None


def test_build_provenance_deck_hash_is_sha256_prefixed(tmp_path):
    deck_file = tmp_path / "mydeck.txt"
    deck_file.write_bytes(b"rules\n")

    prov = _provenance.build_provenance(deck_name="mydeck", deck_path=str(deck_file))
    assert prov["deck"]["name"] == "mydeck"
    digest = hashlib.sha256(b"rules\n").hexdigest()
    assert prov["deck"]["content_hash"] == f"sha256:{digest}"


def test_build_provenance_deck_name_without_resolvable_path():
    # A deck name whose file can't be hashed keeps the name but nulls the
    # hash rather than fabricating one or dropping the field. `released` is
    # also null: with no hash to look up, "is this released" is unanswerable
    # -- not `False`, which would falsely claim a confirmed non-release.
    prov = _provenance.build_provenance(deck_name="ghost", deck_path=None)
    assert prov["deck"] == {"name": "ghost", "content_hash": None, "released": None}


# --------------------------------------------------------------------------- #
# Resolved deck options (issue #2394)
# --------------------------------------------------------------------------- #
#
# `resolve_deck_options=True` (passed by `klt extract`/`klt lvs`, and by
# `klt pex` through the former) records the *resolved* option set -- every
# key the deck declares, defaults included -- plus which of those values the
# caller pinned and a hash over the resolved values. Left `False` (`klt drc
# --deck-var`, every other caller) the block stays exactly as it was.


def test_deck_options_are_caller_only_without_resolution():
    """`klt drc --engine klayout`'s `--deck-var` path: an external `.drc`
    file has no declared option surface klt can enumerate, so the block
    stays the pre-#2394 verbatim echo -- no `options_explicit`, no
    `options_hash`."""
    prov = _provenance.build_provenance(
        deck_name="custom.drc", deck_path=None, deck_options={"feol": "true"}
    )
    assert prov["deck"]["options"] == {"feol": "true"}
    assert "options_explicit" not in prov["deck"]
    assert "options_hash" not in prov["deck"]


def test_resolved_deck_options_fill_in_defaults_for_unpassed_keys():
    prov = _provenance.build_provenance(
        deck_name="gf180mcu",
        deck_path=deck_source_path("gf180mcu"),
        deck_options={"poly_res": "3k"},
        resolve_deck_options=True,
    )
    assert prov["deck"]["options"] == {
        "metal_top": "9K",
        "mim_cap": "cap_mim_2f0_m4m5_noshield",
        "poly_res": "3k",
    }
    assert prov["deck"]["options_explicit"] == {
        "metal_top": False,
        "mim_cap": False,
        "poly_res": True,
    }


def test_resolved_deck_options_omitted_for_a_deck_that_declares_none():
    """No regression for a deck with no `flavour_option` entries: absence of
    the field keeps meaning "this deck has no selectable options"."""
    prov = _provenance.build_provenance(
        deck_name="sky130",
        deck_path=deck_source_path("sky130"),
        resolve_deck_options=True,
    )
    assert set(prov["deck"]) == {"name", "content_hash", "released"}


def test_resolved_deck_options_survive_an_unresolvable_deck_name():
    """Provenance is written after a successful run, so this cannot happen
    in practice -- but a name the extraction registry does not know must
    degrade to the pre-#2394 caller-only echo rather than raise out of a
    completed verb."""
    prov = _provenance.build_provenance(
        deck_name="not-a-registered-deck",
        deck_path=None,
        deck_options={"poly_res": "2k"},
        resolve_deck_options=True,
    )
    assert prov["deck"]["options"] == {"poly_res": "2k"}
    assert prov["deck"]["options_explicit"] == {"poly_res": True}


def test_deck_options_hash_covers_values_not_explicitness():
    """Two runs that resolved the same values hash equal even though one
    pinned a value the other defaulted to -- they extracted the same thing.
    A differing *value* changes the hash."""
    pinned = _provenance.build_provenance(
        deck_name="gf180mcu",
        deck_path=deck_source_path("gf180mcu"),
        deck_options={"poly_res": "1k"},
        resolve_deck_options=True,
    )["deck"]
    defaulted = _provenance.build_provenance(
        deck_name="gf180mcu",
        deck_path=deck_source_path("gf180mcu"),
        resolve_deck_options=True,
    )["deck"]
    other = _provenance.build_provenance(
        deck_name="gf180mcu",
        deck_path=deck_source_path("gf180mcu"),
        deck_options={"poly_res": "2k"},
        resolve_deck_options=True,
    )["deck"]

    assert pinned["options_hash"].startswith("sha256:")
    assert pinned["options_hash"] == defaulted["options_hash"]
    assert pinned["options_explicit"] != defaulted["options_explicit"]
    assert pinned["options_hash"] != other["options_hash"]
    # `content_hash` still pins the deck *source* only -- folding options
    # into it would break `klt deck resolve --content-hash`'s lookup against
    # the released-deck history table for every optioned run.
    assert pinned["content_hash"] == other["content_hash"]


# --------------------------------------------------------------------------- #
# explicit_deck_options (issue #2394) -- the `--rerun` replay filter
# --------------------------------------------------------------------------- #


def test_explicit_deck_options_returns_only_caller_pinned_keys():
    deck_block = {
        "name": "gf180mcu",
        "options": {"metal_top": "9K", "poly_res": "3k"},
        "options_explicit": {"metal_top": False, "poly_res": True},
    }
    assert _provenance.explicit_deck_options(deck_block) == {"poly_res": "3k"}


def test_explicit_deck_options_none_when_everything_was_defaulted():
    deck_block = {
        "options": {"metal_top": "9K"},
        "options_explicit": {"metal_top": False},
    }
    assert _provenance.explicit_deck_options(deck_block) is None


def test_explicit_deck_options_treats_a_pre_2394_record_as_all_explicit():
    """A report written before `options_explicit` existed recorded only
    caller-passed keys, so every one of them was explicit -- such a report
    must rerun exactly as it used to."""
    assert _provenance.explicit_deck_options({"options": {"poly_res": "2k"}}) == {
        "poly_res": "2k"
    }


@pytest.mark.parametrize(
    "deck_block", [None, {}, {"options": None}, {"options": {}}, "not-a-mapping"]
)
def test_explicit_deck_options_none_for_a_block_with_no_options(deck_block):
    assert _provenance.explicit_deck_options(deck_block) is None


# --------------------------------------------------------------------------- #
# klayout_version_mismatch (issue #1490)
# --------------------------------------------------------------------------- #
#
# Opt-in via `include_klayout_version_mismatch=True` -- only `klt drc`/`klt
# lvs` pass it (see each verb's own test module for the end-to-end wiring);
# every other `build_provenance` caller is unaffected, covered by
# `test_build_provenance_always_reports_versions` above staying green with no
# `klayout_version_mismatch` key.


def test_build_provenance_omits_mismatch_by_default():
    prov = _provenance.build_provenance()
    assert "klayout_version_mismatch" not in prov


def test_build_provenance_mismatch_false_when_versions_agree(monkeypatch):
    from klayout_tools import build_identity

    monkeypatch.setattr(_provenance, "_klayout_version", lambda: "0.30.10")
    monkeypatch.setattr(
        build_identity, "_recorded_klayout_version_expected", lambda: "0.30.10"
    )
    prov = _provenance.build_provenance(include_klayout_version_mismatch=True)
    assert prov["klayout_version_mismatch"] is False


def test_build_provenance_mismatch_true_when_versions_differ(monkeypatch, capsys):
    from klayout_tools import build_identity

    monkeypatch.setattr(_provenance, "_klayout_version", lambda: "0.30.12")
    monkeypatch.setattr(
        build_identity, "_recorded_klayout_version_expected", lambda: "0.30.10"
    )
    prov = _provenance.build_provenance(include_klayout_version_mismatch=True)
    assert prov["klayout_version_mismatch"] is True

    # Issue #1490 acceptance criteria: a stderr warning, exactly once.
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.count("klt: warning:") == 1
    assert "0.30.12" in captured.err
    assert "0.30.10" in captured.err


def test_build_provenance_mismatch_false_when_expected_unresolvable(monkeypatch):
    """An editable/dev checkout with no build-time-recorded expected version
    (or any build predating this field) must never fabricate a mismatch --
    `False` means "no *confirmed* mismatch", not "confirmed match"."""
    from klayout_tools import build_identity

    monkeypatch.setattr(_provenance, "_klayout_version", lambda: "0.30.12")
    monkeypatch.setattr(
        build_identity, "_recorded_klayout_version_expected", lambda: None
    )
    prov = _provenance.build_provenance(include_klayout_version_mismatch=True)
    assert prov["klayout_version_mismatch"] is False


def test_build_provenance_mismatch_false_when_actual_unresolvable(monkeypatch):
    from klayout_tools import build_identity

    monkeypatch.setattr(_provenance, "_klayout_version", lambda: None)
    monkeypatch.setattr(
        build_identity, "_recorded_klayout_version_expected", lambda: "0.30.10"
    )
    prov = _provenance.build_provenance(include_klayout_version_mismatch=True)
    assert prov["klayout_version_mismatch"] is False


def test_build_provenance_mismatch_check_does_not_shell_out(monkeypatch):
    """Regression guard: computing `klayout_version_mismatch` must never call
    `subprocess.run` (a live `git`/`uv.lock` probe) from this hot path -- that
    would add a subprocess call to *every* `klt drc`/`klt lvs` run on an
    editable/dev install, and previously leaked into unrelated tests that
    globally monkeypatch `subprocess.run` (e.g. netgen LVS engine tests)."""

    def _fail(*_args, **_kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("build_provenance shelled out to compute the pin")

    # Build identity intentionally probes source installs; isolate the
    # engine-pin check this test covers from that independent resolution.
    monkeypatch.setattr(_provenance.build_identity, "build_version", lambda: "0.5.0")
    monkeypatch.setattr(_provenance.subprocess, "run", _fail)
    prov = _provenance.build_provenance(include_klayout_version_mismatch=True)
    assert prov["klayout_version_mismatch"] in (True, False)


def test_klayout_version_mismatch_helper_is_a_plain_boolean():
    assert _provenance._klayout_version_mismatch("0.30.10", "0.30.10") is False
    assert _provenance._klayout_version_mismatch("0.30.12", "0.30.10") is True
    assert _provenance._klayout_version_mismatch(None, "0.30.10") is False
    assert _provenance._klayout_version_mismatch("0.30.10", None) is False
    assert _provenance._klayout_version_mismatch(None, None) is False


# --------------------------------------------------------------------------- #
# deck.released (issue #1193)
# --------------------------------------------------------------------------- #
#
# `_deck_block` delegates the actual lookup to
# `klayout_tools.decks.history.is_deck_hash_released`, already covered end to
# end (including the missing/malformed-table degradation modes) in
# `test_deck_history.py`. These tests just confirm `build_provenance` wires
# that tri-state answer into `provenance.deck.released` correctly, via a
# synthetic history table so they don't depend on real deck content.


def test_build_provenance_deck_released_true_for_known_hash(tmp_path, monkeypatch):
    from klayout_tools.decks import history

    deck_file = tmp_path / "mydeck.txt"
    deck_file.write_bytes(b"rules\n")
    digest = hashlib.sha256(b"rules\n").hexdigest()
    content_hash = f"sha256:{digest}"

    history_path = tmp_path / "_history.json"
    history_path.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "deck": "mydeck",
                        "content_hash": content_hash,
                        "git_tag": "v0.1.0",
                        "git_commit": "0" * 40,
                        "package_version": "0.1.0",
                    }
                ]
            }
        )
    )
    monkeypatch.setattr(history, "_HISTORY_PATH", history_path)

    prov = _provenance.build_provenance(deck_name="mydeck", deck_path=str(deck_file))
    assert prov["deck"]["content_hash"] == content_hash
    assert prov["deck"]["released"] is True


def test_build_provenance_deck_released_false_for_unreleased_hash(
    tmp_path, monkeypatch
):
    from klayout_tools.decks import history

    deck_file = tmp_path / "mydeck.txt"
    deck_file.write_bytes(b"a dev edit not in any release\n")

    history_path = tmp_path / "_history.json"
    history_path.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "deck": "mydeck",
                        "content_hash": "sha256:" + "a" * 64,
                        "git_tag": "v0.1.0",
                        "git_commit": "0" * 40,
                        "package_version": "0.1.0",
                    }
                ]
            }
        )
    )
    monkeypatch.setattr(history, "_HISTORY_PATH", history_path)

    prov = _provenance.build_provenance(deck_name="mydeck", deck_path=str(deck_file))
    # Deck content doesn't match the single entry in the fixture table -- a
    # confirmed non-release, not merely "unknown".
    assert prov["deck"]["released"] is False


def test_build_provenance_deck_released_none_when_history_missing(
    tmp_path, monkeypatch
):
    from klayout_tools.decks import history

    deck_file = tmp_path / "mydeck.txt"
    deck_file.write_bytes(b"rules\n")
    monkeypatch.setattr(history, "_HISTORY_PATH", tmp_path / "does-not-exist.json")

    prov = _provenance.build_provenance(deck_name="mydeck", deck_path=str(deck_file))
    # A missing/unreadable history table must degrade to "unknown", never a
    # false "confirmed unresolvable" (`False`) claim.
    assert prov["deck"]["content_hash"] is not None
    assert prov["deck"]["released"] is None


def test_build_provenance_pdk_maps_find_pdk_shape():
    pdk = {
        "variant": "sky130A",
        "root": "/opt/pdk",
        "version": "abc123",
        "resolved_via": "volare",
    }
    prov = _provenance.build_provenance(pdk=pdk)
    assert prov["pdk"] == {
        "name": "sky130A",
        "source": "volare",
        "version": "abc123",
    }


def test_build_provenance_pdk_none_when_unresolved():
    assert _provenance.build_provenance(pdk=None)["pdk"] is None


def test_build_provenance_input_hash_is_sha256_prefixed(tmp_path):
    layout_file = tmp_path / "top.gds"
    layout_file.write_bytes(b"gds bytes\n")

    prov = _provenance.build_provenance(input_path=str(layout_file))
    digest = hashlib.sha256(b"gds bytes\n").hexdigest()
    assert prov["input"] == {"content_hash": f"sha256:{digest}", "role": "layout"}


def test_build_provenance_input_none_when_path_not_given():
    # No input_path passed at all -- the default, used by verbs (like `lvs`)
    # that pin their input(s) some other way -- keeps `input` null rather
    # than fabricating a block.
    assert _provenance.build_provenance()["input"] is None


def test_build_provenance_input_hash_changes_with_content(tmp_path):
    layout_file = tmp_path / "top.gds"
    layout_file.write_bytes(b"revision 1\n")
    first = _provenance.build_provenance(input_path=str(layout_file))

    layout_file.write_bytes(b"revision 2\n")
    second = _provenance.build_provenance(input_path=str(layout_file))

    assert first["input"]["content_hash"] != second["input"]["content_hash"]


def test_build_provenance_input_hash_null_for_unresolvable_path(tmp_path):
    # A given-but-nonexistent input path keeps the `input` block present
    # (the caller did ask to pin an input) but nulls the hash rather than
    # raising or fabricating one -- mirrors `deck`'s
    # name-without-resolvable-path behaviour.
    missing = tmp_path / "nope.gds"
    prov = _provenance.build_provenance(input_path=str(missing))
    assert prov["input"] == {"content_hash": None, "role": "layout"}


# --------------------------------------------------------------------------- #
# provenance.input.role (issue #2027)
# --------------------------------------------------------------------------- #
#
# The hash alone is kind-blind. `klt drc`/`klt extract` hash a layout stream,
# but `klt lvs` hashes a SPICE netlist for its pre-extracted
# `layout.netlist` request shape, and `klt place-and-route` hashes the
# gate-level netlist it placed. `klt signoff`'s provenance cross-check
# compares `input.content_hash` across checks -- without a discriminator it
# compared a netlist digest against a layout digest and *refused* to
# aggregate a consistent bundle.


def test_build_provenance_input_role_defaults_to_layout(tmp_path):
    # The default is the field's pre-#2027 meaning ("the input layout stream
    # the run was made against"), so every layout-hashing caller keeps
    # emitting exactly what it emitted before the discriminator existed.
    layout_file = tmp_path / "top.gds"
    layout_file.write_bytes(b"gds bytes\n")

    prov = _provenance.build_provenance(input_path=str(layout_file))

    assert prov["input"]["role"] == _provenance.INPUT_ROLE_LAYOUT


def test_build_provenance_input_role_is_recorded_verbatim(tmp_path):
    netlist_file = tmp_path / "layout.spice"
    netlist_file.write_text("* pre-extracted\n", encoding="utf-8")

    prov = _provenance.build_provenance(
        input_path=str(netlist_file), input_role=_provenance.INPUT_ROLE_NETLIST
    )

    assert prov["input"]["role"] == "netlist"


def test_build_provenance_input_role_absent_when_no_input_pinned():
    # `role` describes a hash; with no hash there is nothing to describe, so
    # the whole block stays `None` rather than degrading to a role-only stub.
    assert _provenance.build_provenance(input_role="netlist")["input"] is None


def test_build_provenance_rejects_an_unknown_input_role(tmp_path):
    # A typo'd role must fail loudly here rather than silently reaching
    # `klt signoff`, where an unrecognised role becomes a group of one and
    # quietly exempts the verb from the cross-check it was meant to join.
    layout_file = tmp_path / "top.gds"
    layout_file.write_bytes(b"gds bytes\n")

    with pytest.raises(ValueError, match="unknown provenance input role"):
        _provenance.build_provenance(input_path=str(layout_file), input_role="laoyut")


# --------------------------------------------------------------------------- #
# _combined_content_hash
# --------------------------------------------------------------------------- #
#
# Shared by `equiv.py` and `synthesize.py` for their multi-source
# `provenance.input` case (issue #1112 -- the two previously carried
# independently-copied implementations that silently diverged: one mixed
# each path into its hash chunk, the other did not, so the same input files
# produced two different `content_hash` values depending on which verb ran).


def test_combined_content_hash_is_sha256_prefixed(tmp_path):
    a = tmp_path / "a.v"
    b = tmp_path / "b.v"
    a.write_bytes(b"module a; endmodule\n")
    b.write_bytes(b"module b; endmodule\n")

    result = _provenance._combined_content_hash([str(a), str(b)])
    assert result is not None
    assert result.startswith("sha256:")


def test_combined_content_hash_is_order_independent(tmp_path):
    a = tmp_path / "a.v"
    b = tmp_path / "b.v"
    a.write_bytes(b"module a; endmodule\n")
    b.write_bytes(b"module b; endmodule\n")

    forward = _provenance._combined_content_hash([str(a), str(b)])
    reverse = _provenance._combined_content_hash([str(b), str(a)])
    assert forward == reverse


def test_combined_content_hash_is_path_independent(tmp_path):
    # Same file *contents*, different paths (e.g. a copy in another
    # directory) hash identically -- the path-independent scheme this dedup
    # standardized on (previously only true for synthesize.py's copy).
    src_dir = tmp_path / "src"
    other_dir = tmp_path / "other"
    src_dir.mkdir()
    other_dir.mkdir()
    (src_dir / "top.v").write_bytes(b"module top; endmodule\n")
    (other_dir / "top_copy.v").write_bytes(b"module top; endmodule\n")

    original = _provenance._combined_content_hash([str(src_dir / "top.v")])
    copy = _provenance._combined_content_hash([str(other_dir / "top_copy.v")])
    assert original == copy


def test_combined_content_hash_none_when_any_file_unresolvable(tmp_path):
    existing = tmp_path / "a.v"
    existing.write_bytes(b"module a; endmodule\n")
    missing = tmp_path / "nope.v"

    assert _provenance._combined_content_hash([str(existing), str(missing)]) is None


# --------------------------------------------------------------------------- #
# layout_geometry_digest (#2065)
# --------------------------------------------------------------------------- #


def _write_probe_layout(path, *, width_dbu=1000, order=(0, 1), array=True):
    """Write a small hierarchical stream: a `SUB` cell instantiated into `TOP`
    (once plainly, once as a regular array) plus two rectangles, a path and a
    text in `TOP`.

    `order` controls the order the two rectangles are inserted in (the file's
    element order, which a writer is free to change without changing the
    geometry), `width_dbu` the width of the first rectangle (real geometry).
    """
    import klayout.db as kdb

    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    sub = layout.create_cell("SUB")
    metal = layout.layer(68, 20)
    poly = layout.layer(66, 20)

    boxes = [kdb.Box(0, 0, width_dbu, 500), kdb.Box(2000, 0, 3000, 500)]
    for index in order:
        top.shapes(metal).insert(boxes[index])
    top.shapes(poly).insert(kdb.Path([kdb.Point(0, 0), kdb.Point(1000, 0)], 100))
    top.shapes(poly).insert(kdb.Text("VDD", kdb.Trans(kdb.Vector(10, 20))))
    sub.shapes(metal).insert(kdb.Box(0, 0, 100, 100))
    top.insert(kdb.CellInstArray(sub.cell_index(), kdb.Trans(kdb.Vector(500, 500))))
    if array:
        top.insert(
            kdb.CellInstArray(
                sub.cell_index(),
                kdb.Trans(kdb.Vector(0, 0)),
                kdb.Vector(1000, 0),
                kdb.Vector(0, 1000),
                3,
                2,
            )
        )
    layout.write(str(path))
    return str(path)


def test_layout_geometry_digest_is_sha256_prefixed(tmp_path):
    path = _write_probe_layout(tmp_path / "a.gds")

    digest = _provenance.layout_geometry_digest(path)
    assert digest.startswith("sha256:")
    assert len(digest) == len("sha256:") + 64


def test_layout_geometry_digest_ignores_element_order_and_timestamps(tmp_path):
    # The reason this helper exists rather than reusing `sha256_file`: two
    # writes of the same geometry differ in raw bytes (BGNLIB/BGNSTR write
    # timestamps, element order) but are the same layout.
    first = _write_probe_layout(tmp_path / "a.gds", order=(0, 1))
    second = _write_probe_layout(tmp_path / "b.gds", order=(1, 0))

    assert _provenance.sha256_file(first) != _provenance.sha256_file(second)
    assert _provenance.layout_geometry_digest(
        first
    ) == _provenance.layout_geometry_digest(second)


def test_layout_geometry_digest_tracks_real_geometry_change(tmp_path):
    # ...and equality still has to mean something: one rectangle 1nm wider is
    # genuine drift.
    first = _write_probe_layout(tmp_path / "a.gds", width_dbu=1000)
    second = _write_probe_layout(tmp_path / "b.gds", width_dbu=1001)

    assert _provenance.layout_geometry_digest(
        first
    ) != _provenance.layout_geometry_digest(second)


def test_layout_geometry_digest_tracks_hierarchy_change(tmp_path):
    # A dropped child-cell array is drift too -- instances are part of the
    # geometry, not container metadata.
    with_array = _write_probe_layout(tmp_path / "a.gds", array=True)
    without_array = _write_probe_layout(tmp_path / "b.gds", array=False)

    assert _provenance.layout_geometry_digest(
        with_array
    ) != _provenance.layout_geometry_digest(without_array)


def test_layout_geometry_digest_is_container_format_independent(tmp_path):
    # The digest covers decoded geometry, so the same layout written as GDS
    # and as OASIS hashes identically -- container metadata is never read.
    gds = _write_probe_layout(tmp_path / "a.gds")
    oas = _write_probe_layout(tmp_path / "a.oas")

    assert _provenance.sha256_file(gds) != _provenance.sha256_file(oas)
    assert _provenance.layout_geometry_digest(
        gds
    ) == _provenance.layout_geometry_digest(oas)


def test_layout_geometry_digest_none_for_missing_unreadable_or_empty_path(tmp_path):
    # Never fabricated -- the same `None` convention every other unresolvable
    # value in this module uses.
    assert _provenance.layout_geometry_digest(None) is None
    assert _provenance.layout_geometry_digest("") is None
    assert _provenance.layout_geometry_digest(str(tmp_path / "nope.gds")) is None

    garbage = tmp_path / "garbage.gds"
    garbage.write_bytes(b"this is not a layout stream")
    assert _provenance.layout_geometry_digest(str(garbage)) is None


# --------------------------------------------------------------------------- #
# _yosys_version
# --------------------------------------------------------------------------- #


def test_yosys_version_returns_none_when_binary_missing(monkeypatch):
    def _fake_run(*_args, **_kwargs):
        raise FileNotFoundError("no such file: yosys")

    monkeypatch.setattr(_provenance.subprocess, "run", _fake_run)
    assert _provenance._yosys_version() is None


def test_yosys_version_returns_none_on_timeout(monkeypatch):
    def _fake_run(*_args, **_kwargs):
        raise _provenance.subprocess.TimeoutExpired(cmd="yosys", timeout=10)

    monkeypatch.setattr(_provenance.subprocess, "run", _fake_run)
    assert _provenance._yosys_version() is None


def test_yosys_version_returns_none_on_nonzero_returncode(monkeypatch):
    class _FakeCompleted:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(_provenance.subprocess, "run", lambda *a, **k: _FakeCompleted())
    assert _provenance._yosys_version() is None


def test_yosys_version_parses_version_token(monkeypatch):
    class _FakeCompleted:
        returncode = 0
        stdout = "Yosys 0.68+48 (git sha1 abc123)\n"

    monkeypatch.setattr(_provenance.subprocess, "run", lambda *a, **k: _FakeCompleted())
    assert _provenance._yosys_version() == "0.68+48"


# --------------------------------------------------------------------------- #
# deck_source_path
# --------------------------------------------------------------------------- #


def test_deck_source_path_resolves_known_decks():
    for name in ("sky130", "gf180mcu"):
        source = deck_source_path(name)
        assert source is not None
        assert source.endswith(f"{name}.py")


def test_deck_source_path_none_for_unknown_deck():
    assert deck_source_path("not-a-real-deck") is None


def test_deck_source_path_feeds_a_stable_content_hash():
    # The resolved deck source hashes to a stable, sha256-prefixed digest --
    # the reproducibility anchor the whole feature exists for.
    source = deck_source_path("sky130")
    prov = _provenance.build_provenance(deck_name="sky130", deck_path=source)
    assert prov["deck"]["content_hash"].startswith("sha256:")
    assert len(prov["deck"]["content_hash"]) == len("sha256:") + 64
