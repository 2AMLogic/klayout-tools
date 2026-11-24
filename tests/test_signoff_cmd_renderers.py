"""Text renderers and refusal paths of ``klt signoff`` (``cli/signoff_cmd.py``).

Issue #6850 moved the 153-line ``--attest`` block out of ``signoff_cmd.py``.
That removed 32 fully covered branches from the module while leaving its 19
uncovered ones in place, so the per-module branch-coverage ratchet
(``coverage-baseline.json``) read the extraction as a regression. These tests
close part of that gap on the code that was left behind, rather than lowering
the floor.

Each case drives one renderer (or refusal) with a minimal synthetic payload of
the shape the grader hands it, and asserts the rendered text -- they pin
output, they do not merely execute it.
"""

from __future__ import annotations

import json

from klayout_tools.cli import main
from klayout_tools.cli.color import PLAIN
from klayout_tools.cli.signoff_cmd import (
    _print_artifact_binding,
    _print_binding_members,
    _print_coverage_undisclosed,
    _print_describe_grader_text,
    _print_fleet_report_text,
    _print_foundry_cell_exceptions,
    _print_input_verified,
    _print_regeneration,
    _print_text,
)

HASH = "sha256:" + "b" * 64


def _out(capsys) -> list[str]:
    return capsys.readouterr().out.splitlines()


# --------------------------------------------------------------------------- #
# --describe-grader
# --------------------------------------------------------------------------- #


def _describe(item_ids):
    return {
        "version": "9.9.9",
        "grading_ruleset_id": "ruleset-x",
        "graded_t1_item_ids": item_ids,
        "source_doc": "docs/design-evidence-tiers.md",
        "opt_in_evidence_kinds": {},
        "pinned_runsets": {},
    }


def test_describe_grader_unreadable_doc_says_unknown_not_empty(capsys):
    """``graded_t1_item_ids: null`` means "this build could not read its own
    doc" and must not render as an empty list."""
    _print_describe_grader_text(_describe(None))

    lines = _out(capsys)
    assert (
        "graded T1 items: unknown (docs/design-evidence-tiers.md could not be "
        "read by this build)"
    ) in lines
    assert not any(line.startswith("graded T1 items (") for line in lines)


def test_describe_grader_lists_ids_when_readable(capsys):
    _print_describe_grader_text(_describe([1, 2, 3]))

    assert "graded T1 items (docs/design-evidence-tiers.md): 1, 2, 3" in _out(capsys)


# --------------------------------------------------------------------------- #
# envelope aggregation: provenance mismatches
# --------------------------------------------------------------------------- #


def _aggregate(*, ok: bool, mismatches=()):
    return {
        "status": "fail" if not ok else "pass",
        "passed_count": 0,
        "check_count": 0,
        "provenance_consistency": {"ok": ok, "mismatches": list(mismatches)},
        "checks": [],
    }


def test_text_report_without_mismatch_has_no_provenance_block(capsys):
    _print_text(_aggregate(ok=True))

    assert "provenance mismatches (refusing to aggregate):" not in _out(capsys)


def test_text_report_names_each_mismatch_and_every_source(capsys):
    _print_text(
        _aggregate(
            ok=False,
            mismatches=[
                {
                    "field": "pdk.commit",
                    "values": [
                        {"source": "drc.json", "value": "aaa"},
                        {"source": "lvs.json", "value": "bbb"},
                    ],
                }
            ],
        )
    )

    lines = _out(capsys)
    assert "provenance mismatches (refusing to aggregate):" in lines
    assert "  pdk.commit:" in lines
    assert "    drc.json: aaa" in lines
    assert "    lvs.json: bbb" in lines


def test_text_report_qualifies_a_role_scoped_mismatch(capsys):
    """Issue #2027: two ``input.content_hash`` entries differ per role, so the
    heading carries the role rather than printing the same line twice."""
    _print_text(
        _aggregate(
            ok=False,
            mismatches=[
                {
                    "field": "input.content_hash",
                    "role": "schematic",
                    "values": [{"source": "pex.json", "value": "x"}],
                },
                {
                    "field": "input.content_hash",
                    "role": "layout",
                    "values": [{"source": "pex.json", "value": "y"}],
                },
            ],
        )
    )

    lines = _out(capsys)
    assert "  input.content_hash (role: schematic):" in lines
    assert "  input.content_hash (role: layout):" in lines


# --------------------------------------------------------------------------- #
# per-item citation lines
# --------------------------------------------------------------------------- #


