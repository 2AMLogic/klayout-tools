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

The mirror-sanitization tests also put a fake `curl` on `PATH` (issue
#1665): `sanitize_sources` probes both candidate mirrors before rewriting
anything, so these tests control which host(s) the fake probe reports as
reachable via `FAKE_CURL_MODE`, independent of real network access.

One test asserts the workflow wiring itself, so a future edit that
reintroduces a bare `sudo apt-get update && sudo apt-get install` into
`.github/workflows/ci.yml` fails here rather than silently re-exposing CI to
the mirror stall.

A final group asserts the retry *budget arithmetic* for every apt step in
`ci.yml` (issue #2662). The Yosys-deps step advertised 4 attempts inside a
270s deadline that could not hold even two of its own 150s apt-get calls, so
a slow-but-not-dead mirror exhausted it mid-retry and flaked a random `Tests
(Python 3.x)` matrix leg roughly every other run. Those tests pin the
relation -- not just the literal numbers -- so a future budget edit that
re-breaks it fails here instead of weeks later as a red leg.
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
    slow_install)
        # The issue #2662 signature: `update` is fine and the *install*
        # is slow-but-not-dead, so it is killed by the per-command cap
        # rather than erroring. A later attempt succeeds -- which only
        # happens if the budget still has room for one.
        if [[ "$verb" == "install" && "$installs" -eq 1 ]]; then
            echo "Get:3 .../cmake amd64 3.28.3-1build7 [11.2 MB]"
            sleep "${FAKE_APT_SLOW_SECONDS:-10}"
            exit 0
        fi
        exit 0
        ;;
    stale_index_install)
        # The one install failure a fresh `apt-get update` fixes: the
        # cached index no longer matches what the mirror serves.
        if [[ "$verb" == "install" && "$installs" -eq 1 ]]; then
            echo "E: Failed to fetch .../cmake.deb  Hash Sum mismatch" >&2
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

