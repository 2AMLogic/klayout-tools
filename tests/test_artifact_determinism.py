"""Tests for `scripts/check_artifact_determinism.py` (issue #2225; float
provenance in issue #2279): the golden-artifact byte-stability check that
regenerates from two checkouts at different filesystem paths under two
different `PYTHONHASHSEED` values.

The load-bearing tests here are the **negative controls**: a synthetic
generator with set-iteration ordering, one that echoes its own absolute
path, and one that computes committed bytes through host libm (`math.pow`),
each of which is invisible to an ordinary single-run regeneration (asserted
explicitly below) and each of which this check must catch and name. That
pairing -- "passes the normal way of looking at it, fails this check" -- is
the whole justification for the extra CI job; a check that only fires on
bugs the existing suite already catches would be pure cost.

The float class needs one extra sentence: both of the check's own runs sit
on one host, so last-ulp libm divergence is invisible to its byte-compare
*by construction*. It is caught instead by a static source scan of each
declared generator script, and by the `pinned-artifact` escape hatch --
where the check regenerates nothing and verifies the recorded pin
(`generator_sha256` / `artifact_sha256`) instead.

Everything runs against synthetic checkouts built in `tmp_path`, never
against this repo's real generators: the real ones take ~1.5 s together, but
more importantly a test that depended on them could not express a *buggy*
generator without shipping one.
"""

from __future__ import annotations

import hashlib
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

# --------------------------------------------------------------------------
# Float provenance fixtures (issue #2279)
# --------------------------------------------------------------------------

# Host-libm float computation feeding committed bytes. On *this* host every
# run produces identical bytes -- the last ulp of math.pow is allowed to
# differ between conforming libms, so the divergence needs a different
# interpreter/platform to observe. That is exactly why the class ships, and
# why it is caught statically rather than by the byte-compare.
HOST_FLOAT_MATH_POW = """
import json, math, pathlib
out = pathlib.Path(__file__).resolve().parent.parent / "artifacts"
out.mkdir(parents=True, exist_ok=True)
gains = [round(math.pow(1.001, n), 9) for n in range(8)]
(out / "report.json").write_text(json.dumps({"gains": gains}, indent=2))
"""

# The same hazard behind an aliased import: `from math import pow` makes the
# call site a bare `pow(...)` -- the scan must follow the import, not the
# spelling at the call site.
HOST_FLOAT_FROM_IMPORT = """
import json, pathlib
from math import pow
out = pathlib.Path(__file__).resolve().parent.parent / "artifacts"
out.mkdir(parents=True, exist_ok=True)
gains = [round(pow(1.001, n), 9) for n in range(4)]
(out / "report.json").write_text(json.dumps({"gains": gains}, indent=2))
"""

# On CPython, float `**` *is* libm pow -- the operator spelling changes
# nothing about the host dependence.
HOST_FLOAT_POW_OPERATOR = """
import json, pathlib
out = pathlib.Path(__file__).resolve().parent.parent / "artifacts"
out.mkdir(parents=True, exist_ok=True)
hypots = [round(n ** 0.5, 9) for n in range(8)]
(out / "report.json").write_text(json.dumps({"hypots": hypots}, indent=2))
"""

# A float that genuinely diverges across the check's own two runs: the
# exponent is hash()-derived, so the two PYTHONHASHSEED values feed math.pow
# different inputs and the bytes differ -- the CI job's dynamic half catching
# a cross-seed float divergence, with the static scan riding along.
FLOAT_CROSS_SEED = """
import json, math, pathlib
out = pathlib.Path(__file__).resolve().parent.parent / "artifacts"
out.mkdir(parents=True, exist_ok=True)
gain = round(math.pow(1.001, hash("gain") % 97), 9)
(out / "report.json").write_text(json.dumps({"gain": gain}, indent=2))
"""

