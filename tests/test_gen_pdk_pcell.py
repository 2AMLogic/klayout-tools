"""Tests for `klt gen --list-pdk-pcells` / `--pdk-pcell` (issue #1535).

Every PDK here is **fabricated** under ``tmp_path`` -- a minimal open_pdks
variant plus a hand-written ``libs.tech/klayout/python/<package>/`` toy PyCell
package -- mirroring `test_gen.py`'s `_isolate`/`pdk_root`/`_make_install`
fixtures. CI never downloads a real PDK, and the two real vendor PyCell
libraries this feature targets (sky130A's ``cells``, ihp-sg13g2's
``sg13g2_native_pcell_lib``) are not importable in a bare CI environment
anyway -- see the module docstring of ``klayout_tools.pdk_pcell``.

Registering a ``pya.Library`` and importing a Python package are both
process-global and permanent, so every fixture package below gets a unique
package/library/class name (see :func:`_unique`); tests must never reuse one.
"""

import itertools
import json
import sys

import pytest

from klayout_tools import pdk, pdk_pcell
from klayout_tools.cli import main
from klayout_tools.pdk_pcell import (
    PdkPCellError,
    generate_pdk_pcell,
    list_pdk_pcells,
    parse_pdk_pcell_ref,
)

_COUNTER = itertools.count()


def _unique(prefix: str) -> str:
    """A process-unique identifier -- see the module docstring."""
    return f"{prefix}_{next(_COUNTER)}"


#: A plain-``pya`` toy vendor PyCell package: one ``PCellDeclarationHelper``
#: subclass drawing a parametrized box, registered into a ``pya.Library``
#: subclass. Mirrors the shape of both real PDK PyCell libraries verified for
#: issue #1535 (sky130A's ``cells``, ihp-sg13g2's
#: ``sg13g2_native_pcell_lib``), minus their third-party imports.
_TOY_PACKAGE = '''\
import pya


class {klass}(pya.PCellDeclarationHelper):
    """Toy vendor PCell: a parametrized box."""

    def __init__(self):
        super().__init__()
        self.param("w_um", self.TypeDouble, "Box width (um)", default=2.0)
        self.param("h_um", self.TypeDouble, "Box height (um)", default=1.0)
        self.param("n", self.TypeInt, "Repeat count", default=1)
        self.param(
            "layer", self.TypeLayer, "Drawing layer", default=pya.LayerInfo(1, 0)
        )

    def display_text_impl(self):
        return "{cell}"

    def produce_impl(self):
        index = self.cell.layout().layer(self.layer)
        for i in range(self.n):
            self.cell.shapes(index).insert(
                pya.DBox(
                    i * self.w_um, 0.0, (i + 1) * self.w_um, self.h_um
                )
            )


class {libklass}(pya.Library):
    def __init__(self):
        super().__init__()
        self.description = "toy vendor PCell library"
        self.layout().register_pcell("{cell}", {klass}())
        self.register("{library}")

{registration}
'''


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


def _toy_pdk(tmp_path, *, self_registering=True, variant="sky130A"):
    """A fabricated install shipping one loadable toy PyCell package.

    Returns ``(root, package, library, cell)``. ``self_registering`` picks
    which of the two real-world registration shapes the package uses: a bare
    constructor call at module scope (ihp-sg13g2's
    ``sg13g2_native_pcell_lib``) or a library class the importer is expected
    to construct (sky130A's ``cells``, constructed by the PDK's own
    ``.lym`` autoload macro).
    """
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
            klass=klass,
            libklass=libklass,
            library=library,
            cell="ToyBox",
            registration=f"{libklass}()" if self_registering else "",
        ),
    )
    return root, package, library, "ToyBox"


# --------------------------------------------------------------------------- #
# PDK-side discovery (klayout_tools.pdk)
# --------------------------------------------------------------------------- #


def test_pdk_pcell_lib_dir_and_libraries(tmp_path):
    root, package, _library, _cell = _toy_pdk(tmp_path)

    lib_dir = pdk.pdk_pcell_lib_dir(variant="sky130A", root=str(root))
    assert lib_dir == str(root / "sky130A" / "libs.tech" / "klayout" / "python")
    assert pdk.pdk_pcell_libraries(variant="sky130A", root=str(root)) == [package]


