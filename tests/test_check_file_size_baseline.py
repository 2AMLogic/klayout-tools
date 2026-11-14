"""Tests for the per-file size ratchet (issue #2766).

`scripts/check_file_size_baseline.py` is a dedicated CI step that gates
per-file line growth against the baseline read from the **comparison
revision** (PR merge-base / push-before SHA), never the working tree, so a
PR cannot loosen its own gate. These tests assert, against isolated
temporary Git repositories (every scenario is real committed history, so
rename detection, bootstrap, and merge-base semantics are exercised for
real):

1. **Counting and governance** -- physical line counting (blank/comment
   lines, final unterminated line, empty files) and the governed-source
   inventory (`.py`/`.rs`/`.sh` outside the vendored agent-tooling trees;
   directory names never exempt).
2. **The two-baseline contract** -- unchanged debt passes; growth, new
   debt, stale ceilings, surviving entries, and new/mismatched vendor
   exemptions fail; shrinkage with a tightened entry passes; deletion and
   crossing under the limit require entry removal.
3. **Bootstrap** -- when the comparison revision predates the checker, the
   trusted baseline derives from that tree's measured content and the
   seed passes only by matching it.
4. **Renames** -- a non-growing rename of a baselined file is permitted;
   growth (through a rename or across the limit) is not.
5. **Vendor exemptions** -- hash-pinned files are exempt; changed bytes
   cease to be exempt; routine PRs cannot add pins; the vendor-refresh
   exception fires only when the whole diff is the baseline plus
   already-pinned paths.
6. **Usable input contract** -- missing refs and malformed baselines exit
   2 with actionable diagnostics; check mode is read-only; output is
   deterministic; a multi-commit push is caught by its before SHA.
7. **This checkout** -- the committed seed accepts unchanged existing
   source, the committed baseline is structurally sound, and the CI
   workflow actually wires the gate.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check_file_size_baseline.py"
BASELINE_PATH = REPO_ROOT / "file-size-baseline.json"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

# Hermetic git for the fixture repos: no global/system config, no leaked
# GIT_* plumbing from the outer harness/CI environment.
GIT_ENV = {
    key: value for key, value in os.environ.items() if not key.startswith("GIT_")
}
GIT_ENV["GIT_CONFIG_GLOBAL"] = os.devnull
GIT_ENV["GIT_CONFIG_SYSTEM"] = os.devnull


def _load_script_module():
    spec = importlib.util.spec_from_file_location("check_file_size_baseline", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register in sys.modules *before* exec_module: the script defines
    # frozen dataclasses, and dataclasses' own field-type resolution looks
    # the module up via `sys.modules[cls.__module__]` (same convention as
    # tests/test_check_complexity_baseline.py).
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


cfsb = _load_script_module()


# ---------------------------------------------------------------------------
# Fixture-repo helpers
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env=GIT_ENV,
    )
    assert result.returncode == 0, f"git {args}: {result.stderr}"
    return result


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _write(repo: Path, files: dict[str, bytes | str]) -> None:
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(
            content.encode("utf-8") if isinstance(content, str) else content
        )


def _commit(repo: Path, files: dict[str, bytes | str], message: str) -> str:
    _write(repo, files)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _head(repo)


def _stage(repo: Path, files: dict[str, bytes | str]) -> None:
    """Stage candidate-tree changes without committing: the checker reads
    the working tree (via `git ls-files`), so staged-but-uncommitted
    files are exactly what it measures."""
    _write(repo, files)
    _git(repo, "add", "-A")


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--repo-root", str(repo), *args],
        capture_output=True,
        text=True,
        env=GIT_ENV,
    )


def _big(lines: int, seed: int = 0) -> str:
    return "".join(f"x = {seed} + {i}\n" for i in range(lines))


def _sha256(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _baseline_doc(
    debts: dict[str, int] | None = None,
    vendor: list[dict[str, str]] | None = None,
) -> str:
    return json.dumps(
        {
            "schema_version": cfsb.BASELINE_SCHEMA_VERSION,
            "hard_limit": cfsb.HARD_LIMIT,
            "debts": [
                {"path": path, "lines": lines}
                for path, lines in sorted((debts or {}).items())
            ],
            "vendor_hashes": vendor or [],
        },
        indent=2,
    )


def _pin(data: bytes | str, path: str) -> dict[str, str]:
    return {"sha256": _sha256(data), "path": path}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    return repo


# ---------------------------------------------------------------------------
# 1. Counting and governance
# ---------------------------------------------------------------------------


def test_count_lines_counts_blanks_comments_and_final_partial_line():
    assert cfsb.count_lines(b"") == 0
    assert cfsb.count_lines(b"\n") == 1
    assert cfsb.count_lines(b"a\nb\n") == 2
    assert cfsb.count_lines(b"a\nb") == 2  # final line without newline
    assert cfsb.count_lines(b"# comment\n\n   \nx\n") == 4


def test_is_governed_by_extension_and_vendored_tree_not_directory_name():
    assert cfsb.is_governed("src/klayout_tools/extract.py")
    assert cfsb.is_governed("native/mom/src/solver.rs")
    assert cfsb.is_governed("scripts/install-yosys.sh")
    assert cfsb.is_governed("tests/test_extract.py")
    assert cfsb.is_governed("src/klayout_tools/_vendor/mutation_variants.py")
    assert cfsb.is_governed("vendor/anything.py")  # a dir named vendor exempts nothing
    assert not cfsb.is_governed("docs/design/notes.md")
    assert not cfsb.is_governed("file-size-baseline.json")
    assert not cfsb.is_governed("complexity-baseline.json")
    assert not cfsb.is_governed(".loom/scripts/check-anything.sh")
    assert not cfsb.is_governed(".claude/skills/x.py")
    assert not cfsb.is_governed(".agents/mirror/x.py")


def test_filenamed_with_spaces_round_trips(repo):
    comparison = _commit(repo, {"my module.py": _big(600)}, "base")
    _stage(repo, {"file-size-baseline.json": _baseline_doc({"my module.py": 600})})

    result = _run(repo, "--comparison-rev", comparison)

    assert result.returncode == 0, result.stderr
    assert "my module.py" not in result.stderr


# ---------------------------------------------------------------------------
# 2. The two-baseline contract
# ---------------------------------------------------------------------------


def test_unchanged_debt_passes_against_its_trusted_ceiling(repo):
    comparison = _commit(
        repo,
        {
            "src/big.py": _big(600),
            "file-size-baseline.json": _baseline_doc({"src/big.py": 600}),
        },
        "base",
    )
    _stage(repo, {"src/big.py": _big(600)})

    result = _run(repo, "--comparison-rev", comparison)

    assert result.returncode == 0, result.stderr
    assert "OK:" in result.stdout


def test_growth_above_trusted_ceiling_fails_even_with_self_approved_entry(repo):
    comparison = _commit(
        repo,
        {
            "src/big.py": _big(600),
            "file-size-baseline.json": _baseline_doc({"src/big.py": 600}),
        },
        "base",
    )
    # The PR edits its own JSON in the same breath: still fails, because
    # the ceiling is validated against the comparison revision, not here.
    _stage(
        repo,
        {
            "src/big.py": _big(650),
            "file-size-baseline.json": _baseline_doc({"src/big.py": 650}),
        },
    )

    result = _run(repo, "--comparison-rev", comparison)

    assert result.returncode == 1
    assert "src/big.py" in result.stderr
    assert "650" in result.stderr and "600" in result.stderr


def test_growth_without_updating_proposed_entry_fails(repo):
    comparison = _commit(
        repo,
        {
            "src/big.py": _big(600),
            "file-size-baseline.json": _baseline_doc({"src/big.py": 600}),
        },
        "base",
    )
    _stage(repo, {"src/big.py": _big(650)})

    result = _run(repo, "--comparison-rev", comparison)

    assert result.returncode == 1
    assert "must equal" in result.stderr or "grew" in result.stderr


def test_reduction_with_tightened_ceiling_passes(repo):
    comparison = _commit(
        repo,
        {
            "src/big.py": _big(600),
            "file-size-baseline.json": _baseline_doc({"src/big.py": 600}),
        },
        "base",
    )
    _stage(
        repo,
        {
            "src/big.py": _big(550),
            "file-size-baseline.json": _baseline_doc({"src/big.py": 550}),
        },
    )

    result = _run(repo, "--comparison-rev", comparison)

    assert result.returncode == 0, result.stderr


def test_stale_too_high_ceiling_fails(repo):
    comparison = _commit(
        repo,
        {
            "src/big.py": _big(600),
            "file-size-baseline.json": _baseline_doc({"src/big.py": 600}),
        },
        "base",
    )
    _stage(
        repo,
        {
            "src/big.py": _big(550),
            "file-size-baseline.json": _baseline_doc({"src/big.py": 600}),
        },
    )

    result = _run(repo, "--comparison-rev", comparison)

    assert result.returncode == 1
    assert "must equal" in result.stderr
    assert "550" in result.stderr


def test_raised_ceiling_fails(repo):
    comparison = _commit(
        repo,
        {
            "src/big.py": _big(600),
            "file-size-baseline.json": _baseline_doc({"src/big.py": 600}),
        },
        "base",
    )
    _stage(repo, {"file-size-baseline.json": _baseline_doc({"src/big.py": 9999})})

    result = _run(repo, "--comparison-rev", comparison)

    assert result.returncode == 1
    assert "must equal" in result.stderr


def test_entry_removed_once_under_limit_passes_and_surviving_entry_fails(repo):
    comparison = _commit(
        repo,
        {
            "src/big.py": _big(600),
            "file-size-baseline.json": _baseline_doc({"src/big.py": 600}),
        },
        "base",
    )

    _stage(
        repo, {"src/big.py": _big(480), "file-size-baseline.json": _baseline_doc({})}
    )
    assert _run(repo, "--comparison-rev", comparison).returncode == 0

    _stage(
        repo,
        {
            "src/big.py": _big(480),
            "file-size-baseline.json": _baseline_doc({"src/big.py": 480}),
        },
    )
    result = _run(repo, "--comparison-rev", comparison)

    assert result.returncode == 1
    assert "remove the entry" in result.stderr


def test_deleted_file_requires_entry_removal(repo):
    comparison = _commit(
        repo,
        {
            "src/big.py": _big(600),
            "src/other.py": _big(300),
            "file-size-baseline.json": _baseline_doc({"src/big.py": 600}),
        },
        "base",
    )

    (repo / "src/big.py").unlink()
    _stage(repo, {"file-size-baseline.json": _baseline_doc({})})
    assert _run(repo, "--comparison-rev", comparison).returncode == 0

    _stage(repo, {"file-size-baseline.json": _baseline_doc({"src/big.py": 600})})
    result = _run(repo, "--comparison-rev", comparison)

    assert result.returncode == 1
    assert "deleted" in result.stderr


def test_new_oversized_file_fails_with_or_without_self_approved_entry(repo):
    comparison = _commit(
        repo,
        {
            "src/big.py": _big(600),
            "file-size-baseline.json": _baseline_doc({"src/big.py": 600}),
        },
        "base",
    )

    for proposed in ({}, {"src/new.py": 700}):
        _stage(
            repo,
            {
                "src/new.py": _big(700, seed=7),
                "file-size-baseline.json": _baseline_doc(
                    {"src/big.py": 600, **proposed}
                ),
            },
        )
        result = _run(repo, "--comparison-rev", comparison)

        assert result.returncode == 1, proposed
        assert "no trusted baseline ceiling" in result.stderr


def test_new_undersized_file_passes(repo):
    comparison = _commit(
        repo,
        {
            "src/big.py": _big(600),
            "file-size-baseline.json": _baseline_doc({"src/big.py": 600}),
        },
        "base",
    )
    _stage(repo, {"src/small.py": _big(100, seed=3)})

    result = _run(repo, "--comparison-rev", comparison)

    assert result.returncode == 0, result.stderr


def test_boundary_at_limit_is_free_but_one_over_needs_an_entry(repo):
    comparison = _commit(
        repo,
        {
            "src/at_limit.py": _big(cfsb.HARD_LIMIT),
            "file-size-baseline.json": _baseline_doc({}),
        },
        "base",
    )
    _stage(repo, {})
    assert _run(repo, "--comparison-rev", comparison).returncode == 0

    # 500 newlines plus a final unterminated line == 501 physical lines.
    _stage(repo, {"src/at_limit.py": _big(cfsb.HARD_LIMIT) + "x = 1"})
    result = _run(repo, "--comparison-rev", comparison)

    assert result.returncode == 1
    assert f"{cfsb.HARD_LIMIT + 1} lines" in result.stderr


def test_empty_file_is_zero_lines(repo):
    comparison = _commit(
        repo,
        {"src/empty.py": "", "file-size-baseline.json": _baseline_doc({})},
        "base",
    )
    _stage(repo, {"src/new.py": ""})

    result = _run(repo, "--comparison-rev", comparison)

    assert result.returncode == 0, result.stderr


def test_unrelated_non_source_growth_is_not_governed(repo):
    comparison = _commit(
        repo,
        {
            "docs/notes.md": _big(900, seed=1),
            "file-size-baseline.json": _baseline_doc({}),
        },
        "base",
    )
    _stage(repo, {"docs/notes.md": _big(1200, seed=1)})

    result = _run(repo, "--comparison-rev", comparison)

    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# 3. Bootstrap
# ---------------------------------------------------------------------------


def test_bootstrap_seed_of_unchanged_debt_passes(repo):
    # The comparison revision predates the checker (no baseline file):
    # this is exactly the gate's own landing PR.
    comparison = _commit(repo, {"src/big.py": _big(600)}, "base")
    _stage(repo, {"file-size-baseline.json": _baseline_doc({"src/big.py": 600})})

    result = _run(repo, "--comparison-rev", comparison)

    assert result.returncode == 0, result.stderr
    assert "bootstrap" in result.stdout


def test_bootstrap_seed_mismatching_comparison_tree_fails(repo):
    comparison = _commit(repo, {"src/big.py": _big(600)}, "base")
    # The proposed seed cannot authorize its own entries: it must record
    # the comparison tree's actual content (601 here).
    _stage(repo, {"file-size-baseline.json": _baseline_doc({"src/big.py": 700})})

    result = _run(repo, "--comparison-rev", comparison)

    assert result.returncode == 1
    assert "must equal" in result.stderr


def test_bootstrap_grants_no_hash_exemptions(repo):
    content = _big(600, seed=9)
    comparison = _commit(repo, {"vendor/dep.py": content}, "base")
    _stage(
        repo,
        {
            "file-size-baseline.json": _baseline_doc(
                vendor=[_pin(content, "vendor/dep.py")]
            )
        },
    )

    result = _run(repo, "--comparison-rev", comparison)

    assert result.returncode == 1
    assert "new or replaced vendor content-hash exemption" in result.stderr


def test_bootstrap_records_new_oversized_files_at_landing_size(repo):
    # The gate's own landing PR is in exactly this position: its new
    # implementation files are over the limit and exist nowhere at the
    # comparison revision. They may enter at exactly their measured size
    # -- and at nothing more.
    comparison = _commit(repo, {"src/small.py": _big(100)}, "base")
    _stage(
        repo,
        {
            "src/new_big.py": _big(600, seed=21),
            "file-size-baseline.json": _baseline_doc({"src/new_big.py": 600}),
        },
    )
    assert _run(repo, "--comparison-rev", comparison).returncode == 0

    _stage(
        repo,
        {"file-size-baseline.json": _baseline_doc({"src/new_big.py": 700})},
    )
    result = _run(repo, "--comparison-rev", comparison)

    assert result.returncode == 1
    assert "must equal" in result.stderr


# ---------------------------------------------------------------------------
# 4. Renames
# ---------------------------------------------------------------------------


def test_rename_of_baselined_file_with_unchanged_size_passes(repo):
    comparison = _commit(
        repo,
        {
            "src/old.py": _big(600),
            "file-size-baseline.json": _baseline_doc({"src/old.py": 600}),
        },
        "base",
    )
    _git(repo, "mv", "src/old.py", "src/new.py")
    _stage(repo, {"file-size-baseline.json": _baseline_doc({"src/new.py": 600})})

    result = _run(repo, "--comparison-rev", comparison)

    assert result.returncode == 0, result.stderr


def test_rename_of_baselined_file_with_growth_fails(repo):
    comparison = _commit(
        repo,
        {
            "src/old.py": _big(600),
            "file-size-baseline.json": _baseline_doc({"src/old.py": 600}),
        },
        "base",
    )
    _git(repo, "mv", "src/old.py", "src/new.py")
    _stage(
        repo,
        {
            "src/new.py": _big(650),
            "file-size-baseline.json": _baseline_doc({"src/new.py": 650}),
        },
    )

    result = _run(repo, "--comparison-rev", comparison)

    assert result.returncode == 1
    assert "rename of src/old.py" in result.stderr


def test_rename_of_unbaselined_file_across_the_limit_fails(repo):
    comparison = _commit(
        repo,
        {
            "src/old.py": _big(cfsb.HARD_LIMIT - 10),
            "file-size-baseline.json": _baseline_doc({}),
        },
        "base",
    )
    _git(repo, "mv", "src/old.py", "src/new.py")
    _stage(repo, {"src/new.py": _big(cfsb.HARD_LIMIT + 1)})

    result = _run(repo, "--comparison-rev", comparison)

    assert result.returncode == 1
    assert "no trusted baseline ceiling" in result.stderr


def test_rename_without_updating_the_proposed_entry_fails(repo):
    comparison = _commit(
        repo,
        {
            "src/old.py": _big(600),
            "file-size-baseline.json": _baseline_doc({"src/old.py": 600}),
        },
        "base",
    )
    _git(repo, "mv", "src/old.py", "src/new.py")

    result = _run(repo, "--comparison-rev", comparison)

    assert result.returncode == 1
    assert "deleted" in result.stderr


# ---------------------------------------------------------------------------
# 5. Vendor content-hash exemptions
# ---------------------------------------------------------------------------


def _vendor_base_repo(repo: Path) -> str:
    content = _big(600, seed=11)
    return _commit(
        repo,
        {
            "vendor/dep.py": content,
            "src/first.py": _big(100, seed=12),
            "file-size-baseline.json": _baseline_doc(
                vendor=[_pin(content, "vendor/dep.py")]
            ),
        },
        "base",
    )


def test_pinned_vendor_bytes_are_exempt(repo):
    comparison = _vendor_base_repo(repo)

    result = _run(repo, "--comparison-rev", comparison)

    assert result.returncode == 0, result.stderr
    assert "vendor/dep.py" not in result.stderr


def test_changed_exempt_bytes_cease_to_be_exempt(repo):
    comparison = _vendor_base_repo(repo)
    # An upstream refresh lands different bytes (same 600-line shape).
    _stage(repo, {"vendor/dep.py": _big(600, seed=14)})
    # Regeneration reflects reality: the changed file is over the limit,
    # no longer exempt, and has no trusted ceiling -> ordinary checking.
    result = _run(repo, "--update-baseline")
    assert result.returncode == 0, result.stderr
    updated = json.loads((repo / "file-size-baseline.json").read_text())
    assert updated["debts"] == [{"path": "vendor/dep.py", "lines": 600}]
    assert updated["vendor_hashes"] == []

    result = _run(repo, "--comparison-rev", comparison)

    assert result.returncode == 1
    assert "no trusted baseline ceiling" in result.stderr


def test_new_vendor_pin_rejected_in_routine_pr(repo):
    comparison = _vendor_base_repo(repo)
    other = _big(600, seed=13)
    _stage(
        repo,
        {
            "vendor/other.py": other,
            "file-size-baseline.json": _baseline_doc(
                vendor=[
                    _pin(_big(600, seed=11), "vendor/dep.py"),
                    _pin(other, "vendor/other.py"),
                ]
            ),
        },
    )

    result = _run(repo, "--comparison-rev", comparison)

    assert result.returncode == 1
    assert "new or replaced vendor content-hash exemption" in result.stderr


def test_vendor_refresh_with_vendor_only_diff_is_permitted(repo):
    comparison = _vendor_base_repo(repo)
    new_upstream = _big(600, seed=14)
    _stage(
        repo,
        {
            "vendor/dep.py": new_upstream,
            "file-size-baseline.json": _baseline_doc(
                vendor=[_pin(new_upstream, "vendor/dep.py")]
            ),
        },
    )

    result = _run(repo, "--comparison-rev", comparison)

    assert result.returncode == 0, result.stderr


def test_vendor_refresh_denied_when_first_party_files_also_changed(repo):
    comparison = _vendor_base_repo(repo)
    new_upstream = _big(600, seed=14)
    _stage(
        repo,
        {
            "vendor/dep.py": new_upstream,
            "src/first.py": _big(400, seed=12),
            "file-size-baseline.json": _baseline_doc(
                vendor=[_pin(new_upstream, "vendor/dep.py")]
            ),
        },
    )

    result = _run(repo, "--comparison-rev", comparison)

    assert result.returncode == 1
    assert "new or replaced vendor content-hash exemption" in result.stderr


def test_vendor_refresh_denied_for_a_newly_pinned_path(repo):
    comparison = _vendor_base_repo(repo)
    other = _big(600, seed=13)
    _stage(
        repo,
        {
            "vendor/other.py": other,
            "file-size-baseline.json": _baseline_doc(
                vendor=[
                    _pin(_big(600, seed=11), "vendor/dep.py"),
                    _pin(other, "vendor/other.py"),
                ]
            ),
        },
    )
    # Remove dep.py entirely so the *whole* diff is vendor-dir + baseline
    # -- yet other.py was never pinned by the trusted baseline, so the
    # refresh exception must not fire.
    _git(repo, "rm", "-q", "vendor/dep.py")

    result = _run(repo, "--comparison-rev", comparison)

    assert result.returncode == 1
    assert "new or replaced vendor content-hash exemption" in result.stderr


def test_moving_first_party_code_into_vendor_directory_exempts_nothing(repo):
    comparison = _commit(
        repo,
        {
            "src/big.py": _big(600),
            "file-size-baseline.json": _baseline_doc({"src/big.py": 600}),
        },
        "base",
    )
    (repo / "vendor").mkdir()
    _git(repo, "mv", "src/big.py", "vendor/big.py")
    moved = (repo / "vendor/big.py").read_bytes()
    # The PR mints a hash pin for its moved file: rejected. (Without the
    # pin the move still carries its debt through the rename proof.)
    _stage(
        repo,
        {
            "file-size-baseline.json": _baseline_doc(
                vendor=[_pin(moved, "vendor/big.py")]
            )
        },
    )

    result = _run(repo, "--comparison-rev", comparison)

    assert result.returncode == 1
    assert "new or replaced vendor content-hash exemption" in result.stderr


# ---------------------------------------------------------------------------
# 6. Usable input contract
# ---------------------------------------------------------------------------


def test_missing_comparison_revision_fails_with_fetch_instructions(repo):
    _commit(repo, {"src/big.py": _big(600)}, "base")
    _stage(repo, {"file-size-baseline.json": _baseline_doc({"src/big.py": 600})})

    result = _run(repo, "--comparison-rev", "deadbeef" * 5)

    assert result.returncode == 2
    assert "not found" in result.stderr
    assert "git fetch" in result.stderr


@pytest.mark.parametrize(
    "doc",
    [
        "not json at all",
        json.dumps(
            {"schema_version": 2, "hard_limit": 500, "debts": [], "vendor_hashes": []}
        ),
        json.dumps(
            {"schema_version": 1, "hard_limit": 400, "debts": [], "vendor_hashes": []}
        ),
        json.dumps(
            {
                "schema_version": 1,
                "hard_limit": 500,
                "debts": [
                    {"path": "src/big.py", "lines": 600},
                    {"path": "src/big.py", "lines": 600},
                ],
                "vendor_hashes": [],
            }
        ),
        json.dumps(
            {
                "schema_version": 1,
                "hard_limit": 500,
                "debts": [{"path": "src/big.py", "lines": -5}],
                "vendor_hashes": [],
            }
        ),
        json.dumps(
            {
                "schema_version": 1,
                "hard_limit": 500,
                "debts": [{"path": "src/big.py", "lines": 600, "extra": 1}],
                "vendor_hashes": [],
            }
        ),
        json.dumps(
            {
                "schema_version": 1,
                "hard_limit": 500,
                "debts": [],
                "vendor_hashes": [{"sha256": "nothex", "path": "v.py"}],
            }
        ),
    ],
)
def test_malformed_proposed_baseline_is_unusable_exit_2(repo, doc):
    _commit(repo, {"src/big.py": _big(600)}, "base")
    _stage(repo, {"file-size-baseline.json": doc})

    result = _run(repo, "--comparison-rev", "HEAD")

    assert result.returncode == 2
    assert "proposed baseline" in result.stderr


def test_deleted_proposed_baseline_is_unusable(repo):
    comparison = _commit(
        repo,
        {
            "src/big.py": _big(600),
            "file-size-baseline.json": _baseline_doc({"src/big.py": 600}),
        },
        "base",
    )
    (repo / "file-size-baseline.json").unlink()
    _git(repo, "add", "-A")

    result = _run(repo, "--comparison-rev", comparison)

    assert result.returncode == 2
    assert "not found" in result.stderr


def test_check_mode_is_read_only(repo):
    comparison = _commit(
        repo,
        {
            "src/big.py": _big(600),
            "file-size-baseline.json": _baseline_doc({"src/big.py": 600}),
        },
        "base",
    )
    _stage(repo, {"src/big.py": _big(650)})
    before = (repo / "file-size-baseline.json").read_bytes()

    result = _run(repo, "--comparison-rev", comparison)

    assert result.returncode == 1
    assert (repo / "file-size-baseline.json").read_bytes() == before


def test_output_is_deterministic(repo):
    comparison = _commit(
        repo,
        {
            "src/big.py": _big(600),
            "src/other_big.py": _big(600, seed=2),
            "file-size-baseline.json": _baseline_doc({"src/big.py": 600}),
        },
        "base",
    )
    _stage(repo, {"src/big.py": _big(650)})

    first = _run(repo, "--comparison-rev", comparison)
    second = _run(repo, "--comparison-rev", comparison)

    assert first.returncode == 1
    assert (first.stdout, first.stderr) == (second.stdout, second.stderr)
    # Both failures are reported, sorted.
    stderr_order = [
        line for line in first.stderr.splitlines() if line.startswith("src/")
    ]
    assert stderr_order == sorted(stderr_order)
    assert len(stderr_order) >= 2


def test_multi_commit_push_is_judged_by_its_before_sha(repo):
    before = _commit(
        repo,
        {
            "src/big.py": _big(600),
            "file-size-baseline.json": _baseline_doc({"src/big.py": 600}),
        },
        "base",
    )
    # A multi-commit push where only the SECOND commit grows the file: a
    # comparison against any intermediate state would miss it; the push's
    # before SHA does not.
    _commit(repo, {"docs/readme.md": "hello\n"}, "unrelated commit")
    _stage(
        repo,
        {
            "src/big.py": _big(650),
            "file-size-baseline.json": _baseline_doc({"src/big.py": 650}),
        },
    )

    result = _run(repo, "--comparison-rev", before)

    assert result.returncode == 1
    assert "grew" in result.stderr or "must equal" in result.stderr


def test_update_baseline_never_mints_exemptions(repo):
    _vendor_base_repo(repo)
    _stage(repo, {"vendor/other.py": _big(600, seed=13)})

    result = _run(repo, "--update-baseline")

    assert result.returncode == 0, result.stderr
    updated = json.loads((repo / "file-size-baseline.json").read_text())
    shas = {entry["sha256"] for entry in updated["vendor_hashes"]}
    assert _sha256(_big(600, seed=13)) not in shas
    assert {"path": "vendor/other.py", "lines": 600} in updated["debts"]


def test_update_baseline_carries_still_matching_pins_and_tightens_debt(repo):
    _vendor_base_repo(repo)
    _stage(repo, {"src/first.py": _big(80, seed=12)})

    result = _run(repo, "--update-baseline")

    assert result.returncode == 0, result.stderr
    updated = json.loads((repo / "file-size-baseline.json").read_text())
    assert updated["vendor_hashes"] == [_pin(_big(600, seed=11), "vendor/dep.py")]
    assert updated["debts"] == []


def test_update_baseline_warns_about_untracked_governed_files(repo):
    _commit(
        repo,
        {
            "src/big.py": _big(600),
            "file-size-baseline.json": _baseline_doc({"src/big.py": 600}),
        },
        "base",
    )
    _write(repo, {"src/stray.py": _big(600, seed=5)})

    result = _run(repo, "--update-baseline")

    assert result.returncode == 0, result.stderr
    assert "untracked governed source file(s)" in result.stderr
    assert "src/stray.py" in result.stderr
    updated = json.loads((repo / "file-size-baseline.json").read_text())
    assert "src/stray.py" not in [entry["path"] for entry in updated["debts"]]


# ---------------------------------------------------------------------------
# 7. This checkout
# ---------------------------------------------------------------------------


def test_committed_baseline_is_well_formed():
    assert BASELINE_PATH.exists(), "file-size-baseline.json must be committed"
    data = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    assert data["schema_version"] == cfsb.BASELINE_SCHEMA_VERSION
    assert data["hard_limit"] == cfsb.HARD_LIMIT
    assert data["vendor_hashes"] == [], (
        "bootstrap grants no hash exemptions, and nothing is pinned today"
    )
    paths = [entry["path"] for entry in data["debts"]]
    assert len(paths) == len(set(paths)), "duplicate debt entries"
    for entry in data["debts"]:
        assert entry["lines"] > cfsb.HARD_LIMIT
        assert cfsb.is_governed(entry["path"]), entry["path"]
        assert (REPO_ROOT / entry["path"]).exists(), entry["path"]


def test_seed_accepts_unchanged_existing_source():
    # The issue's closing test-plan bullet: the initial seed passes the
    # exact check CI runs. HEAD as the comparison revision works both
    # before this PR lands (bootstrap) and after (baseline present).
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--comparison-rev", "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert "OK:" in result.stdout


def test_ci_workflow_runs_the_size_ratchet():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "scripts/check_file_size_baseline.py" in text

    # Must live in the `lint` job (whose check name branch protection
    # already requires), with the history and comparison-revision
    # plumbing it needs.
    lint_job = text.split("\n  native:", 1)[0]
    assert "scripts/check_file_size_baseline.py" in lint_job, (
        "the size ratchet step must live in the lint job"
    )
    assert "fetch-depth: 0" in lint_job, (
        "the lint checkout must fetch sufficient history for the merge-base"
    )
    assert "merge-base" in lint_job
    assert "github.event.before" in lint_job, (
        "push-to-main runs must compare against the push's before SHA"
    )
