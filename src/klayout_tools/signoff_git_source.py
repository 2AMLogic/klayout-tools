"""Local Git object locators for item 1 design sources."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from tempfile import TemporaryFile
from typing import Any

from .signoff_base import AttestationError
from .signoff_upstream_source import full_git_commit


def hex_digest(value: Any, field: str) -> str:
    if not isinstance(value, str) or not re.fullmatch("[0-9a-f]{64}", value):
        raise AttestationError(
            f"'{field}' must be a 64-character lowercase hex sha256 digest"
        )
    return value


@dataclass(frozen=True, order=True)
class GitPath:
    repo: str
    commit: str
    path: str

    @classmethod
    def parse(cls, raw: Mapping[str, Any], where: str) -> GitPath:
        """Validate the same locator at both JSON boundaries."""
        repo, commit, path = raw.get("repo"), raw.get("commit"), raw.get("path")
        if not isinstance(repo, str) or not repo.strip() or "\0" in repo:
            raise AttestationError(f"'{where}.repo' must be a local repository path")
        if not full_git_commit(commit):
            raise AttestationError(f"'{where}.commit' must be a full Git commit ID")
        if (
            not isinstance(path, str)
            or not path.strip()
            or "\0" in path
            or PureWindowsPath(path).is_absolute()
            or any(part in ("", ".", "..") for part in path.split("/"))
        ):
            raise AttestationError(
                f"'{where}.path' must be a repository-relative Git path"
            )
        return cls(repo, commit.lower(), path)

    def binding(self) -> dict[str, str]:
        return {
            "scope": "git",
            "repo": self.repo,
            "commit": self.commit,
            "path": self.path,
        }

    def sha256(self, root: str | Path | None) -> str | None:
        """Hash raw blob bytes in this commit, without checkout or revision peeling."""
        if root is None and not os.path.isabs(self.repo):
            return None
        repo = Path(root or ".") / self.repo
        # Use only the named object database, including its own alternates.
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(GIT_NO_LAZY_FETCH="1", GIT_ALLOW_PROTOCOL="")
        try:
            # An explicit gitdir prevents discovery of some parent repository.
            dot_git = repo / ".git"
            gitdir = dot_git if dot_git.exists() else repo
            git = ["git", "--no-replace-objects", f"--git-dir={gitdir}", "cat-file"]
            info = subprocess.run(
                [*git, "--batch-check=%(objectname) %(objecttype)"],
                input=self.commit.encode("ascii") + b"\n",
                capture_output=True,
                check=True,
                timeout=30,
                env=env,
            )
            if info.stdout != f"{self.commit} commit\n".encode("ascii"):
                return None
            with TemporaryFile() as blob:
                subprocess.run(
                    [*git, "blob", f"{self.commit}:{self.path}"],
                    stdin=subprocess.DEVNULL,
                    stdout=blob,
                    stderr=subprocess.DEVNULL,
                    check=True,
                    timeout=60,
                    env=env,
                )
                blob.seek(0)
                digest = hashlib.sha256()
                for chunk in iter(lambda: blob.read(1 << 20), b""):
                    digest.update(chunk)
                return digest.hexdigest()
        except (OSError, UnicodeError, subprocess.SubprocessError):
            return None


def git_source(raw: Mapping[str, Any], where: str) -> tuple[GitPath, str]:
    if set(raw) != {"repo", "commit", "path", "sha256"}:
        raise AttestationError(
            f"'{where}' Git source requires exactly repo, commit, path and sha256"
        )
    return GitPath.parse(raw, where), hex_digest(raw["sha256"], f"{where}.sha256")


def git_entry(raw: Any) -> bool:
    return isinstance(raw, Mapping) and bool({"repo", "commit"} & raw.keys())


def git_scoped(path: Any) -> bool:
    return isinstance(path, Mapping) and path.get("scope") == "git"


def member_label(path: Any) -> str:
    """A readable locator for per-member CLI output."""
    if isinstance(path, Mapping):
        if path.get("scope") == "git":
            return f"{path.get('repo')}@{path.get('commit')}:{path.get('path')}"
        return str(path.get("path"))
    return str(path)
