"""Tests for `scripts/ci-apt-install.sh` (issue #1219): the mirror-resilient
`apt-get update && apt-get install` wrapper CI's Python test-matrix job uses
for its three package-install steps.

The behavioural tests never invoke the real apt: they put a fake `apt-get`
(and a fake `dpkg`) on `PATH` and run the script with `CI_APT_NO_SUDO=1`, so
every retry/timeout/fatal-error path is exercised deterministically and in
seconds. Two failure signatures from the incident that motivated the script
are reproduced directly -- `update` failing (signature 1) and `update`
succeeding with the following `install` stalling (signature 2) -- because
the second is the reason the retry has to wrap the *pair* rather than just
`apt-get update`.

One test asserts the workflow wiring itself, so a future edit that
reintroduces a bare `sudo apt-get update && sudo apt-get install` into
`.github/workflows/ci.yml` fails here rather than silently re-exposing CI to
the mirror stall.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
SCRIPT = REPO_ROOT / "scripts" / "ci-apt-install.sh"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

# Every fake apt-get invocation appends its argv to this file, so tests can
# assert on attempt counts and on the options the script passed.
_FAKE_APT = r"""#!/usr/bin/env bash
echo "$*" >>"$FAKE_APT_LOG"

verb=""
for arg in "$@"; do
    case "$arg" in
        update|install) verb="$arg"; break ;;
    esac
done

updates="$(grep -c ' update' "$FAKE_APT_LOG" || true)"
installs="$(grep -c ' install ' "$FAKE_APT_LOG" || true)"

case "$FAKE_APT_MODE" in
    ok)
        echo "Reading package lists... Done"
        exit 0
        ;;
    fail_first_update)
        if [[ "$verb" == "update" && "$updates" -eq 1 ]]; then
            echo "E: Failed to fetch .../InRelease  Connection timed out" >&2
            exit 1
        fi
        exit 0
        ;;
    fail_first_install)
        if [[ "$verb" == "install" && "$installs" -eq 1 ]]; then
            echo "E: Failed to fetch .../ngspice.deb  Connection timed out" >&2
            exit 1
        fi
        exit 0
        ;;
    missing_package)
        if [[ "$verb" == "install" ]]; then
            echo "E: Unable to locate package definitely-not-a-real-package" >&2
            exit 100
        fi
        exit 0
        ;;
    always_fail)
        echo "E: Failed to fetch .../InRelease  Connection timed out" >&2
        exit 1
        ;;
    hang)
        sleep 60
        exit 0
        ;;
esac
exit 0
"""

_FAKE_DPKG = """#!/usr/bin/env bash
exit 0
"""


def _fake_bin(tmp_path: Path) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    apt = bindir / "apt-get"
    apt.write_text(_FAKE_APT)
    apt.chmod(0o755)
    dpkg = bindir / "dpkg"
    dpkg.write_text(_FAKE_DPKG)
    dpkg.chmod(0o755)
    return bindir


def _run(
    tmp_path: Path,
    *packages: str,
    mode: str = "ok",
    **extra_env: str,
) -> tuple[subprocess.CompletedProcess, Path]:
    bindir = _fake_bin(tmp_path)
    apt_log = tmp_path / "apt-invocations.txt"
    apt_log.write_text("")
    env = dict(os.environ)
    env.update(
        {
            "PATH": f"{bindir}{os.pathsep}{env['PATH']}",
            "FAKE_APT_LOG": str(apt_log),
            "FAKE_APT_MODE": mode,
            "CI_APT_NO_SUDO": "1",
            "CI_APT_BACKOFF": "0",
            # No real source files unless a test opts in.
            "CI_APT_SOURCE_FILES": str(tmp_path / "no-such-sources.list"),
        }
    )
    env.update(extra_env)
    proc = subprocess.run(
        [str(SCRIPT), *packages],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    return proc, apt_log


def _invocations(apt_log: Path) -> list[str]:
    return [line for line in apt_log.read_text().splitlines() if line.strip()]


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #


def test_script_is_executable():
    assert os.access(SCRIPT, os.X_OK), (
        f"{SCRIPT} must be committed with the executable bit -- CI invokes it "
        "directly as a `run:` command"
    )


def test_no_packages_is_a_usage_error(tmp_path: Path):
    proc, apt_log = _run(tmp_path)
    assert proc.returncode == 2
    assert "usage" in proc.stderr.lower()
    assert _invocations(apt_log) == []


def test_happy_path_runs_update_then_install_once(tmp_path: Path):
    proc, apt_log = _run(tmp_path, "ngspice")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    calls = _invocations(apt_log)
    assert len(calls) == 2, calls
    assert " update" in calls[0]
    assert calls[1].endswith("install -y ngspice")


def test_bounded_acquire_options_are_passed(tmp_path: Path):
    """A blackholed mirror has to error in seconds rather than hang -- these
    options are what make the retry loop able to fit inside the workflow
    step's own budget."""
    _, apt_log = _run(tmp_path, "ngspice")
    for call in _invocations(apt_log):
        assert "Acquire::Retries=2" in call
        assert "Acquire::http::Timeout=15" in call
        assert "Acquire::https::Timeout=15" in call


