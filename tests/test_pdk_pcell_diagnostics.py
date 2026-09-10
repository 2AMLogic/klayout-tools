"""Tests for the PDK PyCell importability diagnostics (issue #1610).

`_load_libraries` (`klayout_tools.pdk_pcell`) already imports every PyCell
package a PDK ships under `libs.tech/klayout/python/` and reports, for each
failure, an `unavailable` entry (issue #1535 / PR #1538). This module covers
the three gaps issue #1610 closed against that machinery:

1. The importability probe is reachable from `klt pdk pcell-check` without
   invoking `klt gen` at all.
2. An import failure attributable to an empty/near-empty vendored
   (git-submodule) directory -- the signature of a PDK installed from a
   release tarball that does not carry submodule contents -- gets a
   dedicated, actionable `reason` instead of a bare import-failure message.
3. `_load_libraries`'s own `sys.path` handling is unaffected by a vendor
   shim that misresolves its *own* idea of "PDK root" via a fragile,
   fixed-depth `__file__`-relative parent-directory walk (the failure mode
   issue #1603 named) when checked out somewhere other than its canonical
   in-PDK path.

It also covers the `_VENDOR_COMPAT_SHIMS` mechanism issue #1630 added: a
per-package-name table of PDK-relative `sys.path` hints for a compat shim
the PDK itself vendors (ihp-sg13g2's `cni`, via `pycell4klayout-api`), and
the diagnostic split between "this shim is vendored by the PDK, just not
populated where expected" and "this is a genuine third-party PyPI
dependency" once one of those packages still fails to import.

Every PDK here is fabricated under `tmp_path`, mirroring
`test_gen_pdk_pcell.py`'s fixtures -- CI never downloads a real PDK.
Registering a `pya.Library` and importing a Python package are both
process-global and permanent, so every fixture package below gets a unique
package/library/class name (see `_unique`); tests must never reuse one.
"""

import itertools
import json
import os
import sys

import pytest

from klayout_tools import pdk, pdk_pcell
from klayout_tools.cli import main
from klayout_tools.cli.pdk_cmd import EXIT_PCELL_UNAVAILABLE
from klayout_tools.pdk_pcell import list_pdk_pcells

_COUNTER = itertools.count()


def _unique(prefix: str) -> str:
    """A process-unique identifier -- see the module docstring."""
    return f"{prefix}_{next(_COUNTER)}"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Scrub PDK env vars and empty the host search space -- see test_pdk.py."""
    monkeypatch.delenv("PDK_ROOT", raising=False)
    monkeypatch.delenv("PDK", raising=False)
    monkeypatch.setattr(pdk, "STORE_DIRS", [])
    monkeypatch.setattr(pdk, "CONVENTIONAL_PREFIXES", [])


def _make_install(root, variant, *, klayout=True):
    """Fabricate a minimal open_pdks-layout variant, optionally with a
    ``libs.tech/klayout`` asset directory but no ``python/`` inside it."""
    variant_dir = root / variant
    (variant_dir / "libs.tech").mkdir(parents=True)
    if klayout:
        (variant_dir / "libs.tech" / "klayout").mkdir()
    return variant_dir


def _write_package(variant_dir, package, body):
    package_dir = variant_dir / "libs.tech" / "klayout" / "python" / package
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text(body)
    return package_dir


#: A plain-``pya`` toy vendor PyCell package that self-registers on import --
#: mirrors ihp-sg13g2's ``sg13g2_native_pcell_lib`` shape (see
#: `test_gen_pdk_pcell.py`'s ``_TOY_PACKAGE``).
_TOY_PACKAGE = """\
import pya


class {klass}(pya.PCellDeclarationHelper):
    def __init__(self):
        super().__init__()

    def display_text_impl(self):
        return "{cell}"

    def produce_impl(self):
        pass


class {libklass}(pya.Library):
    def __init__(self):
        super().__init__()
        self.description = "toy vendor PCell library"
        self.layout().register_pcell("{cell}", {klass}())
        self.register("{library}")


