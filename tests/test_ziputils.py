"""Unit tests for :func:`klayout_tools.ziputils.safe_extract`."""

from __future__ import annotations

import stat
import zipfile
from pathlib import Path

import pytest

from klayout_tools import ZipSmugglingError, safe_extract
from klayout_tools.ziputils import _validated_member_name


def _write_archive(path: Path, members: dict[str, str]) -> Path:
    """Build a zip at ``path`` with ``{name: payload}`` members."""
    with zipfile.ZipFile(path, "w") as archive:
        for name, payload in members.items():
            info = zipfile.ZipInfo(name)
            info.external_attr = 0o644 << 16
            archive.writestr(info, payload)
    return path


def _write_symlink_archive(path: Path, name: str = "link") -> Path:
    """Build a zip whose single member is a symlink entry."""
    with zipfile.ZipFile(path, "w") as archive:
        info = zipfile.ZipInfo(name)
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, "/etc/passwd")
    return path


class TestZipSmuggling:
    """Every smuggle class is rejected and nothing lands outside dest."""

    @pytest.mark.parametrize(
        "member",
        [
            "/etc/passwd",
            "../escape.txt",
            "nested/../../escape.txt",
            "..",
            "C:\\Windows\\escape.txt",
            "C:/Windows/escape.txt",
            "..\\..\\escape.txt",
            "legit\\..\\..\\escape.txt",
            "",
        ],
    )
    def test_member_rejected(self, tmp_path: Path, member: str) -> None:
        archive = _write_archive(tmp_path / "trap.zip", {member: "payload"})
        dest = tmp_path / "dest"
        with pytest.raises(ZipSmugglingError) as excinfo:
            safe_extract(archive, dest)
        assert excinfo.value.member == member
        assert list(dest.iterdir()) == []
        assert not (tmp_path / "escape.txt").exists()

    def test_nul_byte_name_rejected(self) -> None:
        # zipfile.ZipInfo sanitizes names at construction (NUL truncated,
        # separators normalized), so a NUL-carrying name cannot arrive
        # through ZipFile.infolist() on any supported Python; the check is
        # exercised at the validator level, where a raw member info lands.
        member = "evil\x00.yp"
        raw_info = type(
            "MemberInfo", (), {"filename": member, "external_attr": 0o644 << 16}
        )()
        with pytest.raises(ZipSmugglingError, match="NUL") as excinfo:
            _validated_member_name(raw_info)
        assert excinfo.value.member == member

    def test_symlink_member_rejected(self, tmp_path: Path) -> None:
        archive = _write_symlink_archive(tmp_path / "trap.zip")
        dest = tmp_path / "dest"
        with pytest.raises(ZipSmugglingError, match="symlink"):
            safe_extract(archive, dest)
        assert list(dest.iterdir()) == []

    def test_no_partial_extraction_on_rejected_archive(self, tmp_path: Path) -> None:
        archive = _write_archive(
            tmp_path / "trap.zip",
            {"ok.txt": "fine", "../escape.txt": "payload"},
        )
        dest = tmp_path / "dest"
        with pytest.raises(ZipSmugglingError):
            safe_extract(archive, dest)
        assert not (dest / "ok.txt").exists()
        assert not (tmp_path / "escape.txt").exists()

    def test_preexisting_symlink_redirect_rejected(self, tmp_path: Path) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        dest = tmp_path / "dest"
        dest.mkdir()
        (dest / "doorway").symlink_to(outside)
        archive = _write_archive(tmp_path / "trap.zip", {"doorway/x.txt": "payload"})
        with pytest.raises(ZipSmugglingError, match="escapes"):
            safe_extract(archive, dest)
        assert list(outside.iterdir()) == []


class TestZipHappyPath:
    """Benign archives extract exactly like ``extractall`` would."""

    def test_multi_member_nested_dirs(self, tmp_path: Path) -> None:
        archive = _write_archive(
            tmp_path / "good.zip",
            {
                "a.txt": "alpha",
                "dir/b.txt": "bravo",
                "dir/sub/c.txt": "charlie",
            },
        )
        dest = tmp_path / "dest"
        safe_extract(archive, dest)
        assert (dest / "a.txt").read_text() == "alpha"
        assert (dest / "dir" / "b.txt").read_text() == "bravo"
        assert (dest / "dir" / "sub" / "c.txt").read_text() == "charlie"

    def test_accepts_str_and_path(self, tmp_path: Path) -> None:
        archive = _write_archive(tmp_path / "good.zip", {"a.txt": "alpha"})
        safe_extract(str(archive), str(tmp_path / "d1"))
        safe_extract(str(archive), Path(tmp_path / "d2"))
        safe_extract(archive, str(tmp_path / "d3"))
        safe_extract(archive, Path(tmp_path / "d4"))
        for name in ("d1", "d2", "d3", "d4"):
            assert (tmp_path / name / "a.txt").read_text() == "alpha"

    def test_dest_autocreated(self, tmp_path: Path) -> None:
        archive = _write_archive(tmp_path / "good.zip", {"a.txt": "alpha"})
        dest = tmp_path / "deep" / "nested" / "dest"
        assert not dest.exists()
        safe_extract(archive, dest)
        assert (dest / "a.txt").read_text() == "alpha"

    def test_overwrite_last_wins(self, tmp_path: Path) -> None:
        first = _write_archive(tmp_path / "one.zip", {"a.txt": "first"})
        second = _write_archive(tmp_path / "two.zip", {"a.txt": "second"})
        dest = tmp_path / "dest"
        safe_extract(first, dest)
        safe_extract(second, dest)
        assert (dest / "a.txt").read_text() == "second"

    def test_type_validation(self, tmp_path: Path) -> None:
        archive = _write_archive(tmp_path / "good.zip", {"a.txt": "alpha"})
        with pytest.raises(TypeError, match="zip_path"):
            safe_extract(123, tmp_path / "dest")
        with pytest.raises(TypeError, match="dest"):
            safe_extract(archive, 123)
        with pytest.raises(TypeError, match="zip_path"):
            safe_extract(None, tmp_path / "dest")
