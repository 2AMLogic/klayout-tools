"""Tier and fleet signoff reporting for a block with a pinned upstream source."""

import pytest

from klayout_tools.cli import main
from klayout_tools.signoff import (
    SignoffError,
    build_fleet_report,
    build_tier_report,
    check_fleet_report,
    check_tier_report,
)
from test_signoff import (
    DRC_CLEAN_ENVELOPE,
    _fleet_block_manifest,
    _full_t1_evidence,
    _manifest,
    _write,
)

pytestmark = pytest.mark.usefixtures("real_build_identity_git")


_UPSTREAM_REPOSITORY = "https://github.com/2AMLogic/example-blocks"
_UPSTREAM_COMMIT = "0123456789abcdef0123456789abcdef01234567"


def _pinned_manifest(**overrides) -> dict:
    manifest = _manifest(
        upstream_source={
            "repository": _UPSTREAM_REPOSITORY,
            "commit": _UPSTREAM_COMMIT,
        }
    )
    manifest.update(overrides)
    return manifest


def test_upstream_source_echoes_once_on_the_tier_report_for_every_kind():
    """The declaration travels with the report, once at the top level -- it
    describes the design sources of the whole block, so it belongs to no
    item row (unlike a partition boundary, which qualifies the rows of one
    partition) and applies to every block kind."""
    for kind in ("analog", "digital", "mixed-signal"):
        result = build_tier_report(_pinned_manifest(kind=kind))

        assert result["upstream_source"] == {
            "repository": _UPSTREAM_REPOSITORY,
            "commit": _UPSTREAM_COMMIT,
        }
        assert all("upstream_source" not in item for item in result["items"])


@pytest.mark.parametrize("kind", ["analog", "digital", "mixed-signal"])
def test_upstream_source_is_absent_when_the_manifest_declares_none(kind):
    """Additive, not a shape change: a manifest that declares nothing
    renders exactly the report it rendered before this field existed, so an
    already-committed report does not read as drifted under `--check` on an
    upgrade alone."""
    result = build_tier_report(_manifest(kind=kind))

    assert "upstream_source" not in result


def test_upstream_source_null_reads_as_undeclared():
    """An explicit JSON `null` is "declared nothing", the same as omitting
    the key -- not a malformed declaration."""
    result = build_tier_report(_manifest(upstream_source=None))

    assert "upstream_source" not in result


def test_upstream_source_changes_no_verdict(tmp_path):
    """Reported, never graded: declaring a pin (or not) moves no item's
    status, no count, and no tier -- the two reports differ in exactly the
    declared field and nothing else."""
    drc_path = _write(tmp_path, "drc.json", DRC_CLEAN_ENVELOPE)
    undeclared = build_tier_report(_manifest(evidence={"3": drc_path}))
    declared = build_tier_report(_pinned_manifest(evidence={"3": drc_path}))

    assert declared["t1_met_count"] == undeclared["t1_met_count"] > 0
    assert declared["tier"] == undeclared["tier"]
    stripped = {
        key: value for key, value in declared.items() if key != "upstream_source"
    }
    assert stripped == undeclared


def test_upstream_source_normalizes_the_commit_to_lowercase():
    """Git accepts either case on input and prints lowercase; the report
    normalizes the same way, so two manifests naming the same commit in
    different case render the same report bytes."""
    result = build_tier_report(
        _manifest(
            upstream_source={
                "repository": _UPSTREAM_REPOSITORY,
                "commit": _UPSTREAM_COMMIT.upper(),
            }
        )
    )

    assert result["upstream_source"] == {
        "repository": _UPSTREAM_REPOSITORY,
        "commit": _UPSTREAM_COMMIT,
    }


def test_upstream_source_accepts_a_sha256_commit_and_lowercases_it():
    """A SHA-256 repository names objects with 64 hex digits, not 40; the
    documented width is accepted and normalized the same way as the 40-digit
    one. The input is mixed-case so the same assertion also covers the
    normalization path on the wider commit."""
    sha256_commit = "0123456789abcdef" * 4
    mixed_case = sha256_commit[:32].upper() + sha256_commit[32:]
    assert len(sha256_commit) == 64
    assert mixed_case not in (sha256_commit, sha256_commit.upper())

    result = build_tier_report(
        _manifest(
            upstream_source={
                "repository": _UPSTREAM_REPOSITORY,
                "commit": mixed_case,
            }
        )
    )

    assert result["upstream_source"] == {
        "repository": _UPSTREAM_REPOSITORY,
        "commit": sha256_commit,
    }


