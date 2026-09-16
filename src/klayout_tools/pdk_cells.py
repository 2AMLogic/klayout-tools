"""Standard-cell library device-flavor / voltage-domain queries (``klt pdk
cells``) and Liberty-corner resolution.

Split out of :mod:`klayout_tools.pdk` (issue #1884; that module had grown to
2,378 lines aggregating four CLI-subcommand-aligned regions, already marked by
its own internal section-header comments) -- this module is exactly the
``klt pdk cells`` region: standard-cell library discovery
(:func:`list_cell_libraries`), device-flavor/nominal-supply/voltage-class
parsing off the shipped ``spice/``/``lib/`` views, and Liberty timing-corner
resolution/enumeration (:func:`resolve_liberty_for_cell_library`,
:func:`list_lib_corners`) shared by ``synthesize``, ``place_and_route``, and
``post_route_sta``. Pure relocation, no behavior change -- see those modules'
own docstrings/callers for how this is used.

PDK root/variant discovery (:func:`klayout_tools.pdk.find_pdk`) and the
:class:`klayout_tools.pdk.PdkNotFoundError` exception it raises stay in
:mod:`klayout_tools.pdk`, imported here; likewise :func:`klayout_tools.pdk._read_text`,
which :mod:`klayout_tools.pdk`'s own :func:`klayout_tools.pdk.lef_files` also
depends on and so cannot move here without leaving a back-reference the other
way.
"""

from __future__ import annotations

import math
import os
import re
from typing import Any

from .pdk import PdkNotFoundError, _read_text, find_pdk

#: Marker substrings identifying a `libs_ref` entry as a "foundry digital,
#: standard cell" library, as opposed to primitive-device libraries
#: (`*_fd_pr`, no `.lib` timing views), I/O-pad libraries (`*_fd_io`),
#: macros (`*_sram_macros`), or hard-macro IP libraries (`*_fd_ip_*`, see
#: :data:`_HARD_MACRO_LIB_MARKER`/`klt pdk macros` below -- their own sibling
#: command, not a mode of this one). A library qualifies when its name
#: contains **any** of these markers:
#:
#: - ``"_fd_sc_"`` -- the open_pdks-wide convention (`sky130_fd_sc_hd`,
#:   `sky130_fd_sc_hvl`, `gf180mcu_fd_sc_mcu9t5v0`, ...), not a
#:   sky130-specific hardcoded list -- see docs/cli/pdk.md "klt pdk cells"
#:   scope note for the deliberate sky130_fd_io/sky130_sram_macros/
#:   sky130_fd_ip_* exclusion this implies.
#: - ``"_stdcell"`` -- IHP-Open-PDK's own convention (`sg13g2_stdcell`,
#:   and (per issue #1786) `sg13cmos5l_stdcell` on IHP's other process
#:   variant), which does not use `_fd_sc_` at all (issue #1790). Verified
#:   this does not also match IHP's I/O (`sg13g2_io`) or SRAM
#:   (`sg13g2_sram`) `libs_ref` entries -- those ship their own `lib/`
#:   timing views too, so a shape-based ("ships `lib/*.lib` files") rule
#:   would have wrongly included them; a name marker is the only thing
#:   that distinguishes IHP's *standard-cell* library from its other
#:   digital `libs_ref` entries.
_STD_CELL_LIB_MARKERS = ("_fd_sc_", "_stdcell")

#: Nominal-corner selection for a library's `.lib` timing views: the
#: typical-process, room-temperature corner. A tolerance is used because the
#: `nom_process`/`nom_temperature` Liberty attributes in shipped `.lib` files
#: are not always exactly round (e.g. `24.850000000`).
_NOMINAL_PROCESS = 1.0
_NOMINAL_PROCESS_TOLERANCE = 0.01
_NOMINAL_TEMPERATURE_C = 25.0
_NOMINAL_TEMPERATURE_TOLERANCE_C = 1.0

#: Threshold separating a "core logic" nominal supply from an "I/O-class"
#: one. A documented heuristic (not a field the PDK itself declares) -- see
#: docs/cli/pdk.md.
_CORE_VOLTAGE_MAX_V = 2.5

