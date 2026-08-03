"""Discover and resolve an installed PDK (open_pdks layout).

Pure library: :func:`find_pdk` / :func:`list_pdks` return plain Python data
(``dict`` of JSON-serialisable primitives) and never print. Serialisation and
human-readable formatting live in the CLI command module (``cli/pdk_cmd.py``)
so these functions stay reusable — block repos import them instead of
re-implementing ``PDK_ROOT`` lookup in every tool and every language (the
friction this module exists to remove).

Scope (v1): **open_pdks-layout installs** — the layout produced by open_pdks,
volare, and ciel and consumed by every block repo::

    <root>/<variant>/libs.tech/...
    <root>/<variant>/libs.ref/...

A *variant* is an immediate subdirectory of an install *root* that contains a
``libs.tech/`` directory (``sky130A``, ``sky130B``, ``gf180mcuA``–``D``). The
*version stamp* is read from the variant's ``SOURCES`` file when present
(open_pdks writes one); it is ``None`` otherwise — never guessed.

Resolution order (first hit wins; the winning step is reported as
``resolved_via`` so a wrong answer is debuggable):

1. Explicit ``root=`` argument (the ``--pdk-root`` flag).
2. ``$PDK_ROOT`` environment variable, with ``$PDK`` selecting the variant
   when set (the OpenLane-ecosystem convention). A ``$PDK_ROOT`` that does not
   resolve to an install is skipped, falling through to the steps below.
3. The ciel/volare stores: ``~/.ciel``, then ``~/.volare``.
4. Conventional install prefixes: ``/usr/local/share/pdk``,
   ``/usr/share/pdk``, ``~/share/pdk``.

See ``docs/cli/pdk.md`` for the documented CLI surface, JSON payloads, and the
frozen ``klt pdk env`` export-line shape.
"""

from __future__ import annotations

import math
import os
import re
from typing import Any

#: The ciel/volare stores, in resolution order (step 3). ``~`` is expanded at
#: call time against ``$HOME``. Exposed at module scope so tests can override
#: the search space hermetically.
STORE_DIRS: list[str] = ["~/.ciel", "~/.volare"]

#: Conventional open_pdks install prefixes, in resolution order (step 4).
CONVENTIONAL_PREFIXES: list[str] = [
    "/usr/local/share/pdk",
    "/usr/share/pdk",
    "~/share/pdk",
]

#: Tool areas exposed by an open_pdks install, mapped to their location
#: relative to a variant directory. ``libs_ref`` sits directly under the
#: variant; the rest live under ``libs.tech/``.
_ASSET_LAYOUT: dict[str, tuple[str, ...]] = {
    "ngspice": ("libs.tech", "ngspice"),
    "xschem": ("libs.tech", "xschem"),
    "klayout": ("libs.tech", "klayout"),
    "magic": ("libs.tech", "magic"),
    "netgen": ("libs.tech", "netgen"),
    "libs_ref": ("libs.ref",),
}


class PdkNotFoundError(Exception):
    """Raised when no PDK install resolves for a ``find``/``env`` request.

    Carries an actionable message that names the search order tried and points
    at a concrete way to install a PDK. The CLI turns this into a clean stderr
    error envelope + exit code 1, never a traceback.
    """


