"""Behavioural Verilog cell-model resolution for standard-cell libraries
(issue #5782): the ``libs.ref/<lib>/verilog/`` view resolver behind ``klt
functional-verification``'s ``pdk`` request block.

The mirror of :func:`klayout_tools.pdk_cells.resolve_liberty_for_cell_library`
(issue #1652) for the simulation view: same ``error_cls`` injection, same
``variant``/``root`` threading through :func:`klayout_tools.pdk.find_pdk`,
same ``info["assets"]["libs_ref"]`` base -- so the liberty a ``klt
synthesize`` run timed against and the behavioural models a gate-level
re-simulation elaborates against resolve through one shared posture.

Deliberately **not** shared with the liberty resolver:

- no :func:`klayout_tools.pdk_cells._default_variant_shipping` fallback
  (issue #4204) -- a gate-level re-simulation that silently resolved its cell
  models from a *different* install than the one synthesis timed against is
  the plausible-looking-pass failure this resolver exists to prevent, so an
  ambiguous install (``find_pdk`` reported ``ambiguous_roots``) is a refusal,
  never a first-match guess;
- the caller's error class accepts the ``code`` keyword for the named
  refusals below. When no PDK install resolves, the error carries the PDK
  lookup message.

Selection rule (open_pdks-wide by construction, not a sky130-specific list --
the same convention posture ``pdk.py``'s ``_MACRO_VIEW_DIRS`` and
``pdk_cells._STD_CELL_LIB_MARKERS`` already take): probe
``<libs_ref>/<cell_library>/verilog/``; ``primitives.v`` **first when
present**, then ``<cell_library>.v`` (required). Order matters:
``primitives.v`` defines the UDPs ``<cell_library>.v`` instantiates (23 UDPs,
referenced 40+152 times in sky130_fd_sc_hd alone), so dropping it breaks
elaboration. ``primitives.v`` is optional because not every library ships it
(gf180mcu's ``gf180mcu_osu_sc_gp9t3v3`` ships ``<lib>.v`` alone); a library
with no ``verilog/`` view at all (its sibling ``gp12t3v3``) is a named
refusal. ``__blackbox*.v`` siblings (sky130 ships two) can never be selected:
the resolver picks files by name, never by directory scan, and a blackbox has
no behaviour -- including one would elaborate but simulate nothing.

A descriptor-declared root (``klt-pdk.json``, issue #3782) can name the models
itself: ``views.<cell_library>.verilog`` is an ordered file list (issue
#7695), consulted **before** the ``libs.ref`` convention and returned verbatim
-- no ``primitives.v`` probing, no reordering. ASAP7 needs it: its public
release groups models by family and threshold voltage
(``asap7sc7p5t_{AO,INVBUF,OA,SEQ,SIMPLE}_RVT_TT_<date>.v``), so no
``<lib>/verilog/<lib>.v`` exists for the convention to find. A library the
descriptor does not map a ``verilog`` list for falls through to the
convention unchanged.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from .pdk import PdkNotFoundError, descriptor_views, find_pdk


def resolve_cell_models_for_cell_library(
    cell_library: str,
    error_cls: Callable[..., Exception],
    *,
    variant: str | None = None,
    root: str | None = None,
) -> tuple[list[str], dict[str, Any]]:
    """Resolve ordered behavioural-model sources for ``cell_library``.

    Returns ``(model_paths, pdk_info)``: the Verilog files to compile, in
    dependency order (``primitives.v`` before ``<cell_library>.v``, or the
    descriptor's ``views.<cell_library>.verilog`` list as declared), plus
    :func:`find_pdk`'s own resolution dict unchanged for the caller.
    ``error_cls`` is the caller's error class (the same injection
    :func:`klayout_tools.pdk_cells.resolve_liberty_for_cell_library` takes,
    plus a ``code`` keyword -- annotated ``Callable`` because
    ``type[Exception]``'s constructor signature does not carry it).

    ``variant``/``root`` (the CLI's ``--pdk``/``--pdk-root`` flags or the
    ``request.pdk`` block's ``variant``/``root`` members) select a specific
    installed PDK variant/root exactly as :func:`find_pdk` does; ``None``
    leaves that resolver's own default search order in effect -- except that
    an order that lands on an install other roots also ship is a refusal
    (``pdk_ambiguous_install``), because a behavioural model set silently
    taken from a different install than synthesis used is exactly the
    quietly-wrong-verdict class issue #5782 closes. An explicit ``root``
    always wins: ``find_pdk`` disables the search for it, so the ambiguity
    refusal never fires when one was pinned.

    If no PDK install resolves, or its descriptor cannot be loaded, raises
    ``error_cls`` with the lookup error's message. Model resolution failures
    carry a stable ``code``:

    - ``pdk_ambiguous_install`` -- the resolved install is ambiguous
      (``ambiguous_roots`` non-empty) and no explicit ``root`` pinned it;
    - ``pdk_no_verilog_view`` -- the library has no descriptor ``verilog``
      list and the install ships no ``libs_ref`` asset, the named library is
      absent from it, or the library ships no ``verilog/``
      view (real case: gf180mcuD's ``gf180mcu_osu_sc_gp12t3v3`` -- it has
      ``lef``/``lib`` and no simulation models at all);
    - ``pdk_no_cell_models`` -- the ``verilog/`` view exists but ships no
      ``<cell_library>.v`` (including the only-``__blackbox*.v`` case: a
      blackbox has no behaviour, so a view of nothing else is unusable).
    """
    try:
        info = find_pdk(variant=variant, root=root)
    except PdkNotFoundError as exc:
        raise error_cls(str(exc)) from exc

    if root is None and info.get("ambiguous_roots"):
        others = ", ".join(
            f"'{entry['root']}' (via {entry['resolved_via']})"
            for entry in info["ambiguous_roots"]
        )
        raise error_cls(
            f"the PDK install for cell models is ambiguous: variant "
            f"'{info['variant']}' resolved via {info['resolved_via']}, but the "
            f"same variant is also installed at {others} -- pass --pdk-root "
            "(or set request.pdk.root) to pin the install the netlist's cells "
            "were synthesized against",
            code="pdk_ambiguous_install",
        )

    try:
        described = descriptor_views(info, cell_library).get("verilog")
    except PdkNotFoundError as exc:
        raise error_cls(str(exc)) from exc
    if described is not None:
        return list(described), info

    libs_ref = info["assets"]["libs_ref"]
    if libs_ref is None:
        raise error_cls(
            f"cell models not found: resolved PDK install '{info['variant']}' "
            f"at '{info['root']}' ships no libs_ref asset",
            code="pdk_no_verilog_view",
        )

    lib_dir = os.path.join(libs_ref, cell_library)
    if not os.path.isdir(lib_dir):
        raise error_cls(
            f"cell models not found: standard-cell library '{cell_library}' "
            f"not found under resolved PDK install '{info['variant']}' at "
            f"'{info['root']}'",
            code="pdk_no_verilog_view",
        )

    view_dir = os.path.join(lib_dir, "verilog")
    if not os.path.isdir(view_dir):
        raise error_cls(
            f"cell models not found: standard-cell library '{cell_library}' "
            f"ships no verilog/ view under resolved PDK install "
            f"'{info['variant']}' at '{info['root']}' -- it has no "
            "behavioural simulation models",
            code="pdk_no_verilog_view",
        )

    models: list[str] = []
    primitives = os.path.join(view_dir, "primitives.v")
    if os.path.isfile(primitives):
        models.append(primitives)
    library_model = os.path.join(view_dir, f"{cell_library}.v")
    if not os.path.isfile(library_model):
        raise error_cls(
            f"cell models not found: '{cell_library}' ships a verilog/ view "
            f"with no {cell_library}.v under resolved PDK install "
            f"'{info['variant']}' at '{info['root']}'",
            code="pdk_no_cell_models",
        )
    models.append(library_model)
    return models, info