#: Tolerance for the `--supply` compatibility verdict, so a caller-stated
#: "1.8" matches a library characterised at "1.8000000000".
_SUPPLY_MATCH_REL_TOL = 0.02
_SUPPLY_MATCH_ABS_TOL = 0.01

#: `X<n> ... <model> w=... l=...` SPICE instance lines name their device
#: model as the last token; this pattern matches that token directly rather
#: than parsing the whole instance line, and captures the "flavor" suffix
#: (e.g. ``nfet_01v8``) separately from an optional `<family>_fd_pr__`
#: prefix, since the flavor is what encodes the voltage domain -- the family
#: repeats the library's own PDK family and adds no information. The prefix
#: is **optional** because it is a sky130-specific convention
#: (`sky130_fd_pr__nfet_01v8`) -- gf180mcu's SPICE instance lines name the
#: device model with the bare flavor and no `_fd_pr__` prefix at all
#: (`nfet_06v0`), so a prefix-required pattern silently matched nothing on
#: that PDK (issue #537).
_DEVICE_MODEL_RE = re.compile(r"\b(?:[a-z0-9]+_fd_pr__)?((?:n|p)fet_[a-z0-9_]+)")

#: Distinguishes "no SPICE instance lines at all" (ok -- library genuinely
#: has no devices) from "instance lines present but none matched
#: `_DEVICE_MODEL_RE`" (unknown -- a loud signal that device-flavor parsing
#: failed for this family rather than the library shipping no devices; see
#: issue #537 acceptance criterion 4). SPICE instance lines start with `X`.
_SPICE_INSTANCE_LINE_RE = re.compile(r"^X\S+", re.MULTILINE)
_NOM_PROCESS_RE = re.compile(r"nom_process\s*:\s*([0-9.eE+-]+)")
_NOM_TEMPERATURE_RE = re.compile(r"nom_temperature\s*:\s*(-?[0-9.eE+-]+)")
_NOM_VOLTAGE_RE = re.compile(r"nom_voltage\s*:\s*([0-9.eE+-]+)")
_OPERATING_CONDITIONS_RE = re.compile(r'default_operating_conditions\s*:\s*"([^"]+)"')