# The cross-path twin: the float is derived from the checkout's absolute
# path (and the path itself is echoed), so the two checkouts at different
# paths produce different bytes.
FLOAT_CROSS_PATH = """
import json, math, pathlib
root = pathlib.Path(__file__).resolve().parent.parent
out = root / "artifacts"
out.mkdir(parents=True, exist_ok=True)
bias = round(math.exp(len(str(root)) * 0.01), 9)
(out / "report.json").write_text(
    json.dumps({"root": str(root), "bias": bias}, indent=2)
)
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


def _sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _pinned_manifest(
    tmp_path: Path,
    body: str,
    *,
    generator_sha256: str | None = None,
    drift_second_checkout: bool = False,
) -> tuple[Path, Path, Path]:
    """Two synthetic checkouts carrying a *committed, pinned* artifact (the
    issue #2279 escape hatch): the generator runs once here to produce the
    "committed" bytes, and the manifest pins both the generator source hash
    and the artifact content hash, exactly as a real pin would.
    `drift_second_checkout` optionally touches the second checkout's
    committed bytes after the pin.
    """
    a = _checkout(tmp_path, "primary", body)
    b = _checkout(tmp_path, "alt/a-second-checkout-at-another-path", body)
    for checkout in (a, b):
        subprocess.run(
            [sys.executable, str(checkout / "gen" / "generate.py")], check=True
        )
    content = (a / "artifacts" / "report.json").read_bytes()
    if drift_second_checkout:
        # The bytes moved after pinning -- the pin no longer matches.
        (b / "artifacts" / "report.json").write_bytes(
            content + b"\n<!-- touched after pinning -->\n"
        )
    entry: dict[str, object] = {
        "script": "gen/generate.py",
        "artifacts": ["artifacts/*.json"],
        "float_discipline": "pinned-artifact",
        "generator_sha256": generator_sha256 or _sha256_hex(body.encode()),
        "regenerate": "python3 gen/generate.py",
        "artifact_sha256": {"artifacts/report.json": _sha256_hex(content)},
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"schema_version": 1, "generators": [entry]}))
    return a, b, path


def _run_with_manifest_entry(
    tmp_path: Path, entry: dict[str, object], *, body: str = DETERMINISTIC
) -> subprocess.CompletedProcess:
    """Run the check over two synthetic checkouts with one caller-written
    manifest entry -- for exercising the manifest validation itself."""
    a = _checkout(tmp_path, "primary", body)
    b = _checkout(tmp_path, "alt/second", body)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"schema_version": 1, "generators": [entry]}))
    return _run(
        "--checkout",
        str(a),
        "--checkout",
        str(b),
        "--seed",
        "1",
        "--seed",
        "2",
        "--manifest",
        str(manifest),
        "--format",
        "json",
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
# Negative control 3: host-libm float provenance (issue #2279)
# --------------------------------------------------------------------------


def test_host_float_generator_byte_compares_clean_on_one_host(
    tmp_path: Path,
) -> None:
    """The control half of the float negative control: on a single host and
    interpreter, regenerating twice -- even under two different hash seeds --
    reproduces byte-identical output, because last-ulp libm divergence needs
    a *different* libm to observe. This is precisely why the class ships
    past both a normal suite and this check's own byte-compare, and why the
    check catches it statically instead."""
    checkout = _checkout(tmp_path, "primary", HOST_FLOAT_MATH_POW)
    script = checkout / "gen" / "generate.py"
    artifact = checkout / "artifacts" / "report.json"

    subprocess.run(
        [sys.executable, str(script)],
        env=dict(os.environ, PYTHONHASHSEED="12345"),
        check=True,
    )
    first = artifact.read_bytes()
    subprocess.run(
        [sys.executable, str(script)],
        env=dict(os.environ, PYTHONHASHSEED="67890"),
        check=True,
    )
    assert artifact.read_bytes() == first


def test_host_libm_float_generator_is_flagged_by_the_static_scan(
    tmp_path: Path,
) -> None:
    """A generator computing committed bytes through host libm is flagged
    even though both of the check's own runs byte-compare clean."""
    result = _check(tmp_path, HOST_FLOAT_MATH_POW)
    assert result.returncode == EXIT_DIFFER, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "differ"
    assert payload["findings"], "math.pow generator passed the check"
    kinds = {finding["kind"] for finding in payload["findings"]}
    assert kinds == {"host_float_op"}
    finding = payload["findings"][0]
    assert finding["artifact"] == "gen/generate.py"
    assert "math.pow" in finding["detail"]
    # The remedy is in the finding, not just the complaint.
    assert "pinned-artifact" in finding["detail"]


def test_float_op_imported_from_math_is_flagged(tmp_path: Path) -> None:
    result = _check(tmp_path, HOST_FLOAT_FROM_IMPORT)
    assert result.returncode == EXIT_DIFFER, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    kinds = {finding["kind"] for finding in payload["findings"]}
    assert kinds == {"host_float_op"}
    assert "imported from math" in payload["findings"][0]["detail"]


def test_float_power_operator_with_literal_is_flagged(tmp_path: Path) -> None:
    result = _check(tmp_path, HOST_FLOAT_POW_OPERATOR)
    assert result.returncode == EXIT_DIFFER, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    kinds = {finding["kind"] for finding in payload["findings"]}
    assert kinds == {"host_float_op"}


def test_cross_seed_float_divergence_is_caught(tmp_path: Path) -> None:
    """The acceptance fixture for issue #2279: a float that genuinely
    diverges across the check's two hash seeds is caught by the dynamic
    byte-compare (`unstable_ordering`), and the host-libm provenance behind
    it is named by the static scan (`host_float_op`) in the same run."""
    result = _check(tmp_path, FLOAT_CROSS_SEED)
    assert result.returncode == EXIT_DIFFER, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    # Both halves of the catch are named: the artifact (dynamic byte-compare)
    # and the generator script carrying the libm call (static scan).
    assert set(payload["differing_artifacts"]) == {
        "artifacts/report.json",
        "gen/generate.py",
    }
    kinds = {finding["kind"] for finding in payload["findings"]}
    assert kinds == {"unstable_ordering", "host_float_op"}


def test_cross_path_float_divergence_is_caught(tmp_path: Path) -> None:
    """The cross-checkout twin: a path-derived float diverges between the two
    checkouts, and the static scan names the libm call behind it."""
    result = _check(tmp_path, FLOAT_CROSS_PATH)
    assert result.returncode == EXIT_DIFFER, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert "artifacts/report.json" in payload["differing_artifacts"]
    kinds = {finding["kind"] for finding in payload["findings"]}
    # `host_path` rides along (the echoed path is host-absolute), exactly as
    # in the ABSOLUTE_PATH control above; what matters is that the byte
    # difference is attributed to the checkout path, and that the static scan
    # names the libm call behind the float.
    assert "path_dependent" in kinds
    assert "unstable_ordering" not in kinds
    assert "host_float_op" in kinds


def test_pinned_artifact_pattern_passes(tmp_path: Path) -> None:
    """The escape hatch (issue #2279): the *same* math.pow generator,
    committed and pinned with its generator hash, regeneration command, and
    artifact content hashes, passes -- and is verified, not regenerated
    (`artifact_count` 0: the pinned generator never ran)."""
    a, b, manifest = _pinned_manifest(tmp_path, HOST_FLOAT_MATH_POW)
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
        str(manifest),
        "--format",
        "json",
    )
    assert result.returncode == EXIT_OK, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"
    assert payload["findings"] == []
    assert payload["artifact_count"] == 0
    assert payload["pinned_artifact_count"] == 1