def test_multiple_packages_are_installed_in_one_invocation(tmp_path: Path):
    proc, apt_log = _run(tmp_path, "bison", "flex", "gperf")
    assert proc.returncode == 0
    assert _invocations(apt_log)[-1].endswith("install -y bison flex gperf")


# --------------------------------------------------------------------------- #
# Retry behaviour (the two observed failure signatures)
# --------------------------------------------------------------------------- #


def test_retries_when_update_fails(tmp_path: Path):
    """Signature 1: the mirror is unreachable and `apt-get update` itself
    fails."""
    proc, apt_log = _run(tmp_path, "ngspice", mode="fail_first_update")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    calls = _invocations(apt_log)
    # update (fails), update (ok), install (ok)
    assert len(calls) == 3, calls
    assert calls[-1].endswith("install -y ngspice")


def test_retries_the_whole_pair_when_install_fails(tmp_path: Path):
    """Signature 2: `apt-get update` succeeds and the *install* stalls.
    Retrying `update` alone would not help, so the retry re-runs both."""
    proc, apt_log = _run(tmp_path, "ngspice", mode="fail_first_install")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    calls = _invocations(apt_log)
    assert len(calls) == 4, calls
    assert " update" in calls[2], "second attempt must re-run apt-get update"
    assert calls[3].endswith("install -y ngspice")


def test_gives_up_nonzero_after_max_attempts(tmp_path: Path):
    proc, apt_log = _run(
        tmp_path, "ngspice", mode="always_fail", CI_APT_MAX_ATTEMPTS="3"
    )
    assert proc.returncode != 0
    # Three attempts, each failing on `update` before reaching `install`.
    assert len(_invocations(apt_log)) == 3


def test_a_hanging_apt_is_killed_and_does_not_run_forever(tmp_path: Path):
    """The stall this script exists for: apt produces no output and never
    returns. The per-command `timeout(1)` cap must kill it well inside the
    step budget rather than letting it burn the whole thing."""
    started = time.monotonic()
    proc, apt_log = _run(
        tmp_path,
        "ngspice",
        mode="hang",
        CI_APT_MAX_ATTEMPTS="2",
        CI_APT_PER_CMD_TIMEOUT="2",
        CI_APT_DEADLINE="20",
    )
    elapsed = time.monotonic() - started
    assert proc.returncode != 0
    assert elapsed < 30, f"script did not bound the hang (took {elapsed:.1f}s)"
    assert len(_invocations(apt_log)) == 2


def test_overall_deadline_stops_retrying(tmp_path: Path):
    """The retry budget must fit inside the workflow step's own
    `timeout-minutes`, so the deadline -- not the attempt count -- is what
    ends a long stall."""
    proc, _ = _run(
        tmp_path,
        "ngspice",
        mode="hang",
        CI_APT_MAX_ATTEMPTS="20",
        CI_APT_PER_CMD_TIMEOUT="2",
        CI_APT_DEADLINE="6",
    )
    assert proc.returncode != 0
    assert "budget exhausted" in proc.stdout, proc.stdout


# --------------------------------------------------------------------------- #
# A real packaging failure must still fail loudly (issue #1219 AC 3)
# --------------------------------------------------------------------------- #


def test_missing_package_fails_immediately_without_retrying(tmp_path: Path):
    proc, apt_log = _run(
        tmp_path,
        "definitely-not-a-real-package",
        mode="missing_package",
        CI_APT_MAX_ATTEMPTS="4",
    )
    assert proc.returncode != 0
    calls = _invocations(apt_log)
    # Exactly one update + one install: a deterministic packaging error is
    # not retried, and is never swallowed by the mirror-stall workaround.
    assert len(calls) == 2, calls
    assert "packaging error" in proc.stdout, proc.stdout


# --------------------------------------------------------------------------- #
# apt-source sanitization
# --------------------------------------------------------------------------- #


