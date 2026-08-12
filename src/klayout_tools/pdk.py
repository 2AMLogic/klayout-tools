"""Discover and resolve an installed PDK.

Pure library: :func:`find_pdk` / :func:`list_pdks` return plain Python data
(``dict`` of JSON-serialisable primitives) and never print. Serialisation and
human-readable formatting live in the CLI command module (``cli/pdk_cmd.py``)
so these functions stay reusable — block repos import them instead of
re-implementing ``PDK_ROOT`` lookup in every tool and every language (the
friction this module exists to remove).

Scope: two supported layouts, both probed by the same ``libs.tech/``/
``libs.ref/`` marker (see :func:`_probe_root`) so a resolved variant's
*asset* lookup (:func:`_asset_dirs`) never needs to know which one matched —
see ``docs/cli/pdk.md`` -> "Scope" for the authoritative, user-facing list of
what does and does not resolve.

1. **open_pdks-layout installs (nested)** — the layout produced by
   open_pdks, volare, and ciel, and consumed by every sky130/gf180mcu block
   repo::

       <root>/<variant>/libs.tech/...
       <root>/<variant>/libs.ref/...

   A *variant* is an immediate subdirectory of an install *root* that
   contains a ``libs.tech/`` directory (``sky130A``, ``sky130B``,
   ``gf180mcuA``-``D``) -- a root may hold more than one. The *version
   stamp* is read from the variant's ``SOURCES`` file when present
   (open_pdks writes one); it is ``None`` otherwise -- never guessed.

2. **Flat, single-PDK installs (issue #522)** -- IHP-Open-PDK's SG13G2, whose
   own tree already ships ``libs.tech/``/``libs.ref/`` directly (verified
   against a real fetched install, ``scripts/fetch-ihp-sg13g2.sh``)::

       <root>/ihp-sg13g2/libs.tech/...      # nested form: PDK_ROOT at clone root
       <root>/ihp-sg13g2/libs.ref/...       # (matched by the nested probe above)

       <root>/libs.tech/...                 # flat form: PDK_ROOT at the PDK dir itself
       <root>/libs.ref/...                  # (no sibling variant to disambiguate)

   A single-PDK repo has no sibling variant the way a multi-process
   open_pdks store does, so IHP's own tool configs (verified against
   ``ihp-sg13g2/libs.tech/librelane/config.tcl``) and README/installer
   guidance are inconsistent about which directory ``$PDK_ROOT`` should
   name -- the clone root (nested form, already resolved by the probe
   above) or the PDK's own directory (flat form). Rather than pick one and
   leave the other unresolvable, :func:`_probe_root` tries the nested scan
   first and falls back to treating ``root_path`` itself as a single
   variant -- named after its own basename -- only when that scan finds
   nothing, so the fallback can never shadow a real multi-variant
   open_pdks root. This generalises to any future single-PDK, flat-layout
   install, not just IHP's, without adding a second resolver code path
   ("a third and fourth layout" per issue #522's own framing).

**Out of scope**: the repo-local lambdapdk store fetched by
``scripts/fetch-pdks.sh`` into ``pdks/lambdapdk/`` -- a third, distinct tree
shape (``lambdapdk/<process>/{libs,base}``, no ``libs.tech``/``libs.ref``
marker at all) that this resolver deliberately does not probe for; see
``pdks/README.md``. Its bundled ``ihp130`` process tree in particular is
**not** SG13G2 -- different process, different data -- so it is never a
substitute for a real IHP-Open-PDK install even though both names contain
"ihp" (see ``pdks/README.md`` for the explicit distinction).

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
        variant_dir = chosen["_dir"]
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
                # Public payload is name/version only -- `_probe_root`'s
                # internal `_dir` key (the resolved variant directory, needed
                # to support the flat single-PDK layout below) never leaks
                # into the documented `list_pdks` JSON schema.
                "variants": [
                    {"name": entry["name"], "version": entry["version"]}
                    for entry in variants
                ],
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
    """Return the variants under ``root_path``, trying the nested (open_pdks)
    layout first and falling back to the flat, single-PDK layout (issue
    #522) when the nested scan finds nothing.

    Each entry additionally carries a private ``_dir`` key (the variant's
    absolute directory) so :func:`find_pdk` can resolve its assets without
    re-deriving that path -- a flat-layout variant's directory is
    ``root_path`` itself, not ``root_path/<name>``, so the caller can no
    longer assume that join. ``_dir`` never leaves this module (see
    :func:`list_pdks`, which strips it before returning the public payload).

    Returns an empty list when ``root_path`` is missing or holds no variant
    of either shape. Nested results are sorted by name for deterministic
    output; the flat fallback is always exactly zero or one entry.
    """
    if not os.path.isdir(root_path):
        return []
    variants: list[dict[str, Any]] = []
    for name in sorted(os.listdir(root_path)):
        variant_dir = os.path.join(root_path, name)
        if not os.path.isdir(os.path.join(variant_dir, "libs.tech")):
            continue
        variants.append(
            {"name": name, "version": _read_version(variant_dir), "_dir": variant_dir}
        )
    if variants:
        return variants
    return _probe_flat_variant(root_path)


def _probe_flat_variant(root_path: str) -> list[dict[str, Any]]:
    """Fallback probe for a *flat* single-PDK install (issue #522) --
    IHP-Open-PDK's SG13G2 when ``--pdk-root``/``$PDK_ROOT`` names the PDK's
    own directory directly, rather than a parent that holds it as a named
    sibling the way a multi-process open_pdks store does (see the module
    docstring's "Flat, single-PDK installs" section for the real-install
    provenance).

    Only called when :func:`_probe_root`'s nested subdirectory scan finds no
    variant at all, so this can never shadow a real open_pdks-shaped root
    (an install that ships both shapes at once does not occur in practice --
    a variant subdirectory and a variant *at* the root are mutually
    exclusive placements of the same tree).

    Returns a single-entry list -- ``root_path`` treated as its own variant,
    named after its own basename (mirroring every other variant's name being
    read directly off its directory name) -- when ``root_path`` itself ships
    a ``libs.tech/`` directory, or ``[]`` otherwise.
    """
    if not os.path.isdir(os.path.join(root_path, "libs.tech")):
        return []
    name = os.path.basename(root_path.rstrip(os.sep)) or root_path
    return [{"name": name, "version": _read_version(root_path), "_dir": root_path}]


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


def drc_deck_file(variant: str | None = None, root: str | None = None) -> str | None:
    """Resolve the **filename** of the PDK's native, directly-runnable
    KLayout DRC-DSL rule-deck script inside the already-discovered
    ``assets["klayout"]`` directory's ``drc/`` subdirectory (issue #565),
    mirroring :func:`netgen_setup_file`'s shape one asset area over.

    ``find_pdk`` (and its ``_ASSET_LAYOUT`` table) only ever resolved the
    containing ``libs.tech/klayout`` directory -- the specific script a
    caller must hand to ``klayout -b -r <script> -rd input=... -rd
    report=...`` was not looked up anywhere in this repo. Resolves
    ``variant``/``root`` exactly as :func:`find_pdk` does (same precedence,
    same :class:`PdkNotFoundError` on no match).

    Naming convention (verified against a real ``volare``-fetched sky130A
    install for this issue): open_pdks stages sky130's complete, ready-to-run
    deck as a single file named after the variant,
    ``libs.tech/klayout/drc/<variant>.lydrc`` (e.g. ``sky130A.lydrc``) --
    self-contained, no assembly step, and already written to read its input/
    report paths from the ``$input``/``$report`` DRC-DSL globals this
    module's caller sets via ``-rd`` (confirmed by the script's own embedded
    usage comment: ``klayout -b -rd input=... -rd report=... -r
    drc_sky130.drc``). This function prefers that variant-named file (the
    same "prefer the specific name" precedence :func:`netgen_setup_file`
    uses) and falls back to a bare ``<variant>.drc``.

    Not every PDK's native deck fits that single-file shape: gf180mcu (also
    verified against a real fetched install) ships its native deck instead as
    ~60 topic fragments under ``drc/rule_decks/*.drc`` plus a Python
    assembly/CLI wrapper (``drc/run_drc.py``) that concatenates the right
    subset at run time -- a distinct, PDK-specific invocation contract (its
    own ``--variant``/``--table``/``--run_dir`` flags) this function
    deliberately does not attempt to drive generically, the same "avoid
    hand-assembling rule-table fragments ourselves" boundary
    ``docs/cli/drc.md``'s "Engine" section draws for the curated-deck engine.
    For a variant shaped like that, this function returns ``None`` rather
    than guessing or reimplementing that assembly -- a caller who has already
    produced a merged single-file deck (e.g. by running the PDK's own
    ``run_drc.py --macro_gen`` ahead of time) can still reach it via `klt
    drc`'s ``--deck-file`` override, which bypasses this resolver entirely.

    Returns the absolute path to the resolved script, or ``None`` when the
    variant ships no ``klayout`` asset directory at all, no ``drc/``
    subdirectory, or that directory contains neither expected filename --
    never guessed or fabricated, matching this module's existing
    ``None``-means-absent convention (see :func:`_asset_dirs`/
    :func:`netgen_setup_file`).

    Raises :class:`PdkNotFoundError` when no PDK install resolves at all
    (the same condition :func:`find_pdk` raises for).
    """
    info = find_pdk(variant=variant, root=root)
    klayout_dir = info["assets"]["klayout"]
    if klayout_dir is None:
        return None

    drc_dir = os.path.join(klayout_dir, "drc")
    if not os.path.isdir(drc_dir):
        return None

    for name in (f"{info['variant']}.lydrc", f"{info['variant']}.drc"):
        candidate = os.path.join(drc_dir, name)
        if os.path.isfile(candidate):
            return candidate

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
        f"no supported-layout PDK install providing variant '{variant}'"
        if variant is not None
        else "no supported-layout PDK install (open_pdks, or a flat single-PDK"
        " install like IHP-Open-PDK's SG13G2)"
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
#: (`*_fd_pr`, no `.lib` timing views), I/O-pad libraries (`*_fd_io`),
#: macros (`*_sram_macros`), or hard-macro IP libraries (`*_fd_ip_*`, see
#: :data:`_HARD_MACRO_LIB_MARKER`/`klt pdk macros` below -- their own sibling
#: command, not a mode of this one). This is an open_pdks-wide naming
#: convention (shared by sky130 and gf180mcu), not a sky130-specific
#: hardcoded list -- see docs/cli/pdk.md "klt pdk cells" scope note for the
#: deliberate sky130_fd_io/sky130_sram_macros/sky130_fd_ip_* exclusion this
#: implies.
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
#: (e.g. ``nfet_01v8``) separately from an optional `<family>_fd_pr__`
#: prefix, since the flavor is what encodes the voltage domain -- the family
#: repeats the library's own PDK family and adds no information. The prefix
#: is **optional** because it is a sky130-specific convention
#: (`sky130_fd_pr__nfet_01v8`) -- gf180mcu's SPICE instance lines name the
#: device model with the bare flavor and no `_fd_pr__` prefix at all
#: (`nfet_06v0`), so a prefix-required pattern silently matched nothing on
#: that PDK (issue #537).
_DEVICE_MODEL_RE = re.compile(r"(?:[a-z0-9]+_fd_pr__)?((?:n|p)fet_[a-z0-9_]+)")
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
            compatible = _supply_matches(library["supplies_v"], supply)
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
                "supplies_v": nominal["supplies_v"],
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

    ``corner`` is always returned bare (never `<library_name>__<corner>`):
    some vendors' `.lib` files (e.g. gf180mcu_fd_sc_mcu9t5v0) write their own
    `<library_name>__` prefix into `default_operating_conditions`, unlike
    sky130's files, which are already bare. When ``library_name`` is given, a
    leading `f"{library_name}__"` is stripped from the parsed corner so
    callers (``_nominal_supply``/``list_cell_libraries``) always see the bare
    form documented for `nominal_corner` (`docs/cli/pdk.md`).
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
        prefix = f"{library_name}__"
        if corner.startswith(prefix):
            corner = corner[len(prefix) :]

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


def _read_text(path: str) -> str | None:
    """Read ``path`` as UTF-8 text, or ``None`` if missing/unreadable."""
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return None


# --------------------------------------------------------------------------- #
# `klt pdk macros` -- hard-macro IP library discovery
# --------------------------------------------------------------------------- #

#: Marker substring identifying a `libs_ref` entry as an open_pdks "foundry
#: digital, IP" (hard-macro) library -- e.g. an SRAM/ROM compiler output
#: (`<family>_fd_ip_<name>`) -- as opposed to the standard-cell digital
#: libraries :data:`_STD_CELL_LIB_MARKER`/`klt pdk cells` reports. Like
#: `_STD_CELL_LIB_MARKER`, this is an open_pdks-wide naming convention
#: (shared by sky130 and gf180mcu), not a sky130-specific hardcoded list.
#: `klt pdk cells` deliberately excludes `*_fd_ip_*` entries (they are not a
#: "standard-cell digital library"); `klt pdk macros` exists specifically to
#: surface them -- see docs/cli/pdk.md "klt pdk macros".
_HARD_MACRO_LIB_MARKER = "_fd_ip_"

#: View subdirectories under a `libs_ref/<lib>` entry that
#: :func:`list_hard_macro_libraries` reports presence of, keyed by the name
#: surfaced in the JSON ``views`` object. Presence is a plain
#: ``os.path.isdir`` check (the same "does this view directory exist" probe
#: :func:`_nominal_supply` uses for `lib/`) -- this command reports what
#: views are available, it does not parse their contents the way `klt pdk
#: cells` parses `spice/`/`lib/` for device flavors and nominal supply.
_MACRO_VIEW_DIRS = {
    "gds": "gds",
    "lef": "lef",
    "lib": "lib",
    "spice": "spice",
    "cdl": "cdl",
    "verilog": "verilog",
}


def list_hard_macro_libraries(
    variant: str | None = None,
    root: str | None = None,
) -> dict[str, Any]:
    """Report the hard-macro IP libraries a variant ships, and which views
    each provides.

    Resolves one PDK install/variant exactly as :func:`find_pdk` does (same
    ``variant``/``root`` args, same :class:`PdkNotFoundError` on no match),
    then scans its ``libs_ref`` asset for hard-macro IP libraries -- entries
    whose name contains ``_fd_ip_`` (see :data:`_HARD_MACRO_LIB_MARKER`).
    This is the sibling command to `klt pdk cells`, not an alternate mode of
    it (see docs/cli/pdk.md "klt pdk macros" for why): `klt pdk cells`
    deliberately excludes hard-macro IP libraries, and this command's own
    result deliberately excludes standard-cell digital libraries, primitive-
    device libraries (`*_fd_pr`), and I/O-pad libraries (`*_fd_io`) -- none
    of those are the "hard-macro IP library" this query answers for.

    Per library, the returned dict reports ``views`` -- a bool per view kind
    (``gds``, ``lef``, ``lib``, ``spice``, ``cdl``, ``verilog``) recording
    whether that view subdirectory exists under the library entry (see
    :data:`_MACRO_VIEW_DIRS`). Unlike `klt pdk cells`, this command does not
    parse view contents -- a hard-macro IP library has no PDK-wide-consistent
    device-flavor/nominal-supply convention to extract the way a standard-
    cell library's `spice/`/`lib/` views do.

    Returns a dict matching the documented JSON schema (see
    ``docs/cli/pdk.md``)::

        {
            "schema_version": 1,
            "pdk": <variant name>,
            "root": <absolute install root>,
            "macros": [
                {
                    "name": str,
                    "views": {
                        "gds": bool,
                        "lef": bool,
                        "lib": bool,
                        "spice": bool,
                        "cdl": bool,
                        "verilog": bool,
                    },
                },
                ...
            ],
        }

    An empty ``macros`` list is a successful result (the variant ships no
    `_fd_ip_`-named library), not an error.

    Raises :class:`PdkNotFoundError` when no PDK install resolves.
    """
    info = find_pdk(variant=variant, root=root)
    libs_ref = info["assets"]["libs_ref"]
    macros = _scan_hard_macro_libraries(libs_ref) if libs_ref is not None else []

    return {
        "schema_version": 1,
        "pdk": info["variant"],
        "root": info["root"],
        "macros": macros,
    }


def _scan_hard_macro_libraries(libs_ref: str) -> list[dict[str, Any]]:
    """Enumerate `_fd_ip_`-named entries under ``libs_ref``, name-sorted."""
    if not os.path.isdir(libs_ref):
        return []
    macros: list[dict[str, Any]] = []
    for name in sorted(os.listdir(libs_ref)):
        lib_dir = os.path.join(libs_ref, name)
        if not os.path.isdir(lib_dir) or _HARD_MACRO_LIB_MARKER not in name:
            continue
        macros.append(
            {
                "name": name,
                "views": {
                    view: os.path.isdir(os.path.join(lib_dir, subdir))
                    for view, subdir in _MACRO_VIEW_DIRS.items()
                },
            }
        )
    return macros


# --------------------------------------------------------------------------- #
# `klt pdk corners` -- SPICE process-corner enumeration + completeness check
# --------------------------------------------------------------------------- #

#: Known PDK families this command understands, matched by variant-name
#: prefix (``"sky130A"`` -> ``"sky130"``, ``"gf180mcuC"`` -> ``"gf180mcu"``).
#: Deliberately duplicates the same convention as the private
#: ``_pdk_variant_family`` helper in ``pdk_models.py`` rather than importing
#: it across an unrelated module boundary -- this module resolves PDKs in
#: general, ``pdk_models`` resolves MOS device-model tables for
#: extraction/LVS specifically, and neither imports the other today. A
#: variant not matching any entry here is simply unsupported --
#: :func:`list_corners` returns an explanatory empty result (see its
#: docstring), matching this module's "empty is a valid answer" convention
#: (:func:`list_hard_macro_libraries`), never guessed.
_CORNER_PDK_FAMILIES: tuple[str, ...] = ("sky130", "gf180mcu")

#: gf180mcu's golden ngspice model deck filename, stable across variants
#: A-D (verified against a real volare install of each, 2026-08-05).
_GF180MCU_MODEL_FILENAME = "sm141064.ngspice"

#: sky130's unified corner-library filename, stable across sky130A/B
#: (verified against a real volare install of each, 2026-08-05).
_SKY130_MODEL_FILENAME = "sky130.lib.spice"

#: gf180mcu's canonical top-level MOSFET corner names -- this *is* the
#: PDK-wide corner vocabulary
#: (docs/design/pdk-device-corner-metadata-spike.md section 1.2): every
#: other device family's section name is one of these names behind a
#: family-specific prefix (see :data:`_GF180MCU_FAMILY_PREFIXES`), never a
#: name of its own. "typical" is listed first -- :func:`_scan_gf180mcu_corners`
#: relies on the typical corner being processed before any other corner, so
#: each family's skew baseline exists before it is needed.
_GF180MCU_CORNER_NAMES: tuple[str, ...] = ("typical", "ff", "ss", "fs", "sf")

#: The curated half of "own the grouping the PDK omits" from
#: docs/design/pdk-device-corner-metadata-spike.md section 3: which
#: section-name *prefix* groups one family's per-corner sections together.
#: This is convention, not data -- nothing in ``sm141064.ngspice`` states
#: that ``bjt_ss`` is "the BJT family's slow corner", let alone that it
#: belongs to the same overall "ss" corner as bare ``ss`` (MOSFET) or
#: ``mimcap_ss`` (MIM cap); see the spike's "wrap or build?" analysis. The
#: MOSFET family's prefix is the empty string because its section names
#: carry no prefix at all (``typical``/``ff``/``ss``/``fs``/``sf``, bare).
#: Family tokens match docs/design/pdk-device-corner-metadata-spike.md
#: section 2.1's proposed ``device_class`` taxonomy.
_GF180MCU_FAMILY_PREFIXES: tuple[tuple[str, str], ...] = (
    ("mos", ""),
    ("bjt", "bjt_"),
    ("diode", "diode_"),
    ("resistor", "res_"),
    ("mim_cap", "mimcap_"),
    ("mos_cap", "moscap_"),
)

#: sky130's per-corner device-family split, keyed by the leading path
#: component of the `.include` lines a `.lib <corner> ... .endl` block
#: carries (``"corners/tt.spice"`` -> family ``mos``, ``"r+c/..."`` ->
#: family ``resistor_cap``). Unlike gf180mcu's family *prefix* table above,
#: this is derived from data the shipped file itself states (the include
#: path), not a curated naming convention: sky130 groups every family into
#: a *single* `.lib <corner>` block, so there is no cross-block corner-name
#: convention to curate the way gf180mcu's ``bjt_ss``/``mimcap_ss``/...
#: grouping requires. ``resistor_cap`` (not gf180mcu's separate
#: ``resistor``/``mim_cap``) reflects what sky130's own deck actually
#: states: one shared `r+c/` include set covers both, never split further.
_SKY130_INCLUDE_FAMILIES: tuple[tuple[str, str], ...] = (
    ("mos", "corners/"),
    ("resistor_cap", "r+c/"),
)

#: sky130's typical-process corner name -- the skew baseline every other
#: corner's per-family include set is compared against (SkyWater
#: convention; ``tt`` = "typical-typical", see ``sky130.lib.spice``).
_SKY130_TYPICAL_CORNER = "tt"

_LIB_BLOCK_HEADER_RE = re.compile(r"^\s*\.lib\s+([A-Za-z0-9_]+)\s*$", re.IGNORECASE)
_LIB_BLOCK_END_RE = re.compile(r"^\s*\.endl\b", re.IGNORECASE)
_LIB_INCLUDE_TOKEN_RE = re.compile(
    r"^\s*\.lib\s+'([^']+)'\s+(\S+)\s*$", re.IGNORECASE | re.MULTILINE
)
_INCLUDE_PATH_RE = re.compile(
    r'^\s*\.include\s+"([^"]+)"', re.IGNORECASE | re.MULTILINE
)
#: Matches both a `.param` statement line and its `+`-prefixed ngspice
#: line-continuations -- gf180mcu's `bjt_*`/`diode_*`/`res_*`/`moscap_*`
#: sections write their assignments as a bare `.param` line followed by
#: several `+name=value ...` continuation lines (see :func:`_param_lines`),
#: not `.param name=value` on one line the way `mimcap_*` does.
_PARAM_LINE_RE = re.compile(r"^\s*(?:\.param\b|\+)", re.IGNORECASE)


def _param_lines(body: str) -> frozenset[str]:
    """The normalised set of `.param` statement lines (plus their `+`
    line-continuations, concatenated as separate set members) in ``body``,
    for comparing whether a section's parameter overrides genuinely differ
    from its family's typical section.

    A **set** comparison of the *whole* parameter area (not "does this
    section contain any `.param`/`+` line at all") -- both PDKs' decks carry
    `.param` lines that are unconditional boilerplate, unrelated to
    process-corner skew, in *every* section (gf180mcu's per-corner
    `rsh_*_u_m` sheet-resistance restatements that happen to equal the
    typical value at some corners; sky130's `mc_mm_switch`/`mc_pr_switch`
    Monte-Carlo toggle, which is `0` in every non-mismatch,
    non-statistical corner including `tt` itself). Presence alone would
    misclassify those as ``"param"``-skewed even when every value is
    unchanged from typical; comparing the actual line set against the
    family's typical section correctly resolves them to ``"typical"``.
    Internal whitespace is collapsed (``" ".join(line.split())``) so
    incidental spacing differences between corners don't register as a
    content difference.
    """
    return frozenset(
        " ".join(line.split())
        for line in body.splitlines()
        if _PARAM_LINE_RE.match(line)
    )


def list_corners(variant: str | None = None, root: str | None = None) -> dict[str, Any]:
    """Report the SPICE process corners a resolved PDK variant ships, and
    which device families each one actually skews (issue #538).

    Resolves one PDK install/variant exactly as :func:`find_pdk` does (same
    ``variant``/``root`` args, same :class:`PdkNotFoundError` on no match),
    then reports, for the resolved variant's golden ngspice model deck:

    - the available top-level corner names (``corner_names``),
    - per corner, an ordered ``sections: [{family, section, skew}, ...]``
      list -- the shape proposed by
      docs/design/pdk-device-corner-metadata-spike.md section 2.2, kept
      deliberately consistent with it (``family``/``section`` keys
      unchanged; ``skew`` is an additive field the spike's own "room to
      grow without breaking" rule allows), and
    - ``complete`` -- ``False`` when, for a non-typical corner, one or more
      known device families either have no section at all
      (``section: null``, ``skew: null`` -- unlisted) *or* have a section
      that resolves right back to the typical one (``skew: "typical"``) --
      both are the same silent-typical failure mode the issue reports, one
      by omission and one by an unmoved section, made explicit and
      machine-checkable instead of requiring a caller to hand-grep vendor
      model files. A family with no section falls back to whatever was
      separately bound before it (typically its typical section, if the
      caller bound one, or nothing at all otherwise); a family with an
      explicit but unmoved section is bound, just not to anything different
      from typical. For the typical corner itself every family reporting
      ``skew: "typical"`` is the *correct*, expected answer, not a gap --
      ``complete`` there only requires every family to have a section.

    Design choice -- curated family grouping + live scan, **not** a pure
    call-time parser (see docs/design/pdk-device-corner-metadata-spike.md
    section 3, "Wrap or build?"): the spike argues that *which* sections
    belong to the same named corner is PDK convention, not data a shipped
    file states, and recommends owning that grouping in a curated table
    rather than guessing it from a naming pattern at call time -- see
    :data:`_GF180MCU_FAMILY_PREFIXES` (gf180mcu) and
    :data:`_SKY130_INCLUDE_FAMILIES` (sky130, derived from the shipped
    `.include` path instead, since sky130's own file states that grouping
    directly). This function follows that recommendation for the
    *grouping*, but stops short of the spike's further "hand-curated,
    version-pinned table" for the *skew classification*: whether a given,
    curated-grouping-resolved section actually differs from the family's
    typical section (``skew: "skewed"``), differs only via `.param`
    overrides on an otherwise-identical shared model (``skew: "param"``),
    or is the typical section itself (``skew: "typical"``) is derived by
    comparing the section's parsed content against the family's typical
    section at call time, against the real installed file -- the same
    "reflect the real, installed state" argument `klt pdk cells` makes (see
    its "Design choice" note in docs/cli/pdk.md) for the parts of this
    problem a shipped file *does* state directly (per-section include
    tokens), so neither table drifts silently against an upgraded PDK.

    Granularity note: skew is classified per curated *family*
    (mos/bjt/diode/resistor/mim_cap/mos_cap for gf180mcu; mos/resistor_cap
    for sky130), not per individual device flavor within a family. A family
    reported ``"skewed"`` means *some* of its devices moved off the typical
    section for that corner -- it does not guarantee *every* device in the
    family did. For example, gf180mcu's own model deck leaves its 06v0
    MOSFET flavor at the typical-named section inside every corner while
    its 03v3 flavor does skew, both inside the single curated "mos" family
    (the issue's own worked example) -- this command correctly reports
    "mos" as ``"skewed"`` for that corner (some devices moved), but does
    not itself flag the 06v0 sub-flavor as unmoved. Resolving that finer,
    per-flavor question is the spike's own proposed ``klt pdk corner --pdk
    <variant> --corner <name>`` follow-up (section 2.2), not this command.

    Returns a dict matching the documented JSON schema (see
    ``docs/cli/pdk.md``)::

        {
            "schema_version": 1,
            "pdk": <variant name>,
            "root": <absolute install root>,
            "resolved_via": <how the corner/family grouping was obtained>,
            "model_lib": <absolute path to the golden model deck> | None,
            "corner_names": [<corner name>, ...],
            "corners": [
                {
                    "corner": str,
                    "sections": [
                        {"family": str, "section": str | None,
                         "skew": "typical" | "skewed" | "param" | None},
                        ...
                    ],
                    "family_count": int,
                    "complete": bool,
                },
                ...
            ],
        }

    An empty ``corners`` list is a successful result -- an unrecognised PDK
    family (see :data:`_CORNER_PDK_FAMILIES`), a recognised family whose
    variant ships no ``ngspice`` asset directory, or one that ships an
    ``ngspice`` directory without the expected golden model deck filename --
    not an error, matching this module's other list-style commands
    (:func:`list_cell_libraries`, :func:`list_hard_macro_libraries`).
    ``resolved_via`` always explains which of these applied.

    Raises :class:`PdkNotFoundError` when no PDK install resolves at all.
    """
    info = find_pdk(variant=variant, root=root)
    ngspice_dir = info["assets"]["ngspice"]
    model_lib, corners, resolved_via = _resolve_corners(info["variant"], ngspice_dir)

    return {
        "schema_version": 1,
        "pdk": info["variant"],
        "root": info["root"],
        "resolved_via": resolved_via,
        "model_lib": model_lib,
        "corner_names": [entry["corner"] for entry in corners],
        "corners": corners,
    }


def _resolve_corners(
    variant: str, ngspice_dir: str | None
) -> tuple[str | None, list[dict[str, Any]], str]:
    """Dispatch to the per-PDK-family corner scanner, or explain why not."""
    family = _corner_pdk_family(variant)
    if family is None:
        return (
            None,
            [],
            f"no curated corner-family grouping for PDK variant '{variant}' "
            f"(recognised families: {', '.join(_CORNER_PDK_FAMILIES)})",
        )
    if ngspice_dir is None:
        return None, [], f"variant '{variant}' ships no 'ngspice' asset directory"

    if family == "gf180mcu":
        filename, scanner = _GF180MCU_MODEL_FILENAME, _scan_gf180mcu_corners
        grouping = (
            "curated family-prefix grouping (mos/bjt/diode/resistor/mim_cap/mos_cap)"
        )
    else:
        filename, scanner = _SKY130_MODEL_FILENAME, _scan_sky130_corners
        grouping = "include-path family split (mos=corners/, resistor_cap=r+c/)"

    model_path = os.path.join(ngspice_dir, filename)
    if not os.path.isfile(model_path):
        return (
            None,
            [],
            f"expected model deck '{filename}' not found under {ngspice_dir}",
        )

    corners = scanner(model_path)
    resolved_via = f"{grouping} + live scan of {filename}"
    return model_path, corners, resolved_via


def _corner_pdk_family(variant: str) -> str | None:
    """The recognised corner-scanning family for ``variant``, or ``None``."""
    for family in _CORNER_PDK_FAMILIES:
        if variant.startswith(family):
            return family
    return None


def _parse_named_lib_blocks(text: str) -> dict[str, str]:
    """Return ``{block_name: block_body_text}`` for every top-level
    ``.lib``/``.LIB <name> ... .endl``/``.ENDL`` block in an ngspice model
    deck.

    Only a **bare-name** header (``.lib <name>``, no quoted filename) starts
    a block -- an *include* reference (``.lib '<file>' <name>``, always
    quoted-filename-first) is never mistaken for one, since
    ``[A-Za-z0-9_]+`` cannot match a leading ``'``. Blocks are flat (no
    nesting occurs in either PDK's deck), so a single linear scan suffices.
    A block whose terminator is missing (malformed input) runs to end of
    file rather than raising. Later duplicate block names overwrite earlier
    ones (never observed in a real deck; deterministic rather than
    ambiguous if it ever occurred).
    """
    blocks: dict[str, str] = {}
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        header = _LIB_BLOCK_HEADER_RE.match(lines[index])
        if header is None:
            index += 1
            continue
        name = header.group(1)
        index += 1
        body_start = index
        while index < len(lines) and not _LIB_BLOCK_END_RE.match(lines[index]):
            index += 1
        blocks[name] = "\n".join(lines[body_start:index])
        index += 1
    return blocks


def _scan_gf180mcu_corners(model_path: str) -> list[dict[str, Any]]:
    """Scan gf180mcu's golden model deck for its per-family corner sections.

    See :func:`list_corners` for the overall algorithm and its documented
    granularity limits.
    """
    text = _read_text(model_path)
    if text is None:
        return []
    blocks = _parse_named_lib_blocks(text)

    typical_tokens: dict[str, set[tuple[str, str]]] = {}
    typical_params: dict[str, frozenset[str]] = {}
    corners: list[dict[str, Any]] = []
    for corner in _GF180MCU_CORNER_NAMES:
        sections: list[dict[str, Any]] = []
        for family, prefix in _GF180MCU_FAMILY_PREFIXES:
            section_name = f"{prefix}{corner}"
            body = blocks.get(section_name)
            if body is None:
                sections.append({"family": family, "section": None, "skew": None})
                continue
            tokens = set(_LIB_INCLUDE_TOKEN_RE.findall(body))
            params = _param_lines(body)
            if corner == "typical":
                skew = "typical"
                typical_tokens[family] = tokens
                typical_params[family] = params
            else:
                baseline_tokens = typical_tokens.get(family)
                if baseline_tokens is not None and tokens != baseline_tokens:
                    skew = "skewed"
                elif params != typical_params.get(family, frozenset()):
                    skew = "param"
                else:
                    skew = "typical"
            sections.append({"family": family, "section": section_name, "skew": skew})
        corners.append(_finish_corner(corner, sections, corner == "typical"))
    return corners


def _scan_sky130_corners(model_path: str) -> list[dict[str, Any]]:
    """Scan sky130's unified corner-library deck for its per-corner,
    per-family (mos / resistor_cap) include sets.

    See :func:`list_corners` for the overall algorithm and its documented
    granularity limits.
    """
    text = _read_text(model_path)
    if text is None:
        return []
    blocks = _parse_named_lib_blocks(text)

    typical_body = blocks.get(_SKY130_TYPICAL_CORNER, "")
    typical_tokens = {
        family: _sky130_family_tokens(typical_body, path_prefix)
        for family, path_prefix in _SKY130_INCLUDE_FAMILIES
    }
    # sky130 has no per-family `.param` scoping the way gf180mcu's separate
    # per-family blocks do -- every `.param` line in a `.lib <corner>` block
    # (e.g. the `mc_mm_switch`/`mc_pr_switch` Monte-Carlo toggles) applies to
    # the whole corner, not to `mos` or `resistor_cap` individually. Compare
    # the whole block's param-line set for both families; see
    # :func:`_param_lines`.
    typical_params = _param_lines(typical_body)

    corners: list[dict[str, Any]] = []
    for corner_name, body in blocks.items():
        sections: list[dict[str, Any]] = []
        for family, path_prefix in _SKY130_INCLUDE_FAMILIES:
            tokens = _sky130_family_tokens(body, path_prefix)
            if not tokens:
                sections.append({"family": family, "section": None, "skew": None})
                continue
            if corner_name == _SKY130_TYPICAL_CORNER:
                skew = "typical"
            else:
                baseline = typical_tokens.get(family, set())
                if tokens != baseline:
                    skew = "skewed"
                elif _param_lines(body) != typical_params:
                    skew = "param"
                else:
                    skew = "typical"
            sections.append({"family": family, "section": corner_name, "skew": skew})
        corners.append(
            _finish_corner(corner_name, sections, corner_name == _SKY130_TYPICAL_CORNER)
        )
    return corners


def _sky130_family_tokens(body: str, path_prefix: str) -> set[str]:
    """`.include` paths in ``body`` starting with ``path_prefix``."""
    return {
        path for path in _INCLUDE_PATH_RE.findall(body) if path.startswith(path_prefix)
    }


def _finish_corner(
    corner: str, sections: list[dict[str, Any]], is_typical_corner: bool
) -> dict[str, Any]:
    """Assemble one ``corners[]`` entry from its already-classified sections.

    ``complete`` is the acceptance-critical field (issue #538): for the
    typical corner itself, every family reporting ``skew: "typical"`` is
    correct (that *is* the baseline), so completeness there only requires
    every family to have a section at all. For every other corner,
    ``complete`` additionally requires every family to have actually moved
    off ``"typical"`` (``"skewed"`` or ``"param"``) -- a family that is
    *present* but still resolves to ``skew: "typical"`` at a non-typical
    corner is exactly the silent-typical bug the issue reports (e.g.
    sky130's own `ss` corner leaves its `resistor_cap` family at the
    typical R/C section while skewing MOSFETs -- see
    ``tests/test_pdk.py``), not merely a family with no section at all.
    """
    if is_typical_corner:
        complete = all(section["section"] is not None for section in sections)
    else:
        complete = all(
            section["section"] is not None and section["skew"] != "typical"
            for section in sections
        )
    return {
        "corner": corner,
        "sections": sections,
        "family_count": len(sections),
        "complete": complete,
    }