def test_stale_generator_pin_is_flagged(tmp_path: Path) -> None:
    """The generator moved after the pin: the committed artifacts can no
    longer be attributed to the committed generator."""
    a, b, manifest = _pinned_manifest(
        tmp_path, HOST_FLOAT_MATH_POW, generator_sha256=_sha256_hex(b"older source")
    )
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
        str(manifest),
        "--format",
        "json",
    )
    assert result.returncode == EXIT_DIFFER, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "differ"
    kinds = {finding["kind"] for finding in payload["findings"]}
    assert kinds == {"stale_pin"}
    assert payload["findings"][0]["artifact"] == "gen/generate.py"


def test_pinned_artifact_drift_is_flagged(tmp_path: Path) -> None:
    """The committed bytes moved after the pin: drift detection compares
    against the pin, which is the whole point of the escape hatch."""
    a, b, manifest = _pinned_manifest(
        tmp_path, HOST_FLOAT_MATH_POW, drift_second_checkout=True
    )
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
        str(manifest),
        "--format",
        "json",
    )
    assert result.returncode == EXIT_DIFFER, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "differ"
    kinds = {finding["kind"] for finding in payload["findings"]}
    assert kinds == {"pin_drift"}
    assert payload["findings"][0]["artifact"] == "artifacts/report.json"


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


def test_unknown_float_discipline_cannot_run(tmp_path: Path) -> None:
    """A typo'd discipline (`integer_exact`) must be exit 2, never a silent
    fall-through to the default scan -- the same loudness `schema_version`
    is held to."""
    result = _run_with_manifest_entry(
        tmp_path,
        {
            "script": "gen/generate.py",
            "artifacts": ["artifacts/*.json"],
            "float_discipline": "integer_exact",
        },
    )
    assert result.returncode == EXIT_CANNOT_RUN, result.stdout + result.stderr
    assert "float_discipline" in result.stderr


def test_pin_fields_without_pinned_discipline_cannot_run(tmp_path: Path) -> None:
    """A `generator_sha256` recorded against `integer-exact` would look like
    a pin while nothing verified it -- refused at parse time."""
    result = _run_with_manifest_entry(
        tmp_path,
        {
            "script": "gen/generate.py",
            "artifacts": ["artifacts/*.json"],
            "float_discipline": "integer-exact",
            "generator_sha256": _sha256_hex(b"x"),
        },
    )
    assert result.returncode == EXIT_CANNOT_RUN, result.stdout + result.stderr
    assert "only mean something" in result.stderr


def test_pinned_entry_missing_pin_fields_cannot_run(tmp_path: Path) -> None:
    result = _run_with_manifest_entry(
        tmp_path,
        {
            "script": "gen/generate.py",
            "artifacts": ["artifacts/*.json"],
            "float_discipline": "pinned-artifact",
        },
    )
    assert result.returncode == EXIT_CANNOT_RUN, result.stdout + result.stderr
    assert "generator_sha256" in result.stderr


def test_pinned_entry_empty_artifact_map_cannot_run(tmp_path: Path) -> None:
    """A pinned entry that pins no bytes pins nothing and verifies nothing."""
    result = _run_with_manifest_entry(
        tmp_path,
        {
            "script": "gen/generate.py",
            "artifacts": ["artifacts/*.json"],
            "float_discipline": "pinned-artifact",
            "generator_sha256": _sha256_hex(b"x"),
            "regenerate": "python3 gen/generate.py",
            "artifact_sha256": {},
        },
    )
    assert result.returncode == EXIT_CANNOT_RUN, result.stdout + result.stderr
    assert "artifact_sha256" in result.stderr


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
# Declared platform-variable regions + failure forensics (issue #2275)
# --------------------------------------------------------------------------