def test_rewrites_the_azure_mirror_in_deb822_sources(tmp_path: Path):
    sources = tmp_path / "ubuntu.sources"
    sources.write_text(
        "Types: deb\n"
        "URIs: http://azure.archive.ubuntu.com/ubuntu/\n"
        "Suites: noble noble-updates noble-backports\n"
        "Components: main universe restricted multiverse\n"
    )
    proc, _ = _run(tmp_path, "ngspice", CI_APT_SOURCE_FILES=str(sources))
    assert proc.returncode == 0
    text = sources.read_text()
    assert "azure.archive.ubuntu.com" not in text
    assert "http://archive.ubuntu.com/ubuntu/" in text
    # The rest of the stanza is untouched.
    assert "Suites: noble noble-updates noble-backports" in text


def test_rewrites_the_azure_mirror_in_classic_sources_list(tmp_path: Path):
    sources = tmp_path / "sources.list"
    sources.write_text(
        "deb http://azure.archive.ubuntu.com/ubuntu/ noble main\n"
        "deb http://archive.ubuntu.com/ubuntu/ noble-security main\n"
    )
    proc, _ = _run(tmp_path, "ngspice", CI_APT_SOURCE_FILES=str(sources))
    assert proc.returncode == 0
    assert "azure.archive.ubuntu.com" not in sources.read_text()


def test_missing_source_files_are_tolerated(tmp_path: Path):
    """The runner image's exact apt-sources layout has varied; an absent file
    must not fail the install."""
    proc, _ = _run(
        tmp_path, "ngspice", CI_APT_SOURCE_FILES=str(tmp_path / "nope.sources")
    )
    assert proc.returncode == 0