def find_pdk(variant: str | None = None, root: str | None = None) -> dict[str, Any]:
    """Resolve one PDK install/variant and return its discovery payload.

    ``variant`` (the ``--pdk`` flag) selects a variant explicitly and beats
    ``$PDK``; when it is ``None`` the ``$PDK`` environment variable is consulted
    instead. ``root`` (the ``--pdk-root`` flag) pins the install root and
    disables the environment/store/prefix search.

    Returns a dict matching the documented JSON schema (see ``docs/cli/pdk.md``)::

        {
            "schema_version": 1,
            "root": <absolute install root>,
            "variant": <variant name>,
            "version": <str | None>,
            "resolved_via": <how the install was found>,
            "assets": {
                "ngspice": <abs dir | None>, "xschem": ..., "klayout": ...,
                "magic": ..., "netgen": ..., "libs_ref": ...,
            },
        }

    Every ``assets`` key is always present; a value is the absolute directory
    when it exists on disk, or ``None`` when the install does not ship it.

    Raises :class:`PdkNotFoundError` when nothing resolves.
    """
    effective_variant = variant if variant is not None else os.environ.get("PDK")
    candidates = _candidate_roots(root)

    for root_path, resolved_via in candidates:
        variants = _probe_root(root_path)
        if not variants:
            continue
        by_name = {entry["name"]: entry for entry in variants}
        if effective_variant is not None:
            chosen = by_name.get(effective_variant)
            if chosen is None:
                continue
        else:
            chosen = variants[0]  # variants are sorted → deterministic default
        variant_dir = os.path.join(root_path, chosen["name"])
        return {
            "schema_version": 1,
            "root": root_path,
            "variant": chosen["name"],
            "version": chosen["version"],
            "resolved_via": resolved_via,
            "assets": _asset_dirs(variant_dir),
        }

    raise PdkNotFoundError(_not_found_message(candidates, effective_variant))


def list_pdks(root: str | None = None) -> dict[str, Any]:
    """Enumerate every PDK install and variant discovered across the search order.

    Returns a dict matching the documented JSON schema (see ``docs/cli/pdk.md``)::

        {
            "schema_version": 1,
            "installs": [
                {
                    "root": <absolute install root>,
                    "resolved_via": <how the install was found>,
                    "variants": [{"name": str, "version": str | None}, ...],
                },
                ...
            ],
        }

    An empty ``installs`` list is a successful result (nothing installed), not
    an error. ``root`` restricts the scan to a single install root.
    """
    installs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for root_path, resolved_via in _candidate_roots(root):
        if root_path in seen:
            continue
        variants = _probe_root(root_path)
        if not variants:
            continue
        seen.add(root_path)
        installs.append(
            {
                "root": root_path,
                "resolved_via": resolved_via,
                "variants": variants,
            }
        )

    return {"schema_version": 1, "installs": installs}


def _candidate_roots(root: str | None) -> list[tuple[str, str]]:
    """Build the ordered ``(absolute_root, resolved_via)`` search candidates.

    When ``root`` is given it is the sole candidate (search disabled).
    Otherwise the order is: ``$PDK_ROOT`` (if set), the ciel/volare stores,
    then the conventional prefixes. ``resolved_via`` uses the unexpanded
    ``~``-form for stable, human-readable labels.
    """
    if root is not None:
        return [(_abspath(root), "--pdk-root flag")]

    candidates: list[tuple[str, str]] = []
    pdk_root = os.environ.get("PDK_ROOT")
    if pdk_root:
        candidates.append((_abspath(pdk_root), "PDK_ROOT environment variable"))
    for store in STORE_DIRS:
        candidates.append((_abspath(store), f"search root: {store}"))
    for prefix in CONVENTIONAL_PREFIXES:
        candidates.append((_abspath(prefix), f"search root: {prefix}"))
    return candidates


def _abspath(path: str) -> str:
    return os.path.abspath(os.path.expanduser(path))


def _probe_root(root_path: str) -> list[dict[str, Any]]:
    """Return the variants under ``root_path`` (open_pdks layout probe).

    A variant is an immediate subdirectory containing a ``libs.tech/``
    directory. Returns an empty list when ``root_path`` is missing or holds no
    variants. Results are sorted by name for deterministic output.
    """
    if not os.path.isdir(root_path):
        return []
    variants: list[dict[str, Any]] = []
    for name in sorted(os.listdir(root_path)):
        variant_dir = os.path.join(root_path, name)
        if not os.path.isdir(os.path.join(variant_dir, "libs.tech")):
            continue
        variants.append({"name": name, "version": _read_version(variant_dir)})
    return variants


