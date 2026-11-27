"""The git-tracked *input tree* stage of the batch lifecycle, shared by
``netlist_batch``, ``extract_batch`` and ``signoff_generator_batch``:
resolve the checkout root, list its git-tracked files, filter them with the
``--batch-include`` / ``--batch-exclude`` globs, measure the selected set
and write the deterministic ``tree.tar.gz`` the runner extracts.

The submit half lives in :mod:`._batch_common` and the collect half in
:mod:`._batch_collect`; this module is their sibling rather than a part of
either because ``_batch_common`` sits at the file-size ratchet's hard limit.
It imports no verb module, and every refusal is raised as the ``err_cls``
the caller passes -- :class:`~klayout_tools.netlist.NetlistError` for ``klt
netlist``, :class:`~klayout_tools.extract.ExtractError` for ``klt extract``,
:class:`~klayout_tools.signoff_generator_batch.GeneratorApprovalError` for
``klt signoff`` -- so no caller translates another verb's error, and the
root refusal names the file *that* verb ships (``what``). The size-cap
refusal stays with each caller, in its own words: its message and ``code``
are part of the JSON contract (``docs/json-contract.md``), as are the error
strings built here.
"""

from __future__ import annotations

import fnmatch
import io
import os
import subprocess
import tarfile
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import TypeVar

from . import _batch_common

_E = TypeVar("_E", bound=Exception)

#: Cap on the **uncompressed** total size of the git-tracked files uploaded as a
#: batch job's input tree (64 MiB). Schematics, symbols, a layout and a request
#: document are small; a tree above this almost always means the root also
#: covers other large tracked artifacts, so the fix is a narrower
#: ``--batch-root`` or tighter include globs, not a bigger upload.
MAX_BATCH_TREE_BYTES = 64 * 1024 * 1024

#: ``error.code`` for an input tree above :data:`MAX_BATCH_TREE_BYTES`. Each
#: caller raises it with its own error class and hint.
BATCH_INPUT_TOO_LARGE_CODE = "batch_input_too_large"

_GLOB_CHARS = frozenset("*?[")


@dataclass(frozen=True)
class TrackedTree:
    """The selected git-tracked files a batch job ships, measured.

    ``files`` are root-relative POSIX paths that exist on disk, in sorted
    (archive) order; ``total_bytes`` is their uncompressed size.
    """

    root: str
    files: tuple[str, ...]
    total_bytes: int