@pytest.mark.parametrize(
    "raw",
    [
        _UPSTREAM_REPOSITORY,
        [_UPSTREAM_REPOSITORY, _UPSTREAM_COMMIT],
        {},
        {"repository": _UPSTREAM_REPOSITORY},
        {"commit": _UPSTREAM_COMMIT},
        {"repository": "", "commit": _UPSTREAM_COMMIT},
        {"repository": "   ", "commit": _UPSTREAM_COMMIT},
        {"repository": 17, "commit": _UPSTREAM_COMMIT},
        {"repository": _UPSTREAM_REPOSITORY, "commit": "main"},
        {"repository": _UPSTREAM_REPOSITORY, "commit": "v1.2.3"},
        {"repository": _UPSTREAM_REPOSITORY, "commit": "0123456"},
        {"repository": _UPSTREAM_REPOSITORY, "commit": "0" * 41},
        {"repository": _UPSTREAM_REPOSITORY, "commit": "z" * 40},
        {"repository": _UPSTREAM_REPOSITORY, "commit": ""},
        {"repository": _UPSTREAM_REPOSITORY, "commit": None},
        {
            "repository": _UPSTREAM_REPOSITORY,
            "commit": _UPSTREAM_COMMIT,
            "ref": "main",
        },
    ],
)
def test_upstream_source_rejects_a_malformed_declaration(raw):
    """An unpronounceable pin is refused, never accepted-and-ignored: a
    declaration the report silently drops is worse than one never made,
    because the manifest looks like it named its upstream. A branch or tag
    in place of a full commit ID is a moving reference, not a pin, and is
    refused the same way."""
    with pytest.raises(SignoffError) as excinfo:
        build_tier_report(_manifest(upstream_source=raw))

    assert "upstream_source" in str(excinfo.value)


@pytest.mark.parametrize(
    ("raw", "member"),
    [
        ({"repository": _UPSTREAM_REPOSITORY}, "commit"),
        ({"commit": _UPSTREAM_COMMIT}, "repository"),
        ({"repository": "", "commit": _UPSTREAM_COMMIT}, "repository"),
        ({"repository": _UPSTREAM_REPOSITORY, "commit": "main"}, "commit"),
        (
            {
                "repository": _UPSTREAM_REPOSITORY,
                "commit": _UPSTREAM_COMMIT,
                "rev": "main",
            },
            "rev",
        ),
    ],
)
def test_upstream_source_error_names_the_offending_member(raw, member):
    """The refusal names `upstream_source.<member>` for the member that was
    wrong, so the manifest's author can fix it without reading the schema."""
    with pytest.raises(SignoffError) as excinfo:
        build_tier_report(_manifest(upstream_source=raw))

    assert f"upstream_source.{member}" in str(excinfo.value)


def test_fleet_rows_carry_the_declared_upstream_source(tmp_path):
    """The roll-up row carries a block's declared pin beside its `source`
    locator: `source` stays the local manifest path (or `null` for an inline
    manifest) -- the locator for the pin record -- while `upstream_source`
    names the repository and commit the design sources were consumed at.
    An unpinned block in the same mixed fleet carries no key at all."""
    manifest_path = _write(
        tmp_path, "pinned.json", _pinned_manifest(block="pinned-file")
    )
    fleet = {
        "blocks": [
            manifest_path,
            _pinned_manifest(block="pinned-inline"),
            _fleet_block_manifest("unpinned"),
        ]
    }

    result = build_fleet_report(fleet)

    by_name = {block["block"]: block for block in result["blocks"]}
    pin = {"repository": _UPSTREAM_REPOSITORY, "commit": _UPSTREAM_COMMIT}
    assert by_name["pinned-file"]["source"] == manifest_path
    assert by_name["pinned-file"]["upstream_source"] == pin
    assert by_name["pinned-inline"]["source"] is None
    assert by_name["pinned-inline"]["upstream_source"] == pin
    assert by_name["unpinned"]["source"] is None
    assert "upstream_source" not in by_name["unpinned"]


def test_check_matches_an_unchanged_upstream_source(tmp_path):
    """Each check mode returns `match` for the recorded declaration: the pin
    is part of the claim the committed report records, and an unchanged one
    reproduces exactly like any other manifest-derived field."""
    committed_path = _write(
        tmp_path, "committed.json", build_tier_report(_pinned_manifest())
    )

    assert check_tier_report(committed_path, _pinned_manifest())["status"] == "match"


def test_check_matches_a_pre_feature_report_against_a_manifest_omitting_the_pin(
    tmp_path,
):
    """A report committed before the field existed matches a manifest that
    still omits it -- the additive rule: no declaration, no key, no drift."""
    committed_path = _write(tmp_path, "committed.json", build_tier_report(_manifest()))

    assert check_tier_report(committed_path, _manifest())["status"] == "match"