def test_pdk_pcell_lib_dir_is_none_without_python_subdir(tmp_path):
    root = tmp_path / "pdk_install"
    _make_install(root, "gf180mcuD")

    assert pdk.pdk_pcell_lib_dir(variant="gf180mcuD", root=str(root)) is None
    assert pdk.pdk_pcell_libraries(variant="gf180mcuD", root=str(root)) == []


def test_pdk_pcell_lib_dir_is_none_without_klayout_asset(tmp_path):
    root = tmp_path / "pdk_install"
    _make_install(root, "sky130A", klayout=False)

    assert pdk.pdk_pcell_lib_dir(variant="sky130A", root=str(root)) is None
    assert pdk.pdk_pcell_libraries(variant="sky130A", root=str(root)) == []


def test_find_pdk_reports_has_pcell_library(tmp_path):
    root, _package, _library, _cell = _toy_pdk(tmp_path)

    report = pdk.find_pdk(variant="sky130A", root=str(root))
    assert report["has_pcell_library"] is True


def test_find_pdk_has_pcell_library_false_without_packages(tmp_path):
    root = tmp_path / "pdk_install"
    variant_dir = _make_install(root, "sky130A")
    # A `python/` directory that holds no Python package at all.
    (variant_dir / "libs.tech" / "klayout" / "python").mkdir()

    report = pdk.find_pdk(variant="sky130A", root=str(root))
    assert report["has_pcell_library"] is False


def test_list_pdks_reports_has_pcell_library(tmp_path):
    root, _package, _library, _cell = _toy_pdk(tmp_path)
    _make_install(root, "gf180mcuD")

    report = pdk.list_pdks(root=str(root))
    by_name = {v["name"]: v for v in report["installs"][0]["variants"]}
    assert by_name["sky130A"]["has_pcell_library"] is True
    assert by_name["gf180mcuD"]["has_pcell_library"] is False


def test_cli_pdk_find_exposes_has_pcell_library(tmp_path, capsys):
    root, _package, _library, _cell = _toy_pdk(tmp_path)

    exit_code = main(
        ["pdk", "find", "--pdk", "sky130A", "--pdk-root", str(root), "--format", "json"]
    )
    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["has_pcell_library"] is True


# --------------------------------------------------------------------------- #
# --list-pdk-pcells
# --------------------------------------------------------------------------- #


def test_list_pdk_pcells_enumerates_library_cells_and_params(tmp_path):
    root, package, library, cell = _toy_pdk(tmp_path)

    report = list_pdk_pcells(variant="sky130A", root=str(root))

    assert report["schema_version"] == 1
    assert report["pdk"]["variant"] == "sky130A"
    assert report["pcell_lib_dir"].endswith("libs.tech/klayout/python")
    assert report["unavailable"] == []

    assert [lib["library"] for lib in report["libraries"]] == [library]
    entry = report["libraries"][0]
    assert entry["package"] == package
    assert entry["description"] == "toy vendor PCell library"
    assert [c["name"] for c in entry["cells"]] == [cell]

    params = {p["name"]: p for p in entry["cells"][0]["params"]}
    assert params["w_um"]["type"] == "double"
    assert params["w_um"]["default"] == 2.0
    assert params["w_um"]["description"] == "Box width (um)"
    assert params["w_um"]["settable"] is True
    assert params["n"]["type"] == "int"
    # A TypeLayer default is not a JSON primitive -- it is rendered with
    # KLayout's own "<layer>/<datatype>" spelling, which round-trips as an
    # accepted --params value.
    assert params["layer"]["type"] == "layer"
    assert params["layer"]["default"] == "1/0"


def test_list_pdk_pcells_loads_a_define_only_library(tmp_path):
    """sky130A's `cells` package defines `class sky130(pya.Library)` and
    leaves construction to the PDK's own autoload macro -- klt constructs the
    vendor's exported class rather than requiring an import side effect."""
    root, _package, library, cell = _toy_pdk(tmp_path, self_registering=False)

    report = list_pdk_pcells(variant="sky130A", root=str(root))

    assert [lib["library"] for lib in report["libraries"]] == [library]
    assert [c["name"] for c in report["libraries"][0]["cells"]] == [cell]