def run_git(cwd: str, *args: str, err_cls: type[_E]) -> bytes:
    """``git -C cwd args`` stdout. A git that cannot run or exits non-zero is
    ``err_cls``, naming the command, the directory and git's own stderr."""
    try:
        result = subprocess.run(
            ["git", "-C", cwd, *args], capture_output=True, timeout=60
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise err_cls(f"could not run git in {cwd}: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise err_cls(f"git {' '.join(args)} failed in {cwd}: {detail}")
    return result.stdout


def resolve_root(
    anchor: str, batch_root: str | None, *, what: str, err_cls: type[_E]
) -> str:
    """The directory whose git-tracked files are uploaded: ``batch_root``
    (which must be a directory inside a git checkout), else the top level of
    ``anchor``'s own checkout. ``what`` names ``anchor`` (``"schematic"``,
    ``"layout"``) in the refusal for one outside git."""
    if batch_root is not None:
        root = os.path.realpath(batch_root)
        if not os.path.isdir(root):
            raise err_cls(f"--batch-root is not a directory: {batch_root}")
        run_git(root, "rev-parse", "--show-toplevel", err_cls=err_cls)
        return root
    start = os.path.dirname(os.path.realpath(anchor))
    try:
        top = run_git(start, "rev-parse", "--show-toplevel", err_cls=err_cls)
    except err_cls as exc:
        raise err_cls(
            f"--backend batch uploads the git-tracked files around the "
            f"{what}, but {anchor} is not in a git checkout ({exc}); "
            "pass --batch-root, or use --backend local"
        ) from exc
    return os.path.realpath(top.decode("utf-8", errors="surrogateescape").strip())


def tracked_files(root: str, *, err_cls: type[_E]) -> list[str]:
    """The git-tracked files under ``root`` (``git ls-files``), sorted, as
    root-relative POSIX paths. A submodule's gitlink is listed as a path of
    its own; :func:`measure_tree` drops it."""
    raw = run_git(root, "ls-files", "-z", err_cls=err_cls)
    return sorted(
        item.decode("utf-8", errors="surrogateescape")
        for item in raw.split(b"\0")
        if item
    )


def matches_glob(rel: str, pattern: str) -> bool:
    """``rel`` (a root-relative POSIX path) against one ``--batch-include`` /
    ``--batch-exclude`` pattern. ``fnmatch`` semantics, so ``*`` also crosses
    ``/``; a pattern with no glob character names a file or a directory and
    matches that file or everything under that directory."""
    while pattern.startswith("./"):
        pattern = pattern[2:]
    if not any(char in _GLOB_CHARS for char in pattern):
        bare = pattern.rstrip("/")
        return rel == bare or rel.startswith(bare + "/")
    return fnmatch.fnmatchcase(rel, pattern)


def select_files(
    files: Sequence[str], include: Sequence[str], exclude: Sequence[str]
) -> list[str]:
    """``files`` kept by ``include`` (every file when empty) and not dropped
    by ``exclude``, in the given order."""
    return [
        rel
        for rel in files
        if (not include or any(matches_glob(rel, pat) for pat in include))
        and not any(matches_glob(rel, pat) for pat in exclude)
    ]


def relative_tracked(
    path: str,
    root: str,
    tracked: Collection[str],
    *,
    what: str,
    flag: str,
    err_cls: type[_E],
) -> str:
    """``path`` as a root-relative POSIX path, refusing one outside the root
    or not tracked by git (it would not be in the upload). ``what`` names it
    in the message; ``flag`` extends the outside-the-root hint."""
    rel = os.path.relpath(os.path.realpath(path), root)
    if rel == os.pardir or rel.startswith(os.pardir + os.sep) or os.path.isabs(rel):
        raise err_cls(
            f"{what} {path} is outside the batch root {root}; only files under "
            f"the root are uploaded -- pass a wider --batch-root{flag}"
        )
    rel = rel.replace(os.sep, "/")
    if rel not in tracked:
        raise err_cls(
            f"{what} {path} is not tracked by git under {root}; --backend "
            "batch uploads only git-tracked files, so the runner would not "
            "have it -- commit (or `git add`) it first"
        )
    return rel


def require_selected(
    rel: str, selected: Collection[str], *, what: str, err_cls: type[_E]
) -> None:
    """Refuse a file the include / exclude globs leave out of the upload."""
    if rel not in selected:
        raise err_cls(
            f"{what} {rel} is left out of the upload by --batch-include / "
            f"--batch-exclude; the {what} must be in the selected file set"
        )


def measure_tree(root: str, selected: Collection[str]) -> TrackedTree:
    """The selected files that exist on disk, and their total uncompressed
    size. A tracked file deleted in the working tree is skipped (``tar`` would
    fail on it), and so is a directory that is not a symlink (a submodule's
    gitlink). Pass the selected index entries with duplicates intact. Each
    entry counts toward the total and becomes a member in sorted order."""
    present: list[str] = []
    total = 0
    for rel in sorted(selected):
        path = os.path.join(root, rel)
        try:
            info = os.lstat(path)
        except OSError:
            continue
        if os.path.isdir(path) and not os.path.islink(path):
            continue
        present.append(rel)
        total += info.st_size
    return TrackedTree(root=root, files=tuple(present), total_bytes=total)


def write_tarball(
    root: str,
    files: Sequence[str],
    path: str,
    *,
    extra_members: Mapping[str, bytes] | None = None,
) -> None:
    """Write ``files`` (root-relative paths under ``root``, in order) into a
    gzip tarball at ``path`` with root-relative member names; symlinks are
    stored as symlinks. ``extra_members`` adds generated members (name to
    bytes, mode ``0644``) after them. Every member passes through
    :func:`_batch_common.scrub_tarinfo`, so the same input tree always
    yields the same member headers."""
    with tarfile.open(path, "w:gz") as archive:
        for rel in files:
            archive.add(
                os.path.join(root, rel),
                arcname=rel,
                recursive=False,
                filter=_batch_common.scrub_tarinfo,
            )
        for name, body in (extra_members or {}).items():
            info = tarfile.TarInfo(name)
            info.size = len(body)
            info.mode = 0o644
            _batch_common.scrub_tarinfo(info)
            archive.addfile(info, io.BytesIO(body))