@pytest.mark.parametrize(
    "changed",
    [
        {"commit": "fedcba9876543210fedcba9876543210fedcba98"},
        {"repository": "https://github.com/2AMLogic/other-blocks"},
    ],
)
def test_check_reports_drift_when_the_declared_pin_changed(tmp_path, changed):
    """The staleness story: with identical evidence, changing only the
    declared commit or repository makes the old report `drifted` and names
    the changed field -- a report graded against one upstream revision is
    told apart from one graded against another."""
    committed_path = _write(
        tmp_path, "committed.json", build_tier_report(_pinned_manifest())
    )
    drifted_manifest = _pinned_manifest(
        upstream_source={
            "repository": _UPSTREAM_REPOSITORY,
            "commit": _UPSTREAM_COMMIT,
        }
        | changed
    )

    result = check_tier_report(committed_path, drifted_manifest)

    assert result["status"] == "drifted"
    assert {entry["field"] for entry in result["drift"]} == {
        f"upstream_source.{next(iter(changed))}"
    }


def test_fleet_check_reports_drift_when_the_declared_pin_changed(tmp_path):
    """The same staleness story one level up: a committed fleet roll-up
    whose block's pin moved renders `drifted` naming the row's field."""
    fleet = {"blocks": [_pinned_manifest(block="pinned")]}
    committed_path = _write(tmp_path, "committed.json", build_fleet_report(fleet))
    drifted_fleet = {
        "blocks": [
            _pinned_manifest(
                block="pinned",
                upstream_source={
                    "repository": _UPSTREAM_REPOSITORY,
                    "commit": "fedcba9876543210fedcba9876543210fedcba98",
                },
            )
        ]
    }

    result = check_fleet_report(committed_path, drifted_fleet)

    assert result["status"] == "drifted"
    assert {entry["field"] for entry in result["drift"]} == {
        "blocks.0.upstream_source.commit"
    }


def test_cli_manifest_text_states_the_declared_upstream_source(tmp_path, capsys):
    """The terminal rendering a reviewer actually reads names the repository
    and the full commit under the `block:`/`kind:` header, once -- so the
    text report alone answers "where do this block's sources live"."""
    manifest_path = _write(tmp_path, "manifest.json", _pinned_manifest())

    main(["signoff", "--manifest", manifest_path, "--format", "text"])

    out = capsys.readouterr().out
    assert f"upstream source: {_UPSTREAM_REPOSITORY} @ {_UPSTREAM_COMMIT}" in out
    assert "kind: analog" in out


def test_cli_manifest_text_omits_the_upstream_source_when_none_declared(
    tmp_path, capsys
):
    """An undeclared pin prints no line at all -- an absent statement must
    not read as "the sources live in this repo"."""
    manifest_path = _write(tmp_path, "manifest.json", _manifest())

    main(["signoff", "--manifest", manifest_path, "--format", "text"])

    out = capsys.readouterr().out
    assert "upstream source:" not in out


def test_cli_fleet_text_states_the_declared_upstream_source_per_row(tmp_path, capsys):
    """Each pinned row of the text roll-up names its upstream repository and
    full commit beside the block it qualifies; an unpinned row in the same
    fleet states no upstream line at all."""
    manifest_path = _write(
        tmp_path, "pinned.json", _pinned_manifest(block="pinned-file")
    )
    fleet_path = _write(
        tmp_path,
        "fleet.json",
        {
            "blocks": [
                manifest_path,
                _pinned_manifest(block="pinned-inline"),
                _fleet_block_manifest("unpinned"),
            ]
        },
    )

    main(["signoff", "--fleet", fleet_path, "--format", "text"])

    out = capsys.readouterr().out
    assert out.count(f"upstream: {_UPSTREAM_REPOSITORY} @ {_UPSTREAM_COMMIT}") == 2
    assert "[not-T1] unpinned" in out


def test_cli_fleet_text_identifies_a_fully_graded_upstream_block(tmp_path, capsys):
    block = _pinned_manifest(block="upstream-t1", evidence=_full_t1_evidence(tmp_path))
    fleet = _write(tmp_path, "fleet.json", {"blocks": [block]})

    assert main(["signoff", "--fleet", fleet, "--format", "text"]) == 0

    text = capsys.readouterr().out
    assert "1/1 blocks at T1" in text
    assert "upstream-t1" in text
    assert f"upstream: {_UPSTREAM_REPOSITORY} @ {_UPSTREAM_COMMIT}" in text
    assert "blocking:" not in text
    assert "ungraded (no klt verb, uncited)" not in text