def test_rewrites_the_azure_mirror_in_the_real_runner_mirror_list_file(
    tmp_path: Path,
):
    """Reproduces the real GitHub-hosted `ubuntu-24.04` runner layout (issue
    #1224, post-#1226): the deb822 `.sources` file does NOT contain the
    mirror hostname at all -- its `URIs:` line is
    `mirror+file:/etc/apt/apt-mirrors.txt`, apt's "mirror" method, and the
    actual candidate mirror URL lives in that separate plain-text file. The
    previous version of this script only sanitized `*.list`/`*.sources`
    files, so this exact layout made `sanitize_sources` a silent no-op on
    the real runner even though every synthetic-fixture test above passed.
    """
    sources = tmp_path / "ubuntu.sources"
    sources.write_text(
        "Types: deb\n"
        "URIs: mirror+file:/etc/apt/apt-mirrors.txt\n"
        "Suites: noble noble-updates noble-backports noble-security\n"
        "Components: main universe restricted multiverse\n"
        "Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n"
    )
    mirror_list = tmp_path / "apt-mirrors.txt"
    mirror_list.write_text("http://azure.archive.ubuntu.com/ubuntu/\n")

    proc, _ = _run(
        tmp_path,
        "ngspice",
        CI_APT_SOURCE_FILES=str(sources),
        CI_APT_MIRROR_LIST_FILES=str(mirror_list),
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr

    # The rewrite has to happen in the mirror-list file -- the deb822 source
    # file legitimately never contains the hostname in this layout, so
    # asserting on it (as the two tests above do for the layouts where it
    # *is* present) would be testing the wrong file.
    assert "azure.archive.ubuntu.com" not in mirror_list.read_text()
    assert "http://archive.ubuntu.com/ubuntu/" in mirror_list.read_text()
    # sources.list.d's own URIs line (the mirror+file indirection) is
    # untouched -- only the mirror-list file's contents change.
    assert "mirror+file:/etc/apt/apt-mirrors.txt" in sources.read_text()
    assert "rewriting azure.archive.ubuntu.com -> archive.ubuntu.com" in proc.stdout


def test_default_mirror_list_file_knob_points_at_the_real_runner_path():
    """The default (no `CI_APT_MIRROR_LIST_FILES` override) must cover the
    real runner's path -- pinning this here means a future edit can't
    accidentally rename/drop the default and pass every other test purely
    because they all set the env var explicitly."""
    text = SCRIPT.read_text()
    assert "/etc/apt/apt-mirrors.txt" in text


def test_warns_explicitly_when_nothing_matches_the_flaky_mirror(tmp_path: Path):
    """A silent no-op is exactly what let issue #1224 slip past #1226's own
    16/16-passing test suite -- the script must say so out loud instead."""
    sources = tmp_path / "sources.list"
    sources.write_text("deb http://archive.ubuntu.com/ubuntu/ noble main\n")
    mirror_list = tmp_path / "apt-mirrors.txt"
    mirror_list.write_text("http://archive.ubuntu.com/ubuntu/\n")

    proc, _ = _run(
        tmp_path,
        "ngspice",
        CI_APT_SOURCE_FILES=str(sources),
        CI_APT_MIRROR_LIST_FILES=str(mirror_list),
    )
    assert proc.returncode == 0
    assert "WARNING" in proc.stdout
    assert "no candidate apt source/mirror-list file referenced" in proc.stdout
    assert "rewriting" not in proc.stdout


# --------------------------------------------------------------------------- #
# Workflow wiring
# --------------------------------------------------------------------------- #

_APT_STEP_NAMES = (
    "Install ngspice",
    "Install Yosys build dependencies",
    "Install Icarus Verilog + Verilator build dependencies",
)


def test_all_apt_steps_go_through_the_wrapper():
    text = WORKFLOW.read_text(encoding="utf-8")
    for name in _APT_STEP_NAMES:
        assert f"name: {name}" in text, f"CI step '{name}' not found in {WORKFLOW}"
    # Comments legitimately mention both the wrapper and apt-get; only what
    # the workflow actually *runs* is under test here.
    runnable = re.sub(r"^\s*#.*$", "", text, flags=re.MULTILINE)
    # Every apt install in CI runs through the wrapper...
    assert runnable.count("scripts/ci-apt-install.sh") == len(_APT_STEP_NAMES)
    # ...and no step calls apt-get directly any more (which would reintroduce
    # the unretried, unbounded mirror stall of issue #1219).
    assert "apt-get" not in runnable


def test_apt_steps_keep_their_timeout_backstop():
    """The per-step `timeout-minutes` guard (issue #1204 / PR #1210) stays as
    the outer backstop -- the wrapper's own budget is sized to fit inside
    it."""
    text = WORKFLOW.read_text(encoding="utf-8")
    for name in _APT_STEP_NAMES:
        start = text.index(f"name: {name}")
        step = text[start : start + 3000]
        # Stop at the next step in the same job.
        end = step.find("\n      - name:", 1)
        if end != -1:
            step = step[:end]
        assert "timeout-minutes: 5" in step, f"'{name}' lost its timeout backstop"
        assert "DEBIAN_FRONTEND: noninteractive" in step, (
            f"'{name}' lost its noninteractive apt env"
        )


def _step_text(workflow_text: str, name: str) -> str:
    start = workflow_text.index(f"name: {name}")
    step = workflow_text[start : start + 3000]
    end = step.find("\n      - name:", 1)
    if end != -1:
        step = step[:end]
    return step


def test_yosys_step_widens_the_retry_budget_for_its_large_cmake_download():
    """Issue #1224 (post-#1226): `cmake` (11.2 MB) repeatedly got killed
    mid-download by the script's shared default budget, sized for the much
    smaller `ngspice`/Icarus payloads, while those two steps' installs
    completed fine under the same numbers. The Yosys step gets a wider
    per-step budget via the script's existing `CI_APT_*` env knobs; the
    other two steps keep the (smaller, still-adequate) script defaults."""
    text = WORKFLOW.read_text(encoding="utf-8")
    yosys_step = _step_text(text, "Install Yosys build dependencies")

    assert 'CI_APT_DEADLINE: "270"' in yosys_step
    assert 'CI_APT_PER_CMD_TIMEOUT: "150"' in yosys_step
    # Both larger than the script's own un-overridden defaults (250 / 90),
    # per the header comment in scripts/ci-apt-install.sh.
    deadline = int(re.search(r'CI_APT_DEADLINE: "(\d+)"', yosys_step).group(1))
    per_cmd = int(re.search(r'CI_APT_PER_CMD_TIMEOUT: "(\d+)"', yosys_step).group(1))
    assert deadline > 250
    assert per_cmd > 90

    # Still has to fit -- with margin -- inside the outer `timeout-minutes: 5`
    # (300s) backstop from #1204; a bigger DEADLINE than the step's own
    # outer bound would turn the fail-fast guard back into a long hang.
    assert deadline < 300

    # The other two steps are not observed stalling in the same way (their
    # payloads are an order of magnitude smaller) and keep the script's own
    # defaults -- no override needed/expected.
    for name in (
        "Install ngspice",
        "Install Icarus Verilog + Verilator build dependencies",
    ):
        step = _step_text(text, name)
        assert "CI_APT_DEADLINE" not in step
        assert "CI_APT_PER_CMD_TIMEOUT" not in step