def _read_version(variant_dir: str) -> str | None:
    """Read the version stamp from the variant's ``SOURCES`` file, or ``None``.

    open_pdks writes a ``SOURCES`` file recording the upstream commits the
    install was built from. Its non-empty lines are whitespace-normalised and
    joined with ``"; "`` into a single stamp. Absent/unreadable/empty → ``None``
    (never guessed).
    """
    sources = os.path.join(variant_dir, "SOURCES")
    if not os.path.isfile(sources):
        return None
    try:
        with open(sources, encoding="utf-8") as handle:
            lines = [line.strip() for line in handle if line.strip()]
    except OSError:
        return None
    return "; ".join(lines) if lines else None


def _asset_dirs(variant_dir: str) -> dict[str, str | None]:
    """Map each tool area to its absolute directory, or ``None`` if absent."""
    assets: dict[str, str | None] = {}
    for key, parts in _ASSET_LAYOUT.items():
        path = os.path.join(variant_dir, *parts)
        assets[key] = path if os.path.isdir(path) else None
    return assets


def netgen_setup_file(
    variant: str | None = None, root: str | None = None
) -> str | None:
    """Resolve the **filename** of the PDK's netgen LVS setup script inside
    the already-discovered ``assets["netgen"]`` directory (issue #343).

    ``find_pdk`` (and its ``_ASSET_LAYOUT`` table) only ever resolved the
    containing directory (``libs.tech/netgen/``); the specific file a caller
    must hand to ``netgen -batch lvs ... <setup.tcl> ...`` was not looked up
    anywhere in this repo. Resolves ``variant``/``root`` exactly as
    :func:`find_pdk` does (same precedence, same :class:`PdkNotFoundError`
    on no match).

    Naming convention (verified against the open_pdks source tree for both
    families this repo targets, ``RTimothyEdwards/open_pdks``,
    ``sky130/Makefile.in`` and ``gf180mcu/Makefile.in``'s ``netgen-%`` install
    rule): open_pdks stages the setup script as ``<variant>_setup.tcl``
    (e.g. ``sky130A_setup.tcl``, ``gf180mcuC_setup.tcl``) and additionally
    symlinks a generic ``setup.tcl`` alongside it in the same directory. This
    function prefers the variant-named file (unambiguous even if a caller
    copies the directory contents elsewhere and the symlink does not survive
    the copy) and falls back to the generic ``setup.tcl`` name.

    Returns the absolute path to the setup file, or ``None`` when the variant
    ships no ``netgen`` asset directory at all, or that directory exists but
    contains neither expected filename (e.g. a from-source netgen checkout
    laid out by hand, or a partial/custom install) -- never guessed or
    fabricated, matching this module's existing ``None``-means-absent
    convention (see :func:`_asset_dirs`).

    Raises :class:`PdkNotFoundError` when no PDK install resolves at all
    (the same condition :func:`find_pdk` raises for).
    """
    info = find_pdk(variant=variant, root=root)
    netgen_dir = info["assets"]["netgen"]
    if netgen_dir is None:
        return None

    variant_named = os.path.join(netgen_dir, f"{info['variant']}_setup.tcl")
    if os.path.isfile(variant_named):
        return variant_named

    generic = os.path.join(netgen_dir, "setup.tcl")
    if os.path.isfile(generic):
        return generic

    return None


#: Tech-LEF corner suffixes open_pdks ships alongside a standard-cell
#: library's merged macro LEF (issue #397 / #425 -- the OpenROAD survey's own
#: finding that ``_ASSET_LAYOUT`` has no ``lef`` key at all). Unlike
#: liberty's process/temperature/voltage corners, these name a *parasitic*
#: corner (min/nom/max routing-layer resistance/capacitance), so
#: ``"nom"`` -- the nominal, typical-parasitic tech LEF -- is the sensible
#: default for a P&R run's floorplan/routing-layer stack, independent of
#: whichever liberty corner the same run resolves for timing.
_NOMINAL_TECH_LEF_CORNER = "nom"