def test_input_verified_lists_each_pex_input_role(capsys):
    """Issue #2875: the per-role schematic-side verification of a ``pex`` run
    renders one line per input beneath the ``input:`` statement."""
    _print_input_verified(
        {
            "content_hash": HASH,
            "input_verified": True,
            "pex_inputs": [
                {"role": "schematic", "status": "match"},
                {"role": "layout", "status": "drifted"},
            ],
        }
    )

    lines = _out(capsys)
    assert "        pex input schematic: match" in lines
    assert "        pex input layout: drifted" in lines


def test_input_verified_without_a_statement_prints_nothing(capsys):
    _print_input_verified({"pex_inputs": [{"role": "schematic", "status": "match"}]})

    assert _out(capsys) == []


def test_binding_members_unwrap_a_path_object(capsys):
    """A member's ``path`` may be a bare string or a ``{"path": ...}`` object;
    both name the member the same way on the row."""
    _print_binding_members(
        [
            {"path": "gen/a.json", "status": "match"},
            {"path": {"path": "gen/b.json"}, "status": "drifted"},
        ]
    )

    assert _out(capsys) == [
        "        member gen/a.json: match",
        "        member gen/b.json: drifted",
    ]


def test_binding_members_none_prints_nothing(capsys):
    _print_binding_members(None)

    assert _out(capsys) == []


def _bound(**binding):
    return (
        {"tier": "T1", "id": 1},
        {"kind": "generic", "artifact_binding": {"t1_item": 1, **binding}},
    )


def test_artifact_binding_unwraps_a_path_object(capsys):
    item, citation = _bound(path={"path": "gen/out.json"}, content_hash=HASH)

    _print_artifact_binding(item, citation)

    assert f"        bound: T1 item #1 -> gen/out.json (content_hash={HASH})" in _out(
        capsys
    )


def test_artifact_binding_set_reports_member_count_and_each_member(capsys):
    item, citation = _bound(
        members=[
            {"path": "gen/a.json", "status": "match"},
            {"path": {"path": "gen/b.json"}, "status": "match"},
        ],
        content_hash=HASH,
    )

    _print_artifact_binding(item, citation)

    lines = _out(capsys)
    assert f"        bound: T1 item #1 -> 2 members (content_hash={HASH})" in lines
    assert "        member gen/a.json: match" in lines
    assert "        member gen/b.json: match" in lines


def test_artifact_binding_not_bound_says_relevance_is_unjudged(capsys):
    _print_artifact_binding(
        {"tier": "T1", "id": 9}, {"kind": "drc", "artifact_binding": None}
    )

    (line,) = _out(capsys)
    assert line.startswith("        topic: not bound -- a passing drc envelope")


def test_artifact_binding_ignores_items_outside_the_anchored_set(capsys):
    _print_artifact_binding({"tier": "T1", "id": 3}, {"kind": "drc"})

    assert _out(capsys) == []


def test_regeneration_not_claimed(capsys):
    _print_regeneration(None)

    assert _out(capsys) == ["        regeneration: not claimed"]


def test_regeneration_executed_by_klt_names_recipe_and_job(capsys):
    """Issue #2887: a recipe klt itself ran on the batch runner is stated as
    such, with its job id -- distinct from an attested, un-run generator."""
    _print_regeneration(
        {
            "executed_by_klt": True,
            "execution": {
                "recipe": "gen-ota",
                "remote": {"job_id": "job-42"},
                "output_hash": HASH,
            },
        }
    )

    (line,) = _out(capsys)
    assert "klt ran approved recipe `gen-ota` on the batch runner (job job-42)" in line
    assert f"output {HASH} is byte-identical" in line


def test_regeneration_attested_generator_is_not_claimed_as_run(capsys):
    _print_regeneration(
        {"executed_by_klt": False, "regenerated_hash": HASH, "generator": "gen.py"}
    )

    (line,) = _out(capsys)
    assert "generator `gen.py` was attested, not run by klt" in line


def test_foundry_cell_exceptions_render_each_rule_count(capsys):
    _print_foundry_cell_exceptions(
        {
            "foundry_cell_exceptions": [
                {
                    "cell": "sky130_fd_sc_hd__inv_1",
                    "cell_hash": HASH,
                    "rules": {"m1.2": 3, "li.1": 1},
                }
            ]
        }
    )

    assert _out(capsys) == [
        "        foundry-cell exception: sky130_fd_sc_hd__inv_1 "
        f"({HASH}): m1.2=3, li.1=1"
    ]


def test_foundry_cell_exceptions_absent_prints_nothing(capsys):
    _print_foundry_cell_exceptions({})

    assert _out(capsys) == []


# --------------------------------------------------------------------------- #
# item 3 coverage-undisclosed refusal
# --------------------------------------------------------------------------- #


