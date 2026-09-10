"""Reach a resolved PDK's **own** shipped KLayout PyCell libraries (issue #1535).

``klt gen``'s built-in generators (:mod:`klayout_tools.gen`) are klt-authored
``pya.PCellDeclarationHelper`` subclasses defined in this repo. A PDK also
ships *its own* PCell library -- the authoritative drawing of that PDK's
devices -- as an importable Python package under
``libs.tech/klayout/python/``. This module is the thin passthrough to those:
it puts the PDK's own PyCell directory on ``sys.path``, imports the vendor
package, lets the vendor's own ``pya.Library`` register itself, and then
drives KLayout's own ``Layout.add_pcell_variant`` against that library's own
declaration -- exactly the mechanism the PDK's own KLayout autoload macro uses
(verified against sky130A's ``pymacros/sky130_pcells.lym``).

**Wrap the proven engine, don't absorb it** (``docs/ARCHITECTURE.md``): klt
never re-executes, re-implements, or transcribes vendor PCell semantics. It
imports the vendor package as-is or reports, as a clean application error, why
it could not.

Phase 1 scope (issue #1535): only PyCell packages that import with no
dependency beyond the Python standard library, ``pya``/``klayout``, and
whatever klt itself already depends on. A package that needs a third-party
compat layer klt does not ship is reported as *unavailable*, naming the
missing module -- klt does not vendor or reimplement that shim. One real PDK
PyCell library available when this module was written lands in exactly that
bucket:

* ``sky130A`` -- ``libs.tech/klayout/python/cells`` is a **plain-``pya``**
  tree (``class pfet(pya.PCellDeclarationHelper)``, registered by
  ``class sky130(pya.Library)``), *not* a Cadence-DLO/``cni``-style one. It is
  nonetheless not importable here: its ``__init__`` chain reaches
  ``import gdsfactory`` (and ``kfactory``), third-party packages klt has no
  dependency on. The PDK's own autoload macro checks for exactly that import
  and disables the PCells when it is absent.

``ihp-sg13g2`` is a **different** case, and gets different handling (issue
#1630): ``sg13g2_native_pcell_lib`` is plain-``pya``, but transitively imports
``sg13g2_pycell_lib``, whose first statement is ``from cni.tech import Tech``.
That ``cni`` compat layer is *not* a third-party PyPI package klt would need
to vendor -- it ships **inside the PDK itself**, at
``libs.tech/klayout/python/pycell4klayout-api/source/python/cni``, just one
level deeper than the ``sys.path`` entry :func:`_load_libraries` adds for
every other package. :data:`_VENDOR_COMPAT_SHIMS` below names that extra
directory per affected package name (never PDK-wide, so this never touches
sky130A's unrelated ``gdsfactory`` gap) so it is added to ``sys.path`` before
the import is attempted, and separately flags a ``tkinter``-presence crash
inside that same vendor shim's ``PCellWrapper.coerce_parameters`` (a
Cadence-Tcl-callback code path that is unconditionally taken whenever
``"tkinter" in sys.modules`` -- true by default in KLayout's embedded Python,
even in headless batch mode with no widget ever created) that must be worked
around immediately before instantiating any PCell from such a package. A
vendored shim directory that turns out to be empty (an uninitialized git
submodule, as opposed to merely not on ``sys.path`` yet) still reports via
the existing empty-submodule diagnosis below, now phrased to say plainly that
it is a compat shim the PDK itself vendors -- not a third-party dependency
klt would need to add -- since the fix differs completely between the two.

Pure library: every function returns plain, JSON-serialisable Python data and
never prints -- serialisation lives in ``cli/gen_cmd.py``, per this repo's
contract-first rule (``docs/json-contract.md``).
"""

from __future__ import annotations