def lef_files(
    cell_library: str,
    variant: str | None = None,
    root: str | None = None,
    corner: str = _NOMINAL_TECH_LEF_CORNER,
) -> dict[str, str | None]:
    """Resolve the tech + merged-cell LEF pair for ``cell_library`` (issue
    #397 / #425's LEF resolver, alongside :func:`find_pdk`'s existing
    liberty-adjacent resolution -- ``_ASSET_LAYOUT`` never carried a ``lef``
    key since a LEF pair is per-``libs_ref``-library, not a single
    variant-wide directory the way ``libs.tech/klayout``/``libs.tech/magic``
    are).

    Resolves ``variant``/``root`` exactly as :func:`find_pdk` does (same
    precedence, same :class:`PdkNotFoundError` on no match).

    Layout convention (verified live against a real ``volare``-fetched
    ``sky130A`` install, issue #425's own worked example -- see
    ``docs/design/openroad-invocation-survey.md`` section 5): open_pdks
    stages a per-library **tech LEF** at
    ``<libs_ref>/<cell_library>/techlef/<cell_library>__<corner>.tlef``
    (``corner`` one of ``min``/``nom``/``max`` -- a parasitic-extraction
    corner, not a liberty corner) and a **merged macro/cell LEF** (every
    standard cell's pins/obstructions/outline, already merged -- no
    per-cell LEF files to concatenate) at
    ``<libs_ref>/<cell_library>/lef/<cell_library>.lef``.

    Returns::

        {"tech_lef": <abs path | None>, "cell_lef": <abs path | None>}

    Each value is ``None`` when the resolved install ships no ``libs_ref``
    asset at all, no ``cell_library`` entry, or that entry's ``techlef``/
    ``lef`` subdirectory does not contain the expected file -- never guessed
    or fabricated, matching this module's existing ``None``-means-absent
    convention (see :func:`_asset_dirs`/:func:`netgen_setup_file`).

    Raises :class:`PdkNotFoundError` when no PDK install resolves at all
    (the same condition :func:`find_pdk` raises for).
    """
    info = find_pdk(variant=variant, root=root)
    libs_ref = info["assets"]["libs_ref"]
    if libs_ref is None:
        return {"tech_lef": None, "cell_lef": None}

    lib_dir = os.path.join(libs_ref, cell_library)

    tech_lef = os.path.join(lib_dir, "techlef", f"{cell_library}__{corner}.tlef")
    cell_lef = os.path.join(lib_dir, "lef", f"{cell_library}.lef")

    return {
        "tech_lef": tech_lef if os.path.isfile(tech_lef) else None,
        "cell_lef": cell_lef if os.path.isfile(cell_lef) else None,
    }


def _not_found_message(candidates: list[tuple[str, str]], variant: str | None) -> str:
    """Build the actionable ``PdkNotFoundError`` message."""
    tried = ", ".join(f"{via} ({path})" for path, via in candidates)
    subject = (
        f"no open_pdks-layout PDK install providing variant '{variant}'"
        if variant is not None
        else "no open_pdks-layout PDK install"
    )
    return (
        f"{subject} was found. Searched, in order: {tried}. "
        "Point $PDK_ROOT (or --pdk-root) at an install, or install one, e.g. "
        "`ciel enable --pdk-family sky130 <version>` "
        "(or build open_pdks with `make install`)."
    )


# --------------------------------------------------------------------------- #
# `klt pdk cells` -- standard-cell library device flavor / voltage domain
# --------------------------------------------------------------------------- #

