"""Issue #5002: reject an SDF ``CELL`` whose ``(INSTANCE ...)`` names an
instance the netlist does not have, before ``vvp`` starts.

Measured live on cocotb 2.1.0 + Icarus 13.0 through ``klt
functional-verification`` with a flat netlist, ``(DIVIDER /)`` and a top
``CELL`` spelled ``(CELLTYPE "top") (INSTANCE top)`` (the design name used
as the root instance) carrying ordinary ``INTERCONNECT`` entries: the run
aborts at the start of the first test with ``ERROR: NULL handle passed to
vpi_scan.`` / `Assertion `0' failed`` (rc -6). The same SDF with the
design root spelled ``(INSTANCE)`` annotates and passes. The entries'
endpoints are irrelevant -- they resolve fine -- so the existing endpoint
guard (#3740) and the empty-``INSTANCE`` mis-scope guard (#4521) never see
it.

Also home to what the SDF preflight guards and drop passes share: the
gate-level module lookup (:func:`_find_gate_level_module`) and the scope
context both unresolvable-instance guards read (:func:`_load_sdf_scope_context`).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from .functional_verification_base import (
    FunctionalVerificationError,
)
from .functional_verification_sdf_rename import _sdf_divider


def _parse_sdf_inventory_module(chunk: str) -> list[dict[str, object]]:
    """Read cell instances even when a continuous assignment cannot be resolved.

    SDF inventory callers use only cell and instance names. Keep the full parse
    when it works and omit assignment statements on retry. The same tokenizer
    that the netlist parser uses preserves semicolons in escaped identifiers.
    """
    from ._verilog_text import split_top_level, strip_verilog_comments
    from .verilog_netlist import VerilogNetlistError, parse_gate_level_verilog

    try:
        return parse_gate_level_verilog(chunk)
    except VerilogNetlistError:
        statements = split_top_level(strip_verilog_comments(chunk), seps=";")
        structural = [
            stmt for stmt in statements if not re.match(r"assign(?![\w$])", stmt)
        ]
        if len(structural) == len(statements):
            raise
        return parse_gate_level_verilog(" ;\n".join(structural))


def _find_gate_level_module(
    source_texts: list[str],
    hdl_toplevel: str,
    *,
    parse_module: Callable[
        [str], list[dict[str, object]]
    ] = _parse_sdf_inventory_module,
) -> dict[str, Any] | None:
    """The first of ``source_texts`` containing a
    ``module <hdl_toplevel> ... endmodule`` chunk, parsed by ``parse_module``
    into that chunk's module description (``{"name", "ports", "instances",
    "port_aliases"}``,
    :func:`klayout_tools.verilog_netlist.parse_gate_level_verilog`'s shape)
    -- the one lookup behind both
    :func:`functional_verification_sdf._find_gate_level_instances` (which
    reads only the instance list) and
    :func:`functional_verification_sdf_internal_bus._collect_sdf_internal_bus_net_pins`
    (which needs the module's port list too, issue #6075).

    ``parse_module`` defaults to :func:`_parse_sdf_inventory_module`, the
    inventory parse that tolerates an unresolvable continuous assignment;
    the internal-bus pass passes
    :func:`klayout_tools.verilog_netlist.parse_gate_level_verilog` itself.
    Lives here, beside that parse, because the per-file size ratchet lets
    ``functional_verification_sdf.py`` only shrink.

    Returns ``None`` -- never raises -- whenever the module description
    cannot be derived: no such module in any source, or a top module outside
    :func:`klayout_tools.verilog_netlist.parse_gate_level_verilog`'s narrow
    gate-level grammar (notably an **ANSI-style** module header, which that
    grammar does not model).
    """
    from .functional_verification_sdf import _VERILOG_MODULE_CHUNK_TEMPLATE

    chunk_re = re.compile(
        _VERILOG_MODULE_CHUNK_TEMPLATE.format(name=re.escape(hdl_toplevel)), re.DOTALL
    )
    for text in source_texts:
        chunk_match = chunk_re.search(text)
        if chunk_match is None:
            continue
        try:
            modules = parse_module(chunk_match.group(0))
        except Exception:
            # Not parseable as gate-level Verilog (a hand-written RTL top, a
            # construct outside verilog_netlist's narrow grammar) -- no
            # module description. Never fatal.
            return None
        for module in modules:
            if module["name"] == hdl_toplevel:
                return module
        return None
    return None


def _describe_sdf_unresolvable_endpoints(
    offenders: list[tuple[int, str]], divider: str
) -> str:
    """Show the first endpoint for up to three distinct missing instance paths."""
    from .functional_verification_sdf import _sdf_split_unescaped

    first_by_path: dict[tuple[str, ...], tuple[int, str]] = {}
    for line, token in offenders:
        path = tuple(_sdf_split_unescaped(token, divider)[:-1])
        first_by_path.setdefault(path, (line, token))
    examples = [
        f"line {line}: {token}" for line, token in list(first_by_path.values())[:3]
    ]
    if len(first_by_path) > len(examples):
        examples.append("...")
    return (
        f"{'; '.join(examples)}; {len(offenders)} unresolvable endpoint(s) "
        f"across {len(first_by_path)} distinct instance path(s)"
    )


def _load_sdf_scope_context(
    sdf_path: str, source_paths: list[str], hdl_toplevel: str, scope: str | None
) -> tuple[str, str, set[str], str] | None:
    """What both unresolvable-instance guards read from disk: the SDF text,
    the annotation scope's module, that module's instance names, and the
    SDF's ``DIVIDER`` -- or ``None`` when neither guard can apply, which is
    conservative like the guards themselves: an unreadable SDF, a scope
    module that cannot be derived, a module with no derivable instance
    list, or a ``DIVIDER`` that is neither ``.`` nor ``/``.
    """
    from .functional_verification_sdf import (
        _read_verilog_source_texts,
        _resolve_sdf_scope_module,
    )

    try:
        with open(sdf_path, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return None
    source_texts = _read_verilog_source_texts(source_paths)
    # With `options.sdf.scope` the SDF's paths are relative to the scoped
    # instance's module, not to `hdl_toplevel` (issue #4521).
    scope_module = _resolve_sdf_scope_module(source_texts, hdl_toplevel, scope)
    if scope_module is None:
        return None
    module = _find_gate_level_module(source_texts, scope_module)
    if module is None:
        return None
    names = {str(instance["name"]) for instance in module["instances"]}
    if not names:
        return None
    divider = _sdf_divider(text)
    if divider not in (".", "/"):
        return None
    return text, scope_module, names, divider


def _sdf_unresolvable_cell_instances(
    text: str,
    names: set[str],
    divider: str,
    *,
    entries: list[tuple[str, int, str]] | None = None,
) -> list[tuple[int, str]]:
    """``(line, instance path)`` for each ``CELL`` that carries
    ``INTERCONNECT`` entries but whose ``INSTANCE`` path starts at no
    instance in ``names``. ``entries`` is
    :func:`functional_verification_sdf._sdf_interconnect_entries`' walk of
    ``text``, for a caller that already holds it."""
    from .functional_verification_sdf import (
        _sdf_interconnect_entries,
        _sdf_split_unescaped,
    )

    if entries is None:
        entries = _sdf_interconnect_entries(text)
    offenders: dict[str, int] = {}
    for cell_instance, entry_start, _entry in entries:
        if cell_instance in ("", "*") or cell_instance in offenders:
            continue
        parts = _sdf_split_unescaped(cell_instance, divider)
        if parts and parts[0] not in names:
            offenders[cell_instance] = text.count("\n", 0, entry_start) + 1
    return [(line, instance) for instance, line in offenders.items()]


def _raise_sdf_unresolvable_cell_instances(
    text: str,
    scope_module: str,
    names: set[str],
    divider: str,
    *,
    entries: list[tuple[str, int, str]] | None = None,
) -> None:
    """Raise over :func:`_sdf_unresolvable_cell_instances`'s offenders (issue
    #5002); a no-op when there are none."""
    offenders = _sdf_unresolvable_cell_instances(text, names, divider, entries=entries)
    if not offenders:
        return
    shown = "; ".join(f"line {line}: (INSTANCE {path})" for line, path in offenders[:3])
    raise FunctionalVerificationError(
        "options.sdf: this SDF has a CELL with INTERCONNECT entries whose "
        f"INSTANCE names an instance that is not in '{scope_module}' "
        f"({shown}{'; ...' if len(offenders) > 3 else ''}; "
        f"{len(offenders)} unresolvable CELL scope(s)). Icarus Verilog "
        "cannot report such a scope: its $sdf_annotate resolver aborts vvp "
        "outright ('ERROR: NULL handle passed to vpi_scan.', SIGABRT). "
        "Write the design's own root cell with an empty (INSTANCE) -- not "
        "the design name -- or regenerate the SDF from the same netlist the "
        "simulation reads"
    )


def _reject_sdf_unresolvable_cell_instance(
    sdf_path: str,
    source_paths: list[str],
    hdl_toplevel: str,
    scope: str | None = None,
) -> None:
    """Raise ``FunctionalVerificationError`` when a ``CELL`` with
    ``INTERCONNECT`` entries is scoped to an instance that is not in the
    annotation scope's module (issue #5002).

    Conservative like its #3740 sibling: a no-op when the scope module's
    instance list cannot be derived, when the ``DIVIDER`` is not ``.``/``/``,
    and for the design's own root cell (empty or ``*`` ``INSTANCE``).
    """
    context = _load_sdf_scope_context(sdf_path, source_paths, hdl_toplevel, scope)
    if context is not None:
        _raise_sdf_unresolvable_cell_instances(*context)


def _reject_sdf_unresolvable_scopes(
    sdf_path: str,
    source_paths: list[str],
    hdl_toplevel: str,
    scope: str | None = None,
) -> None:
    """Run both pre-launch unresolvable-instance guards: the INTERCONNECT
    endpoint check (#3740) and the CELL ``INSTANCE`` check (#5002). The
    SDF, the Verilog sources, and the ``INTERCONNECT`` walk are read once
    for both; the endpoint check raises first, as it did when the two ran
    back to back."""
    from .functional_verification_sdf import (
        _raise_sdf_unresolvable_interconnect_instances,
        _sdf_interconnect_entries,
    )

    context = _load_sdf_scope_context(sdf_path, source_paths, hdl_toplevel, scope)
    if context is None:
        return
    entries = _sdf_interconnect_entries(context[0])
    _raise_sdf_unresolvable_interconnect_instances(*context, entries=entries)
    _raise_sdf_unresolvable_cell_instances(*context, entries=entries)