def list_cell_libraries(
    variant: str | None = None,
    root: str | None = None,
    supply: float | None = None,
) -> dict[str, Any]:
    """Report the device flavor(s) and nominal supply of a variant's
    standard-cell digital libraries.

    Resolves one PDK install/variant exactly as :func:`find_pdk` does (same
    ``variant``/``root`` args, same :class:`PdkNotFoundError` on no match),
    then scans its ``libs_ref`` asset for standard-cell **digital** libraries
    -- entries whose name contains ``_fd_sc_`` or ``_stdcell`` (see
    :data:`_STD_CELL_LIB_MARKERS`). This is a deliberate, name-convention-
    based filter, not an accident of the glob used to walk `libs_ref`: it
    excludes primitive-device libraries
    (`*_fd_pr`, which ship no `.lib` timing views), I/O-pad libraries
    (`*_fd_io`), macros (`*_sram_macros`), and hard-macro IP libraries
    (`*_fd_ip_*`, see :func:`list_hard_macro_libraries`/`klt pdk macros`) --
    none of those are the "digital standard-cell library" this query answers
    for.

    Design choice (see docs/design/pdk-device-corner-metadata-spike.md and
    issue #147): **live-parses** the shipped `spice/`/`lib/` files at call
    time rather than owning a curated per-release table of device
    flavors/supplies. Unlike the primitive-device/process-corner metadata the
    spike covers (which requires synthesising cross-file knowledge no single
    shipped file states), a standard-cell library's device flavor and nominal
    supply are each stated directly, verbatim, in exactly one file the PDK
    ships (`spice/<lib>.spice`'s instance lines; the nominal `.lib` view's
    `nom_voltage` attribute) -- curating a table here would just be a stale
    copy of what the install already says, and would silently drift on a PDK
    upgrade instead of reflecting what is actually installed (the whole point
    of this being a CI-usable check via `--supply`).

    Per library, the returned dict reports:

    - ``device_flavors`` -- the sorted, deduplicated nfet/pfet device model
      suffixes its cells instantiate (e.g. ``["nfet_01v8", "pfet_01v8_hvt"]``),
      read from `spice/<lib>.spice`'s ``X<n> ... <model> w=... l=...``
      instance lines. ``[]`` when the library ships no `spice/` view, or its
      view has no matching device instance line.
    - ``device_flavors_status`` -- ``"ok"`` or ``"unknown"``, a loud signal
      distinguishing "genuinely no devices" from "device-flavor parsing
      failed". ``"unknown"`` when the library's `spice/` view has SPICE
      instance lines but none matched the device-flavor pattern (so
      ``device_flavors`` is ``[]`` but that doesn't mean the library ships no
      devices); ``"ok"`` otherwise, including when the library ships no
      `spice/` view at all or that view has no instance lines.
    - ``nominal_supply_v`` / ``nominal_corner`` -- the supply (and Liberty
      operating-condition name) its `.lib` timing views are characterised at,
      read from the nominal (typical-process, room-temperature) `.lib`
      file's `nom_voltage` attribute -- see :func:`_nominal_supply`. When a
      library is characterised at more than one supply for that corner (see
      ``supplies_v`` below), this reports the **lowest** of them, preserved
      for backward compatibility as "the library's baseline/minimum
      operating point" -- it is not the *only* supply the library is
      characterised at. Both ``None`` when the library ships no `lib/`
      directory or no parseable `.lib` file.
    - ``supplies_v`` -- the sorted, deduplicated list of **every** supply
      (volts) the library's nominal-corner `.lib` views are characterised
      at, e.g. ``[1.8]`` for a library with a single voltage-distinct view,
      or ``[1.8, 3.3, 5.0]`` for a library separately, fully characterised
      at multiple voltages (e.g. gf180mcu's `gf180mcu_fd_sc_mcu9t5v0`).
      ``nominal_supply_v`` is always ``supplies_v``'s minimum (or ``None``
      alongside an empty list). ``--supply`` matches against this full set,
      not just ``nominal_supply_v`` -- see :func:`_supply_matches`.
    - ``voltage_class`` -- ``"core"`` when ``nominal_supply_v <= 2.5``,
      ``"io"`` above that, ``None`` when ``nominal_supply_v`` is ``None``. A
      documented heuristic threshold (see :data:`_CORE_VOLTAGE_MAX_V`), not a
      field the PDK itself declares.

    When ``supply`` is given (the ``--supply`` flag, volts), each library
    additionally gets a ``"compatible"`` bool (``True`` when **any** entry in
    ``supplies_v`` is within 2%/0.01V of ``supply`` -- see
    :func:`_supply_matches`), and the returned dict gets a top-level
    ``"supply_v"`` echo plus an ``"any_compatible"`` bool the CLI uses to
    pick the CI-gate exit code (see ``docs/cli/pdk.md``).

    Returns a dict matching the documented JSON schema (see
    ``docs/cli/pdk.md``)::

        {
            "schema_version": 1,
            "pdk": <variant name>,
            "root": <absolute install root>,
            "libraries": [
                {
                    "name": str,
                    "device_flavors": [str, ...],
                    "device_flavors_status": "ok" | "unknown",
                    "nominal_supply_v": float | None,
                    "nominal_corner": str | None,
                    "supplies_v": [float, ...],
                    "voltage_class": "core" | "io" | None,
                    # "compatible": bool,   -- present only when supply= is given
                },
                ...
            ],
            # "supply_v": float,           -- present only when supply= is given
            # "any_compatible": bool,      -- present only when supply= is given
        }

    An empty ``libraries`` list is a successful result (the variant ships no
    library matching :data:`_STD_CELL_LIB_MARKERS`), not an error.

    Raises :class:`PdkNotFoundError` when no PDK install resolves.
    """
    info = find_pdk(variant=variant, root=root)
    libs_ref = info["assets"]["libs_ref"]
    libraries = _scan_cell_libraries(libs_ref) if libs_ref is not None else []

    result: dict[str, Any] = {
        "schema_version": 1,
        "pdk": info["variant"],
        "root": info["root"],
        "libraries": libraries,
    }

    if supply is not None:
        any_compatible = False
        for library in libraries:
            compatible = _supply_matches(library["supplies_v"], supply)
            library["compatible"] = compatible
            any_compatible = any_compatible or compatible
        result["supply_v"] = supply
        result["any_compatible"] = any_compatible

    return result