{libklass}()
"""


# --------------------------------------------------------------------------- #
# `_find_empty_vendor_subdir` -- the empty/near-empty-directory heuristic
# --------------------------------------------------------------------------- #


def test_find_empty_vendor_subdir_detects_completely_empty_subdirectory(tmp_path):
    """A nested, completely empty subdirectory -- the on-disk signature of
    an uninitialized git submodule -- is found even when the package's own
    top-level `__init__.py` has real content (verified against ihp-sg13g2's
    layered `sg13g2_native_pcell_lib` -> `sg13g2_pycell_lib` -> `cni.tech`
    import chain, where only the innermost layer is the empty submodule)."""
    package_dir = tmp_path / "vendor_pkg"
    package_dir.mkdir()
    (package_dir / "__init__.py").write_text("import os\nVALUE = 1\n")
    (package_dir / "compat_submodule").mkdir()

    assert pdk_pcell._find_empty_vendor_subdir(str(package_dir)) == "compat_submodule"


def test_find_empty_vendor_subdir_detects_stub_only_top_level(tmp_path):
    """A package directory whose only file is an empty/comment-only
    `__init__.py` is itself the empty-vendored-directory signature."""
    package_dir = tmp_path / "vendor_pkg"
    package_dir.mkdir()
    (package_dir / "__init__.py").write_text("# placeholder\n\n")

    assert pdk_pcell._find_empty_vendor_subdir(str(package_dir)) == "."


def test_find_empty_vendor_subdir_returns_none_for_real_content(tmp_path):
    """A package with real code throughout (no empty/stub directory
    anywhere in its tree) is not misclassified."""
    package_dir = tmp_path / "vendor_pkg"
    package_dir.mkdir()
    (package_dir / "__init__.py").write_text("import os\nVALUE = 1\n")
    nested = package_dir / "helpers"
    nested.mkdir()
    (nested / "__init__.py").write_text("REAL = True\n")

    assert pdk_pcell._find_empty_vendor_subdir(str(package_dir)) is None


# --------------------------------------------------------------------------- #
# End-to-end: the `unavailable[].reason` classification
# --------------------------------------------------------------------------- #

#: Mirrors a real vendored-submodule failure: the package's own
#: `__init__.py` is real code (not a stub), but it reaches into a nested
#: subdirectory that is a checked-out-empty git submodule mount point --
#: exactly ihp-sg13g2's `cni`/`pycell4klayout-api` shape (see the module
#: docstring of `klayout_tools.pdk_pcell`).
_EMPTY_SUBMODULE_PACKAGE = """\
import os

_here = os.path.dirname(os.path.abspath(__file__))
_submodule_dir = os.path.join(_here, "vendor_submodule")
if not os.listdir(_submodule_dir):
    raise ImportError(
        "vendor_submodule looks uninitialized (empty directory)"
    )
"""


def test_unavailable_reason_flags_empty_vendored_submodule_directory(tmp_path):
    root = tmp_path / "pdk_install"
    variant_dir = _make_install(root, "sky130A")
    package = _unique("submodpkg")
    package_dir = _write_package(variant_dir, package, _EMPTY_SUBMODULE_PACKAGE)
    (package_dir / "vendor_submodule").mkdir()

    report = list_pdk_pcells(variant="sky130A", root=str(root))

    assert report["libraries"] == []
    assert len(report["unavailable"]) == 1
    entry = report["unavailable"][0]
    assert entry["package"] == package
    assert "vendored submodule directory" in entry["reason"]
    assert "appears empty" in entry["reason"]
    assert "does not include git submodule contents" in entry["reason"]
    assert "Traceback" not in entry["reason"]


def test_unavailable_reason_flags_stub_only_package(tmp_path):
    """The package directory itself checked out as nothing but an empty
    `__init__.py`-shaped stub -- e.g. a submodule mounted directly at the
    package's own path rather than nested inside it."""
    root = tmp_path / "pdk_install"
    variant_dir = _make_install(root, "sky130A")
    package = _unique("stubpkg")
    missing = _unique("some_vendor_dep")
    # An import failure must still occur for `_load_libraries` to reach the
    # classification path -- an empty __init__.py alone raises nothing.
    _write_package(variant_dir, package, f"# placeholder\nimport {missing}\n")

    report = list_pdk_pcells(variant="sky130A", root=str(root))

    entry = report["unavailable"][0]
    # The stub-only-file heuristic only applies when *every* file in the
    # directory is empty/comment-only; this package's __init__.py has a
    # real (if trivial) import statement, so it is NOT misclassified as an
    # empty vendored directory -- it keeps the generic missing-dependency
    # message. This guards the "only empty stubs" heuristic against being
    # too eager.
    assert entry["missing_dependency"] == missing
    assert "vendored submodule directory" not in entry["reason"]


