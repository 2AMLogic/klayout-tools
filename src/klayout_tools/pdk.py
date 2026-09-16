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

from .lef_header import parse_lef_header

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

#: Subdirectory of ``assets["klayout"]`` a PDK stages its own importable
#: KLayout PyCell (Python PCell) library packages under -- see
#: :func:`pdk_pcell_lib_dir`. Verified against three real installs for issue
#: #1535: a ``volare``-fetched ``sky130A`` (``libs.tech/klayout/python/cells``
#: -- the Mabrains-authored ``sky130`` PCell library the PDK's own
#: ``pymacros/sky130_pcells.lym`` autoloads by putting exactly this directory
#: on ``sys.path``), a fetched ``ihp-sg13g2``
#: (``libs.tech/klayout/python/{sg13g2_native_pcell_lib,sg13g2_pycell_lib,...}``),
#: and a ``volare``-fetched ``gf180mcuD`` (ships **no** ``python/`` at all).
#: Unlike ``drc/``/``lvs/`` (see :func:`drc_deck_file`), no verified install
#: nests this one under ``klayout/tech/``, so no nested fallback is probed.
_PCELL_LIB_SUBDIR = "python"


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
            "broken_symlinks": [{"asset": <asset key>, "path": <abs path>}, ...],
            "has_pcell_library": <bool>,
        }

    Every ``assets`` key is always present; a value is the absolute directory
    when it exists on disk, or ``None`` when the install does not ship it.

    ``has_pcell_library`` (issue #1535) is ``True`` when the resolved variant
    ships at least one importable KLayout PyCell library package under
    ``assets["klayout"]/python/`` (see :func:`pdk_pcell_libraries`), so a
    caller can discover PyCell availability without invoking ``klt gen
    --list-pdk-pcells``. It reports only that a *package* is present, never
    that it is loadable in this environment -- a PDK's PyCell package
    routinely imports third-party modules klt does not depend on (verified:
    sky130A's ``cells`` needs ``gdsfactory``; ihp-sg13g2's needs ``cni``).
    Additive field; see ``docs/json-contract.md``.

    ``broken_symlinks`` (issue #1406) reports every **dangling** symlink
    (:func:`_find_broken_symlinks`) found under any resolved ``assets``
    directory -- e.g. a standalone ``ihp-sg13cmos5l`` install, whose
    ``libs.tech/xschem/sg13cmos5l_pr/*.sym`` device symbols are relative
    symlinks into a sibling ``ihp-sg13g2`` checkout the install does not
    contain, so `xschem` cannot open them at all. ``[]`` when every resolved
    asset directory is clean -- the common, healthy-install case -- so a
    caller can gate on ``bool(report["broken_symlinks"])`` without special-
    casing the empty result. Additive field; see ``docs/json-contract.md``.

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
        assets = _asset_dirs(variant_dir)
        return {
            "schema_version": 1,
            "root": root_path,
            "variant": chosen["name"],
            "version": chosen["version"],
            "resolved_via": resolved_via,
            "assets": assets,
            "broken_symlinks": _broken_symlinks_in_assets(assets),
            "has_pcell_library": bool(_pcell_packages_for_assets(assets)),
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
                    "variants": [
                        {
                            "name": str,
                            "version": str | None,
                            "has_pcell_library": bool,
                        },
                        ...
                    ],
                },
                ...
            ],
        }

    An empty ``installs`` list is a successful result (nothing installed), not
    an error. ``root`` restricts the scan to a single install root.

    ``has_pcell_library`` (issue #1535) carries the same meaning as
    :func:`find_pdk`'s own field of that name -- the variant ships at least
    one importable PyCell library *package* under
    ``libs.tech/klayout/python/``, said nothing about whether it loads in this
    environment. Additive field; see ``docs/json-contract.md``.
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
                    {
                        "name": entry["name"],
                        "version": entry["version"],
                        "has_pcell_library": bool(
                            _pcell_packages_for_assets(_asset_dirs(entry["_dir"]))
                        ),
                    }
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


def _find_broken_symlinks(directory: str) -> list[str]:
    """Return every **dangling** symlink under ``directory``, absolute-path
    sorted (issue #1406).

    A path is dangling when :func:`os.path.islink` is ``True`` but
    :func:`os.path.exists` -- which follows the link -- is ``False``. This is
    exactly the condition a standalone ``ihp-sg13cmos5l`` install hits:
    ``libs.tech/xschem/sg13cmos5l_pr/sg13_hv_pmos.sym`` (and most of its
    sibling device symbols) is a *relative* symlink into a sibling
    ``ihp-sg13g2`` checkout the install does not contain, so it never
    resolves at all.

    Deliberately does **not** flag a relative symlink that *does* resolve --
    that is the normal, intentional pattern this repo's own resolver already
    relies on (open_pdks' variant-named ``netgen`` setup script symlinked
    alongside a generic ``setup.tcl``, see :func:`netgen_setup_file`) -- only
    genuinely unresolvable links are a problem.

    Walks with ``followlinks=False``: :func:`os.walk` never descends into a
    symlinked subdirectory either way, so a dangling *directory* symlink is
    reported once, as itself (via its parent's ``dirnames``), rather than
    raising trying to list a target that does not exist.
    """
    broken: list[str] = []
    for dirpath, dirnames, filenames in os.walk(directory, followlinks=False):
        for name in dirnames + filenames:
            path = os.path.join(dirpath, name)
            if os.path.islink(path) and not os.path.exists(path):
                broken.append(path)
    return sorted(broken)


def _broken_symlinks_in_assets(assets: dict[str, str | None]) -> list[dict[str, str]]:
    """Collect every dangling symlink (:func:`_find_broken_symlinks`) across
    every resolved (non-``None``) entry of an ``assets`` map, tagged with
    which asset area it was found under.

    Returns ``[{"asset": <asset key>, "path": <absolute path>}, ...]``, sorted
    by ``(asset, path)`` -- ``[]`` when every resolved asset directory is
    clean (the common, healthy-install case).
    """
    found: list[dict[str, str]] = []
    for key in sorted(assets):
        directory = assets[key]
        if directory is None:
            continue
        for path in _find_broken_symlinks(directory):
            found.append({"asset": key, "path": path})
    return found


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

    A single-PDK flat install (:func:`_probe_flat_variant`, e.g. IHP-Open-
    PDK's SG13G2/SG13CMOS5L) nests its ``drc/`` one directory deeper, at
    ``libs.tech/klayout/tech/drc/`` rather than ``libs.tech/klayout/drc/``
    directly (verified against a real fetched ``ihp-sg13g2``/
    ``ihp-sg13cmos5l`` install, issue #1399) -- both still ship a single
    ready-to-run, variant-named file at that nested location (e.g.
    ``ihp-sg13cmos5l.drc``). This function tries the open_pdks-shaped
    ``drc/`` first and falls back to the nested ``tech/drc/`` only when the
    former does not exist, so a real open_pdks install (which always has the
    former) never pays for the extra probe and a flat IHP-shaped install
    resolves without a PDK-name special case.

    Returns the absolute path to the resolved script, or ``None`` when the
    variant ships no ``klayout`` asset directory at all, neither the ``drc/``
    nor the nested ``tech/drc/`` subdirectory, or the resolved directory
    contains neither expected filename -- never guessed or fabricated,
    matching this module's existing ``None``-means-absent convention (see
    :func:`_asset_dirs`/:func:`netgen_setup_file`).

    Raises :class:`PdkNotFoundError` when no PDK install resolves at all
    (the same condition :func:`find_pdk` raises for).
    """
    info = find_pdk(variant=variant, root=root)
    klayout_dir = info["assets"]["klayout"]
    if klayout_dir is None:
        return None

    drc_dir = os.path.join(klayout_dir, "drc")
    if not os.path.isdir(drc_dir):
        nested_drc_dir = os.path.join(klayout_dir, "tech", "drc")
        if not os.path.isdir(nested_drc_dir):
            return None
        drc_dir = nested_drc_dir

    for name in (f"{info['variant']}.lydrc", f"{info['variant']}.drc"):
        candidate = os.path.join(drc_dir, name)
        if os.path.isfile(candidate):
            return candidate

    return None


def lvs_deck_file(variant: str | None = None, root: str | None = None) -> str | None:
    """Resolve the **filename** of the PDK's native, directly-runnable
    KLayout LVS-DSL rule-deck script inside the already-discovered
    ``assets["klayout"]`` directory's ``lvs/`` subdirectory (issue #869),
    mirroring :func:`drc_deck_file`'s shape one asset area over (that
    function's own docstring documents the ``drc/`` sibling's naming
    convention and gf180mcu-shaped-fragments caveat; the same reasoning
    applies here).

    Naming convention (verified against a real ``volare``-fetched sky130A
    install for this issue): open_pdks stages sky130's complete,
    ready-to-run LVS/device-extraction deck as a single file named after
    the variant, ``libs.tech/klayout/lvs/<variant>.lvs`` (e.g.
    ``sky130.lvs`` -- unlike :func:`drc_deck_file`'s own
    ``<variant>.lydrc``, the LVS deck's filename is *not* itself
    variant-suffixed on a real sky130A/sky130B install, both of which ship
    the identical ``sky130.lvs``). This function prefers a variant-named
    file (``<variant>.lvs``) if one exists (the same "prefer the specific
    name" precedence :func:`drc_deck_file`/:func:`netgen_setup_file` use)
    and falls back to the bare family name derived by stripping any
    trailing PDK-suite letter from ``variant`` (``sky130A``/``sky130B`` ->
    ``sky130.lvs``; a variant with no such letter, e.g. an already-bare
    ``sky130``, is tried as-is).

    A real gf180mcu install also ships a single ``libs.tech/klayout/lvs/
    gf180mcu.lvs`` (verified for this issue) -- unlike ``drc_deck_file``'s
    gf180mcu fragments-only case, so this resolver does not need a
    "fragments, return None" branch the way that one does. Whether that
    file's own internal device-recognition contract is drivable the same
    way this issue's sky130 cross-check drives ``sky130.lvs`` is untested
    here (issue #869 is a sky130-only cross-check, matching this repo's
    "sky130 first" open-PDK policy and gf180mcu's device-extraction deck
    carrying no ``RuleProvenance`` citations yet to cross-check against --
    see ``docs/cli/extract.md``'s "sky130 native-deck (sky130.lvs) LVS
    device-extraction cross-check" section) -- this function still resolves
    it structurally (same "single file present" check as sky130), a future
    caller is free to attempt it.

    A single-PDK flat install (:func:`_probe_flat_variant`, e.g. IHP-Open-
    PDK's SG13G2/SG13CMOS5L) nests its ``lvs/`` one directory deeper, at
    ``libs.tech/klayout/tech/lvs/`` rather than ``libs.tech/klayout/lvs/``
    directly -- the same nesting :func:`drc_deck_file` falls back to for its
    own ``drc/`` (verified against a real fetched ``ihp-sg13g2``/
    ``ihp-sg13cmos5l`` install, issue #1399). It also drops the variant's
    vendor-prefix segment entirely rather than just its trailing suite
    letter: a real ``ihp-sg13cmos5l`` install's deck is named
    ``sg13cmos5l.lvs`` (bare process name, no ``ihp-`` prefix), and
    ``ihp-sg13g2`` ships ``sg13g2.lvs`` the same way. Generalized as "also
    try the variant name with its leading, hyphen-delimited vendor-prefix
    segment stripped" -- not a literal ``ihp-sg13cmos5l`` special case, so
    any future vendor-prefixed variant name (``vendor-processname``)
    benefits the same way.

    Returns the absolute path to the resolved script, or ``None`` when the
    variant ships no ``klayout`` asset directory at all, neither the
    ``lvs/`` nor the nested ``tech/lvs/`` subdirectory, or the resolved
    directory contains none of the expected filenames -- never guessed or
    fabricated, matching this module's existing ``None``-means-absent
    convention (see :func:`_asset_dirs`/:func:`drc_deck_file`).

    Raises :class:`PdkNotFoundError` when no PDK install resolves at all
    (the same condition :func:`find_pdk` raises for).
    """
    info = find_pdk(variant=variant, root=root)
    klayout_dir = info["assets"]["klayout"]
    if klayout_dir is None:
        return None

    lvs_dir = os.path.join(klayout_dir, "lvs")
    if not os.path.isdir(lvs_dir):
        nested_lvs_dir = os.path.join(klayout_dir, "tech", "lvs")
        if not os.path.isdir(nested_lvs_dir):
            return None
        lvs_dir = nested_lvs_dir

    resolved_variant = info["variant"]
    # Strip a trailing single uppercase PDK-suite designator (sky130A ->
    # sky130, gf180mcuC -> gf180mcu -- both known families end in a digit
    # or lowercase letter, never uppercase, so this is unambiguous) --
    # open_pdks names the LVS deck after the bare family, not the lettered
    # variant. Mirrors `pdk_models._pdk_variant_family`'s own family/variant
    # split, restated locally rather than imported to avoid a dependency
    # from this lower-level asset-discovery module onto that higher-level
    # device-model-resolution one.
    family = resolved_variant
    if len(family) > 1 and family[-1].isupper():
        family = family[:-1]

    candidates = [f"{resolved_variant}.lvs", f"{family}.lvs"]
    # Also drop a leading, hyphen-delimited vendor-prefix segment
    # (`ihp-sg13cmos5l` -> `sg13cmos5l`) -- a distinct mismatch shape from
    # the trailing-suite-letter case above (a missing prefix, not a suffix
    # letter), verified against a real IHP-Open-PDK install (issue #1399).
    if "-" in resolved_variant:
        candidates.append(f"{resolved_variant.rsplit('-', 1)[-1]}.lvs")

    for name in candidates:
        candidate = os.path.join(lvs_dir, name)
        if os.path.isfile(candidate):
            return candidate

    return None


def _pcell_lib_dir_for_assets(assets: dict[str, str | None]) -> str | None:
    """The absolute PyCell-library directory for an already-resolved
    ``assets`` map, or ``None``.

    Split out from :func:`pdk_pcell_lib_dir` so :func:`find_pdk`/
    :func:`list_pdks` can compute their ``has_pcell_library`` field without
    recursing back into :func:`find_pdk`.
    """
    klayout_dir = assets.get("klayout")
    if klayout_dir is None:
        return None
    path = os.path.join(klayout_dir, _PCELL_LIB_SUBDIR)
    return path if os.path.isdir(path) else None


def _pcell_packages_in(lib_dir: str) -> list[str]:
    """The importable PyCell library package names directly under ``lib_dir``,
    sorted; ``[]`` when there are none.

    A "package" is an immediate subdirectory holding an ``__init__.py`` --
    exactly what ``import <name>`` resolves once ``lib_dir`` is on
    ``sys.path``, which is how the PDKs' own KLayout autoload macros load them
    (verified against sky130A's ``pymacros/sky130_pcells.lym``, which does
    precisely this ``sys.path.insert`` + ``from cells import sky130`` pair).
    """
    try:
        entries = os.listdir(lib_dir)
    except OSError:  # pragma: no cover - unreadable directory
        return []
    return sorted(
        name
        for name in entries
        if os.path.isfile(os.path.join(lib_dir, name, "__init__.py"))
    )


def _pcell_packages_for_assets(assets: dict[str, str | None]) -> list[str]:
    """:func:`_pcell_packages_in` for an already-resolved ``assets`` map."""
    lib_dir = _pcell_lib_dir_for_assets(assets)
    return [] if lib_dir is None else _pcell_packages_in(lib_dir)


def pdk_pcell_lib_dir(
    variant: str | None = None, root: str | None = None
) -> str | None:
    """Resolve the **directory** a PDK stages its own KLayout PyCell (Python
    PCell) library packages in, one level under the already-discovered
    ``assets["klayout"]`` directory (issue #1535), mirroring
    :func:`drc_deck_file`/:func:`lvs_deck_file`'s shape one subdirectory over.

    ``find_pdk`` (and its ``_ASSET_LAYOUT`` table) only ever resolved the
    containing ``libs.tech/klayout`` directory; the ``python/`` subdirectory a
    caller must put on ``sys.path`` before importing a PDK's own PCell library
    was not looked up anywhere in this repo. Resolves ``variant``/``root``
    exactly as :func:`find_pdk` does (same precedence, same
    :class:`PdkNotFoundError` on no match).

    Layout convention (verified against three real installs, see
    :data:`_PCELL_LIB_SUBDIR`): both sky130A and ihp-sg13g2 stage their PyCell
    packages directly at ``libs.tech/klayout/python/<package>/``, and
    gf180mcuD ships no such directory at all. Unlike ``drc/``/``lvs/``, no
    verified install nests it under ``klayout/tech/``, so -- keeping this
    module's "enumerate what is present, never guess" convention -- no nested
    fallback is probed.

    Returns the absolute directory, or ``None`` when the variant ships no
    ``klayout`` asset directory at all or no ``python/`` subdirectory under it.

    Raises :class:`PdkNotFoundError` when no PDK install resolves at all.
    """
    info = find_pdk(variant=variant, root=root)
    return _pcell_lib_dir_for_assets(info["assets"])


def pdk_pcell_libraries(
    variant: str | None = None, root: str | None = None
) -> list[str]:
    """Enumerate the PyCell library **package names** the resolved PDK ships
    under :func:`pdk_pcell_lib_dir` (issue #1535), sorted.

    Returns ``[]`` -- a successful "this PDK ships none", not an error -- when
    the variant has no ``python/`` directory (e.g. a real gf180mcuD install)
    or it holds no Python package. Enumerates what is on disk; it deliberately
    does *not* import anything, so a package that needs a third-party module
    klt has no dependency on (sky130A's ``cells`` needs ``gdsfactory``;
    ihp-sg13g2's chain reaches ``cni``) is still listed here. Whether a
    package actually *loads* is :mod:`klayout_tools.pdk_pcell`'s business.

    Raises :class:`PdkNotFoundError` when no PDK install resolves at all.
    """
    info = find_pdk(variant=variant, root=root)
    return _pcell_packages_for_assets(info["assets"])


#: Tech-LEF corner suffixes open_pdks ships alongside a standard-cell
#: library's merged macro LEF (issue #397 / #425 -- the OpenROAD survey's own
#: finding that ``_ASSET_LAYOUT`` has no ``lef`` key at all). Unlike
#: liberty's process/temperature/voltage corners, these name a *parasitic*
#: corner (min/nom/max routing-layer resistance/capacitance), so
#: ``"nom"`` -- the nominal, typical-parasitic tech LEF -- is the sensible
#: default for a P&R run's floorplan/routing-layer stack, independent of
#: whichever liberty corner the same run resolves for timing.
_NOMINAL_TECH_LEF_CORNER = "nom"


def _resolve_tech_lef(lib_dir: str, cell_library: str, corner: str) -> str | None:
    """Resolve the tech LEF for ``cell_library`` under ``lib_dir``, issue
    #1790's generalization of :func:`lef_files`' original open_pdks-only
    resolution.

    Tries the open_pdks-wide convention first: a per-corner tech LEF at
    ``techlef/<cell_library>__<corner>.tlef`` (``corner`` one of
    ``min``/``nom``/``max`` -- a parasitic-extraction corner, not a liberty
    corner).

    Falls back to IHP-Open-PDK's distinct layout when the library ships no
    ``techlef/`` subdirectory at all (verified live against a real fetched
    IHP-Open-PDK v0.3.0 install): a single, corner-invariant tech LEF staged
    directly under ``lef/`` alongside the merged cell LEF -- e.g. IHP's
    ``sg13g2_stdcell`` ships ``lef/sg13g2_stdcell.lef`` (the merged cell LEF)
    and ``lef/sg13g2_tech.lef`` (the tech LEF) side by side, with no
    per-corner variants and no ``techlef/`` subdirectory. Note the tech
    LEF's own filename does not even share ``cell_library``'s name (IHP
    names it after the *process*, not the *library*), so this fallback
    identifies it **structurally** -- "the other ``.lef`` file in ``lef/``"
    -- rather than by any name pattern, and ``corner`` is ignored on this
    path since IHP ships one tech LEF for the whole PDK, not one per
    parasitic-extraction corner.

    A library that *does* ship a ``techlef/`` subdirectory but not the
    requested corner does **not** fall back to guessing inside ``lef/`` --
    that would silently substitute an unrelated file for a corner the
    install genuinely does not stage, exactly the "never guessed or
    fabricated" convention :func:`lef_files` documents. The fallback only
    engages when ``techlef/`` is absent entirely.

    Requires exactly one non-cell-LEF ``.lef`` candidate under ``lef/`` to
    resolve the fallback -- zero or more than one is ambiguous and returns
    ``None`` rather than guessing.
    """
    techlef_dir = os.path.join(lib_dir, "techlef")
    conventional = os.path.join(techlef_dir, f"{cell_library}__{corner}.tlef")
    if os.path.isfile(conventional):
        return conventional
    if os.path.isdir(techlef_dir):
        return None

    lef_dir = os.path.join(lib_dir, "lef")
    cell_lef_name = f"{cell_library}.lef"
    try:
        entries = sorted(os.listdir(lef_dir))
    except OSError:
        return None
    candidates = [
        name
        for name in entries
        if name != cell_lef_name
        and name.endswith(".lef")
        and os.path.isfile(os.path.join(lef_dir, name))
    ]
    if len(candidates) == 1:
        return os.path.join(lef_dir, candidates[0])
    return None


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
    ``<libs_ref>/<cell_library>/lef/<cell_library>.lef``. A second, distinct
    layout (verified live against a real fetched IHP-Open-PDK v0.3.0
    install, issue #1790) is also resolved for the tech LEF -- see
    :func:`_resolve_tech_lef` -- for PDKs like IHP's ``sg13g2_stdcell`` that
    ship one corner-invariant tech LEF directly under ``lef/`` instead of a
    ``techlef/`` subdirectory.

    Returns::

        {"tech_lef": <abs path | None>, "cell_lef": <abs path | None>}

    Each value is ``None`` when the resolved install ships no ``libs_ref``
    asset at all, no ``cell_library`` entry, or that entry ships neither
    layout's expected tech-LEF/cell-LEF file -- never guessed or fabricated,
    matching this module's existing ``None``-means-absent convention (see
    :func:`_asset_dirs`/:func:`netgen_setup_file`).

    Raises :class:`PdkNotFoundError` when no PDK install resolves at all
    (the same condition :func:`find_pdk` raises for).
    """
    info = find_pdk(variant=variant, root=root)
    libs_ref = info["assets"]["libs_ref"]
    if libs_ref is None:
        return {"tech_lef": None, "cell_lef": None}

    lib_dir = os.path.join(libs_ref, cell_library)

    tech_lef = _resolve_tech_lef(lib_dir, cell_library, corner)
    cell_lef = os.path.join(lib_dir, "lef", f"{cell_library}.lef")

    return {
        "tech_lef": tech_lef,
        "cell_lef": cell_lef if os.path.isfile(cell_lef) else None,
    }


# --------------------------------------------------------------------------- #
# `klt pdk em-limits` -- electromigration current-density limits declared
# across every tech LEF an install ships (issue #1215).
# --------------------------------------------------------------------------- #

#: Layer ``TYPE``s a current-carrying design actually sizes an EM budget
#: against -- routing metal and cut/via (including the diffusion/poly
#: contact, ``CON``). Excludes non-current-carrying declarative layers a
#: tech LEF also declares (``MASTERSLICE`` wells/poly, ``OVERLAP``,
#: ``PR_bndry``, ...), keeping the report scoped to what issue #1215 asks
#: for -- "the layers a current-carrying design must size" -- rather than
#: echoing every ``LAYER`` block a tech LEF happens to declare.
_CURRENT_CARRYING_LAYER_TYPES = frozenset({"ROUTING", "CUT"})

#: Tolerance for deciding two tech LEFs' EM-adjacent values "agree" --
#: generous enough to absorb float round-trip noise, tight enough that the
#: issue's own 1.19 vs. 0.99 µm / 1.5 vs. 1.21 mA/µm disagreements are never
#: mistaken for agreement.
_EM_VALUE_ABS_TOL = 1e-6


def _em_values_agree(values: list[float]) -> bool:
    """``True`` when every value in non-empty ``values`` matches the first,
    within :data:`_EM_VALUE_ABS_TOL`."""
    first = values[0]
    return all(
        math.isclose(value, first, abs_tol=_EM_VALUE_ABS_TOL) for value in values
    )


def _summarize_em_field(
    entries: list[dict[str, Any]], field: str, *, conservative: bool
) -> dict[str, Any]:
    """Summarize one EM-adjacent field (``dc_current_density``,
    ``ac_current_density``, ``thickness_um``, ``resistance_rpersq``) across
    every source ``entries`` that parsed a value for it.

    Returns::

        {
            "shipped": bool,               # any source declared this field?
            "agrees": bool | None,         # None when "shipped" is False
            "values": [
                {"cell_library", "corner", "tech_lef", "value"}, ...
            ],
            "conservative": float | None,  # only present when conservative=True
        }

    ``shipped: False`` (with ``values: []``) is itself the answer for a
    layer/field with nothing declared anywhere -- e.g. ``CON``'s DC/AC
    current density -- an explicit "no limit shipped" result rather than a
    silently omitted field (issue #1215's own suggested handling).

    ``conservative`` is the minimum (more restrictive) value across sources
    when they disagree, per issue #1215's "returns the conservative value by
    default" -- only meaningful for a current-density budget (lower is
    always safer), so it is only computed when the caller asks for it.
    """
    present = [entry for entry in entries if entry[field] is not None]
    result: dict[str, Any] = {
        "shipped": bool(present),
        "agrees": _em_values_agree([e[field] for e in present]) if present else None,
        "values": [
            {
                "cell_library": e["cell_library"],
                "corner": e["corner"],
                "tech_lef": e["tech_lef"],
                "value": e[field],
            }
            for e in present
        ],
    }
    if conservative:
        result["conservative"] = min((e[field] for e in present), default=None)
    return result


def _discover_tech_lefs(libs_ref: str) -> list[dict[str, str]]:
    """Enumerate every tech LEF under ``libs_ref``, naming-convention-
    independently: every ``<libs_ref>/<lib>/techlef/<lib>__<corner>.tlef``
    file, for every immediate ``libs_ref`` entry that ships a ``techlef/``
    subdirectory at all.

    Deliberately does **not** reuse
    :func:`klayout_tools.pdk_cells._scan_cell_libraries`'s
    name filter (:data:`klayout_tools.pdk_cells._STD_CELL_LIB_MARKERS`):
    issue #1215's own
    reported disagreement is precisely *between* an `_fd_sc_`-named family
    (`gf180mcu_fd_sc_mcu9t5v0`/`mcu7t5v0`) and an `_osu_sc_`-named one
    (`gf180mcu_osu_sc_gp9t3v3`/`gp12t3v3`) -- a name filter tuned for `klt
    pdk cells`'s different purpose (only digital timing-view libraries)
    would silently drop half of exactly the disagreement this query exists
    to surface. A `libs_ref` entry with no `techlef/` subdirectory
    (primitive-device, I/O-pad, hard-macro IP libraries -- verified against
    real sky130/gf180mcu installs) is naturally excluded without a name
    filter at all: it ships no tech LEF to parse.

    Returns ``[{"cell_library", "corner", "tech_lef"}, ...]``, sorted by
    ``(cell_library, corner)``. ``corner`` is derived from the filename
    (stripping the ``<cell_library>__`` prefix and ``.tlef`` suffix) rather
    than assumed to be one of `min`/`nom`/`max` -- an install shipping only
    `nom` (issue #1215's own `osu_sc_gp*t3v3` example), or a corner name
    this reader does not otherwise recognise, is still reported verbatim.
    """
    sources: list[dict[str, str]] = []
    if not os.path.isdir(libs_ref):
        return sources
    for name in sorted(os.listdir(libs_ref)):
        techlef_dir = os.path.join(libs_ref, name, "techlef")
        if not os.path.isdir(techlef_dir):
            continue
        prefix = f"{name}__"
        for filename in sorted(os.listdir(techlef_dir)):
            if not filename.endswith(".tlef"):
                continue
            if filename.startswith(prefix):
                corner = filename[len(prefix) : -len(".tlef")]
            else:
                corner = filename[: -len(".tlef")]
            sources.append(
                {
                    "cell_library": name,
                    "corner": corner,
                    "tech_lef": os.path.join(techlef_dir, filename),
                }
            )
    return sources


def resolve_pdk_dbu(pdk_info: dict[str, Any]) -> float | None:
    """Resolve a resolved PDK's own database unit (dbu, in micrometres) from
    its tech LEF's declared ``DATABASE MICRONS`` value -- e.g. ``0.0005`` for
    a tech LEF declaring ``DATABASE MICRONS 2000`` (gf180mcu), ``0.001`` for
    one declaring ``DATABASE MICRONS 1000`` (sky130).

    Mirrors :func:`~klayout_tools.place_and_route._merge_def_to_gds`'s own
    ``database_microns is not None: lefdef_config.dbu = 1.0 / database_microns``
    pattern (issue #1032) so a caller with no single ``cell_library`` in hand
    (unlike ``place_and_route``, which always resolves one) can still agree
    with it by construction (issue #1496) -- built on
    :func:`_discover_tech_lefs`'s existing family-wide tech LEF enumeration
    (already used by :func:`em_limits`) rather than :func:`lef_files`, which
    requires a ``cell_library`` argument this call site does not have.

    Enumerates every tech LEF under ``pdk_info["assets"]["libs_ref"]`` (the
    same family-wide set :func:`em_limits` reports) and returns the **finest**
    dbu any of them declares -- ``1.0 / max(database_microns)``. Every real
    open PDK's tech LEFs agree on one ``DATABASE MICRONS`` value across the
    whole family, so "the finest" is normally just "the one value"; picking
    the finest rather than whichever library happens to sort first makes the
    answer independent of library naming, and keeps every library's own grid
    exactly representable whenever the declared values divide each other (as
    LEF's own permitted 100/200/1000/2000/10000/20000 set does).

    Returns ``None`` -- never raises -- when ``libs_ref`` is unset, no tech
    LEF is found, or no discovered tech LEF declares (or can have parsed) a
    ``DATABASE MICRONS`` value; the caller is expected to fall back to its
    own existing default in that case, exactly as
    :func:`~klayout_tools.place_and_route._merge_def_to_gds` already does
    when ``read_lef_header``'s own ``database_microns`` comes back ``None``.
    """
    libs_ref = (pdk_info.get("assets") or {}).get("libs_ref")
    if not libs_ref:
        return None
    declared: list[int] = []
    for source in _discover_tech_lefs(libs_ref):
        text = _read_text(source["tech_lef"])
        if text is None:
            continue
        database_microns = parse_lef_header(text)["database_microns"]
        if database_microns:
            declared.append(database_microns)
    if not declared:
        return None
    return 1.0 / max(declared)


def em_limits(variant: str | None = None, root: str | None = None) -> dict[str, Any]:
    """Report electromigration current-density limits declared across every
    tech LEF a resolved PDK variant ships (issue #1215).

    Resolves one PDK install/variant exactly as :func:`find_pdk` does (same
    ``variant``/``root`` args, same :class:`PdkNotFoundError` on no match),
    then discovers every tech LEF under its ``libs_ref`` asset via
    :func:`_discover_tech_lefs` -- every `libs_ref/<lib>/techlef/*.tlef`
    file, independent of the library's naming convention (**not** filtered
    to `_fd_sc_`-named standard-cell libraries the way
    :func:`klayout_tools.pdk_cells.list_cell_libraries` is -- see
    :func:`_discover_tech_lefs`'s own docstring for why). Each resolved tech
    LEF is parsed with
    :func:`klayout_tools.lef_header.parse_lef_header`.

    For every ``ROUTING``/``CUT`` layer (:data:`_CURRENT_CARRYING_LAYER_TYPES`
    -- the layers a current-carrying design actually sizes against, per the
    issue's own framing) seen in **any** parsed tech LEF, reports the
    per-source ``dc_current_density``/``ac_current_density`` values plus
    ``thickness_um``/``resistance_rpersq`` for context, and whether they
    agree across every source that declared them. A layer present in one
    tech LEF but not another (e.g. an osu library shipping only ``nom``) is
    still reported using whichever sources did declare it -- a smaller
    source set is not itself a disagreement.

    Design choice -- **live-parses** the shipped tech LEFs at call time
    rather than owning a curated table, matching
    :func:`klayout_tools.pdk_cells.list_cell_libraries`'s own rationale
    (docs/cli/pdk.md): the install's own files are the only
    place this data is stated, and a curated copy would silently drift on a
    PDK upgrade instead of reflecting what is actually installed.

    Returns a dict matching the documented JSON schema (see
    ``docs/cli/pdk.md``)::

        {
            "schema_version": 1,
            "pdk": <variant name>,
            "root": <absolute install root>,
            "sources": [
                {"cell_library": str, "corner": str, "tech_lef": <abs path>},
                ...
            ],
            "layers": [
                {
                    "name": str,
                    "type": "ROUTING" | "CUT",
                    "dc_current_density": {
                        "shipped": bool, "agrees": bool | None,
                        "conservative": float | None,
                        "values": [
                            {"cell_library", "corner", "tech_lef", "value"}, ...
                        ],
                    },
                    "ac_current_density": {... same shape ...},
                    "thickness_um": {
                        "shipped": bool, "agrees": bool | None,
                        "values": [...],
                    },
                    "resistance_rpersq": {... same shape as thickness_um ...},
                },
                ...
            ],
            "disagreements": [<layer name>, ...],  # dc/ac current density only
        }

    ``layers`` is name-sorted; a variant shipping no `libs_ref` asset (or one
    whose libraries ship no `techlef/` directory at all) reports
    ``"sources": []``, ``"layers": []`` -- a successful, empty result, not an
    error. ``disagreements`` lists every layer name where
    ``dc_current_density`` or ``ac_current_density`` is ``shipped`` but not
    ``agrees`` (``thickness_um``/``resistance_rpersq`` disagreement is
    visible per-layer but does not, by itself, add a layer to this list -- it
    is context for *why* a current-density disagreement might exist, per the
    issue's own "same resistance, different thickness" observation, not a
    second disagreement axis this query adjudicates).

    Raises :class:`PdkNotFoundError` when no PDK install resolves at all.
    """
    info = find_pdk(variant=variant, root=root)
    libs_ref = info["assets"]["libs_ref"]
    sources = _discover_tech_lefs(libs_ref) if libs_ref is not None else []

    layer_entries: dict[str, list[dict[str, Any]]] = {}
    layer_types: dict[str, str | None] = {}

    for source in sources:
        text = _read_text(source["tech_lef"])
        if text is None:
            continue
        header = parse_lef_header(text)
        for layer in header["layers"]:
            if layer["type"] not in _CURRENT_CARRYING_LAYER_TYPES:
                continue
            layer_entries.setdefault(layer["name"], []).append(
                {
                    "cell_library": source["cell_library"],
                    "corner": source["corner"],
                    "tech_lef": source["tech_lef"],
                    "thickness_um": layer["thickness_um"],
                    "resistance_rpersq": layer["resistance_rpersq"],
                    "dc_current_density": layer["dc_current_density"],
                    "ac_current_density": layer["ac_current_density"],
                }
            )
            layer_types.setdefault(layer["name"], layer["type"])

    layers: list[dict[str, Any]] = []
    disagreements: list[str] = []
    for name in sorted(layer_entries):
        entries = layer_entries[name]
        dc = _summarize_em_field(entries, "dc_current_density", conservative=True)
        ac = _summarize_em_field(entries, "ac_current_density", conservative=True)
        thickness = _summarize_em_field(entries, "thickness_um", conservative=False)
        resistance = _summarize_em_field(
            entries, "resistance_rpersq", conservative=False
        )

        if dc["shipped"] and not dc["agrees"]:
            disagreements.append(name)
        elif ac["shipped"] and not ac["agrees"]:
            disagreements.append(name)

        layers.append(
            {
                "name": name,
                "type": layer_types[name],
                "dc_current_density": dc,
                "ac_current_density": ac,
                "thickness_um": thickness,
                "resistance_rpersq": resistance,
            }
        )

    return {
        "schema_version": 1,
        "pdk": info["variant"],
        "root": info["root"],
        "sources": sources,
        "layers": layers,
        "disagreements": disagreements,
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


def _read_text(path: str) -> str | None:
    """Read ``path`` as UTF-8 text, or ``None`` if missing/unreadable.

    Stays defined here (rather than moving into :mod:`klayout_tools.pdk_cells`
    with the rest of the `klt pdk cells` region, issue #1884) because
    :func:`lef_files` below also calls it -- :mod:`klayout_tools.pdk_cells`
    imports it back from here instead.
    """
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
#: libraries :data:`klayout_tools.pdk_cells._STD_CELL_LIB_MARKERS`/`klt pdk
#: cells` reports. Like that constant, this is an open_pdks-wide naming
#: convention (shared by sky130 and gf180mcu), not a sky130-specific
#: hardcoded list.
#: `klt pdk cells` deliberately excludes `*_fd_ip_*` entries (they are not a
#: "standard-cell digital library"); `klt pdk macros` exists specifically to
#: surface them -- see docs/cli/pdk.md "klt pdk macros".
_HARD_MACRO_LIB_MARKER = "_fd_ip_"

#: View subdirectories under a `libs_ref/<lib>` entry that
#: :func:`list_hard_macro_libraries` reports presence of, keyed by the name
#: surfaced in the JSON ``views`` object. Presence is a plain
#: ``os.path.isdir`` check (the same "does this view directory exist" probe
#: :func:`klayout_tools.pdk_cells._nominal_supply` uses for `lib/`) -- this
#: command reports what
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
    (:func:`klayout_tools.pdk_cells.list_cell_libraries`,
    :func:`list_hard_macro_libraries`).
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