def resolve_liberty_for_cell_library(
    cell_library: str,
    requested_corner: str | None,
    error_cls: type[Exception],
    *,
    variant: str | None = None,
    root: str | None = None,
) -> tuple[str, str, dict[str, Any]]:
    """Resolve ``(liberty_path, corner, pdk_info)`` for ``cell_library``.

    Shared implementation behind ``synthesize._resolve_liberty``,
    ``place_and_route._resolve_liberty``, and
    ``post_route_sta._resolve_liberty`` (issue #1652) -- those three verb
    modules previously carried byte-identical copies of this resolution,
    differing only in which module-specific error class each raised. Callers
    pass their own ``error_cls`` (e.g. ``SynthesizeError``,
    ``PlaceAndRouteError``, ``PostRouteStaError``) so every failure mode
    below raises *that* type -- never :class:`PdkNotFoundError` -- keeping
    each verb module's own exception-type contract with its callers intact.

    ``variant``/``root`` (the CLI's ``--pdk``/``--pdk-root`` flags, threaded
    through from each verb's own ``run_*`` entry point) select a specific
    installed PDK variant/root exactly as :func:`find_pdk` does; ``None`` for
    either leaves that resolver's own default search order in effect.

    ``pdk_info`` is :func:`find_pdk`'s own resolution dict, returned
    unchanged for the caller to pass through to its own provenance builder.

    Liberty filename convention: tries open_pdks' double-underscore
    ``<cell_library>__<corner>.lib`` first, falling back to a single
    underscore (``<cell_library>_<corner>.lib``) only when that file does
    not exist -- IHP-Open-PDK's `sg13g2_stdcell` (and per issue #1786,
    `sg13cmos5l_stdcell`) uses the single-underscore form (issue #1790,
    verified live against a real fetched IHP-Open-PDK v0.3.0 install).
    """
    try:
        info = find_pdk(variant=variant, root=root)
    except PdkNotFoundError as exc:
        raise error_cls(str(exc)) from exc

    libs_ref = info["assets"]["libs_ref"]
    if libs_ref is None:
        raise error_cls(
            f"liberty not found for deck: resolved PDK install "
            f"'{info['variant']}' at '{info['root']}' ships no libs_ref asset"
        )

    lib_dir = os.path.join(libs_ref, cell_library)
    if not os.path.isdir(lib_dir):
        raise error_cls(
            f"liberty not found for deck: standard-cell library "
            f"'{cell_library}' not found under resolved PDK install "
            f"'{info['variant']}' at '{info['root']}'"
        )

    corner = requested_corner
    if corner is None:
        libraries = list_cell_libraries(variant=info["variant"], root=info["root"])
        entry = next(
            (lib for lib in libraries["libraries"] if lib["name"] == cell_library),
            None,
        )
        corner = entry["nominal_corner"] if entry else None
        if corner is None:
            raise error_cls(
                f"liberty not found for deck: could not determine a nominal "
                f"corner for '{cell_library}' -- pass request.pdk.corner "
                "explicitly"
            )

    liberty_path = os.path.join(lib_dir, "lib", f"{cell_library}__{corner}.lib")
    if not os.path.isfile(liberty_path):
        # Issue #1790: IHP-Open-PDK's `sg13g2_stdcell` (and per issue #1786,
        # `sg13cmos5l_stdcell`) names its liberty views with a single
        # underscore before the corner tag (`sg13g2_stdcell_typ_1p20V_25C.lib`),
        # not open_pdks' double-underscore convention
        # (`sky130_fd_sc_hd__tt_025C_1v80.lib`). Fall back to that naming
        # only when the double-underscore file does not exist, so this never
        # masks a genuinely-missing corner on an open_pdks-shaped install
        # with a false "found" from an unrelated same-named file.
        single_underscore_path = os.path.join(
            lib_dir, "lib", f"{cell_library}_{corner}.lib"
        )
        if os.path.isfile(single_underscore_path):
            liberty_path = single_underscore_path
    if not os.path.isfile(liberty_path):
        raise error_cls(
            f"liberty not found for deck: no '{corner}' corner for "
            f"'{cell_library}' under resolved PDK install '{info['variant']}' "
            f"(expected '{liberty_path}')"
        )
    return liberty_path, corner, info


