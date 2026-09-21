"""Tests for `scripts/check_artifact_determinism.py` (issue #2225): the
golden-artifact byte-stability check that regenerates from two checkouts at
different filesystem paths under two different `PYTHONHASHSEED` values.

The load-bearing tests here are the **negative controls**: a synthetic
generator with set-iteration ordering, and one that echoes its own absolute
path, each of which is invisible to an ordinary single-run regeneration
(asserted explicitly below) and each of which this check must catch and name.
That pairing -- "passes the normal way of looking at it, fails this check" --
is the whole justification for the extra CI job; a check that only fires on
bugs the existing suite already catches would be pure cost.

Everything runs against synthetic checkouts built in `tmp_path`, never
against this repo's real generators: the real ones take ~1.5 s together, but
more importantly a test that depended on them could not express a *buggy*
generator without shipping one.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check_artifact_determinism.py"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
BUDGET_FILE = REPO_ROOT / ".github" / "ci-wall-clock-budget.json"

#: ci.yml's job name for this check -- asserted in both the workflow and the
#: wall-clock budget file below, so the job cannot drift out of either.
JOB_NAME = "Golden artifacts (hash seed + path varied)"

EXIT_OK = 0
EXIT_DIFFER = 1
EXIT_CANNOT_RUN = 2

# A deterministic generator: the same bytes from any path, under any seed.
DETERMINISTIC = """
import json, pathlib
out = pathlib.Path(__file__).resolve().parent.parent / "artifacts"
out.mkdir(parents=True, exist_ok=True)
rules = sorted(["b", "a", "c"])
(out / "report.json").write_text(json.dumps({"rules": rules}, indent=2))
"""

# Set-iteration ordering: byte-stable for any *fixed* hash seed (so a suite
# that regenerates once and byte-compares never sees it), byte-unstable the
# moment the seed changes. 64 elements makes two seeds agreeing on an order
# vanishingly unlikely -- measured distinct across 24 consecutive seeds.
SET_ORDERING = """
import json, pathlib
out = pathlib.Path(__file__).resolve().parent.parent / "artifacts"
out.mkdir(parents=True, exist_ok=True)
tied = {f"rule-{n}" for n in range(64)}
(out / "report.json").write_text(json.dumps({"rules": list(tied)}, indent=2))
"""

# Absolute-path echo: byte-stable on the machine that produced it, different
# on every other checkout.
ABSOLUTE_PATH = """
import json, pathlib
root = pathlib.Path(__file__).resolve().parent.parent
out = root / "artifacts"
out.mkdir(parents=True, exist_ok=True)
(out / "report.json").write_text(json.dumps({"source": str(root)}, indent=2))
"""

# A foreign host's absolute path, committed from somebody's laptop: identical
# in both runs (nothing regenerates it), so only a scan can find it.
FOREIGN_HOST_PATH = """
import json, pathlib
out = pathlib.Path(__file__).resolve().parent.parent / "artifacts"
out.mkdir(parents=True, exist_ok=True)
(out / "report.json").write_text(
    json.dumps({"provenance": "/Users/someone/dev/gf180-surge/build"}, indent=2)
)
"""

FAILING = """
import sys
sys.exit(3)
"""


def _checkout(root: Path, name: str, body: str) -> Path:
    """Build a synthetic checkout containing one generator."""
    checkout = root / name
    (checkout / "gen").mkdir(parents=True)
    (checkout / "gen" / "generate.py").write_text(body)
    return checkout


def _manifest(root: Path) -> Path:
    path = root / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "generators": [
                    {"script": "gen/generate.py", "artifacts": ["artifacts/*.json"]}
                ],
            }
        )
    )
    return path


def _job_block(workflow: str, job_id: str) -> str:
    """The body of one `ci.yml` job, so an assertion about this job cannot be
    satisfied by an unrelated one elsewhere in the file."""
    import re

    match = re.search(
        rf"^  {re.escape(job_id)}:\n(.*?)(?=^  [A-Za-z][\w-]*:\n|\Z)",
        workflow,
        re.MULTILINE | re.DOTALL,
    )
    assert match is not None, f"job {job_id!r} not found in ci.yml"
    return match.group(1)


def _run(*args: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "GITHUB_STEP_SUMMARY"}
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], capture_output=True, text=True, env=env
    )


def _check(
    tmp_path: Path, body: str, *, seeds: tuple[str, str] = ("1", "2"), fmt: str = "json"
) -> subprocess.CompletedProcess:
    """Run the check over two synthetic checkouts at different paths, each
    carrying the same generator `body`."""
    a = _checkout(tmp_path, "primary", body)
    # A deliberately different path length as well as a different prefix:
    # a bug that truncates or pads on path length shows up too.
    b = _checkout(tmp_path, "alt/a-second-checkout-at-another-path", body)
    return _run(
        "--checkout",
        str(a),
        "--checkout",
        str(b),
        "--seed",
        seeds[0],
        "--seed",
        seeds[1],
        "--manifest",
        str(_manifest(tmp_path)),
        "--format",
        fmt,
    )


# --------------------------------------------------------------------------
# The clean case
# --------------------------------------------------------------------------


def test_deterministic_generator_passes(tmp_path: Path) -> None:
    result = _check(tmp_path, DETERMINISTIC)
    assert result.returncode == EXIT_OK, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"
    assert payload["findings"] == []
    assert payload["artifact_count"] == 1
    # The seeds actually used are echoed, so a failure is reproducible.
    assert [run["seed"] for run in payload["runs"]] == ["1", "2"]


def test_text_report_names_the_two_runs(tmp_path: Path) -> None:
    result = _check(tmp_path, DETERMINISTIC, fmt="text")
    assert result.returncode == EXIT_OK, result.stdout + result.stderr
    assert "PYTHONHASHSEED=1" in result.stdout
    assert "PYTHONHASHSEED=2" in result.stdout
    assert "byte-identical" in result.stdout


# --------------------------------------------------------------------------
# Negative control 1: set-iteration ordering
# --------------------------------------------------------------------------


def test_set_iteration_ordering_is_invisible_to_a_single_fixed_seed_run(
    tmp_path: Path,
) -> None:
    """The control half of the negative control: regenerating twice the way
    an ordinary suite does -- same checkout, same (effectively fixed) seed --
    reproduces byte-identical output, which is exactly why this bug class
    ships."""
    checkout = _checkout(tmp_path, "primary", SET_ORDERING)
    script = checkout / "gen" / "generate.py"
    artifact = checkout / "artifacts" / "report.json"

    env = dict(os.environ, PYTHONHASHSEED="12345")
    subprocess.run([sys.executable, str(script)], env=env, check=True)
    first = artifact.read_bytes()
    subprocess.run([sys.executable, str(script)], env=env, check=True)
    assert artifact.read_bytes() == first


def test_set_iteration_ordering_fails_the_check_and_names_the_artifact(
    tmp_path: Path,
) -> None:
    result = _check(tmp_path, SET_ORDERING)
    assert result.returncode == EXIT_DIFFER, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "differ"
    assert payload["differing_artifacts"] == ["artifacts/report.json"]
    kinds = {finding["kind"] for finding in payload["findings"]}
    assert kinds == {"unstable_ordering"}


def test_ordering_failure_prints_the_differing_paths_in_the_text_report(
    tmp_path: Path,
) -> None:
    """Failing *with the differing artifact paths named* is an acceptance
    criterion of issue #2225, not a nicety: a byte-compare failure that only
    says "artifacts differ" leaves the reader to bisect by hand."""
    result = _check(tmp_path, SET_ORDERING, fmt="text")
    assert result.returncode == EXIT_DIFFER, result.stdout + result.stderr
    assert "Differing artifact paths:" in result.stdout
    assert "artifacts/report.json" in result.stdout
    assert "set/dict-iteration ordering" in result.stdout


def test_annotations_name_the_artifact_as_a_file(tmp_path: Path) -> None:
    a = _checkout(tmp_path, "primary", SET_ORDERING)
    b = _checkout(tmp_path, "alt/second", SET_ORDERING)
    result = _run(
        "--checkout",
        str(a),
        "--checkout",
        str(b),
        "--seed",
        "1",
        "--seed",
        "2",
        "--manifest",
        str(_manifest(tmp_path)),
        "--annotate",
    )
    assert result.returncode == EXIT_DIFFER, result.stdout + result.stderr
    annotations = [ln for ln in result.stdout.splitlines() if ln.startswith("::error")]
    assert annotations
    assert all("file=artifacts/report.json" in ln for ln in annotations)


# --------------------------------------------------------------------------
# Negative control 2: absolute paths
# --------------------------------------------------------------------------


def test_absolute_path_echo_is_classified_as_path_dependent(tmp_path: Path) -> None:
    result = _check(tmp_path, ABSOLUTE_PATH)
    assert result.returncode == EXIT_DIFFER, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    kinds = {finding["kind"] for finding in payload["findings"]}
    # `host_path` rides along (the echoed path is also host-absolute); the
    # classification that matters is that the byte difference is attributed
    # to the checkout path, not to the hash seed.
    assert "path_dependent" in kinds
    assert "unstable_ordering" not in kinds
    assert payload["differing_artifacts"] == ["artifacts/report.json"]


def test_absolute_path_echo_survives_an_identical_seed(tmp_path: Path) -> None:
    """Path-dependence must be caught by the *path* variation alone -- with
    both runs on the same seed there is no ordering noise to hide behind."""
    result = _check(tmp_path, ABSOLUTE_PATH, seeds=("7", "7"))
    assert result.returncode == EXIT_DIFFER, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert {f["kind"] for f in payload["findings"]} >= {"path_dependent"}


def test_foreign_host_path_is_caught_even_though_both_runs_agree(
    tmp_path: Path,
) -> None:
    """A path committed from another machine is byte-identical in both runs,
    so the comparison alone cannot see it -- the scan is what catches the
    gf180-surge SXT-013 shape (a `/Users/...` provenance string)."""
    result = _check(tmp_path, FOREIGN_HOST_PATH)
    assert result.returncode == EXIT_DIFFER, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert [f["kind"] for f in payload["findings"]] == ["host_path"]
    assert "/Users/someone/dev/gf180-surge" in payload["findings"][0]["detail"]


def test_real_repo_artifacts_carry_no_host_absolute_paths() -> None:
    """The same scan, applied to what is actually committed today: a guard
    against a host path landing in a golden artifact between CI runs."""
    import re

    pattern = re.compile(
        rb"(?:/Users/|/home/|/root/|/private/var/folders/|/var/folders/"
        rb"|/tmp/|[A-Za-z]:\\{1,2}Users\\{1,2})"
    )
    artifacts = [
        *(REPO_ROOT / "tests" / "golden_deck").glob("*/manifest.json"),
        *(REPO_ROOT / "tests" / "corpus" / "golden").glob("*/*.layers.json"),
        *(REPO_ROOT / "tests" / "golden_metrics").glob("*.json"),
    ]
    assert artifacts, "no golden artifacts found -- the globs have drifted"
    offenders = [p for p in artifacts if pattern.search(p.read_bytes())]
    assert not offenders, f"host-absolute path committed in: {offenders}"


# --------------------------------------------------------------------------
# Cannot-run cases (exit 2, never a silent green)
# --------------------------------------------------------------------------


def test_one_checkout_cannot_run(tmp_path: Path) -> None:
    a = _checkout(tmp_path, "primary", DETERMINISTIC)
    result = _run("--checkout", str(a), "--manifest", str(_manifest(tmp_path)))
    assert result.returncode == EXIT_CANNOT_RUN, result.stdout + result.stderr
    assert "two" in result.stderr


def test_identical_checkout_paths_cannot_run(tmp_path: Path) -> None:
    """Two runs in the same directory would quietly demote this to a
    seed-only check -- the path half of the contract would stop being
    tested while the job stayed green."""
    a = _checkout(tmp_path, "primary", DETERMINISTIC)
    result = _run(
        "--checkout",
        str(a),
        "--checkout",
        str(a),
        "--manifest",
        str(_manifest(tmp_path)),
    )
    assert result.returncode == EXIT_CANNOT_RUN, result.stdout + result.stderr
    assert "distinct" in result.stderr


def test_failing_generator_cannot_run(tmp_path: Path) -> None:
    result = _check(tmp_path, FAILING)
    assert result.returncode == EXIT_CANNOT_RUN, result.stdout + result.stderr
    assert "gen/generate.py" in result.stderr


def test_generator_writing_nothing_cannot_run(tmp_path: Path) -> None:
    """Zero collected artifacts is exit 2, not a spurious green: a check that
    silently stops checking is worse than no check."""
    result = _check(tmp_path, "pass\n")
    assert result.returncode == EXIT_CANNOT_RUN, result.stdout + result.stderr
    assert "no artifacts" in result.stderr


def test_seed_count_must_match_checkout_count(tmp_path: Path) -> None:
    a = _checkout(tmp_path, "primary", DETERMINISTIC)
    b = _checkout(tmp_path, "alt/second", DETERMINISTIC)
    result = _run(
        "--checkout",
        str(a),
        "--checkout",
        str(b),
        "--seed",
        "1",
        "--manifest",
        str(_manifest(tmp_path)),
    )
    assert result.returncode == EXIT_CANNOT_RUN, result.stdout + result.stderr


def test_unknown_manifest_schema_version_cannot_run(tmp_path: Path) -> None:
    a = _checkout(tmp_path, "primary", DETERMINISTIC)
    b = _checkout(tmp_path, "alt/second", DETERMINISTIC)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"schema_version": 99, "generators": []}))
    result = _run(
        "--checkout", str(a), "--checkout", str(b), "--manifest", str(manifest)
    )
    assert result.returncode == EXIT_CANNOT_RUN, result.stdout + result.stderr


def test_random_seeds_are_drawn_distinct_when_none_are_given(tmp_path: Path) -> None:
    """`PYTHONHASHSEED` only randomises when unset; this check sets it
    explicitly, so it has to draw the variation itself -- and two runs that
    happened to share a seed would silently degrade to a path-only check."""
    result = _check(tmp_path, DETERMINISTIC, seeds=("1", "2"))  # sanity: ok path
    assert result.returncode == EXIT_OK

    a = _checkout(tmp_path, "primary2", DETERMINISTIC)
    b = _checkout(tmp_path, "alt2/second", DETERMINISTIC)
    result = _run(
        "--checkout",
        str(a),
        "--checkout",
        str(b),
        "--manifest",
        str(_manifest(tmp_path)),
        "--format",
        "json",
    )
    assert result.returncode == EXIT_OK, result.stdout + result.stderr
    seeds = [run["seed"] for run in json.loads(result.stdout)["runs"]]
    assert len(set(seeds)) == 2
    assert all(seed.isdigit() for seed in seeds)


# --------------------------------------------------------------------------
# Wiring against this repo's own files
# --------------------------------------------------------------------------


def test_default_manifest_names_real_generators() -> None:
    """The built-in subset must keep naming scripts that exist: a renamed
    generator would otherwise turn the job into a no-op the day it lands."""
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    try:
        import check_artifact_determinism as checker
    finally:
        sys.path.pop(0)

    generators = checker.load_generators(None)
    assert generators
    for generator in generators:
        assert (REPO_ROOT / generator.script).is_file(), generator.script
        assert generator.artifacts, generator.script
        matched = [
            path
            for pattern in generator.artifacts
            for path in REPO_ROOT.glob(pattern)
            if path.is_file()
        ]
        assert matched, f"no committed artifact matches {generator.artifacts}"


def test_ci_workflow_runs_the_determinism_check_from_two_checkouts() -> None:
    """The job is only a path-variation check if the two paths actually
    differ -- a copy-paste that pointed both `--checkout` flags at the same
    directory would leave a green job checking half of what it claims."""
    import re

    workflow = WORKFLOW.read_text()
    assert JOB_NAME in workflow

    job = _job_block(workflow, "artifact-determinism")
    assert "scripts/check_artifact_determinism.py" in job

    passed = re.findall(r"--checkout\s+\"?([^\"\s\\]+)", job)
    assert len(passed) == 2, passed
    assert passed[0] != passed[1]

    # ...and the two `actions/checkout` steps must land at those two paths.
    checkout_paths = re.findall(r"^\s+path:\s+(\S+)\s*$", job, re.MULTILINE)
    assert len(set(checkout_paths)) == 2, checkout_paths
    for path in checkout_paths:
        assert any(passed_path.endswith(path) for passed_path in passed), path


def test_wall_clock_budget_job_waits_for_the_determinism_job() -> None:
    """Otherwise the budget job can measure a run in which this job has not
    finished, and quietly drop it from the compute total."""
    job = _job_block(WORKFLOW.read_text(), "wall-clock-budget")
    assert "- artifact-determinism" in job


def test_determinism_job_has_a_wall_clock_budget_entry() -> None:
    """Per docs/guides/ci-wall-clock-budget.md: a new job either carries a
    measured budget row or silently inherits the 600 s default."""
    budget = json.loads(BUDGET_FILE.read_text())
    assert JOB_NAME in budget["jobs"]
    assert budget["jobs"][JOB_NAME] > 0
