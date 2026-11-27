"""`klt signoff --manifest` re-hashes a `klt sim` citation's recorded
`.include` closure (issue #7860).

`provenance.input.content_hash` of a sim report digests the top deck only, so
a wrapper whose included sub-netlist changed still matched its own pin and the
citation read `input_verified: true`. The closure (`environment.netlist_closure`,
#2799) was already recorded; signoff now re-hashes it.

`input_verified` is disclosure-only for native kinds (docs/cli/signoff.md,
"No verdict changes"), so every drift case asserts BOTH halves of the
contract: the item stays `met` with `reason: None`, and the disclosure says
otherwise. Reports are hand-written JSON over files in `tmp_path` -- no engine.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from klayout_tools.signoff import build_tier_report
from klayout_tools.signoff_sim_closure import check_sim_closure
from test_signoff import (
    DRC_CLEAN_ENVELOPE,
    SIM_PASS_ENVELOPE,
    _hash_of,
    _item,
    _manifest,
    _write,
)
from test_signoff_input_index_basis import _git, _git_checkout

DECK = ".include sub.spice\nV1 in 0 1.8\nXD in out divider\nRL out 0 1e6\n"
SUB = ".subckt divider in out\nR1 in out 1k\nR2 out 0 1k\n.ends\n"


def _hex(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _entry(root: Path, name: str) -> dict:
    return {"path": name, "scope": "repo", "sha256": _hex(root / name)}


def _circuit(root: Path) -> None:
    (root / "deck.spice").write_text(DECK)
    (root / "sub.spice").write_text(SUB)


def _sim_report(root: Path, *, closure="recorded", **environment) -> str:
    """A passing sim report pinning `deck.spice`; `closure` is `"recorded"`
    (deck + sub.spice as they are now), `None` (a pre-#2799 report) or an
    explicit `netlist_closure` list."""
    if closure == "recorded":
        closure = [_entry(root, "deck.spice"), _entry(root, "sub.spice")]
    env = dict(environment)
    if closure is not None:
        env["netlist_closure"] = closure
    envelope = {
        **SIM_PASS_ENVELOPE,
        "netlist": {"path": "deck.spice", "scope": "repo"},
        "environment": env,
        "provenance": {
            **SIM_PASS_ENVELOPE["provenance"],
            "input": {"content_hash": _hash_of(root / "deck.spice"), "role": "netlist"},
        },
    }
    return _write(root, "sim.json", envelope)


def _grade(report: str, root: Path, **kwargs) -> dict:
    manifest = _manifest(evidence={"5": report})
    return _item(build_tier_report(manifest, repo_root=root, **kwargs), 5)


def _cited(item: dict) -> dict:
    assert item["status"] == "met" and item["reason"] is None, item
    return item["citation"]


def test_intact_closure_grades_as_before_and_says_so(tmp_path):
    _circuit(tmp_path)
    citation = _cited(_grade(_sim_report(tmp_path), tmp_path))

    assert citation["input_verified"] is True
    assert citation["sim_closure"] == {
        "status": "verified",
        "includes": 1,
        "stale": [],
        "unverifiable": [],
    }


def test_changed_include_with_unchanged_top_deck_is_not_verified(tmp_path):
    """The issue's failure mode: only `sub.spice` moves (R2 1k -> 2k)."""
    _circuit(tmp_path)
    report = _sim_report(tmp_path)
    recorded = _hex(tmp_path / "sub.spice")
    (tmp_path / "sub.spice").write_text(SUB.replace("R2 out 0 1k", "R2 out 0 2k"))

    item = _grade(report, tmp_path)

    citation = _cited(item)  # disclosure-only: still met, no reason
    assert citation["input_verified"] is False
    block = citation["sim_closure"]
    assert block["status"] == "stale"
    assert block["stale"] == [
        {
            "path": "sub.spice",
            "recorded": recorded,
            "actual": _hex(tmp_path / "sub.spice"),
        }
    ]
    assert block["unverifiable"] == []


def test_top_deck_only_edit_keeps_the_existing_behaviour(tmp_path):
    """The maintainer's control: a comment on the wrapper alone flips
    `input_verified` via the top-deck check; the includes are intact, so the
    closure block does not blame them."""
    _circuit(tmp_path)
    report = _sim_report(tmp_path)
    (tmp_path / "deck.spice").write_text(DECK + "* later comment\n")

    citation = _cited(_grade(report, tmp_path))

    assert citation["input_verified"] is False
    assert citation["sim_closure"]["status"] == "verified"


def test_comment_only_edit_in_an_include_reads_stale_like_the_top_deck(tmp_path):
    _circuit(tmp_path)
    report = _sim_report(tmp_path)
    (tmp_path / "sub.spice").write_text(SUB + "* tidy\n")

    citation = _cited(_grade(report, tmp_path))

    assert citation["input_verified"] is False
    assert [row["path"] for row in citation["sim_closure"]["stale"]] == ["sub.spice"]


def test_report_without_a_closure_degrades_to_top_deck_only_and_says_so(tmp_path):
    _circuit(tmp_path)
    citation = _cited(_grade(_sim_report(tmp_path, closure=None), tmp_path))

    assert citation["input_verified"] is True
    assert citation["sim_closure"] == {
        "status": "not_recorded",
        "reason": "no_netlist_closure",
    }


def test_closure_error_is_named_without_echoing_its_free_text(tmp_path):
    _circuit(tmp_path)
    report = _sim_report(
        tmp_path, closure=None, netlist_closure_error="missing include /home/x/y.spice"
    )

    block = _cited(_grade(report, tmp_path))["sim_closure"]

    assert block == {"status": "not_recorded", "reason": "netlist_closure_error"}


def test_host_resolved_include_is_disclosed_not_verified_and_not_an_error(tmp_path):
    _circuit(tmp_path)
    closure = [
        _entry(tmp_path, "deck.spice"),
        _entry(tmp_path, "sub.spice"),
        {
            "target": "$PDK_ROOT/models.spice",
            "sha256": None,
            "unhashed_reason": "env_var",
        },
    ]
    citation = _cited(_grade(_sim_report(tmp_path, closure=closure), tmp_path))

    assert citation["input_verified"] is True  # top deck, unchanged from before
    assert citation["sim_closure"]["status"] == "partial"
    assert citation["sim_closure"]["unverifiable"] == [
        {"path": "$PDK_ROOT/models.spice", "reason": "env_var"}
    ]


def test_deleted_include_is_unverifiable_never_false(tmp_path):
    """`input_verified: false` requires genuinely reading a named file and
    finding it different (`_verify_input_artifact`); a file that is gone from
    this grading context was not read."""
    _circuit(tmp_path)
    report = _sim_report(tmp_path)
    (tmp_path / "sub.spice").unlink()

    citation = _cited(_grade(report, tmp_path))

    assert citation["input_verified"] is True
    assert citation["sim_closure"]["status"] == "partial"
    assert citation["sim_closure"]["unverifiable"] == [
        {"path": "sub.spice", "reason": "not_found"}
    ]


def test_unreadable_include_preserves_top_deck_verification(tmp_path, monkeypatch):
    _circuit(tmp_path)
    report = _sim_report(tmp_path)

    def deny_read(path):
        assert Path(path) == tmp_path / "sub.spice"
        raise PermissionError(path)

    monkeypatch.setattr("klayout_tools.signoff_sim_closure.sha256_file", deny_read)
    citation = _cited(_grade(report, tmp_path))

    assert citation["input_verified"] is True
    assert citation["sim_closure"] == {
        "status": "partial",
        "includes": 1,
        "stale": [],
        "unverifiable": [{"path": "sub.spice", "reason": "unreadable"}],
    }


def test_external_scope_include_has_no_resolvable_path(tmp_path):
    _circuit(tmp_path)
    closure = [
        _entry(tmp_path, "deck.spice"),
        {"path": None, "scope": "external", "sha256": "ab" * 32},
    ]
    block = _cited(_grade(_sim_report(tmp_path, closure=closure), tmp_path))[
        "sim_closure"
    ]

    assert block["status"] == "partial"
    assert block["unverifiable"][0]["reason"] == "unresolvable_scope"


def test_a_stale_include_outranks_an_unverifiable_one(tmp_path):
    _circuit(tmp_path)
    closure = [
        _entry(tmp_path, "deck.spice"),
        _entry(tmp_path, "sub.spice"),
        {"target": "$MODELS_DIR/m.lib", "sha256": None, "unhashed_reason": "env_var"},
    ]
    report = _sim_report(tmp_path, closure=closure)
    (tmp_path / "sub.spice").write_text(SUB + "R3 a b 1\n")

    citation = _cited(_grade(report, tmp_path))

    assert citation["input_verified"] is False
    assert citation["sim_closure"]["status"] == "stale"
    assert len(citation["sim_closure"]["unverifiable"]) == 1


def test_repo_scope_resolves_from_the_evidence_location_without_repo_root(tmp_path):
    """No `--repo-root`: `scope: repo` anchors at the `.git` ancestor of the
    evidence file, as `klt sim --check` does."""
    (tmp_path / ".git").mkdir()
    _circuit(tmp_path)
    report = _sim_report(tmp_path)
    (tmp_path / "sub.spice").write_text(SUB + "* moved\n")

    item = _item(build_tier_report(_manifest(evidence={"5": report})), 5)

    assert _cited(item)["sim_closure"]["status"] == "stale"


def test_include_git_does_not_track_is_unverifiable(tmp_path):
    repo = _git_checkout(tmp_path, gitignore="scratch/\n")
    (repo / "scratch").mkdir()
    (repo / "deck.spice").write_text(DECK)
    (repo / "scratch" / "sub.spice").write_text(SUB)
    closure = [
        _entry(repo, "deck.spice"),
        {
            "path": "scratch/sub.spice",
            "scope": "repo",
            "sha256": _hex(repo / "scratch" / "sub.spice"),
        },
    ]
    report = _sim_report(repo, closure=closure)
    _git(repo, "add", "deck.spice", "sim.json")
    _git(repo, "commit", "-qm", "evidence")

    block = _cited(_grade(report, repo))["sim_closure"]

    assert block["status"] == "partial"
    assert block["unverifiable"] == [
        {"path": "scratch/sub.spice", "reason": "untracked"}
    ]


def test_non_sim_citations_gain_no_closure_key(tmp_path):
    drc = _write(tmp_path, "drc.json", DRC_CLEAN_ENVELOPE)

    item = _item(build_tier_report(_manifest(evidence={"3": drc})), 3)

    assert "sim_closure" not in item["citation"]


def test_block_is_json_serialisable_and_garbage_closures_never_raise(tmp_path):
    _circuit(tmp_path)
    for closure in (
        [{"path": "deck.spice"}, "junk", 7, None, {"sha256": 5}],
        "not-a-list",
        [],
        {},
    ):
        envelope = json.loads(Path(_sim_report(tmp_path, closure=closure)).read_text())
        block = check_sim_closure(envelope, str(tmp_path / "sim.json"), tmp_path)
        json.dumps(block)
        assert block["status"] in {"verified", "partial", "not_recorded"}


def test_committed_include_rename_is_not_found(tmp_path):
    repo = _git_checkout(tmp_path)
    _circuit(repo)
    report = _sim_report(repo)
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "Original simulation inputs")
    _git(repo, "mv", "sub.spice", "renamed.spice")
    _git(repo, "commit", "-qm", "Rename the included deck")

    citation = _cited(_grade(report, repo))
    assert citation["sim_closure"]["unverifiable"] == [
        {"path": "sub.spice", "reason": "not_found"}
    ]


def test_tracked_include_lookup_denied_is_unreadable(tmp_path, monkeypatch):
    repo = _git_checkout(tmp_path)
    _circuit(repo)
    report = _sim_report(repo)
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "Track the divider inputs")
    denied = repo / "sub.spice"
    real_stat = Path.stat

    def deny_lookup(path, *args, **kwargs):
        if path == denied:
            raise PermissionError("Permission denied")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", deny_lookup)
    citation = _cited(_grade(report, repo))
    assert citation["sim_closure"]["unverifiable"] == [
        {"path": "sub.spice", "reason": "unreadable"}
    ]