def _scan_cell_libraries(libs_ref: str) -> list[dict[str, Any]]:
    """Enumerate :data:`_STD_CELL_LIB_MARKERS`-named entries under
    ``libs_ref``, name-sorted."""
    if not os.path.isdir(libs_ref):
        return []
    libraries: list[dict[str, Any]] = []
    for name in sorted(os.listdir(libs_ref)):
        lib_dir = os.path.join(libs_ref, name)
        if not os.path.isdir(lib_dir) or not any(
            marker in name for marker in _STD_CELL_LIB_MARKERS
        ):
            continue
        nominal = _nominal_supply(lib_dir)
        flavors, flavors_status = _device_flavors(name, lib_dir)
        libraries.append(
            {
                "name": name,
                "device_flavors": flavors,
                "device_flavors_status": flavors_status,
                "nominal_supply_v": nominal["voltage"],
                "nominal_corner": nominal["corner"],
                "supplies_v": nominal["supplies_v"],
                "voltage_class": _voltage_class(nominal["voltage"]),
            }
        )
    return libraries


def _device_flavors(name: str, lib_dir: str) -> tuple[list[str], str]:
    """Sorted, deduplicated nfet/pfet device flavors from `spice/<name>.spice`,
    plus a ``"ok"``/``"unknown"`` status.

    ``"unknown"`` is a loud signal that the library's `spice/` view has SPICE
    instance lines but none matched `_DEVICE_MODEL_RE` -- distinct from
    genuinely shipping no devices, which is also an empty list but reports
    ``"ok"`` (see issue #537 acceptance criterion 4).
    """
    spice_path = os.path.join(lib_dir, "spice", f"{name}.spice")
    text = _read_text(spice_path)
    if text is None:
        return [], "ok"
    flavors = sorted({match.group(1) for match in _DEVICE_MODEL_RE.finditer(text)})
    if flavors:
        return flavors, "ok"
    if _SPICE_INSTANCE_LINE_RE.search(text):
        return [], "unknown"
    return [], "ok"


def _nominal_supply(lib_dir: str) -> dict[str, Any]:
    """Return ``{"voltage": float | None, "corner": str | None, "supplies_v":
    list[float]}`` for the library's `.lib` timing views.

    Selection: parse every `<lib_dir>/lib/*.lib` file's `nom_process`/
    `nom_temperature`/`nom_voltage` Liberty attributes, then prefer files
    whose process/temperature are both typical/room-temperature (within
    tolerance) -- this is the library's "nominal corner" candidate set.
    Falls back to considering every parsed `.lib` file (any process/
    temperature) when none matches the typical/room-temperature filter, so a
    library using a different corner-naming convention still gets a
    best-effort answer instead of `None`/`[]`.

    A library may be characterised at more than one supply within that
    candidate set -- either a split/multi-rail library sharing one process/
    temperature point (e.g. sky130_fd_sc_hvl ships 2.64V/2.97V/3.3V variants
    at `tt_025C`), or a library separately, fully characterised at multiple
    voltages (e.g. gf180mcu_fd_sc_mcu9t5v0 at 1.8V/3.3V/5.0V, issue #537).
    ``supplies_v`` reports **every** distinct voltage in the candidate set,
    sorted ascending. ``voltage``/``corner`` report the **lowest** of them
    (deterministically tie-broken by filename) as the single-value "nominal"
    pick, preserved for backward compatibility: the library's baseline/
    minimum operating point, not its only characterised supply.
    """
    lib_views_dir = os.path.join(lib_dir, "lib")
    if not os.path.isdir(lib_views_dir):
        return {"voltage": None, "corner": None, "supplies_v": []}

    library_name = os.path.basename(os.path.normpath(lib_dir))
    parsed = [
        _parse_lib_corner(os.path.join(lib_views_dir, filename), library_name)
        for filename in sorted(os.listdir(lib_views_dir))
        if filename.endswith(".lib")
    ]
    parsed = [entry for entry in parsed if entry["voltage"] is not None]
    if not parsed:
        return {"voltage": None, "corner": None, "supplies_v": []}

    nominal = [
        entry
        for entry in parsed
        if entry["process"] is not None
        and math.isclose(
            entry["process"], _NOMINAL_PROCESS, abs_tol=_NOMINAL_PROCESS_TOLERANCE
        )
        and entry["temperature"] is not None
        and math.isclose(
            entry["temperature"],
            _NOMINAL_TEMPERATURE_C,
            abs_tol=_NOMINAL_TEMPERATURE_TOLERANCE_C,
        )
    ]
    candidates = nominal if nominal else parsed
    best = min(candidates, key=lambda entry: (entry["voltage"], entry["filename"]))
    supplies_v = sorted({entry["voltage"] for entry in candidates})
    return {
        "voltage": best["voltage"],
        "corner": best["corner"],
        "supplies_v": supplies_v,
    }