# The synthetic checkouts are at path lengths 7 ("primary") and 37
# ("alt/a-second-checkout-at-another-path") -- a difference of 30, so a
# `len % 7` digest selector differs between the two runs by 2 modulo 7 (2
# or 5 units depending on where the runner's tmp path length lands the
# first run in the cycle): the drift is deterministic -- never zero, never
# huge -- on every host, interpreter, and CI runner, and exactly measurable
# in ULPs.
#
# `ulp = 2^-52`; `1.0 + k * 4 * ulp` is exact for k <= 6 (both operands
# representable, sum representable in [1, 2)), so two runs' digests differ
# by exactly `4 * |k1 - k2|` ULPs -- 8 or 20 here -- and a declared
# threshold of 32 admits either while still refusing the 400-ulp-step
# fixture below.
PLATFORM_DRIFT = """
import json, pathlib
root = pathlib.Path(__file__).resolve().parent.parent
out = root / "artifacts"
out.mkdir(parents=True, exist_ok=True)
ulp = 2.220446049250313e-16
k = len(str(root)) % 7
digest = 1.0 + k * 4 * ulp
(out / "digests.json").write_text(
    json.dumps({"digests": {"gain": digest}}, indent=2)
)
"""

# The same path-derived drift, but 400 ulp per step: 800 ulp apart, far
# outside any honest threshold.
PLATFORM_DRIFT_HUGE = """
import json, pathlib
root = pathlib.Path(__file__).resolve().parent.parent
out = root / "artifacts"
out.mkdir(parents=True, exist_ok=True)
ulp = 2.220446049250313e-16
k = len(str(root)) % 7
digest = 1.0 + k * 400 * ulp
(out / "digests.json").write_text(
    json.dumps({"digests": {"gain": digest}}, indent=2)
)
"""

# Declared field drifts *and* an undeclared field drifts: the guarantee
# bounds the declared region only -- the undeclared one must still fail.
PLATFORM_DRIFT_WITH_LEAK = """
import json, pathlib
root = pathlib.Path(__file__).resolve().parent.parent
out = root / "artifacts"
out.mkdir(parents=True, exist_ok=True)
ulp = 2.220446049250313e-16
digest = 1.0 + (len(str(root)) % 7) * 4 * ulp
(out / "digests.json").write_text(
    json.dumps(
        {"digests": {"gain": digest}, "meta": {"count": len(str(root))}},
        indent=2,
    )
)
"""

# Structural drift (list length derived from the checkout path): 0 elements
# in one run, 2 in the other -- no field threshold can paper over a shape
# change, declared or not.
PLATFORM_STRUCTURAL = """
import json, pathlib
root = pathlib.Path(__file__).resolve().parent.parent
out = root / "artifacts"
out.mkdir(parents=True, exist_ok=True)
n = len(str(root)) % 7
(out / "digests.json").write_text(
    json.dumps({"digests": [0.5] * n}, indent=2)
)
"""

# Declared format json, payload not json: the declaration must refuse to
# check nothing and stay green.
PLATFORM_UNPARSEABLE = """
import pathlib
out = pathlib.Path(__file__).resolve().parent.parent / "artifacts"
out.mkdir(parents=True, exist_ok=True)
(out / "digests.json").write_text("not json at all")
"""

# A deterministic numeric artifact, for the declared-but-no-drift case.
DETERMINISTIC_NUMERIC = """
import json, pathlib
out = pathlib.Path(__file__).resolve().parent.parent / "artifacts"
out.mkdir(parents=True, exist_ok=True)
(out / "digests.json").write_text(json.dumps({"counts": {"a": 3}}, indent=2))
"""


def _pv_manifest_entry(
    block: dict[str, object] | None = None, **overrides: object
) -> dict[str, object]:
    """A valid `platform-variable` manifest entry over gen/generate.py --
    the declared-guarantee shape a real manifest would carry (issue
    #2275). `overrides` replace keys *inside* the platform_variable block;
    `block` replaces the whole block (for delete-a-key validation tests)."""
    merged: dict[str, object] = {
        "artifacts": ["artifacts/digests.json"],
        "format": "json",
        "guarantee": "libm-transcendental-digest",
        "max_abs_ulps": 32,
        "fields": ["digests.gain"],
    }
    merged.update(overrides)
    if block is not None:
        merged = block
    return {
        "script": "gen/generate.py",
        "artifacts": ["artifacts/*.json"],
        "float_discipline": "platform-variable",
        "platform_variable": merged,
    }


