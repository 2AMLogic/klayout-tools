"""The installed signoff modules define the reported grading-ruleset identity."""

from pathlib import Path

from klayout_tools import build_identity


def test_grading_ruleset_id_is_a_sha256_prefixed_hash():
    ruleset_id = build_identity.grading_ruleset_id()
    assert ruleset_id is not None
    assert ruleset_id.startswith("sha256:")
    assert len(ruleset_id) == len("sha256:") + 64


def test_grading_ruleset_id_is_stable_across_calls():
    """Same running build, called twice -- must not fabricate churn."""
    assert build_identity.grading_ruleset_id() == build_identity.grading_ruleset_id()


def _grading_install(tmp_path: Path, name: str) -> Path:
    """A throwaway "install" dir holding byte copies of this build's entire
    shipped `signoff*.py` family -- the fixture every test below points
    `build_identity.__file__` at, so the real glob/sort/digest path runs
    against a tree the test can then edit."""
    package_dir = Path(build_identity.__file__).resolve().parent
    install = tmp_path / name
    install.mkdir()
    for module in sorted(package_dir.glob("signoff*.py")):
        (install / module.name).write_bytes(module.read_bytes())
    return install


def _grading_id_for(monkeypatch, install: Path) -> str | None:
    monkeypatch.setattr(build_identity, "__file__", str(install / "build_identity.py"))
    return build_identity.grading_ruleset_id()


def test_grading_ruleset_id_identical_for_byte_identical_content_at_a_different_path(
    tmp_path,
    monkeypatch,
):
    """AC: a registry wheel and a `git+...` snapshot of the same tag ship
    byte-identical `signoff*.py` source at different install paths (neither
    has the other's `.git` history to compare against) -- the id must match
    regardless of where the files physically live."""
    real_id = build_identity.grading_ruleset_id()

    id_a = _grading_id_for(monkeypatch, _grading_install(tmp_path, "install_a"))
    id_b = _grading_id_for(monkeypatch, _grading_install(tmp_path, "install_b"))

    assert id_a == id_b == real_id


def test_grading_ruleset_id_changes_when_the_grading_module_content_changes(
    tmp_path, monkeypatch
):
    """AC: the identifier changes when `signoff.py`'s grading logic changes."""
    install = _grading_install(tmp_path, "install")
    before = _grading_id_for(monkeypatch, install)
    assert before is not None and before.startswith("sha256:")

    module = install / "signoff.py"
    module.write_bytes(
        module.read_bytes() + b"\n# grading logic, version B -- a rule changed\n"
    )

    after = _grading_id_for(monkeypatch, install)
    assert after is not None and after.startswith("sha256:")
    assert before != after


def test_grading_ruleset_id_changes_when_a_leaf_module_changes(tmp_path, monkeypatch):
    """AC (issue #7434): an edit confined to a `signoff*.py` leaf -- not
    `signoff.py` itself -- still moves the id; that lost coverage is the
    gap this issue was filed to close."""
    install = _grading_install(tmp_path, "install")
    before = _grading_id_for(monkeypatch, install)

    leaf = next(
        path
        for path in sorted(install.iterdir())
        if path.name.startswith("signoff")
        and path.name.endswith(".py")
        and path.name != "signoff.py"
    )
    leaf.write_bytes(leaf.read_bytes() + b"\n# a leaf-only grading edit\n")

    assert _grading_id_for(monkeypatch, install) != before


def test_grading_ruleset_id_ignores_non_signoff_files(tmp_path, monkeypatch):
    """AC (issue #7434): a module in the package directory that is outside
    the `signoff*.py` family is outside the id's coverage -- adding or
    editing it must not move the id."""
    install = _grading_install(tmp_path, "install")
    before = _grading_id_for(monkeypatch, install)

    (install / "coverage.py").write_text("# not a grading module\n")

    assert _grading_id_for(monkeypatch, install) == before


def test_grading_ruleset_id_changes_when_content_moves_between_modules(
    tmp_path, monkeypatch
):
    """AC (issue #7434): each basename is mixed into the digest before its
    bytes, so swapping two leaves' content between their filenames -- a
    rename, in effect -- moves the id even though the multiset of bytes in
    the directory is unchanged."""
    install = _grading_install(tmp_path, "install")
    before = _grading_id_for(monkeypatch, install)

    leaves = [
        path
        for path in sorted(install.iterdir())
        if path.name.startswith("signoff")
        and path.name.endswith(".py")
        and path.name != "signoff.py"
    ]
    leaf_a, leaf_b = leaves[0], leaves[1]
    bytes_a, bytes_b = leaf_a.read_bytes(), leaf_b.read_bytes()
    assert bytes_a != bytes_b
    leaf_a.write_bytes(bytes_b)
    leaf_b.write_bytes(bytes_a)

    assert _grading_id_for(monkeypatch, install) != before


def test_grading_ruleset_id_none_when_the_module_is_missing(tmp_path, monkeypatch):
    install = tmp_path / "hand-truncated-install"
    install.mkdir()
    (install / "some_other_module.py").write_text("# the grading modules are gone\n")
    monkeypatch.setattr(build_identity, "__file__", str(install / "build_identity.py"))
    assert build_identity.grading_ruleset_id() is None


def test_grading_ruleset_id_none_when_install_directory_is_missing(
    tmp_path, monkeypatch
):
    assert _grading_id_for(monkeypatch, tmp_path / "missing-install") is None


def test_grading_ruleset_id_none_when_a_discovered_leaf_cannot_be_read(
    tmp_path, monkeypatch
):
    install = _grading_install(tmp_path, "broken-install")
    (install / "signoff_broken.py").symlink_to(install / "missing.py")

    assert _grading_id_for(monkeypatch, install) is None


def test_version_report_includes_grading_ruleset_id(monkeypatch):
    monkeypatch.setattr(
        build_identity, "grading_ruleset_id", lambda: "sha256:" + "a" * 64
    )
    report = build_identity.version_report()
    assert report["grading_ruleset_id"] == "sha256:" + "a" * 64
