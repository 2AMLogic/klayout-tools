"""``klt extract`` flat netlist-write block (issue #5196, stage 6).

Moved verbatim out of ``run_extract``: writer/delegate construction, the
netlist description header, ``netlist.write`` (with scratch-dir cleanup on
failure), narrow-width substitution notes, pin ties and the hash/finish step.
Returns ``(reported_netlist_path, netlist_sha256, pin_ties)``.

``warnings`` is mutated in place, exactly as the inline block did. The helpers
it calls that still live in ``extract.py`` are imported inside the function
body: ``extract.py`` imports this module at its top level, so a module-level
``from .extract import ...`` would be a circular import.
"""

from __future__ import annotations

import re
import shutil
from typing import Any

#: KLayout's stock ``NetlistSpiceWriter`` (``use_net_names`` on; reported on
#: 0.30.12, reproduced on 0.30.10, issue #6732) throws from inside ``Netlist.write`` --
#: before it calls any delegate hook, so no delegate can intercept it -- for a
#: net whose name *starts with* lowercase ``nc_`` unless the rest is a number
#: (``nc_a``, ``nc_ab``, ``nc_a,vss`` fail; ``nc_1``, ``NC_a``, ``nca``,
#: ``a_nc`` and ``vss,nc_a`` write fine). Only net names are scanned: a pin or
#: circuit named ``nc_a`` is accepted.
_NC_NET_PREFIX = "nc_"
_NC_WRITE_ERROR = "Unexpected text after numeric value"

#: Stem of the placeholder that stands in for ``nc_`` while the writer runs.
_NC_PLACEHOLDER_STEM = "klt_nc"


def _substrate_tie_header_line(requested_ohms: float) -> str:
    """The ``* - substrate tie: ...`` header line (issue #3089).

    Moved verbatim from ``extract.py`` (issue #6899 pays for its emission
    growth with the move; behavior unchanged); the extract-side tie helpers
    are imported lazily, the same direction this module's writer already
    imports everything else from ``extract``.
    """
    from .extract import substrate_tie_mode, substrate_tie_written_ohms

    written = substrate_tie_written_ohms(requested_ohms)
    if substrate_tie_mode(requested_ohms) == "hard":
        return (
            f"* - substrate tie: hard tie to ground ({written:g} ohm; "
            "SPICE has no zero-ohm resistor) -- the substrate node is "
            "DC-fixed at 0 V in transient analysis"
        )
    from .extract import SUBSTRATE_DC_TIE_RESISTANCE_OHM

    if requested_ohms >= SUBSTRATE_DC_TIE_RESISTANCE_OHM:
        consequence = (
            "the substrate is capacitively floating in transient analysis "
            "(DC reference only); see --substrate-tie-ohms"
        )
    else:
        consequence = (
            "the substrate is held near 0 V in transient analysis through "
            "this resistance"
        )
    return f"* - substrate tie: {written:g} ohm to ground -- {consequence}"


def _parasitic_model_header_comment(substrate_tie_ohms: float | None = None) -> str:
    """Render ``PARASITIC_MODEL_SCOPE`` as `*`-prefixed SPICE comment
    lines for the written netlist's header (issue #728).

    ``kdb.Netlist.write``'s ``description`` argument only `*`-comments its
    *first* line -- every subsequent line is written verbatim, which would
    otherwise land as unprefixed text a SPICE reader could try to parse as a
    circuit element. Each line here is pre-prefixed with ``* `` so the whole
    block stays a comment regardless of how many lines it spans. Called only
    when ``--parasitics`` was given (``parasitics_report is not None`` in
    ``run_extract``) -- a netlist with no parasitics has nothing to declare
    the scope of.

    Moved verbatim from ``extract.py`` (issue #6899); ``substrate_tie_ohms``
    defaults lazily to ``SUBSTRATE_DC_TIE_RESISTANCE_OHM`` (every current
    caller passes it explicitly), and the extract-side names import lazily
    for the same circular-import reason this module's docstring records.
    """
    from .extract import (
        PARASITIC_MODEL_SCOPE,
        SUBSTRATE_DC_TIE_RESISTANCE_OHM,
    )

    if substrate_tie_ohms is None:
        substrate_tie_ohms = SUBSTRATE_DC_TIE_RESISTANCE_OHM
    lines = ["* parasitic model (--parasitics):"]
    for key, value in PARASITIC_MODEL_SCOPE.items():
        lines.append(f"* - {key}: {value}")
    lines.append(_substrate_tie_header_line(substrate_tie_ohms))
    return "\n".join(lines)


def _nc_prefixed_nets(netlist: Any) -> list[Any]:
    return [
        net
        for circuit in netlist.each_circuit()
        for net in circuit.each_net()
        if net.name.startswith(_NC_NET_PREFIX)
    ]


def _unused_placeholder_prefix(netlist: Any) -> str:
    """A ``klt_nc<n>_`` prefix no existing name in ``netlist`` starts with.

    The restore rewrites *every* token that starts with the prefix, so a net,
    pin, circuit, device or device-class name that already did would be
    corrupted along with the placeholders."""
    names = [
        name.casefold()
        for name in (
            *(cls.name for cls in netlist.each_device_class()),
            *(
                name
                for circuit in netlist.each_circuit()
                for name in (
                    circuit.name,
                    *(net.name for net in circuit.each_net()),
                    *(pin.name() for pin in circuit.each_pin()),
                    *(device.expanded_name() for device in circuit.each_device()),
                )
            ),
        )
        if name
    ]
    index = 0
    while any(name.startswith(f"{_NC_PLACEHOLDER_STEM}{index}_") for name in names):
        index += 1
    return f"{_NC_PLACEHOLDER_STEM}{index}_"