def test_list_pdk_pcells_is_empty_when_pdk_ships_no_python_dir(tmp_path):
    """A resolved PDK with `assets["klayout"]` but no `python/` (a real
    gf180mcuD install) enumerates empty -- a success, not an error."""
    root = tmp_path / "pdk_install"
    _make_install(root, "gf180mcuD")

    report = list_pdk_pcells(variant="gf180mcuD", root=str(root))

    assert report["pcell_lib_dir"] is None
    assert report["libraries"] == []
    assert report["unavailable"] == []


def test_cli_list_pdk_pcells_json(tmp_path, capsys):
    root, _package, library, cell = _toy_pdk(tmp_path)

    exit_code = main(
        [
            "gen",
            "--list-pdk-pcells",
            "--pdk",
            "sky130A",
            "--pdk-root",
            str(root),
            "--format",
            "json",
        ]
    )
    assert exit_code == 0
    data = json.loads(capsys.readouterr().out)
    assert set(data.keys()) == {
        "schema_version",
        "pdk",
        "pcell_lib_dir",
        "libraries",
        "unavailable",
    }
    assert data["libraries"][0]["library"] == library
    assert data["libraries"][0]["cells"][0]["name"] == cell


def test_cli_list_pdk_pcells_text(tmp_path, capsys):
    root, _package, library, cell = _toy_pdk(tmp_path)

    exit_code = main(
        ["gen", "--list-pdk-pcells", "--pdk", "sky130A", "--pdk-root", str(root)]
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    assert f"{library}/{cell}" in out
    assert "w_um (double" in out
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)


def test_cli_list_pdk_pcells_empty_is_success(tmp_path, capsys):
    root = tmp_path / "pdk_install"
    _make_install(root, "gf180mcuD")

    exit_code = main(
        [
            "gen",
            "--list-pdk-pcells",
            "--pdk",
            "gf180mcuD",
            "--pdk-root",
            str(root),
            "--format",
            "json",
        ]
    )
    assert exit_code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["libraries"] == []
    assert data["pcell_lib_dir"] is None


def test_list_pdk_pcells_unresolvable_pdk_is_application_error(tmp_path, capsys):
    exit_code = main(
        [
            "gen",
            "--list-pdk-pcells",
            "--pdk-root",
            str(tmp_path / "nope"),
            "--format",
            "json",
        ]
    )
    assert exit_code == 1
    err = json.loads(capsys.readouterr().err)
    assert err["error"]["command"] == "gen"


# --------------------------------------------------------------------------- #
# --pdk-pcell instantiation
# --------------------------------------------------------------------------- #


def test_generate_pdk_pcell_writes_gds_and_matches_contract(tmp_path):
    root, package, library, cell = _toy_pdk(tmp_path)
    output = tmp_path / "toy_0.gds"

    report = generate_pdk_pcell(
        {
            "pdk_pcell": f"{library}/{cell}",
            "pdk": {"variant": "sky130A", "root": str(root)},
            "params": {"w_um": 3.0, "h_um": 1.5},
            "options": {"cell_name": "toy_0", "output": str(output)},
        }
    )

    assert output.is_file()
    assert report["schema_version"] == 1
    assert report["generator"] == f"{library}/{cell}"
    assert report["pdk_pcell"] == {
        "library": library,
        "cell": cell,
        "package": package,
    }
    assert report["cell_name"] == "toy_0"
    assert report["gds_path"] == str(output)
    assert report["pdk"] == {
        "name": "sky130A",
        "variant": "sky130A",
        "version": None,
    }
    assert report["dbu_um"] == pytest.approx(0.001)
    assert report["bbox_um"]["x1"] == pytest.approx(3.0)
    assert report["bbox_um"]["y1"] == pytest.approx(1.5)
    # A vendor PCell is instantiated opaquely -- klt never interprets its
    # geometry to infer devices or pins.
    assert report["device_count"] == 1
    assert report["ports"] == []
    assert report["drc_hints"]["notes"]
    assert report["warnings"] == []


