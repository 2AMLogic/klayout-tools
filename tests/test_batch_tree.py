"""Direct tests for the shared git-tracked batch input-tree stage
``_batch_tree`` that ``netlist_batch``, ``extract_batch`` and
``signoff_generator_batch`` share (issue #8023): checkout-root resolution,
the tracked-file listing and glob filter, the size measurement and the
deterministic tarball -- each refusal raised as the *caller's* error class,
so no verb translates another verb's error. The only real subprocess is
``git``, against a throwaway repository in ``tmp_path``."""

from __future__ import annotations

import hashlib
import subprocess
import sys
import tarfile
from dataclasses import replace
from pathlib import Path

import pytest

from klayout_tools import _batch_tree
from klayout_tools import netlist_batch as nb
from klayout_tools import signoff_generator_batch as sgb


class _Boom(Exception):
    """A caller's own error class: nothing here should raise anything else."""


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path) -> Path:
    """Three tracked files (one deleted in the working tree), one gitlink
    and one untracked file."""
    root = tmp_path / "repo"
    (root / "design").mkdir(parents=True)
    (root / "design" / "a.sch").write_bytes(b"aaaa")
    (root / "design" / "b.sym").write_bytes(b"bb")
    (root / "README.md").write_bytes(b"r")
    _git(root, "init", "-q")
    _git(root, "add", ".")
    (root / "design" / "b.sym").unlink()
    (root / "design" / "sub").mkdir()
    _git(
        root,
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{'1' * 40},design/sub",
    )
    (root / "design" / "scratch").write_bytes(b"untracked")
    return root