def _restore_nc_prefix(netlist_path: str, placeholder: str) -> None:
    """Rewrite the placeholder back to ``nc_`` in the written SPICE.

    Only the *prefix* was swapped, so the writer has already applied its own
    escaping to the rest of the name (``nc_a-b`` is spelled ``nc_a\\x2db``
    exactly as ``nc-a`` would be). A placeholder the pattern cannot reach (not
    at the start of a token) is an error, never a silently corrupted deck."""
    with open(netlist_path, encoding="utf-8", newline="") as handle:
        text = handle.read()
    restored = re.sub(rf"(?<!\S){re.escape(placeholder)}", _NC_NET_PREFIX, text)
    if placeholder in restored:
        raise RuntimeError(
            f"internal net-name placeholder '{placeholder}' remained in the "
            "written netlist after restoring net names that start with "
            f"'{_NC_NET_PREFIX}'"
        )
    with open(netlist_path, "w", encoding="utf-8", newline="") as handle:
        handle.write(restored)


def write_spice_netlist(
    netlist: Any, netlist_path: str, writer: Any, description: str
) -> None:
    """``netlist.write(netlist_path, writer, description)``, tolerating the
    stock writer's rejection of a net name starting with lowercase ``nc_``
    (issue #6732).

    The plain write is tried first, so every netlist that wrote before is
    written exactly as before. Only that one confirmed error -- with a net
    whose name starts with ``nc_`` present -- takes the fallback: those nets are
    renamed to an unused placeholder prefix, written, and the placeholder is put
    back in the written text. The real net names are restored in ``netlist``
    whether or not the write succeeds, so the report and every later pass still
    see the layout's own label. Any other write error propagates unchanged."""
    try:
        netlist.write(netlist_path, writer, description)
        return
    except Exception as exc:
        nc_nets = _nc_prefixed_nets(netlist)
        if _NC_WRITE_ERROR not in str(exc) or not nc_nets:
            raise

    placeholder = _unused_placeholder_prefix(netlist)
    originals = [(net, net.name) for net in nc_nets]
    for net, name in originals:
        net.name = placeholder + name[len(_NC_NET_PREFIX) :]
    try:
        netlist.write(netlist_path, writer, description)
    finally:
        for net, name in originals:
            net.name = name
    _restore_nc_prefix(netlist_path, placeholder)


def write_flat_netlist(
    *,
    kdb: Any,
    netlist: Any,
    circuit: Any,
    deck_name: Any,
    model_bindings: Any,
    substrate_global_nets: Any,
    parasitics_report: Any,
    substrate_tie_requested: Any,
    fixed_value_capacitor_note: Any,
    netlist_path: Any,
    scratch_dir: Any,
    pin_renames: Any,
    warnings: Any,
    pin_order: Any = (),
) -> tuple[Any, Any, Any]:
    from .extract import (
        ExtractError,
        _apply_pin_ties_if_circuit,
        _finish_netlist_write,
        _model_delegate_factory,
        _substitution_notes,
    )

    # Issue #2761: a FinFET deck gets its own card writer (`L`/`W`/`NFIN`,
    # the PDK reference CDL's parameter set); every other deck keeps the
    # model-binding delegate exactly as before.
    from .extract_finfet import spice_writer_delegate_for
    from .extract_pin_order import _apply_pin_order_to_spice
    from .extract_progress import stage

    # The model-binding delegate records any narrow-width fallback
    # substitution it makes (issue #4575); keep a handle to surface it below.
    model_delegates: list[Any] = []
    writer = kdb.NetlistSpiceWriter(
        spice_writer_delegate_for(
            deck_name,
            _model_delegate_factory(
                model_bindings, substrate_global_nets, model_delegates
            ),
        )
    )
    writer.use_net_names = True
    netlist_description = f"extracted by klt extract --deck {deck_name}"
    if parasitics_report is not None:
        netlist_description += "\n" + _parasitic_model_header_comment(
            substrate_tie_requested
        )
        netlist_description += "".join(
            "\n* " + note for note in fixed_value_capacitor_note
        )
    try:
        with stage("netlist_write"):
            write_spice_netlist(netlist, netlist_path, writer, netlist_description)
    except Exception as exc:
        if scratch_dir is not None:
            shutil.rmtree(scratch_dir, ignore_errors=True)
        raise ExtractError(f"could not write netlist '{netlist_path}': {exc}") from exc

    warnings.extend(_substitution_notes(model_delegates))

    pin_ties = (pin_renames or {}).get("ties") or []
    _apply_pin_ties_if_circuit(netlist_path, circuit, pin_ties)

    # Issue #5255: `--pins` order defines the top `.SUBCKT` pin order.
    if circuit is not None:
        _apply_pin_order_to_spice(netlist_path, circuit.name, pin_order)

    with stage("netlist_hash"):
        reported_netlist_path, netlist_sha256 = _finish_netlist_write(
            netlist_path, scratch_dir
        )
    return reported_netlist_path, netlist_sha256, pin_ties