def test_coverage_undisclosed_without_cited_evidence_names_only_the_refusal(capsys):
    """No ``cited_evidence``: there is no ``cite:`` or ``coverage:`` line to
    print, but the refusal itself still must be."""
    _print_coverage_undisclosed(
        {"detail": {"coverage_undisclosed": {"reason": "no_coverage_block"}}}
    )

    assert _out(capsys) == [
        "        coverage undisclosed: cited evidence reports no coverage block"
    ]


def test_coverage_undisclosed_cited_evidence_without_coverage_skips_that_line(
    capsys,
):
    cited = {
        "file": "block/drc.json",
        "command": None,
        "kind": "drc",
        "check_status": "clean",
        "content_hash": HASH,
        "exit_status": 0,
    }
    _print_coverage_undisclosed(
        {
            "detail": {
                "coverage_undisclosed": {"layers": {"missing": ["met5"]}},
                "cited_evidence": cited,
            }
        }
    )

    lines = _out(capsys)
    assert lines[0].startswith("        cite: block/drc.json (kind=drc")
    assert not any("        coverage: " in line for line in lines)
    assert lines[-1] == "        coverage undisclosed: layers: missing met5"


def test_coverage_undisclosed_absent_prints_nothing(capsys):
    _print_coverage_undisclosed({"detail": {}})
    _print_coverage_undisclosed({})

    assert _out(capsys) == []


# --------------------------------------------------------------------------- #
# --fleet text roll-up
# --------------------------------------------------------------------------- #


def _fleet_block(**overrides):
    block = {
        "block": "ota",
        "kind": "analog",
        "tier": "not-T1",
        "t1_met_count": 3,
        "t1_item_count": 11,
        "build_t1_item_count": 11,
        "blocking_item": None,
        "ungraded_items": [],
        "drc_coverage": [],
    }
    block.update(overrides)
    return block


def _fleet(*blocks):
    return {
        "t1_count": 0,
        "block_count": len(blocks),
        "not_t1_count": len(blocks),
        "blocks": list(blocks),
        "source_doc": "docs/design-evidence-tiers.md",
        "source_doc_content_hash": HASH,
        "build": {"version": "9.9.9"},
    }


def test_fleet_text_block_without_blocker_or_ungraded_items_is_one_line(capsys):
    _print_fleet_report_text(_fleet(_fleet_block()), PLAIN)

    lines = _out(capsys)
    assert any(line.startswith("[not-T1]") and "ota" in line for line in lines)
    assert not any("blocking:" in line for line in lines)
    assert not any("ungraded (no klt verb, uncited)" in line for line in lines)
    assert f"source: docs/design-evidence-tiers.md (content_hash={HASH})" in lines
    assert "build: klt 9.9.9" in lines


def test_fleet_text_names_the_blocking_item_and_its_partition(capsys):
    block = _fleet_block(
        blocking_item={
            "id": 4,
            "partition": "analog",
            "title": "DRC clean",
            "reason": "no evidence",
        }
    )

    _print_fleet_report_text(_fleet(block), PLAIN)

    lines = _out(capsys)
    assert "        blocking: #4 [analog] DRC clean (reason: no evidence)" in lines


def test_fleet_text_blocking_item_without_partition_omits_the_brackets(capsys):
    block = _fleet_block(
        blocking_item={
            "id": 4,
            "partition": None,
            "title": "DRC clean",
            "reason": "no evidence",
        }
    )

    _print_fleet_report_text(_fleet(block), PLAIN)

    assert "        blocking: #4 DRC clean (reason: no evidence)" in _out(capsys)


def test_fleet_text_lists_ungraded_items_with_optional_partition(capsys):
    block = _fleet_block(
        ungraded_items=[
            {"id": 1, "partition": "analog"},
            {"id": 2, "partition": None},
        ]
    )

    _print_fleet_report_text(_fleet(block), PLAIN)

    assert "        ungraded (no klt verb, uncited): #1 [analog], #2" in _out(capsys)


# --------------------------------------------------------------------------- #
# CLI refusals that precede grading
# --------------------------------------------------------------------------- #


def _error(capsys) -> dict:
    return json.loads(capsys.readouterr().err)


def test_artifact_manifest_without_manifest_or_fleet_is_refused(capsys):
    """``--artifact-manifest`` resolves citations, which envelope aggregation
    has none of -- refused up front, before any file is read."""
    exit_code = main(
        ["signoff", "--artifact-manifest", "nope.json", "--format", "json"]
    )

    assert exit_code != 0
    assert "--artifact-manifest only applies to --manifest/--fleet" in (
        json.dumps(_error(capsys))
    )


def test_fleet_manifest_must_be_a_json_object(tmp_path, capsys):
    fleet = tmp_path / "fleet.json"
    fleet.write_text("[]")

    exit_code = main(["signoff", "--fleet", str(fleet), "--format", "json"])

    assert exit_code != 0
    assert "must be a JSON object, got list" in json.dumps(_error(capsys))
