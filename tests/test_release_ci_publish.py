"""Branch recovery after a failed release-PR creation (issue #6911).

Each case runs ``release_ci.publish`` against a real temporary bare remote and
asserts the *actual remote ref* afterwards: deleted only when no open PR exists,
the lookups are conclusive, and the remote is still at the SHA this run pushed.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from _release_ci_stub import ForgeError, Git, GitError, StubForge, git, rc

BRANCH = "release/v0.10.0"
DENIAL = ForgeError(
    "GitHub Actions is not permitted to create or approve pull requests "
    "(createPullRequest)",
    status=403,
)


class Env:
    """A bare remote, the release run's checkout, and a second "someone else" clone."""

    def __init__(self, tmp_path: Path) -> None:
        self.remote = tmp_path / "remote.git"
        subprocess.run(
            ["git", "init", "-q", "--bare", "-b", "main", str(self.remote)], check=True
        )
        self.work = self._clone(tmp_path / "work")
        (self.work / "f.txt").write_text("base\n")
        git("add", "f.txt", cwd=self.work)
        git("commit", "-q", "-m", "base", cwd=self.work)
        git("push", "-q", "origin", "main", cwd=self.work)
        git("switch", "-q", "-c", BRANCH, cwd=self.work)
        self.other = self._clone(tmp_path / "other")
        self.commit_release(self.work, "chore(release): bump version to 0.10.0")

    def _clone(self, path: Path) -> Path:
        subprocess.run(["git", "clone", "-q", str(self.remote), str(path)], check=True)
        git("config", "user.email", "t@example.com", cwd=path)
        git("config", "user.name", "T", cwd=path)
        return path

    @staticmethod
    def commit_release(repo: Path, message: str) -> str:
        (repo / "release.txt").write_text(message + "\n")
        git("add", "release.txt", cwd=repo)
        git("commit", "-q", "-m", message, cwd=repo)
        return git("rev-parse", "HEAD", cwd=repo)

    def remote_ref(self) -> str | None:
        out = git("ls-remote", "--heads", str(self.remote), BRANCH, cwd=self.work)
        return out.split()[0] if out else None

    def push_from_other(self) -> str:
        """Someone else adds a commit on the release branch on the remote.

        Builds on the remote branch when it exists; otherwise creates it from
        ``main`` (the stale orphan an earlier failed run left behind).
        """
        base = "origin/main"
        if self.remote_ref():
            git("fetch", "-q", "origin", BRANCH, cwd=self.other)
            base = "FETCH_HEAD"
        git("switch", "-q", "-C", BRANCH, base, cwd=self.other)
        sha = self.commit_release(self.other, "someone else's follow-up")
        git("push", "-q", "origin", BRANCH, cwd=self.other)
        return sha

    def publish(self, forge: StubForge, git_client: Git | None = None) -> int:
        return rc.publish(
            forge,
            git_client or Git(self.work),
            repo="acme/widgets",
            version="0.10.0",
        )


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def _head(env: Env) -> str:
    return git("rev-parse", "HEAD", cwd=env.work)


def test_success_pushes_the_branch_and_opens_the_pr(env: Env) -> None:
    forge = StubForge()
    assert env.publish(forge) == 0
    assert env.remote_ref() == _head(env)
    [pr] = forge.created
    assert pr["head"] == BRANCH and pr["base"] == "main"
    assert pr["title"] == "chore(release): bump version to 0.10.0"


def test_failed_pr_creation_removes_the_branch_and_a_retry_succeeds(
    env: Env, capsys
) -> None:
    forge = StubForge()
    forge.create_error = DENIAL
    pushed = _head(env)
    assert env.publish(forge) == 1
    out = capsys.readouterr().out
    assert env.remote_ref() is None  # the orphan the live failure left is gone
    assert "deleted it at " + pushed in out
    # The denial names who can fix it, on the post-push path too.
    assert "Allow GitHub Actions to create and approve pull requests" in out
    assert out.count("::error::") == 2

    # Retry: a fresh run re-prepares the commit (new SHA) and pushes cleanly.
    forge.create_error = None
    git(
        "commit",
        "-q",
        "--amend",
        "-m",
        "chore(release): bump version to 0.10.0 v2",
        cwd=env.work,
    )
    assert _head(env) != pushed
    assert env.publish(forge) == 0
    assert env.remote_ref() == _head(env)
    assert len(forge.created) == 1


