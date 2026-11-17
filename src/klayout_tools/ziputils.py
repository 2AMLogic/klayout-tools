"""Hardened zip extraction for untrusted archives.

``ZipFile.extractall()`` trusts member names enough to write outside the
destination when a name carries ``..`` components, an absolute path, or
Windows drive syntax. :func:`safe_extract` validates every member against
the resolved destination before any byte is written, then extracts one
member at a time through :meth:`zipfile.ZipFile.extract`.
"""

from __future__ import annotations

import os
import stat
import zipfile
from pathlib import Path, PureWindowsPath

__all__ = ["ZipSmugglingError", "safe_extract"]


class ZipSmugglingError(ValueError):
    """A zip member's name or metadata tries to escape the destination."""

    def __init__(self, member: str, reason: str) -> None:
        super().__init__(f"unsafe zip member {member!r}: {reason}")
        self.member = member
        self.reason = reason


def _validated_member_name(info: zipfile.ZipInfo) -> str:
    """Return the member's name, or raise :class:`ZipSmugglingError`."""
    name = info.filename
    if not name:
        raise ZipSmugglingError(name, "empty member name")
    if "\x00" in name:
        raise ZipSmugglingError(name, "NUL byte in member name")
    if stat.S_ISLNK(info.external_attr >> 16):
        raise ZipSmugglingError(name, "symlink entry")
    if name.startswith(("/", "\\")):
        raise ZipSmugglingError(name, "absolute path")
    if PureWindowsPath(name).drive:
        raise ZipSmugglingError(name, "drive-letter path")
    if ".." in name.replace("\\", "/").split("/"):
        raise ZipSmugglingError(name, ".. traversal")
    return name


def _contained_target(root: Path, name: str) -> None:
    """Check where ``name`` lands under ``root``; reject escapes.

    ``root`` must already be resolved. Lexically clean names cannot
    escape, but the resolve-and-contain check also catches targets that a
    pre-existing symlink inside the destination would redirect outward.
    """
    parts = [p for p in name.replace("\\", "/").split("/") if p not in ("", ".")]
    target = root.joinpath(*parts).resolve()
    if not target.is_relative_to(root):
        raise ZipSmugglingError(name, "resolved path escapes the destination")


def safe_extract(
    zip_path: str | os.PathLike[str], dest: str | os.PathLike[str]
) -> None:
    """Extract the archive at ``zip_path`` into ``dest``, refusing escapes.

    Creates ``dest`` (with parents) when it does not exist, validates every
    member -- absolute names, ``..`` traversal (``/`` or ``\\`` separators),
    Windows drive letters, symlink entries, and NUL bytes -- before writing
    anything, then extracts member by member. Overwrite behavior matches
    ``extractall``: a repeated name is truncated, last writer wins.

    Raises :class:`ZipSmugglingError` for a rejected member (nothing is
    extracted from a rejected archive) and ``TypeError`` for a non-path
    ``zip_path`` or ``dest``.
    """
    if not isinstance(zip_path, (str, os.PathLike)):
        raise TypeError(
            f"zip_path must be str or os.PathLike, got {type(zip_path).__name__}"
        )
    if not isinstance(dest, (str, os.PathLike)):
        raise TypeError(f"dest must be str or os.PathLike, got {type(dest).__name__}")
    dest_dir = Path(dest)
    dest_dir.mkdir(parents=True, exist_ok=True)
    root = dest_dir.resolve()
    with zipfile.ZipFile(zip_path) as archive:
        infos = archive.infolist()
        for info in infos:
            _contained_target(root, _validated_member_name(info))
        for info in infos:
            archive.extract(info, str(root))