#: Marker substring identifying a `libs_ref` entry as an open_pdks "foundry
#: digital, standard cell" library (`sky130_fd_sc_hd`, `sky130_fd_sc_hvl`,
#: `gf180mcu_fd_sc_mcu9t5v0`, ...), as opposed to primitive-device libraries
#: (`*_fd_pr`, no `.lib` timing views), I/O-pad libraries (`*_fd_io`), or
#: macros (`*_sram_macros`). This is an open_pdks-wide naming convention
#: (shared by sky130 and gf180mcu), not a sky130-specific hardcoded list --
#: see docs/cli/pdk.md "klt pdk cells" scope note for the deliberate
#: sky130_fd_io/sky130_sram_macros exclusion this implies.
_STD_CELL_LIB_MARKER = "_fd_sc_"

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
#: (e.g. ``nfet_01v8``) separately from the `<family>_fd_pr__` prefix, since
#: the flavor is what encodes the voltage domain -- the family repeats the
#: library's own PDK family and adds no information.
_DEVICE_MODEL_RE = re.compile(r"[a-z0-9]+_fd_pr__((?:n|p)fet_[a-z0-9_]+)")
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
    -- entries whose name contains ``_fd_sc_`` (see :data:`_STD_CELL_LIB_MARKER`).
    This is a deliberate, name-convention-based filter, not an accident of the
    glob used to walk `libs_ref`: it excludes primitive-device libraries
    (`*_fd_pr`, which ship no `.lib` timing views), I/O-pad libraries
    (`*_fd_io`), and macros (`*_sram_macros`) -- none of those are the
    "digital standard-cell library" this query answers for.

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
    - ``nominal_supply_v`` / ``nominal_corner`` -- the supply (and Liberty
      operating-condition name) its `.lib` timing views are characterised at,
      read from the nominal (typical-process, room-temperature) `.lib`
      file's `nom_voltage` attribute -- see :func:`_nominal_supply`. Both
      ``None`` when the library ships no `lib/` directory or no parseable
      `.lib` file.
    - ``voltage_class`` -- ``"core"`` when ``nominal_supply_v <= 2.5``,
      ``"io"`` above that, ``None`` when ``nominal_supply_v`` is ``None``. A
      documented heuristic threshold (see :data:`_CORE_VOLTAGE_MAX_V`), not a
      field the PDK itself declares.

    When ``supply`` is given (the ``--supply`` flag, volts), each library
    additionally gets a ``"compatible"`` bool (``nominal_supply_v`` within
    2%/0.01V of ``supply`` -- see :func:`_supply_matches`), and the returned
    dict gets a top-level ``"supply_v"`` echo plus an ``"any_compatible"``
    bool the CLI uses to pick the CI-gate exit code (see ``docs/cli/pdk.md``).

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
                    "nominal_supply_v": float | None,
                    "nominal_corner": str | None,
                    "voltage_class": "core" | "io" | None,
                    # "compatible": bool,   -- present only when supply= is given
                },
                ...
            ],
            # "supply_v": float,           -- present only when supply= is given
            # "any_compatible": bool,      -- present only when supply= is given
        }

    An empty ``libraries`` list is a successful result (the variant ships no
    `_fd_sc_`-named library), not an error.

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
            compatible = _supply_matches(library["nominal_supply_v"], supply)
            library["compatible"] = compatible
            any_compatible = any_compatible or compatible
        result["supply_v"] = supply
        result["any_compatible"] = any_compatible

    return result


def _scan_cell_libraries(libs_ref: str) -> list[dict[str, Any]]:
    """Enumerate `_fd_sc_`-named entries under ``libs_ref``, name-sorted."""
    if not os.path.isdir(libs_ref):
        return []
    libraries: list[dict[str, Any]] = []
    for name in sorted(os.listdir(libs_ref)):
        lib_dir = os.path.join(libs_ref, name)
        if not os.path.isdir(lib_dir) or _STD_CELL_LIB_MARKER not in name:
            continue
        nominal = _nominal_supply(lib_dir)
        libraries.append(
            {
                "name": name,
                "device_flavors": _device_flavors(name, lib_dir),
                "nominal_supply_v": nominal["voltage"],
                "nominal_corner": nominal["corner"],
                "voltage_class": _voltage_class(nominal["voltage"]),
            }
        )
    return libraries