def test_open_pr_after_a_lost_response_preserves_the_branch(env: Env, capsys) -> None:
    forge = StubForge()
    forge.create_error = ForgeError("HTTP 502 Bad Gateway", status=502)

    def server_made_the_pr(stub: StubForge, head: str) -> None:
        stub.pulls.append(
            {"number": 8, "html_url": "https://github.com/acme/widgets/pull/8",
             "head": f"acme:{head}"}
        )  # fmt: skip

    forge.on_create = server_made_the_pr
    assert env.publish(forge) == 0  # the PR exists, so there is nothing to retry
    assert env.remote_ref() == _head(env)
    assert "pull/8" in capsys.readouterr().out


def test_changed_remote_sha_is_left_alone(env: Env, capsys) -> None:
    forge = StubForge()
    forge.create_error = DENIAL
    forge.on_create = lambda _stub, _head: env.push_from_other()
    pushed = _head(env)
    assert env.publish(forge) == 1
    remote = env.remote_ref()
    assert remote is not None and remote != pushed  # still the other commit
    assert "someone else's commits" in capsys.readouterr().out


def test_inconclusive_pr_lookup_preserves_the_branch(env: Env, capsys) -> None:
    forge = StubForge()
    forge.create_error = DENIAL
    forge.pull_lookup_error = ForgeError("HTTP 502", status=502)
    assert env.publish(forge) == 1
    assert env.remote_ref() == _head(env)
    assert "inconclusive" in capsys.readouterr().out


def test_inconclusive_remote_lookup_preserves_the_branch(env: Env, capsys) -> None:
    class BlindGit(Git):
        def remote_sha(self, branch: str) -> str | None:
            raise GitError("ls-remote: connection reset")

    forge = StubForge()
    forge.create_error = DENIAL
    assert env.publish(forge, BlindGit(env.work)) == 1
    assert env.remote_ref() == _head(env)
    assert "Could not read the remote" in capsys.readouterr().out


def test_the_deletion_lease_rejects_a_remote_that_moved_after_the_lookup(
    env: Env, capsys
) -> None:
    """The ref is correct when read, then moves: only the lease can catch that."""

    class RacyGit(Git):
        def remote_sha(self, branch: str) -> str | None:
            sha = super().remote_sha(branch)
            env.push_from_other()  # moves between the lookup and the delete
            return sha

    forge = StubForge()
    forge.create_error = DENIAL
    pushed = _head(env)
    assert env.publish(forge, RacyGit(env.work)) == 1
    remote = env.remote_ref()
    assert remote is not None and remote != pushed
    assert "Could not delete" in capsys.readouterr().out


def test_an_existing_remote_branch_is_never_overwritten(env: Env, capsys) -> None:
    stale = env.push_from_other()  # e.g. the v0.10.0 orphan an old run left
    forge = StubForge()
    assert env.publish(forge) == 1
    assert env.remote_ref() == stale
    assert not any(c.startswith("create_pull") for c in forge.calls)
    assert "left nothing on the remote" in capsys.readouterr().out


def test_cli_publish_wires_the_checkout_and_forge(env: Env, monkeypatch) -> None:
    forge = StubForge()
    monkeypatch.chdir(env.work)
    code = rc.main(
        ["publish", "--repo", "acme/widgets", "--version", "0.10.0"],
        forge_factory=lambda _repo: forge,
    )
    assert code == 0 and env.remote_ref() == _head(env)