def _run_with_pv_entry(
    tmp_path: Path,
    entry: dict[str, object],
    *,
    body: str = PLATFORM_DRIFT,
    extra_args: tuple[str, ...] = (),
    tag: str = "",
) -> subprocess.CompletedProcess:
    """Two synthetic checkouts + a caller-written manifest entry (the
    platform-variable analogue of `_run_with_manifest_entry`, which cannot
    express the block). `tag` distinguishes repeated calls in one
    `tmp_path`."""
    a = _checkout(tmp_path, f"primary{tag}", body)
    b = _checkout(tmp_path, f"alt/a-second-checkout-at-another-path{tag}", body)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"schema_version": 1, "generators": [entry]}))
    return _run(
        "--checkout",
        str(a),
        "--checkout",
        str(b),
        "--seed",
        "1",
        "--seed",
        "2",
        "--manifest",
        str(manifest),
        "--format",
        "json",
        *extra_args,
    )


def test_platform_variable_drift_within_guarantee_passes_and_is_named(
    tmp_path: Path,
) -> None:
    """Acceptance (a) of issue #2275: a declared platform-variable artifact
    whose bytes differ -- within its declared threshold -- passes, and the
    accepted drift is named in the machine-readable output, not swallowed."""
    result = _run_with_pv_entry(tmp_path, _pv_manifest_entry())
    assert result.returncode == EXIT_OK, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"
    assert payload["findings"] == []
    (comparison,) = payload["platform_variable"]
    assert comparison["artifact"] == "artifacts/digests.json"
    assert comparison["guarantee"] == "libm-transcendental-digest"
    assert comparison["max_abs_ulps"] == 32
    # Exactly 8 or 20 ulp, by construction (path lengths differing by 30,
    # selector % 7, 4 ulp per unit) -- a threshold is only honest if the
    # report measures.
    assert comparison["max_observed_ulps"] in (8, 20)
    (moved,) = comparison["moved"]
    assert moved["field"] == "digests.gain"
    assert moved["ulp"] == comparison["max_observed_ulps"]


def test_platform_variable_declaration_and_drift_are_named_in_text(
    tmp_path: Path,
) -> None:
    """The declaration is loud (issue #2275): even on a green run the report
    names every threshold-compared artifact and its guarantee, and the
    accepted drift gets its own OK line -- a green must never masquerade as
    byte-identical everywhere."""
    a = _checkout(tmp_path, "primary", PLATFORM_DRIFT)
    b = _checkout(tmp_path, "alt/a-second-checkout-at-another-path", PLATFORM_DRIFT)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"schema_version": 1, "generators": [_pv_manifest_entry()]})
    )
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
        str(manifest),
        "--format",
        "text",
    )
    assert result.returncode == EXIT_OK, result.stdout + result.stderr
    assert "platform-variable artifacts" in result.stdout
    assert "artifacts/digests.json" in result.stdout
    assert "libm-transcendental-digest" in result.stdout
    assert "OK (declared platform-variable drift)" in result.stdout
    assert "ulp (declared max 32)" in result.stdout
    assert "digests.gain=" in result.stdout


def test_platform_variable_artifact_is_named_even_without_drift(
    tmp_path: Path,
) -> None:
    """A declared artifact that happened to come out byte-identical is still
    listed as threshold-compared: the header documents the comparison style
    actually in force, not just the exceptions."""
    result = _run_with_pv_entry(
        tmp_path,
        _pv_manifest_entry(),
        body=DETERMINISTIC_NUMERIC,
    )
    assert result.returncode == EXIT_OK, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    (comparison,) = payload["platform_variable"]
    assert comparison["artifact"] == "artifacts/digests.json"
    assert comparison["moved"] == []
    assert comparison["max_observed_ulps"] == 0


def test_undeclared_drift_still_fails(tmp_path: Path) -> None:
    """Acceptance (b) of issue #2275: the identical drifting generator
    without a declaration still fails -- the mechanism is per-artifact
    opt-in, never a blanket tolerance."""
    result = _run_with_pv_entry(
        tmp_path,
        {
            "script": "gen/generate.py",
            "artifacts": ["artifacts/*.json"],
        },
    )
    assert result.returncode == EXIT_DIFFER, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "differ"
    assert payload["differing_artifacts"] == ["artifacts/digests.json"]
    assert payload["platform_variable"] == []


def test_platform_variable_drift_beyond_threshold_fails(tmp_path: Path) -> None:
    """A declared field that moves further than its declared threshold is a
    finding, not an accepted drift: the guarantee bounds, it does not excuse."""
    result = _run_with_pv_entry(
        tmp_path, _pv_manifest_entry(), body=PLATFORM_DRIFT_HUGE
    )
    assert result.returncode == EXIT_DIFFER, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    (finding,) = payload["findings"]
    assert finding["kind"] == "platform_variable_exceeded"
    assert finding["artifact"] == "artifacts/digests.json"
    assert "ulp (declared max 32)" in finding["detail"]


def test_exceeded_declaration_is_annotated_as_a_file(tmp_path: Path) -> None:
    result = _run_with_pv_entry(
        tmp_path,
        _pv_manifest_entry(),
        body=PLATFORM_DRIFT_HUGE,
        extra_args=("--annotate",),
    )
    assert result.returncode == EXIT_DIFFER, result.stdout + result.stderr
    annotations = [ln for ln in result.stdout.splitlines() if ln.startswith("::error")]
    assert annotations
    assert all("file=artifacts/digests.json" in ln for ln in annotations)
    assert all("platform_variable_exceeded" in ln for ln in annotations)