def test_generate_pdk_pcell_uses_vendor_defaults_when_params_omitted(tmp_path):
    root, _package, library, cell = _toy_pdk(tmp_path)
    output = tmp_path / "out.gds"

    report = generate_pdk_pcell(
        {
            "pdk_pcell": f"{library}/{cell}",
            "pdk": {"variant": "sky130A", "root": str(root)},
            "options": {"output": str(output)},
        }
    )

    # The vendor's own defaults (2.0 x 1.0), resolved by KLayout -- klt never
    # restates them.
    assert report["cell_name"] == f"{cell}_0"
    assert report["bbox_um"]["x1"] == pytest.approx(2.0)
    assert report["bbox_um"]["y1"] == pytest.approx(1.0)


def test_generate_pdk_pcell_accepts_a_layer_param(tmp_path):
    root, _package, library, cell = _toy_pdk(tmp_path)
    output = tmp_path / "out.gds"

    generate_pdk_pcell(
        {
            "pdk_pcell": f"{library}/{cell}",
            "pdk": {"variant": "sky130A", "root": str(root)},
            "params": {"layer": "67/20"},
            "options": {"output": str(output)},
        }
    )

    import klayout.db as kdb

    layout = kdb.Layout()
    layout.read(str(output))
    assert layout.layer_infos()[0].to_s() == "67/20"


# --------------------------------------------------------------------------- #
# The `tkinter`-presence workaround for `_VENDOR_COMPAT_SHIMS` packages
# (issue #1630)
# --------------------------------------------------------------------------- #

#: A toy vendor PCell that raises if `tkinter` is present in `sys.modules`
#: at produce time -- mirrors pycell4klayout-api's own
#: `PCellWrapper.coerce_parameters` branch, which is unconditionally taken
#: (and crashes) whenever `"tkinter" in sys.modules`, even in pure headless
#: batch mode with no widget ever created.
_TKINTER_SENSITIVE_PACKAGE = """\
import sys

import pya


class {klass}(pya.PCellDeclarationHelper):
    def __init__(self):
        super().__init__()

    def display_text_impl(self):
        return "{cell}"

    def produce_impl(self):
        if "tkinter" in sys.modules:
            raise RuntimeError("tkinter must not be present at produce time")
        index = self.cell.layout().layer(pya.LayerInfo(1, 0))
        self.cell.shapes(index).insert(pya.DBox(0.0, 0.0, 1.0, 1.0))


class {libklass}(pya.Library):
    def __init__(self):
        super().__init__()
        self.description = "toy tkinter-sensitive vendor PCell library"
        self.layout().register_pcell("{cell}", {klass}())
        self.register("{library}")


{libklass}()
"""


def test_generate_pdk_pcell_applies_tkinter_workaround_for_known_compat_shim(
    tmp_path, monkeypatch
):
    """A package listed in `_VENDOR_COMPAT_SHIMS` with
    `needs_tkinter_workaround=True` must have `tkinter` removed from
    `sys.modules` before KLayout calls into the vendor PCell's
    `produce_impl` -- otherwise ihp-sg13g2's `pycell4klayout-api` shim
    crashes on every PCell in the package."""
    root = tmp_path / "pdk_install"
    variant_dir = _make_install(root, "ihp-sg13g2")
    package = _unique("tkshimlib")
    library = _unique("tk_vendor_lib")
    klass = _unique("TkBox")
    libklass = _unique("TkLibrary")
    _write_package(
        variant_dir,
        package,
        _TKINTER_SENSITIVE_PACKAGE.format(
            klass=klass, libklass=libklass, library=library, cell="TkBox"
        ),
    )
    monkeypatch.setitem(
        pdk_pcell._VENDOR_COMPAT_SHIMS,
        package,
        pdk_pcell._VendorCompatShim(
            sys_path_hint=(),
            shim_root=(),
            provides_module="unused",
            needs_tkinter_workaround=True,
        ),
    )
    monkeypatch.setitem(sys.modules, "tkinter", object())

    output = tmp_path / "out.gds"
    report = generate_pdk_pcell(
        {
            "pdk_pcell": f"{library}/TkBox",
            "pdk": {"variant": "ihp-sg13g2", "root": str(root)},
            "options": {"output": str(output)},
        }
    )

    assert output.is_file()
    assert report["bbox_um"]["x1"] == pytest.approx(1.0)


