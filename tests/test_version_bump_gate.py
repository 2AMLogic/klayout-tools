"""Tests for ``scripts/version-bump-gate.sh`` (issue #5005): the release
cadence gate release.yml consults before computing a bump.

Runs entirely against synthetic throwaway git repos under ``tmp_path``
(never this repo's own history). Each case builds a first-parent history with
a known last bump, then runs the script with ``--now``/``--interval`` pins so
the verdict never depends on wall-clock time.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "version-bump-gate.sh"

DAY = 86_400
T0 = 1_700_000_000  # arbitrary epoch for a deterministic history


def _git(repo: Path, *args: str, env: dict | None = None) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout


def _commit(
    repo: Path,
    message: str,
    *,
    date_epoch: int,
    touch: str | None = None,
) -> None:
    if touch is not None:
        (repo / touch).parent.mkdir(parents=True, exist_ok=True)
        (repo / touch).write_text(f"content for {touch}\n")
    _git(repo, "add", "-A")
    _git(
        repo,
        "-c",
        f"user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "--allow-empty",
        "-m",
        message,
        env={"GIT_COMMITTER_DATE": str(date_epoch), "GIT_AUTHOR_DATE": str(date_epoch)}
        | dict(__import__("os").environ),
    )


def _make_repo(base: Path) -> Path:
    repo = base if base.name == "repo" else base / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.com")
    (repo / "pyproject.toml").write_text(
        '[tool.hatch.version]\nbase-version = "0.9.2"\n'
    )
    _commit(repo, "chore: initial", date_epoch=T0, touch="pyproject.toml")
    _commit(repo, "chore: bump version to 0.9.2", date_epoch=T0)
    return repo


def _run(
    repo: Path,
    *args: str,
    interval: str | None = None,
    now: str | None = None,
    event: str = "push",
    env_extra: dict | None = None,
) -> subprocess.CompletedProcess:
    env: dict = {"PATH": __import__("os").environ["PATH"], "GIT_DIR": ""}
    env.pop("GIT_DIR")
    if interval is not None:
        env["RELEASE_MIN_INTERVAL"] = interval
    if env_extra:
        env.update(env_extra)
    argv = ["bash", str(SCRIPT), "--event", event, "--ref", "HEAD", *args]
    if now is not None:
        argv += ["--now", now]
    if interval is not None:
        argv += ["--interval", interval]
    return subprocess.run(
        argv, cwd=repo, capture_output=True, text=True, env=env
    )


def _field(out: str, key: str) -> str:
    for line in out.splitlines():
        if line.startswith(f"{key}="):
            return line.partition("=")[2]
    raise AssertionError(f"no {key}= in output:\n{out}")


# --------------------------------------------------------------------------- #
# the cadence rule
# --------------------------------------------------------------------------- #


def test_push_after_interval_with_src_change_bumps(tmp_path):
    repo = _make_repo(tmp_path)
    _commit(repo, "feat: a deck change", date_epoch=T0 + 100, touch="src/x.py")

    r = _run(repo, now=str(T0 + DAY + 200), interval="86400")

    assert r.returncode == 0
    assert _field(r.stdout, "decision") == "bump"
    assert _field(r.stdout, "last_bump_sha")


def test_two_merges_inside_the_interval_produce_one_bump(tmp_path):
    """The acceptance criterion: a second release-worthy merge inside the
    window defers, so both ship in the next single bump."""
    repo = _make_repo(tmp_path)
    # First merge lands long after the last bump: eligible.
    _commit(repo, "feat: first", date_epoch=T0 + DAY + 100, touch="src/a.py")
    r1 = _run(repo, now=str(T0 + DAY + 200), interval="86400")
    assert _field(r1.stdout, "decision") == "bump"

    # The bump commit itself lands now; a second merge follows inside the
    # window and must defer.
    bump_at = T0 + DAY + 300
    _commit(repo, "chore: bump version to 0.9.3", date_epoch=bump_at)
    _commit(repo, "feat: second", date_epoch=bump_at + 60, touch="src/b.py")

    r2 = _run(repo, now=str(bump_at + 120), interval="86400")

    assert r2.returncode == 0
    assert _field(r2.stdout, "decision") == "deferred"
    # ... and becomes eligible again once the window has elapsed.
    r3 = _run(repo, now=str(bump_at + DAY + 60), interval="86400")
    assert _field(r3.stdout, "decision") == "bump"


def test_no_release_path_change_since_last_bump_is_nothing(tmp_path):
    repo = _make_repo(tmp_path)
    _commit(repo, "docs: only docs", date_epoch=T0 + DAY + 100, touch="docs/guide.md")

    r = _run(repo, now=str(T0 + DAY + 200), interval="86400")

    assert r.returncode == 0
    assert _field(r.stdout, "decision") == "nothing"


def test_changelog_only_change_is_not_a_release_path(tmp_path):
    """The bump commit rewrites CHANGELOG.md; counting it would make every
    interval-elapsed run see phantom work."""
    repo = _make_repo(tmp_path)
    _commit(repo, "chore: notes", date_epoch=T0 + DAY + 100, touch="CHANGELOG.md")

    r = _run(repo, now=str(T0 + DAY + 200), interval="86400")

    assert _field(r.stdout, "decision") == "nothing"


def test_pyproject_dependency_change_is_a_release_path(tmp_path):
    """Wider than the push trigger's src/** filter: the hourly schedule
    catches a pyproject-only change (operator ruling: dependency bumps are
    release-worthy)."""
    repo = _make_repo(tmp_path)
    (repo / "pyproject.toml").write_text(
        '[tool.hatch.version]\nbase-version = "0.9.2"\n'
    )
    _commit(repo, "chore(deps): bump a dependency", date_epoch=T0 + DAY + 100)

    r = _run(repo, now=str(T0 + DAY + 200), interval="86400", event="schedule")

    assert _field(r.stdout, "decision") == "bump"


def test_zero_interval_never_defers(tmp_path):
    repo = _make_repo(tmp_path)
    _commit(repo, "feat: x", date_epoch=T0 + 10, touch="src/x.py")

    r = _run(repo, now=str(T0 + 20), interval="0")

    assert _field(r.stdout, "decision") == "bump"


def test_non_integer_interval_fails_red(tmp_path):
    repo = _make_repo(tmp_path)
    _commit(repo, "feat: x", date_epoch=T0 + 10, touch="src/x.py")

    for bad in ("86400s", "-1", "one day", "3.5"):
        r = _run(repo, now=str(T0 + 20), interval=bad)
        assert r.returncode == 2, bad
        assert _field(r.stdout, "decision") == "error"


def test_default_interval_is_86400(tmp_path):
    repo = _make_repo(tmp_path)
    _commit(repo, "feat: x", date_epoch=T0 + 10, touch="src/x.py")

    # 86399s after the bump: deferred under the default, no interval given.
    r = _run(repo, now=str(T0 + DAY - 1))
    assert _field(r.stdout, "decision") == "deferred"
    r = _run(repo, now=str(T0 + DAY + 1))
    assert _field(r.stdout, "decision") == "bump"


# --------------------------------------------------------------------------- #
# last-bump detection across the three subject spellings
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("subject", "spell"),
    [
        ("chore: bump version to 0.9.3", "publisher"),
        ("chore(release): bump version to 0.9.3", "release PR"),
        (
            "Merge pull request #42 from 2AMLogic/release/v0.9.3",
            "merge commit",
        ),
    ],
)
def test_last_bump_subject_spellings(tmp_path, subject, spell):
    repo = _make_repo(tmp_path)
    # A pyproject-touching NON-bump commit sits between the seed bump and the
    # real one: it must never be mistaken for the last bump.
    _commit(repo, "chore(deps): touch pyproject", date_epoch=T0 + 50)
    at = T0 + 100
    _commit(repo, subject, date_epoch=at)
    _commit(repo, "feat: after the bump", date_epoch=at + 10, touch="src/new.py")

    # Inside the new bump's window: deferred (the seed bump is far past, so a
    # wrong lookup that found the SEED bump would say "bump" here).
    r = _run(repo, now=str(at + 60), interval="86400")
    assert _field(r.stdout, "decision") == "deferred", spell

    # And eligible again one interval after the real bump.
    r2 = _run(repo, now=str(at + DAY + 60), interval="86400")
    assert _field(r2.stdout, "decision") == "bump", spell


def test_no_bump_commit_in_history_fails_closed(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.com")
    _commit(repo, "chore: never released anything", date_epoch=T0, touch="src/x.py")

    r = _run(repo, now=str(T0 + DAY))

    assert r.returncode == 2
    assert _field(r.stdout, "decision") == "error"


def test_shallow_clone_is_refused(tmp_path):
    repo = _make_repo(tmp_path)
    _commit(repo, "feat: x", date_epoch=T0 + 10, touch="src/x.py")
    shallow = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "-q", "--depth=1", f"file://{repo}", str(shallow)],
        check=True,
        capture_output=True,
    )

    r = _run(shallow, now=str(T0 + DAY))

    assert r.returncode == 2
    assert "shallow" in _field(r.stdout, "reason")


# --------------------------------------------------------------------------- #
# force (the operator hotfix path)
# --------------------------------------------------------------------------- #


def test_force_skips_interval_but_not_the_unreleased_check(tmp_path):
    repo = _make_repo(tmp_path)
    _commit(repo, "feat: hotfix", date_epoch=T0 + 10, touch="src/fix.py")

    r = _run(
        repo,
        "--force",
        "true",
        "--reason",
        "hotfix floor needs to ship",
        "--actor",
        "someone",
        now=str(T0 + 20),
        interval="86400",
        event="workflow_dispatch",
    )
    assert _field(r.stdout, "decision") == "bump"

    # Nothing unreleased: force cannot cut a no-op version.
    repo2 = _make_repo(tmp_path / "repo2")
    r2 = _run(
        repo2,
        "--force",
        "true",
        "--reason",
        "hotfix",
        "--actor",
        "someone",
        now=str(T0 + DAY),
        interval="86400",
        event="workflow_dispatch",
    )
    assert _field(r2.stdout, "decision") == "nothing"


def test_force_requires_reason_and_dispatch_and_a_human(tmp_path):
    repo = _make_repo(tmp_path)
    _commit(repo, "feat: x", date_epoch=T0 + 10, touch="src/x.py")

    no_reason = _run(
        repo, "--force", "true", "--actor", "someone",
        now=str(T0 + 20), interval="86400", event="workflow_dispatch",
    )
    assert no_reason.returncode == 2

    from_push = _run(
        repo, "--force", "true", "--reason", "why", "--actor", "someone",
        now=str(T0 + 20), interval="86400", event="push",
    )
    assert from_push.returncode == 2

    bot = _run(
        repo, "--force", "true", "--reason", "why", "--actor", "x[bot]",
        now=str(T0 + 20), interval="86400", event="workflow_dispatch",
    )
    assert bot.returncode == 2
    assert "bot" in _field(bot.stdout, "reason")


def test_bad_event_and_unknown_arg_fail(tmp_path):
    repo = _make_repo(tmp_path)
    assert _run(repo, "--event", "pull_request").returncode == 2
    assert _run(repo, "--nonsense", "x").returncode == 2