def test_undeclared_region_inside_a_declared_artifact_fails(
    tmp_path: Path,
) -> None:
    """Byte-drift inside a non-declared region of a *declared* artifact
    still fails, and names the leaking field: declaring one region never
    loosens its neighbours (issue #2275's no-blanket-tolerance rule)."""
    result = _run_with_pv_entry(
        tmp_path, _pv_manifest_entry(), body=PLATFORM_DRIFT_WITH_LEAK
    )
    assert result.returncode == EXIT_DIFFER, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    (finding,) = payload["findings"]
    assert finding["kind"] == "platform_variable_region_leak"
    assert finding["artifact"] == "artifacts/digests.json"
    assert "meta.count" in finding["detail"]


def test_structural_drift_inside_a_declared_artifact_fails(
    tmp_path: Path,
) -> None:
    """A shape change (list length) is a finding even under a declaration:
    a ULP guarantee over fields that no longer line up says nothing."""
    result = _run_with_pv_entry(
        tmp_path,
        _pv_manifest_entry(fields=["digests.*"]),
        body=PLATFORM_STRUCTURAL,
    )
    assert result.returncode == EXIT_DIFFER, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    (finding,) = payload["findings"]
    assert finding["kind"] == "platform_variable_structure"
    assert "list length changed" in finding["detail"]


def test_unparseable_declared_artifact_fails(tmp_path: Path) -> None:
    """Declaring format json while emitting non-JSON must fail loudly: a
    guarantee that checks nothing is worse than none."""
    result = _run_with_pv_entry(
        tmp_path, _pv_manifest_entry(), body=PLATFORM_UNPARSEABLE
    )
    assert result.returncode == EXIT_DIFFER, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    kinds = {finding["kind"] for finding in payload["findings"]}
    assert kinds == {"platform_variable_unparseable"}
    assert "does not parse as JSON" in payload["findings"][0]["detail"]


# --------------------------------------------------------------------------
# Forensics on failure (issue #2275)
# --------------------------------------------------------------------------


def test_forensics_writes_both_variants_reports_and_recipe(tmp_path: Path) -> None:
    """Acceptance (c) of issue #2275: on failure the check writes the
    evidence pack -- both variants of the differing artifact (so the
    bit-identical-inputs question can be answered from CI artifacts alone),
    both reports, and a rerun script carrying the exact seeds and
    checkouts."""
    a = _checkout(tmp_path, "primary", SET_ORDERING)
    b = _checkout(tmp_path, "alt/second", SET_ORDERING)
    forensics = tmp_path / "forensics"
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
        "--forensics-dir",
        str(forensics),
    )
    assert result.returncode == EXIT_DIFFER, result.stdout + result.stderr

    first = forensics / "artifacts" / "run-1" / "artifacts" / "report.json"
    second = forensics / "artifacts" / "run-2" / "artifacts" / "report.json"
    assert first.is_file() and second.is_file()
    assert first.read_bytes() != second.read_bytes()
    # The variants are the actual run outputs, not re-generations.
    assert first.read_bytes() == (a / "artifacts" / "report.json").read_bytes()
    assert second.read_bytes() == (b / "artifacts" / "report.json").read_bytes()

    report = json.loads((forensics / "report.json").read_text())
    assert report["status"] == "differ"
    assert report["differing_artifacts"] == ["artifacts/report.json"]
    text = (forensics / "report.txt").read_text()
    assert "Differing artifact paths:" in text
    assert "Reproduce this failure" in text

    recipe = (forensics / "rerun.sh").read_text()
    assert "--seed 1" in recipe and "--seed 2" in recipe
    assert str(a) in recipe and str(b) in recipe
    assert "check_artifact_determinism.py" in recipe


def test_failing_text_report_echoes_the_rerun_recipe(tmp_path: Path) -> None:
    """The recipe is echoed in the report itself, not only written to the
    forensics directory -- the log alone must be enough to re-run exactly."""
    result = _check(tmp_path, SET_ORDERING, fmt="text")
    assert result.returncode == EXIT_DIFFER, result.stdout + result.stderr
    assert "Reproduce this failure (rerun recipe" in result.stdout
    assert "--seed 1" in result.stdout and "--seed 2" in result.stdout
    assert "Triage before blaming code" in result.stdout


def test_forensics_directory_not_created_on_a_green_run(tmp_path: Path) -> None:
    a = _checkout(tmp_path, "primary", DETERMINISTIC)
    b = _checkout(tmp_path, "alt/second", DETERMINISTIC)
    forensics = tmp_path / "forensics"
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
        "--forensics-dir",
        str(forensics),
    )
    assert result.returncode == EXIT_OK, result.stdout + result.stderr
    assert not forensics.exists()