# Stands in for the real `curl` reachability probe `sanitize_sources` (issue
# #1665) runs before rewriting anything. The probed host is embedded in the
# last argument (`http://<host><path>`); which host(s) "answer" is
# controlled by $FAKE_CURL_MODE so tests can simulate either mirror being
# the degraded one without touching the network.
_FAKE_CURL = r"""#!/usr/bin/env bash
url="${@: -1}"
host="$(echo "$url" | sed -E 's#https?://([^/]+).*#\1#')"

case "$FAKE_CURL_MODE" in
    all_ok)
        exit 0
        ;;
    good_only)
        [[ "$host" == "archive.ubuntu.com" ]] && exit 0
        exit 7
        ;;
    flaky_only)
        [[ "$host" == "azure.archive.ubuntu.com" ]] && exit 0
        exit 7
        ;;
    all_fail)
        exit 7
        ;;
    *)
        # Default: mirrors the historical assumption (issue #1219) that
        # archive.ubuntu.com is healthy -- keeps every pre-existing rewrite
        # test's expectations (azure -> archive) unchanged.
        [[ "$host" == "archive.ubuntu.com" ]] && exit 0
        exit 7
        ;;
esac
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
    curl = bindir / "curl"
    curl.write_text(_FAKE_CURL)
    curl.chmod(0o755)
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
            # Likewise for the mirror-list file the deb822 `mirror+file:`
            # indirection points at: without this default, every test that
            # doesn't override it falls through to the script's real default
            # (`/etc/apt/apt-mirrors.txt`), so running this file standalone on
            # a real Ubuntu host would `sed -i` the live system apt config as a
            # side effect of unrelated unit tests.
            "CI_APT_MIRROR_LIST_FILES": str(tmp_path / "no-such-mirrors.txt"),
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


def test_retries_the_install_when_update_already_succeeded(tmp_path: Path):
    """Signature 2: `apt-get update` succeeds and the *install* stalls.
    Retrying `update` alone would not help, so the retry has to re-run the
    `install` -- which is what this asserts.

    It must NOT re-run the already-successful `update` (issue #2662). Doing
    so spent up to a whole per-command cap of the deadline re-fetching
    indices the job already had, which is what left the Yosys-deps step
    unable to afford a second full-length install attempt and flaked a
    random `Tests` matrix leg roughly every other run.
    """
    proc, apt_log = _run(tmp_path, "ngspice", mode="fail_first_install")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    calls = _invocations(apt_log)
    assert len(calls) == 3, calls
    assert " update" in calls[0]
    assert calls[1].endswith("install -y ngspice")
    assert calls[2].endswith("install -y ngspice")
    assert sum(1 for c in calls if " update" in c) == 1, (
        "a successful apt-get update must not be re-run on a retry: " + repr(calls)
    )
    assert "reusing the package index" in proc.stdout, proc.stdout


def test_update_is_re_run_when_the_install_failure_is_a_stale_index(
    tmp_path: Path,
):
    """The exception to the reuse above (issue #2662): a hash/size mismatch
    or a 404 on a `.deb` the cached index still lists means the index itself
    is the problem, and that is the one failure a fresh `apt-get update`
    actually fixes. Retrying `install` against an index that cannot work
    would burn the rest of the budget for nothing."""
    proc, apt_log = _run(tmp_path, "ngspice", mode="stale_index_install")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    calls = _invocations(apt_log)
    assert len(calls) == 4, calls
    assert " update" in calls[2], (
        "a stale-index install failure must re-arm apt-get update: " + repr(calls)
    )
    assert calls[3].endswith("install -y ngspice")
    assert "stale/mismatched package index" in proc.stdout, proc.stdout


def test_a_slow_attempt_leaves_budget_for_a_second_full_length_attempt(
    tmp_path: Path,
):
    """The issue #2662 failure mode, reproduced at 1/50 scale: one
    slow-but-eventually-successful mirror day where the first `install` is
    killed by the per-command cap after consuming most of the budget.

    The live incident (PR #2661, runs 36892466647 / 36893713413) had the
    Yosys step advertise `attempt 2/4` and then give up with `budget
    exhausted after attempt 2`, because 270s could not hold two 150s
    commands, let alone four attempts. The budget shape here mirrors the
    fixed one (380s deadline / 150s per command / 60s update cap -> 12 / 3 /
    1 at 1/50 scale): the first install burns its whole cap, and a *second,
    full-length* install attempt still fits and succeeds.
    """
    started = time.monotonic()
    proc, apt_log = _run(
        tmp_path,
        "cmake",
        mode="slow_install",
        FAKE_APT_SLOW_SECONDS="10",
        CI_APT_DEADLINE="12",
        CI_APT_PER_CMD_TIMEOUT="3",
        CI_APT_UPDATE_TIMEOUT="1",
        CI_APT_MAX_ATTEMPTS="4",
    )
    elapsed = time.monotonic() - started
    assert proc.returncode == 0, proc.stdout + proc.stderr
    calls = _invocations(apt_log)
    # update (ok) + install (killed at its cap) + install (ok) -- and no
    # second update, so the retry's budget went to the call that stalled.
    assert len(calls) == 3, calls
    assert sum(1 for c in calls if " update" in c) == 1, calls
    assert elapsed < 12, f"the run did not fit its own deadline ({elapsed:.1f}s)"
    assert "installed: cmake" in proc.stdout, proc.stdout


def test_the_pre_2662_budget_shape_could_not_fit_that_second_attempt(
    tmp_path: Path,
):
    """The same slow mirror against the budget shape that was live when the
    issue was filed -- 270s deadline with one 150s cap governing both
    commands (5 / 3 / 3 at the same 1/50 scale). It exhausts the budget
    mid-retry exactly as the real runs did, which is what makes the resize in
    `.github/workflows/ci.yml` load-bearing rather than cosmetic."""
    proc, apt_log = _run(
        tmp_path,
        "cmake",
        mode="slow_install",
        FAKE_APT_SLOW_SECONDS="10",
        CI_APT_DEADLINE="5",
        CI_APT_PER_CMD_TIMEOUT="3",
        CI_APT_UPDATE_TIMEOUT="3",
        CI_APT_MAX_ATTEMPTS="4",
    )
    assert proc.returncode != 0
    assert "budget exhausted" in proc.stdout, proc.stdout
    # Gave up long before the 4 attempts it advertised.
    assert len(_invocations(apt_log)) < 4


def test_update_gets_a_tighter_cap_than_the_package_fetch(tmp_path: Path):
    """`CI_APT_PER_CMD_TIMEOUT` was widened (issue #1224) for an 11.2 MB
    package download; letting the index refresh claim the same share of the
    deadline is what made the Yosys step's retry unaffordable (issue #2662).
    `update` therefore gets its own, smaller cap by default."""
    proc, _ = _run(
        tmp_path,
        "cmake",
        CI_APT_DEADLINE="380",
        CI_APT_PER_CMD_TIMEOUT="150",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    update_cap = int(re.search(r"apt-get .*update \(cap (\d+)s", proc.stdout).group(1))
    install_cap = int(
        re.search(r"apt-get .*install [^\n]*\(cap (\d+)s", proc.stdout).group(1)
    )
    assert install_cap == 150
    assert update_cap == 60, proc.stdout
    assert update_cap < install_cap


def test_the_script_states_how_many_attempts_its_budget_actually_fits(
    tmp_path: Path,
):
    """A budget that cannot fit what `attempt i/N` advertises has to say so
    in the log (issue #2662) -- the live incident was only diagnosable
    because someone correlated `attempt 2/4` with `budget exhausted` by
    hand."""
    proc, _ = _run(
        tmp_path,
        "cmake",
        CI_APT_DEADLINE="380",
        CI_APT_PER_CMD_TIMEOUT="150",
        CI_APT_BACKOFF="5",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    # 60 (update) + 2x150 (installs) + 1x5 (backoff) = 365 <= 380, and a
    # third would need 520.
    assert "4 attempt(s) configured, of which 2 fit the deadline" in proc.stdout, (
        proc.stdout
    )
    # No budget warning for a budget that does fit two (the unrelated
    # "no apt source/mirror-list file found" warning every fixture-less run
    # emits is not under test here).
    assert "cannot fit a second full-length attempt" not in proc.stdout


def test_a_budget_too_small_for_a_second_full_attempt_warns(tmp_path: Path):
    """The exact pre-#2662 Yosys numbers: 270s cannot hold two 150s
    commands, so the retry loop existed in name only. That must be a visible
    WARNING in the CI log, not something a future edit can reintroduce
    silently."""
    proc, _ = _run(
        tmp_path,
        "cmake",
        CI_APT_DEADLINE="270",
        CI_APT_PER_CMD_TIMEOUT="150",
        CI_APT_BACKOFF="5",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "of which 1 fit the deadline" in proc.stdout, proc.stdout
    assert "WARNING" in proc.stdout
    assert "cannot fit a second full-length attempt" in proc.stdout
    # Names the smallest deadline that would: 60 + 2x150 + 5.
    assert "at least 365s" in proc.stdout, proc.stdout


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
    16/16-passing test suite -- the script must say so out loud instead, but
    only for genuine drift (neither mirror referenced anywhere), not for the
    already-fixed case covered by the test below."""
    sources = tmp_path / "sources.list"
    # Neither the flaky mirror nor its healthy replacement -- a layout the
    # script genuinely doesn't recognize (e.g. an enterprise apt proxy).
    sources.write_text("deb http://apt-proxy.example.internal/ubuntu/ noble main\n")
    mirror_list = tmp_path / "apt-mirrors.txt"
    mirror_list.write_text("http://apt-proxy.example.internal/ubuntu/\n")

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


def test_no_false_warning_when_a_prior_call_already_rewrote_the_file(
    tmp_path: Path,
):
    """Real CI evidence (PR #1228, run 32295025496, Python 3.11 job): the
    ngspice step's rewrite persists on the shared runner filesystem, so by
    the time the Yosys/Icarus steps' own `ci-apt-install.sh` invocations run
    later in the same job, the flaky mirror is legitimately already gone.
    That must read as success, not trigger the drift warning above."""
    sources = tmp_path / "sources.list"
    # Already references only the healthy mirror -- e.g. a previous
    # `ci-apt-install.sh` call in the same job already rewrote it.
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
    assert "WARNING" not in proc.stdout
    assert "rewriting" not in proc.stdout


# --------------------------------------------------------------------------- #
# Mirror reachability probing (issue #1665): the rewrite direction is no
# longer hard-coded toward archive.ubuntu.com -- both candidates are probed
# and the rewrite (if any) follows whichever one actually answers.
# --------------------------------------------------------------------------- #


def test_does_not_delete_a_healthy_flaky_mirror(tmp_path: Path):
    """The exact regression from issue #1665: a fresh runner ships with only
    azure.archive.ubuntu.com configured. If that mirror is healthy, the
    unconditional pre-#1665 rewrite still deleted it in favor of
    archive.ubuntu.com -- which is exactly wrong when archive.ubuntu.com is
    the one that's degraded. The probe must see azure answering and leave
    the sources alone."""
    sources = tmp_path / "sources.list"
    sources.write_text("deb http://azure.archive.ubuntu.com/ubuntu/ noble main\n")
    mirror_list = tmp_path / "apt-mirrors.txt"
    mirror_list.write_text("http://azure.archive.ubuntu.com/ubuntu/\n")

    proc, _ = _run(
        tmp_path,
        "ngspice",
        CI_APT_SOURCE_FILES=str(sources),
        CI_APT_MIRROR_LIST_FILES=str(mirror_list),
        FAKE_CURL_MODE="flaky_only",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    # Untouched: still exactly the original, unrewritten content.
    assert (
        sources.read_text()
        == "deb http://azure.archive.ubuntu.com/ubuntu/ noble main\n"
    )
    assert mirror_list.read_text() == "http://azure.archive.ubuntu.com/ubuntu/\n"
    assert "rewriting" not in proc.stdout
    assert "answered a reachability probe" in proc.stdout


def test_rewrites_toward_the_flaky_mirror_when_the_good_one_is_down(
    tmp_path: Path,
):
    """The scenario reported in issue #1665: archive.ubuntu.com is the
    degraded mirror this time, and the sources already reference only it
    (e.g. from an earlier call in the same job, or a runner default). The
    probe must find archive.ubuntu.com unreachable and azure.archive.ubuntu.com
    reachable, and rewrite *toward* azure -- the reverse of #1219's
    original, one-directional fix."""
    sources = tmp_path / "sources.list"
    sources.write_text("deb http://archive.ubuntu.com/ubuntu/ noble main\n")
    mirror_list = tmp_path / "apt-mirrors.txt"
    mirror_list.write_text("http://archive.ubuntu.com/ubuntu/\n")

    proc, _ = _run(
        tmp_path,
        "ngspice",
        CI_APT_SOURCE_FILES=str(sources),
        CI_APT_MIRROR_LIST_FILES=str(mirror_list),
        FAKE_CURL_MODE="flaky_only",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (
        sources.read_text()
        == "deb http://azure.archive.ubuntu.com/ubuntu/ noble main\n"
    )
    assert mirror_list.read_text() == "http://azure.archive.ubuntu.com/ubuntu/\n"
    assert "rewriting archive.ubuntu.com -> azure.archive.ubuntu.com" in proc.stdout


def test_leaves_sources_untouched_when_neither_mirror_answers(tmp_path: Path):
    """Both candidates can be down (or the probe itself can be blocked)
    at once. The invariant is that apt must never be left with exactly one
    mirror that hasn't been verified reachable -- so the script must not
    guess: it leaves the (already-configured, unverified) mirror as-is and
    lets the apt-get retry loop have a shot, rather than deleting it for an
    equally-unverified alternative."""
    sources = tmp_path / "sources.list"
    sources.write_text("deb http://azure.archive.ubuntu.com/ubuntu/ noble main\n")
    mirror_list = tmp_path / "apt-mirrors.txt"
    mirror_list.write_text("http://azure.archive.ubuntu.com/ubuntu/\n")

    proc, _ = _run(
        tmp_path,
        "ngspice",
        CI_APT_SOURCE_FILES=str(sources),
        CI_APT_MIRROR_LIST_FILES=str(mirror_list),
        FAKE_CURL_MODE="all_fail",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "azure.archive.ubuntu.com" in sources.read_text()
    assert "rewriting" not in proc.stdout
    assert "WARNING" in proc.stdout
    assert "neither" in proc.stdout


def test_unusable_curl_is_tolerated_and_leaves_sources_untouched(tmp_path: Path):
    """A probe that can't succeed at all -- curl missing from a minimal
    image, or present but unable to connect -- must fail closed the same way
    an unreachable probe does, never treated as "reachable". This exercises
    `probe_mirror`'s `command -v curl || return 1` guard: the fake `curl` on
    `PATH` here always exits non-zero, standing in for "no usable curl"."""
    sources = tmp_path / "sources.list"
    sources.write_text("deb http://azure.archive.ubuntu.com/ubuntu/ noble main\n")
    mirror_list = tmp_path / "apt-mirrors.txt"
    mirror_list.write_text("http://azure.archive.ubuntu.com/ubuntu/\n")

    proc, _ = _run(
        tmp_path,
        "ngspice",
        CI_APT_SOURCE_FILES=str(sources),
        CI_APT_MIRROR_LIST_FILES=str(mirror_list),
        FAKE_CURL_MODE="all_fail",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "azure.archive.ubuntu.com" in sources.read_text()
    assert "rewriting" not in proc.stdout


# --------------------------------------------------------------------------- #
# Workflow wiring
# --------------------------------------------------------------------------- #

_APT_STEP_NAMES = (
    "Install ngspice",
    "Install Yosys build dependencies",
    "Install SymbiYosys (sby) + Bitwuzla runtime dependencies",
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
    it.

    The Yosys step's backstop is 7 minutes rather than 5 (issue #2662): its
    deadline has to hold two full-length 150s `cmake` fetches, which 300s
    cannot. Every other step keeps the script's defaults and the original
    5-minute backstop.
    """
    text = WORKFLOW.read_text(encoding="utf-8")
    for name in _APT_STEP_NAMES:
        step = _step_text(text, name)
        expected = 7 if name == "Install Yosys build dependencies" else 5
        assert f"timeout-minutes: {expected}" in step, (
            f"'{name}' lost its timeout backstop (expected {expected} minutes)"
        )
        assert "DEBIAN_FRONTEND: noninteractive" in step, (
            f"'{name}' lost its noninteractive apt env"
        )


def _step_text(workflow_text: str, name: str) -> str:
    """The full YAML of one `- name: <name>` step, up to the next step in the
    same job. Deliberately unbounded in length: an earlier version sliced a
    fixed 3000 characters, which silently truncated a step whose explanatory
    comment grew past that and made assertions on its `timeout-minutes` /
    `env:` block vacuous."""
    start = workflow_text.index(f"name: {name}")
    step = workflow_text[start:]
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

    assert 'CI_APT_DEADLINE: "380"' in yosys_step
    assert 'CI_APT_PER_CMD_TIMEOUT: "150"' in yosys_step
    # Both larger than the script's own un-overridden defaults (250 / 90),
    # per the header comment in scripts/ci-apt-install.sh.
    deadline = int(re.search(r'CI_APT_DEADLINE: "(\d+)"', yosys_step).group(1))
    per_cmd = int(re.search(r'CI_APT_PER_CMD_TIMEOUT: "(\d+)"', yosys_step).group(1))
    assert deadline > 250
    assert per_cmd > 90

    # Still has to fit -- with margin -- inside the step's own
    # `timeout-minutes` backstop from #1204; a DEADLINE at or past that outer
    # bound would turn the fail-fast guard back into a long hang. The margin
    # is what the mirror probes and the inter-attempt `dpkg --configure -a`
    # run in.
    outer = 60 * int(re.search(r"timeout-minutes: (\d+)", yosys_step).group(1))
    assert deadline < outer
    assert outer - deadline >= 30

    # The other steps are not observed stalling in the same way (their
    # payloads are an order of magnitude smaller) and keep the script's own
    # defaults -- no override needed/expected.
    for name in (
        "Install ngspice",
        "Install SymbiYosys (sby) + Bitwuzla runtime dependencies",
        "Install Icarus Verilog + Verilator build dependencies",
    ):
        step = _step_text(text, name)
        assert "CI_APT_DEADLINE" not in step
        assert "CI_APT_PER_CMD_TIMEOUT" not in step


# --------------------------------------------------------------------------- #
# Budget feasibility (issue #2662): the arithmetic that `attempt i/N` claims
# --------------------------------------------------------------------------- #


def _script_default(name: str, pattern: str) -> int:
    """Read a `CI_APT_*` default straight out of the script, so this file
    cannot drift from it the way the hard-coded 270/150 pair did."""
    text = SCRIPT.read_text(encoding="utf-8")
    match = re.search(pattern, text)
    assert match, f"could not find the {name} default in {SCRIPT}"
    return int(match.group(1))


def _worst_case_attempts_that_fit(
    deadline: int, per_cmd: int, update_cap: int, backoff: int, max_attempts: int
) -> int:
    """Python mirror of `worst_case_attempts_that_fit` in the script: with a
    successful `apt-get update` reused for the rest of the run, N attempts
    whose every call times out at its cap cost
    `update_cap + N*per_cmd + (N-1)*backoff`."""
    fits = 0
    while fits < max_attempts:
        cost = update_cap + (fits + 1) * per_cmd + fits * backoff
        if cost > deadline:
            break
        fits += 1
    return fits


def test_every_apt_step_budget_fits_two_full_length_attempts():
    """The defect behind issue #2662 was arithmetic, not a bug: 270s cannot
    hold two 150s apt-get calls, so the Yosys step's "4 attempts" was really
    one-and-a-bit, and a single slow-but-not-dead mirror flaked a random
    `Tests (Python 3.x)` matrix leg roughly every other run.

    This asserts the arithmetic statically for every apt step in ci.yml --
    using each step's own overrides where it has them and the script's own
    defaults where it doesn't -- so a future budget edit that reintroduces
    the mismatch fails here instead of weeks later as a random red leg.
    """
    text = WORKFLOW.read_text(encoding="utf-8")
    default_deadline = _script_default(
        "CI_APT_DEADLINE", r'DEADLINE="\$\{CI_APT_DEADLINE:-(\d+)\}"'
    )
    default_per_cmd = _script_default(
        "CI_APT_PER_CMD_TIMEOUT",
        r'PER_CMD_TIMEOUT="\$\{CI_APT_PER_CMD_TIMEOUT:-(\d+)\}"',
    )
    default_backoff = _script_default(
        "CI_APT_BACKOFF", r'BACKOFF="\$\{CI_APT_BACKOFF:-(\d+)\}"'
    )
    default_max_attempts = _script_default(
        "CI_APT_MAX_ATTEMPTS", r'MAX_ATTEMPTS="\$\{CI_APT_MAX_ATTEMPTS:-(\d+)\}"'
    )

    def _env(step: str, knob: str, fallback: int) -> int:
        match = re.search(rf'{knob}: "(\d+)"', step)
        return int(match.group(1)) if match else fallback

    for name in _APT_STEP_NAMES:
        step = _step_text(text, name)
        deadline = _env(step, "CI_APT_DEADLINE", default_deadline)
        per_cmd = _env(step, "CI_APT_PER_CMD_TIMEOUT", default_per_cmd)
        backoff = _env(step, "CI_APT_BACKOFF", default_backoff)
        max_attempts = _env(step, "CI_APT_MAX_ATTEMPTS", default_max_attempts)
        # The script's own default: min(60, per-command cap).
        update_cap = _env(step, "CI_APT_UPDATE_TIMEOUT", min(60, per_cmd))

        fits = _worst_case_attempts_that_fit(
            deadline, per_cmd, update_cap, backoff, max_attempts
        )
        assert fits >= 2, (
            f"'{name}': a {deadline}s budget fits only {fits} full-length "
            f"attempt(s) at {per_cmd}s per apt-get ({update_cap}s for update, "
            f"{backoff}s backoff) while advertising {max_attempts} -- the "
            f"issue #2662 mismatch. Raise CI_APT_DEADLINE to at least "
            f"{update_cap + 2 * per_cmd + backoff}s (and its timeout-minutes "
            f"with it) or lower CI_APT_PER_CMD_TIMEOUT."
        )

        # ...and the deadline still has to sit inside the step's own outer
        # backstop, or the resize above just relocates the hang.
        outer = 60 * int(re.search(r"timeout-minutes: (\d+)", step).group(1))
        assert deadline < outer, f"'{name}': deadline {deadline}s >= {outer}s backstop"