def _parse_lib_corner(path: str, library_name: str | None = None) -> dict[str, Any]:
    """Extract the nominal-condition fields from one `.lib` timing view.

    Returns ``voltage``/``process``/``temperature`` (``float | None``, from
    the file's `nom_*` Liberty attributes), ``corner`` (the
    `default_operating_conditions` name, or the filename stem when that
    attribute is absent), and ``filename`` (for deterministic tie-breaking).
    This is a targeted attribute scrape, not a Liberty parser.

    ``corner`` is always returned bare (never `<library_name>__<corner>` or
    `<library_name>_<corner>`): some vendors' `.lib` files (e.g.
    gf180mcu_fd_sc_mcu9t5v0) write their own `<library_name>__` prefix into
    `default_operating_conditions`, unlike sky130's files, which are already
    bare. IHP-Open-PDK's `sg13g2_stdcell` (issue #1790, verified live
    against a real fetched v0.3.0 install) writes a single-underscore
    `<library_name>_` prefix instead (its own naming convention has no
    double underscore anywhere). When ``library_name`` is given, a leading
    `f"{library_name}__"` is stripped first, falling back to a leading
    `f"{library_name}_"` only when the double-underscore form does not
    match, so callers (``_nominal_supply``/``list_cell_libraries``) always
    see the bare form documented for `nominal_corner` (`docs/cli/pdk.md`) --
    and so :func:`resolve_liberty_for_cell_library`'s own single-underscore
    liberty-filename fallback (shared by ``synthesize``/``place_and_route``/
    ``post_route_sta``'s own ``_resolve_liberty`` wrappers, issue #1652)
    receives a bare corner, not one still carrying a duplicated
    library-name prefix.
    """
    filename = os.path.basename(path)
    text = _read_text(path)
    if text is None:
        return {
            "voltage": None,
            "process": None,
            "temperature": None,
            "corner": None,
            "filename": filename,
        }

    def _first_float(pattern: re.Pattern[str]) -> float | None:
        match = pattern.search(text)
        return float(match.group(1)) if match else None

    corner_match = _OPERATING_CONDITIONS_RE.search(text)
    corner = corner_match.group(1) if corner_match else os.path.splitext(filename)[0]
    if library_name is not None:
        double_prefix = f"{library_name}__"
        single_prefix = f"{library_name}_"
        if corner.startswith(double_prefix):
            corner = corner[len(double_prefix) :]
        elif corner.startswith(single_prefix):
            corner = corner[len(single_prefix) :]

    return {
        "voltage": _first_float(_NOM_VOLTAGE_RE),
        "process": _first_float(_NOM_PROCESS_RE),
        "temperature": _first_float(_NOM_TEMPERATURE_RE),
        "corner": corner,
        "filename": filename,
    }


#: Filename-embedded marker for a `.lib` view carrying Composite Current
#: Source (CCS) *noise* models for the identical PVT operating point as its
#: non-suffixed sibling (sky130's own convention, e.g.
#: ``sky130_fd_sc_hd__ff_n40C_1v95_ccsnoise.lib`` alongside
#: ``sky130_fd_sc_hd__ff_n40C_1v95.lib``) -- not a distinct timing corner, an
#: alternate view of an already-enumerated one. Loading both into the same
#: OpenSTA session raises ``[WARNING STA-1140] ... library <name> already
#: exists`` (live-verified against a real ``openroad/orfs:latest`` container
#: over a real volare-fetched ``sky130A`` install, 2026-08-13, issue #949):
#: the file's own internal Liberty ``library (...)`` declaration name
#: collides with its non-noise sibling's, so a second ``read_liberty`` for
#: the same PVT point is a near-no-op, not a second corner.
#: :func:`list_lib_corners` excludes any ``.lib`` file whose stem ends with
#: this marker.
_CCSNOISE_LIB_SUFFIX = "_ccsnoise"