def test_generate_pdk_pcell_leaves_tkinter_untouched_for_unrelated_packages(
    tmp_path, monkeypatch
):
    """Regression guard: the `tkinter` workaround is scoped to packages
    named in `_VENDOR_COMPAT_SHIMS` -- an ordinary toy vendor package (no
    entry in that table) must never have `tkinter` touched."""
    root, _package, library, cell = _toy_pdk(tmp_path)
    sentinel = object()
    monkeypatch.setitem(sys.modules, "tkinter", sentinel)

    generate_pdk_pcell(
        {
            "pdk_pcell": f"{library}/{cell}",
            "pdk": {"variant": "sky130A", "root": str(root)},
            "options": {"output": str(tmp_path / "out.gds")},
        }
    )

    assert sys.modules["tkinter"] is sentinel


def test_cli_pdk_pcell_json_contract_keys(tmp_path, capsys):
    root, _package, library, cell = _toy_pdk(tmp_path)
    output = tmp_path / "toy.gds"

    exit_code = main(
        [
            "gen",
            "--pdk-pcell",
            f"{library}/{cell}",
            "--params",
            '{"n": 3}',
            "--pdk",
            "sky130A",
            "--pdk-root",
            str(root),
            "-o",
            str(output),
            "--format",
            "json",
        ]
    )

    assert exit_code == 0
    data = json.loads(capsys.readouterr().out)
    # Same envelope a built-in generator emits, plus the additive `pdk_pcell`
    # object -- an existing `klt gen-compose` block consumer needs no change.
    assert set(data.keys()) == {
        "schema_version",
        "generator",
        "pdk_pcell",
        "cell_name",
        "gds_path",
        "pdk",
        "dbu_um",
        "bbox_um",
        "device_count",
        "ports",
        "drc_hints",
        "warnings",
    }
    assert data["bbox_um"]["x1"] == pytest.approx(6.0)
    assert output.is_file()


def test_cli_pdk_pcell_default_format_is_text(tmp_path, capsys):
    root, _package, library, cell = _toy_pdk(tmp_path)
    output = tmp_path / "toy.gds"

    exit_code = main(
        [
            "gen",
            "--pdk-pcell",
            f"{library}/{cell}",
            "--pdk",
            "sky130A",
            "--pdk-root",
            str(root),
            "-o",
            str(output),
        ]
    )

    assert exit_code == 0
    out = capsys.readouterr().out
    assert f"pdk_pcell: {library}/{cell}" in out
    assert "cell_name:" in out
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)


# --------------------------------------------------------------------------- #
# Application errors (exit 1) -- same shape as an unknown built-in generator
# --------------------------------------------------------------------------- #


def test_cli_unknown_pdk_pcell_library_is_application_error(tmp_path, capsys):
    root, _package, library, cell = _toy_pdk(tmp_path)
    output = tmp_path / "toy.gds"

    exit_code = main(
        [
            "gen",
            "--pdk-pcell",
            "does_not_exist/Nope",
            "--pdk",
            "sky130A",
            "--pdk-root",
            str(root),
            "-o",
            str(output),
            "--format",
            "json",
        ]
    )

    assert exit_code == 1
    err = json.loads(capsys.readouterr().err)
    assert err["error"]["command"] == "gen"
    assert "unknown PDK PCell library" in err["error"]["message"]
    # Names what IS available, the way an unknown generator name does.
    assert library in err["error"]["message"]
    assert not output.exists()
    assert cell  # fixture sanity