def _device_flavors(name: str, lib_dir: str) -> list[str]:
    """Sorted, deduplicated nfet/pfet device flavors from `spice/<name>.spice`."""
    spice_path = os.path.join(lib_dir, "spice", f"{name}.spice")
    text = _read_text(spice_path)
    if text is None:
        return []
    return sorted({match.group(1) for match in _DEVICE_MODEL_RE.finditer(text)})


def _nominal_supply(lib_dir: str) -> dict[str, Any]:
    """Return ``{"voltage": float | None, "corner": str | None}`` for the
    library's nominal (typical-process, room-temperature) `.lib` timing view.

    Selection: parse every `<lib_dir>/lib/*.lib` file's `nom_process`/
    `nom_temperature`/`nom_voltage` Liberty attributes, then prefer files
    whose process/temperature are both typical/room-temperature (within
    tolerance). A library characterised at more than one supply for that
    corner -- a split/multi-rail library, e.g. sky130_fd_sc_hvl ships
    2.64V/2.97V/3.3V variants at `tt_025C` -- reports the **lowest** voltage
    among them (deterministically tie-broken by filename): the library's
    baseline/minimum operating point. Falls back to considering every parsed
    `.lib` file (any process/temperature) when none matches the typical/room-
    temperature filter, so a library using a different corner-naming
    convention still gets a best-effort answer instead of `None`.
    """
    lib_views_dir = os.path.join(lib_dir, "lib")
    if not os.path.isdir(lib_views_dir):
        return {"voltage": None, "corner": None}

    parsed = [
        _parse_lib_corner(os.path.join(lib_views_dir, filename))
        for filename in sorted(os.listdir(lib_views_dir))
        if filename.endswith(".lib")
    ]
    parsed = [entry for entry in parsed if entry["voltage"] is not None]
    if not parsed:
        return {"voltage": None, "corner": None}

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
    return {"voltage": best["voltage"], "corner": best["corner"]}


def _parse_lib_corner(path: str) -> dict[str, Any]:
    """Extract the nominal-condition fields from one `.lib` timing view.

    Returns ``voltage``/``process``/``temperature`` (``float | None``, from
    the file's `nom_*` Liberty attributes), ``corner`` (the
    `default_operating_conditions` name, or the filename stem when that
    attribute is absent), and ``filename`` (for deterministic tie-breaking).
    This is a targeted attribute scrape, not a Liberty parser.
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

    return {
        "voltage": _first_float(_NOM_VOLTAGE_RE),
        "process": _first_float(_NOM_PROCESS_RE),
        "temperature": _first_float(_NOM_TEMPERATURE_RE),
        "corner": corner,
        "filename": filename,
    }


def _voltage_class(voltage: float | None) -> str | None:
    """``"core"``/``"io"`` classification from ``voltage`` (see
    :data:`_CORE_VOLTAGE_MAX_V`); ``None`` when ``voltage`` is ``None``."""
    if voltage is None:
        return None
    return "core" if voltage <= _CORE_VOLTAGE_MAX_V else "io"


def _supply_matches(nominal_v: float | None, supply: float) -> bool:
    """Compatibility verdict: ``nominal_v`` within 2%/0.01V of ``supply``."""
    if nominal_v is None:
        return False
    return math.isclose(
        nominal_v,
        supply,
        rel_tol=_SUPPLY_MATCH_REL_TOL,
        abs_tol=_SUPPLY_MATCH_ABS_TOL,
    )


def _read_text(path: str) -> str | None:
    """Read ``path`` as UTF-8 text, or ``None`` if missing/unreadable."""
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return None
