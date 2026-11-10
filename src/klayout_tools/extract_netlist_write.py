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

import shutil
from typing import Any


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
        _apply_pin_order_to_spice,
        _apply_pin_ties_if_circuit,
        _finish_netlist_write,
        _model_delegate_factory,
        _parasitic_model_header_comment,
        _substitution_notes,
    )

    # Issue #2761: a FinFET deck gets its own card writer (`L`/`W`/`NFIN`,
    # the PDK reference CDL's parameter set); every other deck keeps the
    # model-binding delegate exactly as before.
    from .extract_finfet import spice_writer_delegate_for
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
            netlist.write(netlist_path, writer, netlist_description)
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