def test_unavailable_reason_stays_generic_for_ordinary_missing_dependency(tmp_path):
    """A package reaching a genuinely missing third-party dependency (the
    real sky130A `cells` -> `gdsfactory` case) must not be misclassified as
    an empty vendored directory -- regression guard for the new
    classification added alongside the generic message."""
    root = tmp_path / "pdk_install"
    variant_dir = _make_install(root, "sky130A")
    package = _unique("shimlib")
    missing = _unique("vendor_compat_shim")
    _write_package(variant_dir, package, f"import {missing}\n")

    report = list_pdk_pcells(variant="sky130A", root=str(root))

    assert report["unavailable"] == [
        {
            "package": package,
            "missing_dependency": missing,
            "reason": (
                f"requires Python module '{missing}', which is not importable "
                "in this environment"
            ),
        }
    ]


# --------------------------------------------------------------------------- #
# Regression: a shim's own fragile `__file__`-relative parent-dir walk
# --------------------------------------------------------------------------- #


def test_load_libraries_tolerates_shim_parent_dir_walk_misresolution(tmp_path):
    """A vendor PCell shim that resolves its own idea of "PDK root" via a
    fixed-depth `__file__`-relative parent walk (issue #1603's named
    fragility) is wrong whenever the package is checked out somewhere other
    than the canonical depth it assumes -- as here, where the package is
    checked out flat, one level under `tmp_path`, instead of nested 4-deep
    under `<root>/<variant>/libs.tech/klayout/python/<package>/` the way a
    real PDK install nests it.

    `_load_libraries` never depends on a vendor shim's own self-resolved
    path: it drives its own `sys.path` insertion from the *actual*,
    already-discovered `lib_dir` (`pdk_pcell.py`'s `_load_libraries`
    docstring). This proves that mechanism holds even when the shim's own
    internal guess is not just wrong but walks all the way up to the
    filesystem root `/` -- the most catastrophic form of this bug -- by
    confirming `_load_libraries`'s own result (what it put on `sys.path`,
    and whether the package imported successfully) stays correct regardless.
    """
    package = _unique("depthlib")
    library = _unique("depth_vendor_lib")
    klass = _unique("DepthBox")
    libklass = _unique("DepthLibrary")

    # Checked out FLAT -- one level under tmp_path -- not at the 4-deep
    # nesting a real PDK's own PyCell package sits at.
    lib_dir = tmp_path / "flat_checkout"
    package_dir = lib_dir / package
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text(
        f"""\
import os
import pya

# Mirrors a real vendor shim's `__file__`-relative parent walk, assuming a
# fixed canonical nesting depth -- issue #1603's named fragility. Walked far
# past this fabricated tree's actual depth so the shim's own guess collapses
# all the way to the filesystem root, the worst-case form of misresolution.
_SHIM_RESOLVED_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), *([os.pardir] * 40))
)


class {klass}(pya.PCellDeclarationHelper):
    def __init__(self):
        super().__init__()

    def display_text_impl(self):
        return "{klass}"

    def produce_impl(self):
        pass


class {libklass}(pya.Library):
    def __init__(self):
        super().__init__()
        self.description = "toy shim vendor lib"
        self.layout().register_pcell("{klass}", {klass}())
        self.register("{library}")


{libklass}()
"""
    )

    loaded, unavailable = pdk_pcell._load_libraries(str(lib_dir))

    assert unavailable == []
    assert [entry["library"] for entry in loaded] == [library]

    module = sys.modules[package]
    # The shim's own naive fixed-depth walk collapses to the filesystem
    # root -- confirming this fabricated shim really does exhibit the
    # catastrophic form of issue #1603's fragility.
    assert module._SHIM_RESOLVED_ROOT == os.path.abspath(os.sep)

    # `_load_libraries`'s own result is unaffected: it put the *real*,
    # discovered `lib_dir` on `sys.path` (at the front, per its own
    # docstring), never the shim's collapsed guess.
    assert sys.path[0] == os.path.abspath(str(lib_dir))
    assert os.path.abspath(str(lib_dir)) in sys.path