def list_lib_corners(
    cell_library: str, pdk_info: dict[str, Any]
) -> list[dict[str, str]]:
    """Enumerate every distinct ``.lib`` timing corner ``cell_library`` ships
    under the resolved PDK install ``pdk_info`` (as returned by
    :func:`find_pdk`) -- one entry per shipped ``.lib`` file, not only the
    single nominal (typical-process, room-temperature) pick
    :func:`_nominal_supply` reports (issue #949, closing the "single corner,
    always" gap ``docs/design/post-route-sta-survey.md`` section 1.2 names).

    A small, additive generalisation of the same per-file walk
    :func:`_nominal_supply`/:func:`_parse_lib_corner` already perform.
    Callers pass an already-resolved ``pdk_info`` (never re-resolves via
    :func:`find_pdk` itself) -- the same ``(cell_library, pdk_info)`` calling
    convention :func:`_resolve_lef` in ``place_and_route.py`` already uses,
    so a caller that already resolved a PDK install (e.g. via
    :func:`klayout_tools.place_and_route._resolve_liberty`) never re-walks
    the filesystem to find it again.

    Returns a list of ``{"name": str, "path": str}`` dicts, sorted by
    filename for determinism:

    - ``name`` -- a unique, Tcl-safe corner tag for OpenSTA's own
      ``define_corners``/``read_liberty -corner`` pair, derived from the
      ``.lib`` file's own **filename** (stripping the leading
      ``f"{cell_library}__"`` prefix and the ``.lib`` suffix) -- deliberately
      **never** the file's own ``default_operating_conditions`` Liberty
      attribute :func:`_parse_lib_corner` reads for ``nominal_corner``: that
      attribute is not guaranteed unique across a library's own shipped
      files (see :data:`_CCSNOISE_LIB_SUFFIX`), while every PDK's own
      filenames are unique by construction.
    - ``path`` -- the ``.lib`` file's absolute path.

    Excludes any ``.lib`` file whose stem ends with
    :data:`_CCSNOISE_LIB_SUFFIX` -- see that constant's own docstring for the
    live-verified collision this avoids.

    Returns ``[]`` when ``cell_library`` ships no ``lib/`` directory (or the
    resolved install has no ``libs_ref`` asset at all) -- not an error,
    matching :func:`_nominal_supply`'s own empty-result convention.
    """
    libs_ref = pdk_info["assets"]["libs_ref"]
    if libs_ref is None:
        return []
    lib_views_dir = os.path.join(libs_ref, cell_library, "lib")
    if not os.path.isdir(lib_views_dir):
        return []

    prefix = f"{cell_library}__"
    corners: list[dict[str, str]] = []
    for filename in sorted(os.listdir(lib_views_dir)):
        if not filename.endswith(".lib"):
            continue
        stem = filename[: -len(".lib")]
        if stem.endswith(_CCSNOISE_LIB_SUFFIX):
            continue
        name = stem[len(prefix) :] if stem.startswith(prefix) else stem
        corners.append({"name": name, "path": os.path.join(lib_views_dir, filename)})
    return corners


def _voltage_class(voltage: float | None) -> str | None:
    """``"core"``/``"io"`` classification from ``voltage`` (see
    :data:`_CORE_VOLTAGE_MAX_V`); ``None`` when ``voltage`` is ``None``."""
    if voltage is None:
        return None
    return "core" if voltage <= _CORE_VOLTAGE_MAX_V else "io"


def _supply_matches(supplies_v: list[float], supply: float) -> bool:
    """Compatibility verdict: ``True`` when **any** entry of ``supplies_v`` is
    within 2%/0.01V of ``supply`` -- matches against the library's full
    characterised-supply set (see :func:`_nominal_supply`), not only its
    single lowest ``nominal_supply_v`` pick, so a library separately
    characterised at multiple voltages (e.g. gf180mcu's 1.8V/3.3V/5.0V) is
    correctly reported compatible with any of them (issue #537)."""
    return any(
        math.isclose(
            voltage,
            supply,
            rel_tol=_SUPPLY_MATCH_REL_TOL,
            abs_tol=_SUPPLY_MATCH_ABS_TOL,
        )
        for voltage in supplies_v
    )