def test_batch_tree_imports_standalone():
    """``_batch_common`` is the only ``_batch_*`` sibling it needs; no
    ``sim_batch`` (which imports ``_batch_common``) and no verb module."""
    script = (
        "import sys, klayout_tools._batch_tree\n"
        "loaded = {m.rsplit('.', 1)[-1] for m in sys.modules "
        "if m.startswith('klayout_tools.')}\n"
        "assert 'sim_batch' not in loaded, loaded\n"
        "assert not loaded & {'netlist_batch', 'extract_batch', "
        "'signoff_generator_batch'}, loaded\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr


def test_shared_cap_and_code_are_what_netlist_batch_reexports():
    assert _batch_tree.MAX_BATCH_TREE_BYTES == 64 * 1024 * 1024
    assert _batch_tree.BATCH_INPUT_TOO_LARGE_CODE == "batch_input_too_large"
    assert nb.MAX_BATCH_TREE_BYTES == _batch_tree.MAX_BATCH_TREE_BYTES
    assert nb.BATCH_INPUT_TOO_LARGE_CODE == _batch_tree.BATCH_INPUT_TOO_LARGE_CODE
    assert "MAX_BATCH_TREE_BYTES" in nb.__all__


def test_run_git_raises_the_callers_error_with_gits_detail(tmp_path, monkeypatch):
    with pytest.raises(_Boom, match="git rev-parse --show-toplevel failed in"):
        _batch_tree.run_git(
            str(tmp_path / "missing"), "rev-parse", "--show-toplevel", err_cls=_Boom
        )

    def _explode(*args, **kwargs):
        raise OSError("no git")

    monkeypatch.setattr(subprocess, "run", _explode)
    with pytest.raises(_Boom, match="could not run git in .*: no git"):
        _batch_tree.run_git(str(tmp_path), "ls-files", err_cls=_Boom)


def test_resolve_root_names_the_anchor_in_the_refusal(repo, tmp_path):
    loose = tmp_path / "loose"
    loose.mkdir()
    anchor = loose / "top.gds"
    anchor.write_bytes(b"GDS")
    with pytest.raises(_Boom) as err:
        _batch_tree.resolve_root(str(anchor), None, what="layout", err_cls=_Boom)
    message = str(err.value)
    assert (
        f"around the layout, but {anchor} is not in a git checkout "
        "(git rev-parse --show-toplevel failed in " in message
    )
    assert message.endswith("; pass --batch-root, or use --backend local")
    assert "schematic" not in message


def test_resolve_root_is_the_batch_root_else_the_anchors_checkout(repo, tmp_path):
    schematic = str(repo / "design" / "a.sch")
    top = str(repo.resolve())
    assert (
        _batch_tree.resolve_root(schematic, None, what="schematic", err_cls=_Boom)
        == top
    )
    sub = repo / "design"
    assert _batch_tree.resolve_root(
        schematic, str(sub), what="schematic", err_cls=_Boom
    ) == str(sub.resolve())
    with pytest.raises(_Boom, match="--batch-root is not a directory: "):
        _batch_tree.resolve_root(
            schematic, str(sub / "missing"), what="schematic", err_cls=_Boom
        )
    (tmp_path / "loose").mkdir()
    with pytest.raises(_Boom, match="git rev-parse --show-toplevel failed in"):
        _batch_tree.resolve_root(
            schematic, str(tmp_path / "loose"), what="schematic", err_cls=_Boom
        )


def test_select_files_keeps_the_filter_flags_glob_semantics():
    files = ["README.md", "design/a.sch", "design/b.sym", "design/sub/c.sym"]
    matches = _batch_tree.matches_glob
    assert matches("design/a.sch", "design")
    assert matches("design/a.sch", "./design/")
    assert not matches("README.md", "design")
    assert matches("design/sub/c.sym", "design/*.sym")
    assert not matches("design/a.sch", "design/*.sym")
    assert matches("design/a.sch", "design/?.sch")
    assert matches("design/a.sch", "design/[ab].sch")
    assert not matches("design/a.sch", "DESIGN/A.SCH")
    assert _batch_tree.select_files(files, [], []) == files
    assert _batch_tree.select_files(files, ["design"], ["*.sym"]) == ["design/a.sch"]
    assert _batch_tree.select_files(files, ["design/b.sym"], []) == ["design/b.sym"]


def test_relative_tracked_and_require_selected_raise_the_callers_error(repo, tmp_path):
    root = str(repo.resolve())
    tracked = {"README.md", "design/a.sch"}
    relative = _batch_tree.relative_tracked
    assert (
        relative(
            str(repo / "design" / "a.sch"),
            root,
            tracked,
            what="schematic",
            flag="",
            err_cls=_Boom,
        )
        == "design/a.sch"
    )
    with pytest.raises(_Boom, match="schematic .*scratch is not tracked by git under "):
        relative(
            str(repo / "design" / "scratch"),
            root,
            tracked,
            what="schematic",
            flag="",
            err_cls=_Boom,
        )
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"x")
    with pytest.raises(
        _Boom,
        match=r"schematic .*outside\.txt is outside the batch root .*; only files "
        r"under the root are uploaded -- pass a wider --batch-root, or X$",
    ):
        relative(
            str(outside), root, tracked, what="schematic", flag=", or X", err_cls=_Boom
        )
    with pytest.raises(
        _Boom,
        match=r"--rcfile design/a\.sch is left out of the upload by --batch-include / "
        r"--batch-exclude; the --rcfile must be in the selected file set$",
    ):
        _batch_tree.require_selected(
            "design/a.sch", set(), what="--rcfile", err_cls=_Boom
        )
    _batch_tree.require_selected(
        "design/a.sch", {"design/a.sch"}, what="schematic", err_cls=_Boom
    )


def test_tracked_files_lists_gitlinks_too_and_raises_the_callers_error(repo, tmp_path):
    root = str(repo.resolve())
    assert _batch_tree.tracked_files(root, err_cls=_Boom) == [
        "README.md",
        "design/a.sch",
        "design/b.sym",
        "design/sub",
    ]
    with pytest.raises(_Boom, match="git ls-files -z failed in"):
        _batch_tree.tracked_files(str(tmp_path / "missing"), err_cls=_Boom)