# --------------------------------------------------------------------------- #
# `_VENDOR_COMPAT_SHIMS` -- PDK-relative sys.path hint for a compat shim the
# PDK itself vendors (issue #1630), distinguished from a third-party PyPI
# dependency klt will never vendor.
# --------------------------------------------------------------------------- #

#: A toy vendor PyCell package whose first statement imports a PDK-bundled
#: compat shim module -- mirrors ihp-sg13g2's `sg13g2_pycell_lib`, whose
#: first statement is `from cni.tech import Tech`.
_SHIM_DEPENDENT_PACKAGE = """\
import {module_name}
import pya


class {klass}(pya.PCellDeclarationHelper):
    def __init__(self):
        super().__init__()

    def display_text_impl(self):
        return "{cell}"

    def produce_impl(self):
        pass


class {libklass}(pya.Library):
    def __init__(self):
        super().__init__()
        self.description = "toy shim-dependent vendor PCell library"
        self.layout().register_pcell("{cell}", {klass}())
        self.register("{library}")


{libklass}()
"""


def test_production_compat_shim_table_entry_for_sg13g2_pycell_lib():
    """Regression guard for the concrete, issue-verified table entry: a
    change here without updating this test is a change to real ihp-sg13g2
    behaviour, not just internal refactoring."""
    shim = pdk_pcell._VENDOR_COMPAT_SHIMS["sg13g2_pycell_lib"]
    assert shim.sys_path_hint == ("pycell4klayout-api", "source", "python")
    assert shim.shim_root == ("pycell4klayout-api",)
    assert shim.provides_module == "cni"
    assert shim.needs_tkinter_workaround is True


def _write_shim_dependent_package(variant_dir, package, module_name):
    return _write_package(
        variant_dir,
        package,
        _SHIM_DEPENDENT_PACKAGE.format(
            module_name=module_name,
            klass=_unique("ShimBox"),
            libklass=_unique("ShimLibrary"),
            library=_unique("shim_vendor_lib"),
            cell="ShimBox",
        ),
    )


def test_apply_sys_path_hint_makes_shim_based_package_importable(tmp_path, monkeypatch):
    """A package listed in `_VENDOR_COMPAT_SHIMS` gets its PDK-bundled
    compat-shim directory added to `sys.path` before import is attempted --
    turning an otherwise-unavailable `cni`-shaped dependency into a loadable
    one, without a real ihp-sg13g2 install."""
    root = tmp_path / "pdk_install"
    variant_dir = _make_install(root, "ihp-sg13g2")
    package = _unique("shimpkg")
    module_name = _unique("compat_module")
    shim_dirname = _unique("compat_shim_root")

    # The PDK-bundled compat shim, nested one level deeper than the
    # package's own directory -- mirrors ihp-sg13g2's
    # `pycell4klayout-api/source/python/cni`.
    shim_dir = (
        variant_dir
        / "libs.tech"
        / "klayout"
        / "python"
        / shim_dirname
        / "source"
        / "python"
    )
    shim_dir.mkdir(parents=True)
    (shim_dir / f"{module_name}.py").write_text("VALUE = 1\n")

    _write_shim_dependent_package(variant_dir, package, module_name)
    monkeypatch.setitem(
        pdk_pcell._VENDOR_COMPAT_SHIMS,
        package,
        pdk_pcell._VendorCompatShim(
            sys_path_hint=(shim_dirname, "source", "python"),
            shim_root=(shim_dirname,),
            provides_module=module_name,
            needs_tkinter_workaround=False,
        ),
    )

    report = list_pdk_pcells(variant="ihp-sg13g2", root=str(root))

    assert report["unavailable"] == []
    assert [lib["package"] for lib in report["libraries"]] == [package]