def test_forensics_written_even_when_the_check_cannot_run(tmp_path: Path) -> None:
    """Exit 2 writes why, so the uploaded evidence explains its own
    absence -- a forensics upload that finds nothing must mean a green, not
    a crash."""
    forensics = tmp_path / "forensics"
    a = _checkout(tmp_path, "primary2", FAILING)
    b = _checkout(tmp_path, "alt2/second", FAILING)
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
        "--forensics-dir",
        str(forensics),
    )
    assert result.returncode == EXIT_CANNOT_RUN, result.stdout + result.stderr
    report = json.loads((forensics / "report.json").read_text())
    assert report["status"] == "cannot_run"
    assert "gen/generate.py" in report["error"]
    assert not (forensics / "artifacts").exists()


# --------------------------------------------------------------------------
# platform_variable manifest validation (issue #2275: loudness rules)
# --------------------------------------------------------------------------


def test_platform_variable_block_on_integer_exact_cannot_run(tmp_path: Path) -> None:
    """A declaration on an entry the check byte-compares anyway could never
    bind -- refused at parse time, like pin fields on a non-pinned entry."""
    result = _run_with_manifest_entry(
        tmp_path,
        {
            "script": "gen/generate.py",
            "artifacts": ["artifacts/*.json"],
            "float_discipline": "integer-exact",
            "platform_variable": _pv_manifest_entry()["platform_variable"],
        },
    )
    assert result.returncode == EXIT_CANNOT_RUN, result.stdout + result.stderr
    assert "only mean something" in result.stderr


def test_platform_variable_discipline_without_block_cannot_run(
    tmp_path: Path,
) -> None:
    """A declared discipline with no guarantee format + threshold is a
    blanket tolerance in disguise -- the exact thing issue #2275 rules out."""
    result = _run_with_pv_entry(
        tmp_path,
        {
            "script": "gen/generate.py",
            "artifacts": ["artifacts/*.json"],
            "float_discipline": "platform-variable",
        },
    )
    assert result.returncode == EXIT_CANNOT_RUN, result.stdout + result.stderr
    assert "platform_variable" in result.stderr


def test_platform_variable_missing_required_keys_cannot_run(tmp_path: Path) -> None:
    block = dict(_pv_manifest_entry()["platform_variable"])
    del block["max_abs_ulps"]
    result = _run_with_pv_entry(tmp_path, _pv_manifest_entry(block=block))
    assert result.returncode == EXIT_CANNOT_RUN, result.stdout + result.stderr
    assert "max_abs_ulps" in result.stderr


def test_platform_variable_unknown_key_cannot_run(tmp_path: Path) -> None:
    """A typo'd extra key (`max_abs_ulp` alongside a valid `max_abs_ulps`)
    must fail loudly, not be quietly ignored."""
    block = dict(_pv_manifest_entry()["platform_variable"])
    block["max_abs_ulp"] = block["max_abs_ulps"]
    result = _run_with_pv_entry(tmp_path, _pv_manifest_entry(block=block))
    assert result.returncode == EXIT_CANNOT_RUN, result.stdout + result.stderr
    assert "unknown platform_variable key" in result.stderr


def test_platform_variable_zero_threshold_cannot_run(tmp_path: Path) -> None:
    """0 ulp is byte-exact, which is what not declaring the region means."""
    result = _run_with_pv_entry(tmp_path, _pv_manifest_entry(max_abs_ulps=0))
    assert result.returncode == EXIT_CANNOT_RUN, result.stdout + result.stderr
    assert "integer >= 1" in result.stderr


def test_platform_variable_unsupported_format_cannot_run(tmp_path: Path) -> None:
    result = _run_with_pv_entry(tmp_path, _pv_manifest_entry(format="csv"))
    assert result.returncode == EXIT_CANNOT_RUN, result.stdout + result.stderr
    assert "not supported" in result.stderr


def test_platform_variable_empty_or_malformed_fields_cannot_run(
    tmp_path: Path,
) -> None:
    """Empty, empty-segment, and space-bearing patterns are refused at load:
    a malformed field path would declare a region that matches nothing."""
    for index, fields in enumerate(([], [""], ["digests..gain"], ["digests gain"])):
        result = _run_with_pv_entry(
            tmp_path, _pv_manifest_entry(fields=fields), tag=f"-{index}"
        )
        assert result.returncode == EXIT_CANNOT_RUN, result.stdout + result.stderr
        assert "dotted leaf paths" in result.stderr


def test_platform_variable_block_on_pinned_entry_cannot_run(tmp_path: Path) -> None:
    """A pinned artifact's guarantee is the pin -- it is never regenerated,
    so a threshold declaration could never bind."""
    result = _run_with_manifest_entry(
        tmp_path,
        {
            "script": "gen/generate.py",
            "artifacts": ["artifacts/*.json"],
            "float_discipline": "pinned-artifact",
            "generator_sha256": _sha256_hex(b"x"),
            "regenerate": "python3 gen/generate.py",
            "artifact_sha256": {"artifacts/report.json": _sha256_hex(b"y")},
            "platform_variable": _pv_manifest_entry()["platform_variable"],
        },
    )
    assert result.returncode == EXIT_CANNOT_RUN, result.stdout + result.stderr
    assert "only mean something" in result.stderr