import importlib
import os
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ._layout import write_layout
from .gen import _PARAM_TYPE_NAMES, GenError, _coerce_param
from .pdk import (
    PdkNotFoundError,
    _pcell_lib_dir_for_assets,
    _pcell_packages_in,
    find_pdk,
    resolve_pdk_dbu,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    import klayout.db as kdb

#: Bumped only on a non-additive (breaking) change to this module's response
#: JSON shapes -- see docs/json-contract.md.
SCHEMA_VERSION = 1

#: Fallback database unit (um) when the resolved PDK's tech LEF does not
#: declare one -- the same 0.001 every built-in generator's ``_GeneratorSpec``
#: carries (see :func:`klayout_tools.gen._produce`).
_FALLBACK_DBU_UM = 0.001

#: PCell parameter types this module can accept a request value for. KLayout
#: also declares ``TypeShape`` (5), ``TypeCallback`` (7) and ``TypeNone`` (8),
#: none of which have a meaningful JSON spelling -- a vendor PCell that
#: declares one can still be instantiated, that parameter just cannot be
#: overridden from ``--params`` (it keeps the vendor's own default).
_SETTABLE_PARAM_TYPES = frozenset({0, 1, 2, 3, 4, 6})

#: ``TypeLayer``. Held as its own constant because it is the one settable type
#: :func:`klayout_tools.gen._coerce_param` does not handle (no built-in
#: generator exposes a layer parameter -- they are all in ``_HIDDEN_PARAMS``).
_TYPE_LAYER = 4


@dataclass(frozen=True)
class _VendorCompatShim:
    """A PDK-bundled compat layer one specific vendor PyCell *package* needs
    (issue #1630) -- as opposed to a genuine third-party PyPI dependency klt
    has no relationship to (sky130A's ``gdsfactory``/``kfactory``).

    Keyed by *package name* in :data:`_VENDOR_COMPAT_SHIMS`, never applied
    PDK-wide: this must only ever affect the package(s) that actually need
    it (ihp-sg13g2's ``sg13g2_pycell_lib``), never sky130A's unrelated
    ``cells`` package or any other PDK's packages.
    """

    #: Path segments, relative to the PDK's PyCell ``lib_dir``, of the
    #: directory to add to ``sys.path`` before importing the package --
    #: ihp-sg13g2 nests its ``cni`` compat layer one level deeper
    #: (``pycell4klayout-api/source/python/cni``) than the packages
    #: :func:`_load_libraries` already knows how to reach.
    sys_path_hint: tuple[str, ...]

    #: Path segments, relative to ``lib_dir``, of the shim's own top-level
    #: vendored directory (``pycell4klayout-api``) -- checked for the
    #: uninitialized-git-submodule signature :func:`_find_empty_vendor_subdir`
    #: already detects, distinct from ``package``'s own directory.
    shim_root: tuple[str, ...]

    #: The top-level module name this shim provides (``"cni"``) -- used both
    #: to recognise "this *is* the shim's own module failing to import" (as
    #: opposed to some unrelated missing dependency of the same package) and
    #: to name it in diagnostic text.
    provides_module: str

    #: Whether a ``tkinter``-presence crash is known to occur inside this
    #: shim's PCell instantiation path (verified against ihp-sg13g2's
    #: ``pycell4klayout-api``, see the module docstring) and must be worked
    #: around immediately before instantiating any PCell from the package.
    needs_tkinter_workaround: bool


#: Packages (by name, as returned by :func:`klayout_tools.pdk._pcell_packages_in`)
#: known to need a PDK-bundled compat shim beyond what :func:`_load_libraries`
#: puts on ``sys.path`` by default. See :class:`_VendorCompatShim`.
_VENDOR_COMPAT_SHIMS: dict[str, _VendorCompatShim] = {
    "sg13g2_pycell_lib": _VendorCompatShim(
        sys_path_hint=("pycell4klayout-api", "source", "python"),
        shim_root=("pycell4klayout-api",),
        provides_module="cni",
        needs_tkinter_workaround=True,
    ),
}

#: Per-process memo of ``lib_dir -> (libraries, unavailable)``. Registering a
#: ``pya.Library`` is process-global and permanent, and ``sys.modules`` caches
#: the vendor package, so the "which library names appeared" diff below is only
#: correct on the *first* load of a directory -- every later call must be
#: answered from here. See :func:`_load_libraries`.
_LOAD_MEMO: dict[str, tuple[list[dict[str, Any]], list[dict[str, Any]]]] = {}


class PdkPCellError(GenError):
    """Raised when a PDK-PCell request cannot be fulfilled.

    Subclasses :class:`~klayout_tools.gen.GenError` so ``klt gen``'s existing
    handler turns it into the same clean stderr error envelope + exit code 1
    every other ``klt gen`` application error uses (unknown generator,
    unresolvable PDK, invalid params) -- see ``docs/cli/gen.md``'s exit-code
    table. Never a traceback, including for a vendor package whose own
    top-level imports fail.
    """


def parse_pdk_pcell_ref(value: str) -> tuple[str, str]:
    """Split a ``--pdk-pcell`` value into ``(library, cell)``.

    Raises :class:`PdkPCellError` (application error, exit 1 -- same class of
    failure as naming a generator that does not exist) when ``value`` is not
    exactly ``<library>/<cell>`` with both halves non-empty.
    """
    library, separator, cell = value.partition("/")
    if not separator or not library or not cell:
        raise PdkPCellError(
            f"--pdk-pcell must be '<library>/<cell>', got '{value}' "
            "(see `klt gen --list-pdk-pcells`)"
        )
    return library, cell


def list_pdk_pcells(
    variant: str | None = None, root: str | None = None
) -> dict[str, Any]:
    """Enumerate the PCell libraries the resolved PDK ships (``klt gen
    --list-pdk-pcells``).

    Returns a dict matching the documented JSON schema (see
    ``docs/cli/gen.md``)::

        {
            "schema_version": 1,
            "pdk": {"name": str, "variant": str, "version": str | None},
            "pcell_lib_dir": str | None,
            "libraries": [
                {
                    "library": str,      # registered pya.Library name
                    "package": str,      # Python package it was imported from
                    "description": str,
                    "cells": [
                        {
                            "name": str,
                            "params": [
                                {
                                    "name": str,
                                    "type": str,
                                    "default": <JSON value>,
                                    "description": str,
                                    "hidden": bool,
                                    "settable": bool,
                                },
                                ...
                            ],
                        },
                        ...
                    ],
                },
                ...
            ],
            "unavailable": [
                {
                    "package": str,
                    "missing_dependency": str | None,
                    "reason": str,
                },
                ...
            ],
        }

    A PDK that ships no ``libs.tech/klayout/python/`` at all (a real
    gf180mcuD install) resolves ``pcell_lib_dir`` to ``None`` with both lists
    empty -- a **successful** result, not an error.

    A package that cannot be imported here (missing third-party compat layer,
    an interpreter incompatibility, a broken vendor module) lands in
    ``unavailable`` with the missing module named, rather than failing the
    whole enumeration -- one unloadable package must not hide the libraries
    that *did* load, and discovering "this PDK ships PCells but needs ``X``
    installed" is precisely what this listing exists to answer. The
    instantiation path (:func:`generate_pdk_pcell`) is the one that turns an
    unloadable package into a hard application error, since there it is
    fatal.

    Raises :class:`PdkPCellError` when no PDK install resolves at all.
    """
    pdk_info = _resolve_pdk(variant=variant, root=root)
    lib_dir = _pcell_lib_dir_for_assets(pdk_info["assets"])

    libraries: list[dict[str, Any]] = []
    unavailable: list[dict[str, Any]] = []
    if lib_dir is not None:
        loaded, unavailable = _load_libraries(lib_dir)
        libraries = [_describe_library(entry) for entry in loaded]

    return {
        "schema_version": SCHEMA_VERSION,
        "pdk": {
            "name": pdk_info["variant"],
            "variant": pdk_info["variant"],
            "version": pdk_info["version"],
        },
        "pcell_lib_dir": lib_dir,
        "libraries": libraries,
        "unavailable": unavailable,
    }


def generate_pdk_pcell(request: dict[str, Any]) -> dict[str, Any]:
    """Instantiate one PDK-shipped PCell and write it out (``klt gen
    --pdk-pcell``).

    ``request`` mirrors :func:`klayout_tools.gen.generate`'s envelope, with
    ``pdk_pcell`` (``"<library>/<cell>"``) where that one carries
    ``generator``::

        {
            "schema": "klt.gen.request/1",
            "pdk_pcell": "SG13_native_pcell_lib/Via",
            "pdk": {"variant": "ihp-sg13g2", "root": None},
            "params": {"w": 1.0},
            "options": {"cell_name": "via_0", "output": "via_0.gds"},
        }

    The response is the same shape ``generate()`` returns -- so an existing
    ``klt gen-compose`` block consumer needs no change -- plus one additive
    ``pdk_pcell`` object naming what was instantiated::

        {
            ..., as `klt gen`,
            "generator": "<library>/<cell>",
            "pdk_pcell": {"library": str, "cell": str, "package": str},
        }

    ``device_count`` is always 1 and ``ports`` always empty: a vendor PCell is
    instantiated as an **opaque** cell. klt does not interpret its geometry to
    infer devices or pins, which would be exactly the "transcribe a vendor
    artifact" step this feature exists to avoid. ``drc_hints.notes`` carries a
    line saying so.

    Raises :class:`PdkPCellError` for an unresolvable PDK, a PDK shipping no
    PyCell library, an unknown library or cell name, a vendor package that
    cannot be imported in this environment (naming the missing dependency), an
    invalid ``params`` value, or a write failure.
    """
    if not isinstance(request, dict):
        raise PdkPCellError("request must be a JSON object")

    ref = request.get("pdk_pcell")
    if not isinstance(ref, str) or not ref:
        raise PdkPCellError("request.pdk_pcell is required")
    library_name, cell_name_ref = parse_pdk_pcell_ref(ref)

    pdk_request = request.get("pdk") or {}
    if not isinstance(pdk_request, dict):
        raise PdkPCellError("request.pdk must be a JSON object")
    pdk_info = _resolve_pdk(
        variant=pdk_request.get("variant"), root=pdk_request.get("root")
    )

    raw_params = request.get("params") or {}
    if not isinstance(raw_params, dict):
        raise PdkPCellError("request.params must be a JSON object")

    options = request.get("options") or {}
    if not isinstance(options, dict):
        raise PdkPCellError("request.options must be a JSON object")
    cell_name = options.get("cell_name") or f"{cell_name_ref}_0"
    output_path = options.get("output") or f"{cell_name}.gds"

    output_dir = os.path.dirname(os.path.abspath(output_path))
    if output_dir and not os.path.isdir(output_dir):
        raise PdkPCellError(f"output directory does not exist: {output_dir}")

    entry = _resolve_library(pdk_info, library_name)
    layout, top_cell = _produce_pdk_pcell(
        entry, cell_name_ref, cell_name, raw_params, pdk_info
    )

    write_layout(layout, output_path, PdkPCellError)

    dbu = layout.dbu
    bbox = top_cell.bbox()

    return {
        "schema_version": SCHEMA_VERSION,
        "generator": f"{entry['library']}/{cell_name_ref}",
        "pdk_pcell": {
            "library": entry["library"],
            "cell": cell_name_ref,
            "package": entry["package"],
        },
        "cell_name": cell_name,
        "gds_path": output_path,
        "pdk": {
            "name": pdk_info["variant"],
            "variant": pdk_info["variant"],
            "version": pdk_info["version"],
        },
        "dbu_um": dbu,
        "bbox_um": {
            "x0": bbox.left * dbu,
            "y0": bbox.bottom * dbu,
            "x1": bbox.right * dbu,
            "y1": bbox.top * dbu,
        },
        "device_count": 1,
        "ports": [],
        "drc_hints": {
            "min_spacing_um": None,
            "matched_group_id": None,
            "snapped_to_grid": False,
            "notes": [
                "instantiated from the PDK's own PCell library -- klt does "
                "not interpret vendor geometry, so device_count is the one "
                "placed instance and ports is empty"
            ],
        },
        "warnings": [],
    }


# --------------------------------------------------------------------------- #
# PDK + vendor-package resolution
# --------------------------------------------------------------------------- #


def _resolve_pdk(variant: str | None, root: str | None) -> dict[str, Any]:
    try:
        return find_pdk(variant=variant, root=root)
    except PdkNotFoundError as exc:
        raise PdkPCellError(str(exc)) from exc


def _resolve_library(pdk_info: dict[str, Any], library_name: str) -> dict[str, Any]:
    """Return the loaded-library entry named ``library_name``.

    Raises :class:`PdkPCellError` -- the same application-error class an
    unknown built-in generator name raises -- naming what *is* available, and
    naming every package that could not be loaded (with its missing
    dependency) so the "the library you asked for is in that unloadable
    package" case is diagnosable rather than silently indistinguishable from a
    typo.
    """
    lib_dir = _pcell_lib_dir_for_assets(pdk_info["assets"])
    if lib_dir is None:
        raise PdkPCellError(
            f"PDK '{pdk_info['variant']}' ships no KLayout PCell library "
            "(no libs.tech/klayout/python/ directory)"
        )

    loaded, unavailable = _load_libraries(lib_dir)
    for entry in loaded:
        if entry["library"] == library_name:
            return entry

    if not loaded and unavailable:
        raise PdkPCellError(
            f"PDK '{pdk_info['variant']}' ships a KLayout PCell library, but "
            f"none of its packages could be loaded here: {_render(unavailable)}. "
            "klt does not vendor or reimplement a PDK's PCell compatibility "
            "layer -- install the named module(s) into this environment (see "
            "the PDK's own setup docs) and retry."
        )

    available = sorted(entry["library"] for entry in loaded)
    message = (
        f"unknown PDK PCell library '{library_name}' -- available: "
        f"{', '.join(available) if available else '(none)'} "
        "(see `klt gen --list-pdk-pcells`)"
    )
    if unavailable:
        message += f"; not loadable here: {_render(unavailable)}"
    raise PdkPCellError(message)


def _render(unavailable: list[dict[str, Any]]) -> str:
    return "; ".join(f"{item['package']} ({item['reason']})" for item in unavailable)


def _load_libraries(
    lib_dir: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Import every PyCell package under ``lib_dir`` and report what registered.

    Returns ``(loaded, unavailable)`` where ``loaded`` is a list of
    ``{"library": <registered pya.Library name>, "package": <Python package>,
    "handle": <pya.Library>}`` and ``unavailable`` is the JSON-facing
    ``{"package", "missing_dependency", "reason"}`` shape.

    A library is discovered by diffing ``pya.Library.library_names()`` across
    the import: a PDK's PyCell package normally registers itself as an import
    side effect (verified: ihp-sg13g2's ``sg13g2_native_pcell_lib`` ends with a
    bare ``SG13G2_ViaLib()`` call). A package that instead only *defines* its
    library class (verified: sky130A's ``cells`` exports ``class
    sky130(pya.Library)`` for its ``.lym`` autoload macro to instantiate) is
    handled by instantiating the exported subclass -- the vendor's own
    ``__init__``, which calls the vendor's own ``register()``. Either way klt
    never names, renames, or synthesizes a library: the name is whatever the
    vendor registered.

    Memoized per ``lib_dir`` (see :data:`_LOAD_MEMO`) because both
    ``sys.modules`` and KLayout's library registry are process-global, so the
    diff only yields anything on the first pass.
    """
    memo_key = os.path.abspath(lib_dir)
    memoized = _LOAD_MEMO.get(memo_key)
    if memoized is not None:
        return memoized

    import klayout.db as kdb

    # The PDK's own autoload macro inserts this directory permanently
    # (sky130A's sky130_pcells.lym: `sys.path.insert(0, lib_path)`), and it
    # must stay: a vendor PCell produces its geometry lazily, at
    # add_pcell_variant time, and may import a sibling module then. This is a
    # deliberate divergence from `functional_verification`'s insert/remove
    # pattern, which imports a self-contained testbench module up front.
    if memo_key not in sys.path:
        sys.path.insert(0, memo_key)

    loaded: list[dict[str, Any]] = []
    unavailable: list[dict[str, Any]] = []
    for package in _pcell_packages_in(memo_key):
        package_dir = os.path.join(memo_key, package)
        _apply_sys_path_hint(memo_key, package)
        before = set(kdb.Library.library_names())
        try:
            module = importlib.import_module(package)
        except Exception as exc:  # noqa: BLE001 - reported, never re-raised
            unavailable.append(_unavailable_entry(package, exc, package_dir, memo_key))
            continue

        new_names = set(kdb.Library.library_names()) - before
        if not new_names:
            failure = _instantiate_library_classes(module, package, kdb)
            new_names = set(kdb.Library.library_names()) - before
            if not new_names and failure is not None:
                unavailable.append(
                    _unavailable_entry(package, failure, package_dir, memo_key)
                )
                continue

        for name in sorted(new_names):
            handle = kdb.Library.library_by_name(name)
            if handle is not None:
                loaded.append({"library": name, "package": package, "handle": handle})

    loaded.sort(key=lambda entry: entry["library"])
    _LOAD_MEMO[memo_key] = (loaded, unavailable)
    return loaded, unavailable


def _apply_sys_path_hint(lib_dir: str, package: str) -> None:
    """Add ``package``'s known compat-shim directory (if any) to ``sys.path``
    before it is imported (issue #1630).

    A no-op for every package with no :data:`_VENDOR_COMPAT_SHIMS` entry --
    in particular sky130A's ``cells``, whose missing ``gdsfactory`` is a
    genuine third-party PyPI dependency with no PDK-relative directory to
    add. Scoped to exactly the named package, never applied ``lib_dir``-wide,
    so it cannot change import behaviour for any other package.

    Harmless when the hinted directory does not exist on disk (an
    uninitialized-submodule install, or a PDK layout this table's author
    never saw): :func:`_unavailable_entry` classifies that case separately,
    from :data:`_VendorCompatShim.shim_root`, once the subsequent import
    still fails.
    """
    shim = _VENDOR_COMPAT_SHIMS.get(package)
    if shim is None:
        return
    hint_path = os.path.join(lib_dir, *shim.sys_path_hint)
    if hint_path not in sys.path:
        sys.path.insert(0, hint_path)


def _instantiate_library_classes(
    module: Any, package: str, kdb: Any
) -> BaseException | None:
    """Instantiate every ``pya.Library`` subclass ``package`` itself defines.

    Only called when importing the package registered nothing by itself. The
    vendor's own ``__init__`` does the registering -- klt just constructs the
    class the vendor exported, exactly as the PDK's own autoload macro does
    (sky130A's ``sky130_pcells.lym``: ``from cells import sky130`` then
    ``sky130()``).

    Restricted to classes *defined by* this package (``__module__`` inside it)
    so a class merely re-exported from a sibling package is not registered
    twice under two different owners. Returns the first exception raised by a
    vendor constructor, or ``None``; a failure never aborts the remaining
    classes.
    """
    prefix = f"{package}."
    failure: BaseException | None = None
    for value in list(vars(module).values()):
        if not isinstance(value, type) or not issubclass(value, kdb.Library):
            continue
        if value is kdb.Library:
            continue
        origin = getattr(value, "__module__", "")
        if origin != package and not origin.startswith(prefix):
            continue
        try:
            value()
        except Exception as exc:  # noqa: BLE001 - reported, never re-raised
            failure = failure or exc
    return failure


def _unavailable_entry(
    package: str, exc: BaseException, package_dir: str, lib_dir: str
) -> dict[str, Any]:
    """Turn a vendor-package load failure into the JSON ``unavailable`` shape.

    A :class:`ModuleNotFoundError` names the module it could not find, which
    is the actionable half of the answer, so it gets a dedicated
    ``missing_dependency`` field and a message shaped around it -- but which
    message depends on *what kind* of dependency it is (issue #1630):

    1. ``package_dir`` (the package's own directory under ``lib_dir``) is
       checked first for the signature of an **uninitialized git submodule**
       (issue #1610): a directory that exists but is completely empty, or
       whose only content is an empty/near-empty ``__init__.py``-shaped
       stub -- exactly what a submodule mount point looks like when a PDK
       was installed from a release tarball that does not carry submodule
       contents. When found, the reason names that specific, actionable
       cause instead of a bare import-failure message.
    2. Otherwise, if ``package`` is listed in :data:`_VENDOR_COMPAT_SHIMS`
       and the missing module is the one that shim provides, the reason
       distinguishes "the PDK's own bundled compat shim is missing/empty at
       its expected location" (:func:`_classify_missing_compat_shim`) from
       the generic case below -- this is a compat shim the PDK itself
       vendors, not a third-party PyPI dependency klt would need to add, so
       the actionable fix is completely different.
    3. Otherwise it is a genuine third-party PyPI dependency klt has no
       relationship to (sky130A's ``gdsfactory``/``kfactory``), reported with
       the original generic message.

    Anything else keeps its own text -- never a traceback.
    """
    missing = getattr(exc, "name", None) if isinstance(exc, ImportError) else None
    empty_subdir = _find_empty_vendor_subdir(package_dir)
    if empty_subdir is not None:
        where = package if empty_subdir == "." else f"{package}/{empty_subdir}"
        reason = (
            f"vendored submodule directory '{where}' appears empty -- PDK "
            "was likely installed from a release tarball that does not "
            "include git submodule contents"
        )
        if missing:
            reason += f" (import failed looking for module '{missing}')"
    elif missing:
        shim_reason = _classify_missing_compat_shim(package, missing, lib_dir)
        reason = shim_reason or (
            f"requires Python module '{missing}', which is not importable in "
            "this environment"
        )
    else:
        reason = f"{type(exc).__name__}: {exc}"
    return {"package": package, "missing_dependency": missing, "reason": reason}


def _classify_missing_compat_shim(
    package: str, missing: str, lib_dir: str
) -> str | None:
    """Distinguish "PDK-bundled compat shim, just not populated where
    expected" from a genuine third-party PyPI dependency (issue #1630).

    Returns ``None`` -- defer to :func:`_unavailable_entry`'s generic
    missing-dependency message -- unless ``package`` has a
    :data:`_VENDOR_COMPAT_SHIMS` entry *and* ``missing`` is the module that
    specific shim provides (never for some other, genuinely third-party
    dependency the same package happens to also need).
    """
    shim = _VENDOR_COMPAT_SHIMS.get(package)
    if shim is None:
        return None
    if missing != shim.provides_module and not missing.startswith(
        f"{shim.provides_module}."
    ):
        return None

    shim_root_dir = os.path.join(lib_dir, *shim.shim_root)
    shim_root_display = "/".join(shim.shim_root)
    if not os.path.isdir(shim_root_dir) or (
        _find_empty_vendor_subdir(shim_root_dir) is not None
    ):
        return (
            f"requires the PDK's own bundled compat shim '{shim.provides_module}' "
            f"(normally vendored at '{shim_root_display}', next to '{package}'), "
            "but that directory is missing or appears empty in this install -- "
            "likely an uninitialized git submodule. This is a compat shim the "
            "PDK itself vendors, not a third-party PyPI dependency klt would "
            "need to add."
        )

    hint_display = "/".join(shim.sys_path_hint)
    return (
        f"requires Python module '{missing}' -- klt already added this PDK's "
        f"own bundled compat shim directory ('{hint_display}') to sys.path, "
        "but the module still failed to import; this looks like a bug in the "
        "vendor shim itself, not a missing third-party dependency."
    )


def _find_empty_vendor_subdir(package_dir: str) -> str | None:
    """Return the path (relative to ``package_dir``) of the first empty or
    near-empty subdirectory found under it, or ``None``.

    An uninitialized git submodule checks out as a **completely empty**
    directory -- git creates the mount point but never populates it without
    `git submodule update --init`, and a release tarball export of the
    superproject (no `.gitmodules` processing at all) leaves the same empty
    mount point behind. A directory containing nothing but a stub
    ``__init__.py`` (empty, or only blank lines/comments) is treated the
    same way, since a vendored package's own top-level ``__init__.py`` is
    sometimes real (not submoduled) while a *nested* subdirectory is the
    actual submodule -- walked depth-first so that nested case is found
    without misclassifying a real top-level ``__init__.py`` that merely
    re-exports from it.

    Never walks above ``package_dir`` itself -- only *within* the package
    directory this loader was about to import -- so this is a narrow,
    bounded probe, not a filesystem scan.
    """
    for root, dirs, files in os.walk(package_dir):
        entries = dirs + files
        if not entries or (not dirs and _only_empty_stub_files(root, files)):
            relative = os.path.relpath(root, package_dir)
            return "." if relative == "." else relative
    return None


def _only_empty_stub_files(directory: str, files: list[str]) -> bool:
    """``True`` when every file in ``files`` (under ``directory``) is empty
    or contains nothing but blank lines/``#`` comments -- the shape of a
    placeholder ``__init__.py`` left behind by an uninitialized submodule
    mount, as opposed to a real (if small) vendor module."""
    for name in files:
        try:
            with open(
                os.path.join(directory, name), encoding="utf-8", errors="ignore"
            ) as handle:
                content = handle.read()
        except OSError:  # pragma: no cover - unreadable file, be conservative
            return False
        if any(
            line.strip() and not line.strip().startswith("#")
            for line in content.splitlines()
        ):
            return False
    return True


# --------------------------------------------------------------------------- #
# Enumeration + instantiation against a loaded vendor library
# --------------------------------------------------------------------------- #


def _describe_library(entry: dict[str, Any]) -> dict[str, Any]:
    handle = entry["handle"]
    layout = handle.layout()
    cells = [
        {"name": name, "params": _describe_params(layout.pcell_declaration(name))}
        for name in sorted(layout.pcell_names())
    ]
    return {
        "library": entry["library"],
        "package": entry["package"],
        "description": handle.description or "",
        "cells": cells,
    }


def _describe_params(declaration: Any) -> list[dict[str, Any]]:
    if declaration is None:  # pragma: no cover - defensive
        return []
    return [
        {
            "name": param.name,
            "type": _PARAM_TYPE_NAMES.get(param.type, _type_name(param.type)),
            "default": _jsonable(param.default),
            "description": param.description,
            "hidden": bool(param.hidden),
            "settable": param.type in _SETTABLE_PARAM_TYPES,
        }
        for param in declaration.get_parameters()
    ]


def _type_name(ptype: int) -> str:
    """Name the PCell parameter types no built-in generator uses.

    ``_PARAM_TYPE_NAMES`` in :mod:`klayout_tools.gen` only covers the types
    klt's own generators declare; a vendor PCell may declare a layer, shape,
    or callback parameter too, and reporting ``"unknown"`` for a perfectly
    well-known ``TypeLayer`` would be misleading.
    """
    return {_TYPE_LAYER: "layer", 5: "shape", 7: "callback", 8: "none"}.get(
        ptype, "unknown"
    )


def _jsonable(value: Any) -> Any:
    """Coerce a PCell parameter default into something ``json.dumps`` accepts.

    JSON primitives pass through untouched; a ``pya`` value (e.g. a
    ``LayerInfo`` default on a ``TypeLayer`` parameter) is rendered with its
    own ``str()``, which for ``LayerInfo`` is the canonical ``"<layer>/
    <datatype>"`` spelling this module also *accepts* as a request value.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return str(value)


def _apply_tkinter_workaround(package: str) -> None:
    """Work around a ``tkinter``-presence crash inside a compat shim's PCell
    instantiation path, for a package listed in :data:`_VENDOR_COMPAT_SHIMS`
    with ``needs_tkinter_workaround=True`` (issue #1630).

    ihp-sg13g2's ``pycell4klayout-api`` shim (``sg13g2_pycell_lib``'s ``cni``
    dependency) branches its ``PCellWrapper.coerce_parameters`` into a
    Cadence-Tcl-callback path whenever ``"tkinter" in sys.modules`` -- true
    by default in KLayout's embedded Python even in pure headless batch mode
    with no widget ever created -- and that path unconditionally raises
    (``self._callBackPath`` is never set outside a real Cadence integration).
    That branch is never applicable to this open-source, headless flow, so
    removing ``tkinter`` from ``sys.modules`` immediately before
    instantiation is a reliable, narrowly-scoped workaround.

    Applied unconditionally before *every* PCell instantiation from an
    affected package -- not just specific device types -- since the crash is
    unconditional on ``tkinter``'s mere presence, not on any PCell-specific
    parameter. A no-op for every package with no matching
    :data:`_VENDOR_COMPAT_SHIMS` entry, or whose entry does not need it, so
    this can never affect sky130A or ihp-sg13g2's own
    ``sg13g2_native_pcell_lib``.
    """
    shim = _VENDOR_COMPAT_SHIMS.get(package)
    if shim is None or not shim.needs_tkinter_workaround:
        return
    sys.modules.pop("tkinter", None)


def _produce_pdk_pcell(
    entry: dict[str, Any],
    pcell_name: str,
    cell_name: str,
    raw_params: dict[str, Any],
    pdk_info: dict[str, Any],
) -> tuple[kdb.Layout, kdb.Cell]:
    """Instantiate ``entry``'s ``pcell_name`` into a fresh layout.

    Identical in shape to :func:`klayout_tools.gen._produce` -- resolve the
    declaration, ``add_pcell_variant``, insert into a fresh top cell -- except
    the declaration comes from the *vendor's* library rather than klt's own.
    The output dbu is resolved from the PDK's tech LEF the same way, so a
    ``--pdk-pcell`` block and a built-in-generator block resolved against one
    PDK agree by construction (a `klt gen-compose` precondition).
    """
    import klayout.db as kdb

    handle = entry["handle"]
    declaration = handle.layout().pcell_declaration(pcell_name)
    if declaration is None:
        available = sorted(handle.layout().pcell_names())
        raise PdkPCellError(
            f"unknown PCell '{pcell_name}' in PDK PCell library "
            f"'{entry['library']}' -- available: "
            f"{', '.join(available) if available else '(none)'} "
            "(see `klt gen --list-pdk-pcells`)"
        )

    values = _resolve_pcell_params(
        entry["library"], pcell_name, declaration, raw_params
    )

    layout = kdb.Layout()
    layout.dbu = resolve_pdk_dbu(pdk_info) or _FALLBACK_DBU_UM
    _apply_tkinter_workaround(entry["package"])
    try:
        variant = layout.add_pcell_variant(handle, declaration.id(), values)
        top = layout.create_cell(cell_name)
        top.insert(kdb.CellInstArray(variant, kdb.Trans()))
    except PdkPCellError:
        raise
    except Exception as exc:  # noqa: BLE001 - vendor code, never a traceback
        raise PdkPCellError(
            f"PDK PCell '{entry['library']}/{pcell_name}' failed while "
            f"producing geometry: {type(exc).__name__}: {exc}"
        ) from exc
    return layout, top


def _resolve_pcell_params(
    library: str,
    pcell_name: str,
    declaration: Any,
    raw_params: dict[str, Any],
) -> dict[str, Any]:
    """Type-check ``raw_params`` against the vendor declaration's own schema.

    Only the parameters the request names are returned -- every other one keeps
    the *vendor's* default, resolved by KLayout itself inside
    ``add_pcell_variant``. klt never restates a vendor default.
    """
    ref = f"{library}/{pcell_name}"
    declared = {param.name: param for param in declaration.get_parameters()}

    unknown = sorted(set(raw_params) - set(declared))
    if unknown:
        raise PdkPCellError(
            f"PDK PCell '{ref}': unknown params: {', '.join(unknown)} "
            "(see `klt gen --list-pdk-pcells`)"
        )

    values: dict[str, Any] = {}
    for name, value in raw_params.items():
        param = declared[name]
        if param.type == _TYPE_LAYER:
            values[name] = _coerce_layer_param(ref, name, value)
        elif param.type in _SETTABLE_PARAM_TYPES:
            try:
                values[name] = _coerce_param(ref, name, value, param.type)
            except GenError as exc:
                raise PdkPCellError(str(exc)) from exc
        else:
            raise PdkPCellError(
                f"PDK PCell '{ref}': params.{name} has PCell parameter type "
                f"'{_type_name(param.type)}', which has no JSON spelling -- "
                "it cannot be set from --params (the vendor's default is used)"
            )
    return values


def _coerce_layer_param(ref: str, name: str, value: Any) -> Any:
    """Coerce a request value for a ``TypeLayer`` parameter into a ``LayerInfo``.

    Accepts KLayout's own ``"<layer>/<datatype>"`` spelling (the same one
    ``LayerInfo.to_s()`` emits, so a ``--list-pdk-pcells`` default round-trips
    verbatim), a layer *name* string, or an explicit
    ``{"layer": int, "datatype": int}`` object.
    """
    import klayout.db as kdb

    if isinstance(value, str):
        try:
            return kdb.LayerInfo.from_string(value)
        except Exception as exc:  # noqa: BLE001 - reported as a param error
            raise PdkPCellError(
                f"PDK PCell '{ref}': params.{name} is not a valid layer "
                f"specification: {value!r}"
            ) from exc
    if isinstance(value, dict):
        layer = value.get("layer")
        datatype = value.get("datatype", 0)
        if isinstance(layer, int) and isinstance(datatype, int):
            return kdb.LayerInfo(layer, datatype)
    raise PdkPCellError(
        f"PDK PCell '{ref}': params.{name} must be a layer string "
        '(e.g. \'67/20\') or a {"layer": int, "datatype": int} object'
    )