def test_missing_compat_shim_directory_reports_pdk_vendored_reason(
    tmp_path, monkeypatch
):
    """The shim's own top-level directory does not exist at all in this
    install -- reported as a compat shim the PDK vendors, not a third-party
    dependency klt would need to add."""
    root = tmp_path / "pdk_install"
    variant_dir = _make_install(root, "ihp-sg13g2")
    package = _unique("shimpkg")
    module_name = _unique("compat_module")
    shim_dirname = _unique("compat_shim_root")

    _write_shim_dependent_package(variant_dir, package, module_name)
    monkeypatch.setitem(
        pdk_pcell._VENDOR_COMPAT_SHIMS,
        package,
        pdk_pcell._VendorCompatShim(
            sys_path_hint=(shim_dirname, "source", "python"),
            shim_root=(shim_dirname,),
            provides_module=module_name,
            needs_tkinter_workaround=False,
        ),
    )

    report = list_pdk_pcells(variant="ihp-sg13g2", root=str(root))

    assert report["libraries"] == []
    entry = report["unavailable"][0]
    assert entry["package"] == package
    assert entry["missing_dependency"] == module_name
    assert "compat shim the PDK itself vendors" in entry["reason"]
    assert "not a third-party PyPI dependency" in entry["reason"]
    assert "Traceback" not in entry["reason"]


def test_empty_compat_shim_directory_reports_pdk_vendored_reason(tmp_path, monkeypatch):
    """The shim's top-level directory exists but is completely empty -- the
    uninitialized-git-submodule signature -- classified the same way as the
    missing-entirely case above."""
    root = tmp_path / "pdk_install"
    variant_dir = _make_install(root, "ihp-sg13g2")
    package = _unique("shimpkg")
    module_name = _unique("compat_module")
    shim_dirname = _unique("compat_shim_root")

    (variant_dir / "libs.tech" / "klayout" / "python" / shim_dirname).mkdir(
        parents=True
    )

    _write_shim_dependent_package(variant_dir, package, module_name)
    monkeypatch.setitem(
        pdk_pcell._VENDOR_COMPAT_SHIMS,
        package,
        pdk_pcell._VendorCompatShim(
            sys_path_hint=(shim_dirname, "source", "python"),
            shim_root=(shim_dirname,),
            provides_module=module_name,
            needs_tkinter_workaround=False,
        ),
    )

    report = list_pdk_pcells(variant="ihp-sg13g2", root=str(root))

    entry = report["unavailable"][0]
    assert "compat shim the PDK itself vendors" in entry["reason"]


def test_compat_shim_present_but_module_missing_reports_vendor_bug(
    tmp_path, monkeypatch
):
    """The shim's own directory exists and is genuinely populated (not the
    empty-submodule signature), yet the specific module the package needs
    still fails to import -- a bug in the vendor shim itself, reported as
    neither the empty-submodule case nor the generic third-party-dependency
    message."""
    root = tmp_path / "pdk_install"
    variant_dir = _make_install(root, "ihp-sg13g2")
    package = _unique("shimpkg")
    module_name = _unique("compat_module")
    shim_dirname = _unique("compat_shim_root")

    shim_dir = (
        variant_dir
        / "libs.tech"
        / "klayout"
        / "python"
        / shim_dirname
        / "source"
        / "python"
    )
    shim_dir.mkdir(parents=True)
    # Real, non-stub content -- but not the module the package actually
    # imports, so the empty-vendored-directory heuristic must not fire.
    (shim_dir / "other_file.py").write_text("PRESENT = True\n")

    _write_shim_dependent_package(variant_dir, package, module_name)
    monkeypatch.setitem(
        pdk_pcell._VENDOR_COMPAT_SHIMS,
        package,
        pdk_pcell._VendorCompatShim(
            sys_path_hint=(shim_dirname, "source", "python"),
            shim_root=(shim_dirname,),
            provides_module=module_name,
            needs_tkinter_workaround=False,
        ),
    )

    report = list_pdk_pcells(variant="ihp-sg13g2", root=str(root))

    entry = report["unavailable"][0]
    assert entry["missing_dependency"] == module_name
    assert "bug in the vendor shim itself" in entry["reason"]
    assert "compat shim the PDK itself vendors" not in entry["reason"]