def test_platform_variable_generator_is_not_float_scanned(tmp_path: Path) -> None:
    """The scan skip is the point of the discipline: the *same* generator
    -- a host-libm `**` riding along with the committed digest -- is a
    `host_float_op` finding under `integer-exact`, but under its
    declaration the check is dynamic instead: no static finding, only the
    threshold."""
    entry = _pv_manifest_entry()
    integer_exact = {
        "script": entry["script"],
        "artifacts": entry["artifacts"],
        "float_discipline": "integer-exact",
    }
    body = PLATFORM_DRIFT + "\ncoefficient = 2.0 ** 3\n"
    result = _run_with_pv_entry(tmp_path, integer_exact, body=body)
    assert result.returncode == EXIT_DIFFER, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    kinds = {finding["kind"] for finding in payload["findings"]}
    assert "host_float_op" in kinds

    declared = _run_with_pv_entry(tmp_path, entry, body=body, tag="-declared")
    assert declared.returncode == EXIT_OK, declared.stdout + declared.stderr
    assert "host_float_op" not in declared.stdout


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


def test_default_manifest_generators_declare_integer_exact_and_scan_clean() -> None:
    """The shipped float disciplines (issue #2279) must stay honest twice
    over: declared as the vocabulary the check speaks, and actually clean
    under the scan -- a generator that starts computing committed bytes
    through host libm fails here before it can ship a host-dependent
    artifact. Mirrors
    `test_real_repo_artifacts_carry_no_host_absolute_paths`' role for the
    path scan."""
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    try:
        import check_artifact_determinism as checker
    finally:
        sys.path.pop(0)

    generators = checker.load_generators(None)
    assert generators
    for generator in generators:
        assert generator.float_discipline == "integer-exact", (
            f"unexpected discipline for {generator.script}"
        )
        source = (REPO_ROOT / generator.script).read_text(encoding="utf-8")
        ops = checker.scan_float_ops(source, generator.script)
        assert not ops, f"host-libm float op in committed-artifact generator: {ops}"


def _checkout_step_paths(job: str) -> list[str]:
    """The `path:` values of the job's `actions/checkout` steps only --
    scoped per step, so another step's `path:` (an artifact upload's, say)
    cannot be mistaken for a checkout location."""
    import re

    steps = re.split(r"\n(?=      - )", job)
    paths = []
    for step in steps:
        if "actions/checkout" not in step:
            continue
        paths.extend(re.findall(r"^\s+path:\s+(\S+)\s*$", step, re.MULTILINE))
    return paths


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
    checkout_paths = _checkout_step_paths(job)
    assert len(set(checkout_paths)) == 2, checkout_paths
    for path in checkout_paths:
        assert any(passed_path.endswith(path) for passed_path in passed), path


def test_determinism_job_uploads_failure_forensics() -> None:
    """Issue #2275 deliverable: a failing byte-compare must arrive with its
    evidence -- the job passes --forensics-dir and uploads the pack (both
    artifact variants + reports + rerun script) on failure, retained like
    the repo's other evidence artifacts."""
    import re

    job = _job_block(WORKFLOW.read_text(), "artifact-determinism")
    assert "--forensics-dir" in job
    upload = [s for s in re.split(r"\n(?=      - )", job) if "upload-artifact" in s]
    assert len(upload) == 1, upload
    step = upload[0]
    assert "if: failure()" in step
    assert "retention-days: 90" in step
    assert "if-no-files-found: ignore" in step
    # The `with:` block's name, not the step's display name (both are
    # spelled `name:`) -- anchored to its own line for exactly that reason.
    name = re.search(r"^\s+name:\s+(\S+)\s*$", step, re.MULTILINE)
    assert name is not None and name.group(1) == "determinism-forensics"


def test_numerical_job_uploads_failure_forensics_under_a_distinct_name() -> None:
    """The dep-full drift-detector leg (issue #2276) gets the same forensics
    upload (issue #2275) -- under a DIFFERENT artifact name, since both jobs
    can fail in one workflow run and a shared name would 409 the upload."""
    import re

    job = _job_block(WORKFLOW.read_text(), "test-numerical")
    assert "--forensics-dir" in job
    upload = [s for s in re.split(r"\n(?=      - )", job) if "upload-artifact" in s]
    assert len(upload) == 1, upload
    step = upload[0]
    assert "if: failure()" in step
    assert "retention-days: 90" in step
    name = re.search(r"^\s+name:\s+(\S+)\s*$", step, re.MULTILINE)
    assert name is not None and name.group(1) == "determinism-forensics-numerical"
    # The forensics directory must live OUTSIDE both checkouts: it is
    # written after the runs complete, and writing inside a checkout would
    # leave the tree dirty for any subsequent inspection.
    assert "determinism-forensics" in job
    assert "$GITHUB_WORKSPACE/determinism-forensics" in job


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