def test_cli_unknown_pdk_pcell_cell_is_application_error(tmp_path, capsys):
    root, _package, library, _cell = _toy_pdk(tmp_path)
    output = tmp_path / "toy.gds"

    exit_code = main(
        [
            "gen",
            "--pdk-pcell",
            f"{library}/NotACell",
            "--pdk",
            "sky130A",
            "--pdk-root",
            str(root),
            "-o",
            str(output),
            "--format",
            "json",
        ]
    )

    assert exit_code == 1
    err = json.loads(capsys.readouterr().err)
    assert "unknown PCell 'NotACell'" in err["error"]["message"]
    assert not output.exists()


def test_unknown_pdk_pcell_library_raises(tmp_path):
    root, _package, _library, _cell = _toy_pdk(tmp_path)

    with pytest.raises(PdkPCellError, match="unknown PDK PCell library"):
        generate_pdk_pcell(
            {
                "pdk_pcell": "nope/Nope",
                "pdk": {"variant": "sky130A", "root": str(root)},
            }
        )


def test_pdk_without_pcell_library_is_application_error(tmp_path):
    root = tmp_path / "pdk_install"
    _make_install(root, "gf180mcuD")

    with pytest.raises(PdkPCellError, match="ships no KLayout PCell library"):
        generate_pdk_pcell(
            {
                "pdk_pcell": "nope/Nope",
                "pdk": {"variant": "gf180mcuD", "root": str(root)},
            }
        )


def test_unimportable_package_is_named_application_error(tmp_path, capsys):
    """A vendor PyCell package needing a compat layer klt does not ship (the
    real `cni` / `gdsfactory` case) must fail as a named application error,
    never a raw traceback -- and klt must not vendor the shim."""
    root = tmp_path / "pdk_install"
    variant_dir = _make_install(root, "sky130A")
    package = _unique("shimlib")
    missing = _unique("vendor_compat_shim")
    _write_package(variant_dir, package, f"import {missing}\n")
    output = tmp_path / "toy.gds"

    exit_code = main(
        [
            "gen",
            "--pdk-pcell",
            "whatever/Cell",
            "--pdk",
            "sky130A",
            "--pdk-root",
            str(root),
            "-o",
            str(output),
            "--format",
            "json",
        ]
    )

    assert exit_code == 1
    err = json.loads(capsys.readouterr().err)
    assert err["error"]["command"] == "gen"
    message = err["error"]["message"]
    assert missing in message
    assert package in message
    assert "does not vendor or reimplement" in message
    assert "Traceback" not in message
    assert not output.exists()


def test_unimportable_package_is_listed_as_unavailable(tmp_path):
    """`--list-pdk-pcells` reports an unloadable package structurally instead
    of failing the whole enumeration -- one broken package must not hide the
    libraries that did load, and "ships PCells but needs X installed" is
    exactly what this listing exists to answer."""
    root = tmp_path / "pdk_install"
    variant_dir = _make_install(root, "sky130A")
    broken = _unique("shimlib")
    missing = _unique("vendor_compat_shim")
    _write_package(variant_dir, broken, f"import {missing}\n")

    working = _unique("toylib")
    library = _unique("toy_vendor_lib")
    klass = _unique("ToyBox")
    libklass = _unique("ToyLibrary")
    _write_package(
        variant_dir,
        working,
        _TOY_PACKAGE.format(
            klass=klass,
            libklass=libklass,
            library=library,
            cell="ToyBox",
            registration=f"{libklass}()",
        ),
    )

    report = list_pdk_pcells(variant="sky130A", root=str(root))

    assert [lib["library"] for lib in report["libraries"]] == [library]
    assert report["unavailable"] == [
        {
            "package": broken,
            "missing_dependency": missing,
            "reason": (
                f"requires Python module '{missing}', which is not importable "
                "in this environment"
            ),
        }
    ]


def test_non_import_load_failure_is_reported_without_traceback(tmp_path):
    root = tmp_path / "pdk_install"
    variant_dir = _make_install(root, "sky130A")
    package = _unique("angrylib")
    _write_package(variant_dir, package, "raise RuntimeError('vendor exploded')\n")

    report = list_pdk_pcells(variant="sky130A", root=str(root))

    assert report["libraries"] == []
    assert report["unavailable"] == [
        {
            "package": package,
            "missing_dependency": None,
            "reason": "RuntimeError: vendor exploded",
        }
    ]