def test_measure_tree_skips_deleted_files_and_gitlinks_in_sorted_order(repo):
    root = str(repo.resolve())
    files = _batch_tree.tracked_files(root, err_cls=_Boom)
    tree = _batch_tree.measure_tree(root, files)
    assert tree == _batch_tree.TrackedTree(
        root=root, files=("README.md", "design/a.sch"), total_bytes=1 + 4
    )
    assert _batch_tree.measure_tree(root, set(files)).files == (
        "README.md",
        "design/a.sch",
    )
    assert _batch_tree.measure_tree(root, []).total_bytes == 0


def test_write_tarball_is_scrubbed_ordered_and_carries_extra_members(repo, tmp_path):
    root = str(repo.resolve())
    (repo / "link.sym").symlink_to("design/a.sch")
    path = tmp_path / "tree.tar.gz"
    _batch_tree.write_tarball(
        root,
        ["design/a.sch", "link.sym"],
        str(path),
        extra_members={"request.json": b'{"a": 1}', "zz/empty": b""},
    )
    with tarfile.open(path) as archive:
        members = archive.getmembers()
        assert [m.name for m in members] == [
            "design/a.sch",
            "link.sym",
            "request.json",
            "zz/empty",
        ]
        by_name = {m.name: m for m in members}
        assert by_name["link.sym"].issym()
        assert by_name["link.sym"].linkname == "design/a.sch"
        assert by_name["request.json"].mode == 0o644
        assert by_name["request.json"].size == 8
        for member in members:
            assert (member.uid, member.gid, member.uname, member.gname) == (
                0,
                0,
                "",
                "",
            )
            assert member.mtime == 0
        assert archive.extractfile("design/a.sch").read() == b"aaaa"
        assert archive.extractfile("request.json").read() == b'{"a": 1}'

    _batch_tree.write_tarball(root, ["README.md"], str(path))
    with tarfile.open(path) as archive:
        assert [m.name for m in archive.getmembers()] == ["README.md"]


def test_signoff_adapter_refuses_its_tree_with_its_own_error(
    repo, tmp_path, monkeypatch
):
    """``signoff_generator_batch`` no longer stuffs a script into a
    ``netlist_batch.BatchTree`` nor translates ``NetlistError``: every
    packaging refusal is its own ``GeneratorApprovalError``, which
    ``run_approved_generator`` already turns into ``package_failed``."""
    script = repo / "scripts" / "gen.py"
    script.parent.mkdir()
    script.write_bytes(b"print('x')\n")
    _git(repo, "add", "scripts/gen.py")
    approval = sgb.GeneratorApproval(
        id="gen",
        interpreter="python3",
        script="scripts/gen.py",
        script_sha256=_sha(b"print('x')\n"),
        artifact="netlist/ota.spice",
        output="netlist/ota.spice",
    )
    tree = sgb._select_tree(str(repo), approval)
    assert type(tree) is _batch_tree.TrackedTree
    assert tree.files == ("README.md", "design/a.sch", "scripts/gen.py")
    with pytest.raises(sgb.GeneratorApprovalError, match="left out by the approval"):
        sgb._select_tree(str(repo), replace(approval, include=("design",)))
    with pytest.raises(sgb.GeneratorApprovalError, match="not tracked by git under"):
        sgb._select_tree(str(repo), replace(approval, script="scripts/missing.py"))
    with pytest.raises(
        sgb.GeneratorApprovalError, match="does not hash to the approved"
    ):
        sgb._package(
            str(repo),
            replace(approval, script_sha256="sha256:" + "0" * 64),
            str(tmp_path),
        )
    monkeypatch.setattr(_batch_tree, "MAX_BATCH_TREE_BYTES", 3)
    with pytest.raises(
        sgb.GeneratorApprovalError, match="above the 3-byte cap; narrow the"
    ):
        sgb._select_tree(str(repo), approval)