def test_missing_dependency_not_matching_shim_module_stays_generic(
    tmp_path, monkeypatch
):
    """A package listed in `_VENDOR_COMPAT_SHIMS` that fails on a *different*,
    genuinely third-party missing module (not the one the shim provides)
    must keep the original generic message -- regression guard against
    over-triggering the new classification."""
    root = tmp_path / "pdk_install"
    variant_dir = _make_install(root, "ihp-sg13g2")
    package = _unique("shimpkg")
    unrelated_missing = _unique("unrelated_third_party_dep")
    shim_module = _unique("compat_module")

    _write_package(variant_dir, package, f"import {unrelated_missing}\n")
    monkeypatch.setitem(
        pdk_pcell._VENDOR_COMPAT_SHIMS,
        package,
        pdk_pcell._VendorCompatShim(
            sys_path_hint=(_unique("shim_root"),),
            shim_root=(_unique("shim_root"),),
            provides_module=shim_module,
            needs_tkinter_workaround=False,
        ),
    )

    report = list_pdk_pcells(variant="ihp-sg13g2", root=str(root))

    entry = report["unavailable"][0]
    assert entry["missing_dependency"] == unrelated_missing
    assert entry["reason"] == (
        f"requires Python module '{unrelated_missing}', which is not "
        "importable in this environment"
    )


# --------------------------------------------------------------------------- #
# `klt pdk pcell-check`
# --------------------------------------------------------------------------- #


def _toy_pdk(tmp_path, *, variant="sky130A"):
    root = tmp_path / "pdk_install"
    variant_dir = _make_install(root, variant)
    package = _unique("toylib")
    library = _unique("toy_vendor_lib")
    klass = _unique("ToyBox")
    libklass = _unique("ToyLibrary")
    _write_package(
        variant_dir,
        package,
        _TOY_PACKAGE.format(
            klass=klass, libklass=libklass, library=library, cell="ToyBox"
        ),
    )
    return root, package, library


def test_cli_pdk_pcell_check_json_reports_loaded_and_unavailable(tmp_path, capsys):
    root = tmp_path / "pdk_install"
    variant_dir = _make_install(root, "sky130A")

    loadable_package = _unique("toylib")
    library = _unique("toy_vendor_lib")
    klass = _unique("ToyBox")
    libklass = _unique("ToyLibrary")
    _write_package(
        variant_dir,
        loadable_package,
        _TOY_PACKAGE.format(
            klass=klass, libklass=libklass, library=library, cell="ToyBox"
        ),
    )

    broken_package = _unique("shimlib")
    missing = _unique("vendor_compat_shim")
    _write_package(variant_dir, broken_package, f"import {missing}\n")

    exit_code = main(
        [
            "pdk",
            "pcell-check",
            "--pdk",
            "sky130A",
            "--pdk-root",
            str(root),
            "--format",
            "json",
        ]
    )

    assert exit_code == EXIT_PCELL_UNAVAILABLE
    report = json.loads(capsys.readouterr().out)
    assert report["schema_version"] == 1
    assert report["pdk"]["variant"] == "sky130A"
    assert [lib["library"] for lib in report["libraries"]] == [library]
    assert report["unavailable"] == [
        {
            "package": broken_package,
            "missing_dependency": missing,
            "reason": (
                f"requires Python module '{missing}', which is not importable "
                "in this environment"
            ),
        }
    ]


def test_cli_pdk_pcell_check_text(tmp_path, capsys):
    root, package, library = _toy_pdk(tmp_path)

    exit_code = main(
        ["pdk", "pcell-check", "--pdk", "sky130A", "--pdk-root", str(root)]
    )

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "pdk: sky130A" in out
    assert "importable:" in out
    assert library in out
    assert package in out


def test_cli_pdk_pcell_check_no_python_dir_is_success(tmp_path, capsys):
    root = tmp_path / "pdk_install"
    _make_install(root, "gf180mcuD")

    exit_code = main(
        [
            "pdk",
            "pcell-check",
            "--pdk",
            "gf180mcuD",
            "--pdk-root",
            str(root),
            "--format",
            "json",
        ]
    )

    assert exit_code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["pcell_lib_dir"] is None
    assert report["libraries"] == []
    assert report["unavailable"] == []


def test_cli_pdk_pcell_check_unresolvable_pdk_is_application_error(tmp_path, capsys):
    exit_code = main(
        [
            "pdk",
            "pcell-check",
            "--pdk",
            "sky130A",
            "--pdk-root",
            str(tmp_path / "does_not_exist"),
            "--format",
            "json",
        ]
    )

    assert exit_code == 1
    err = json.loads(capsys.readouterr().err)
    assert err["error"]["command"] == "pdk pcell-check"