def test_unknown_param_is_application_error(tmp_path):
    root, _package, library, cell = _toy_pdk(tmp_path)

    with pytest.raises(PdkPCellError, match="unknown params: nope"):
        generate_pdk_pcell(
            {
                "pdk_pcell": f"{library}/{cell}",
                "pdk": {"variant": "sky130A", "root": str(root)},
                "params": {"nope": 1},
                "options": {"output": str(tmp_path / "out.gds")},
            }
        )


def test_wrong_param_type_is_application_error(tmp_path):
    root, _package, library, cell = _toy_pdk(tmp_path)

    with pytest.raises(PdkPCellError, match="params.w_um must be a number"):
        generate_pdk_pcell(
            {
                "pdk_pcell": f"{library}/{cell}",
                "pdk": {"variant": "sky130A", "root": str(root)},
                "params": {"w_um": "wide"},
                "options": {"output": str(tmp_path / "out.gds")},
            }
        )


def test_bad_layer_param_is_application_error(tmp_path):
    root, _package, library, cell = _toy_pdk(tmp_path)

    with pytest.raises(PdkPCellError, match="params.layer must be a layer string"):
        generate_pdk_pcell(
            {
                "pdk_pcell": f"{library}/{cell}",
                "pdk": {"variant": "sky130A", "root": str(root)},
                "params": {"layer": 67},
                "options": {"output": str(tmp_path / "out.gds")},
            }
        )


def test_output_directory_missing_is_application_error(tmp_path):
    root, _package, library, cell = _toy_pdk(tmp_path)

    with pytest.raises(PdkPCellError, match="output directory does not exist"):
        generate_pdk_pcell(
            {
                "pdk_pcell": f"{library}/{cell}",
                "pdk": {"variant": "sky130A", "root": str(root)},
                "options": {"output": str(tmp_path / "nope" / "out.gds")},
            }
        )


def test_cli_pdk_pcell_bad_params_is_application_error(tmp_path, capsys):
    root, _package, library, cell = _toy_pdk(tmp_path)

    exit_code = main(
        [
            "gen",
            "--pdk-pcell",
            f"{library}/{cell}",
            "--params",
            "{not json",
            "--pdk",
            "sky130A",
            "--pdk-root",
            str(root),
            "--format",
            "json",
        ]
    )

    assert exit_code == 1
    err = json.loads(capsys.readouterr().err)
    assert err["error"]["command"] == "gen"


@pytest.mark.parametrize("ref", ["", "no_slash", "/Cell", "lib/"])
def test_malformed_pdk_pcell_ref_is_application_error(ref):
    with pytest.raises(PdkPCellError, match="must be '<library>/<cell>'"):
        parse_pdk_pcell_ref(ref)


# --------------------------------------------------------------------------- #
# Usage errors (exit 2) -- unchanged for the existing modes
# --------------------------------------------------------------------------- #


def test_cli_missing_generator_is_usage_error(capsys):
    assert main(["gen"]) == 2
    assert "--list-pdk-pcells" in capsys.readouterr().err


def test_cli_generator_with_pdk_pcell_is_usage_error(tmp_path, capsys):
    root, _package, library, cell = _toy_pdk(tmp_path)

    exit_code = main(
        [
            "gen",
            "resistor_strip",
            "--pdk-pcell",
            f"{library}/{cell}",
            "--pdk",
            "sky130A",
            "--pdk-root",
            str(root),
        ]
    )

    assert exit_code == 2
    assert "cannot be given too" in capsys.readouterr().err


def test_cli_two_mode_flags_is_usage_error():
    with pytest.raises(SystemExit) as excinfo:
        main(["gen", "--list", "--list-pdk-pcells"])
    assert excinfo.value.code == 2


def test_cli_list_and_pdk_pcell_is_usage_error():
    with pytest.raises(SystemExit) as excinfo:
        main(["gen", "--list", "--pdk-pcell", "lib/Cell"])
    assert excinfo.value.code == 2
