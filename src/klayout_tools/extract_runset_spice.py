"""SPICE card writing for ``klt extract --engine klayout-runset`` (issue
#5481): every extracted device becomes an ``X`` subcircuit call.

A runset's device-class names *are* the PDK's own device names (``pnpMPA``,
``ptap1``, ``sg13_lv_nmos``, ...), and an open PDK's ngspice library declares
those as **subcircuits** -- IHP's ``sg13cmos5l_pnpMPA_mod.lib:38`` is
``.subckt pnpMPA c b e``, ``resistors_mod.lib:23`` is ``.subckt ptap1 1 2``.
KLayout's default primitive writer instead emits ``Q$1 ... pnpMPA`` and
``R$2 ... ptap1``, whose trailing token ngspice reads as a ``.model``
reference no PDK model library declares; neither card binds.

So the delegate below writes **every** extracted device as an ``X`` call onto
the class's own name, in the class's own terminal order (``C B E`` for
``pnpMPA``, matching the subcircuit's declared ``c b e``). ``--device-model
CLASS=SUBCKT`` renames the target for a class whose PDK spelling differs --
the same flag the curated engine uses for its own unbound bipolar classes
(issue #4234), generalised here to any class because under a runset there is
no curated binding table to consult at all. A class the PDK model library does
not declare fails loudly in the simulator as an unknown subcircuit rather than
binding to something wrong.

**Measured parameters are not carried onto the call.** KLayout reports a
device's geometry in micron units (``A=2`` um^2, ``P=6`` um for the ``pnpMPA``
testcase) while the PDK subcircuit declares its own parameters in SI
(``.param a=2p p=6u``), and the parameter *names* differ per class and per
PDK. Guessing a unit rescale per device family is exactly the per-PDK
workaround that disqualified ``tests/helpers/extract_klayout_oracle.py`` as
the basis for this engine, so the ``X`` call is written parameter-free and the
subcircuit's own declared defaults apply. This is **disclosed, never silent**:
every dropped parameter is named per class in ``warnings[]`` (issue #695's
rule), and ``devices[].params`` in the JSON report still carries every
measured value the report has a field for.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .extract_runset_flags import RUNSET_ENGINE


def _runset_spice_writer_delegate(
    kdb: Any, device_models: Mapping[str, str], dropped: dict[str, set[str]]
) -> Any:
    """``kdb.NetlistSpiceWriterDelegate`` writing every device as an ``X``
    subcircuit call onto its own class name (or ``device_models``' override).

    Records each class's un-carried measured parameter names in ``dropped``
    so the caller can name them in ``warnings[]``. A device with a dangling
    terminal, or a class with no name, is left to KLayout's own writer rather
    than turned into an ``X`` call with a missing node.
    """

    class _RunsetSpiceWriterDelegate(kdb.NetlistSpiceWriterDelegate):
        def write_device(self, device: Any) -> None:
            device_class = device.device_class()
            class_name = device_class.name
            if not class_name:
                super().write_device(device)
                return

            terminals = list(device_class.terminal_definitions())
            nets = [device.net_for_terminal(t.id()) for t in terminals]
            if not terminals or any(net is None for net in nets):
                super().write_device(device)
                return

            subckt = device_models.get(class_name, class_name)
            params = [p.name for p in device_class.parameter_definitions()]
            if params:
                dropped.setdefault(class_name, set()).update(params)

            name = self.format_name(device.expanded_name())
            pins = " ".join(self.net_to_string(net) for net in nets)
            self.emit_line(f"X{name} {pins} {subckt}")

    return _RunsetSpiceWriterDelegate()


def _dropped_parameter_warnings(
    dropped: Mapping[str, set[str]], device_models: Mapping[str, str]
) -> list[str]:
    """One warning per device class whose measured parameters the written
    ``X`` call does not carry (issue #695: naming them is the contract)."""
    warnings: list[str] = []
    for class_name in sorted(dropped):
        params = ", ".join(sorted(dropped[class_name]))
        subckt = device_models.get(class_name, class_name)
        warnings.append(
            f"device class '{class_name}' is written as an X call onto "
            f"subcircuit '{subckt}' with no parameters: the measured "
            f"{params} {'is' if len(dropped[class_name]) == 1 else 'are'} not "
            "carried onto the card, so the subcircuit's own declared defaults "
            "apply (every measured value this report has a field for is in "
            "devices[].params). The "
            f"{RUNSET_ENGINE} engine does not guess a per-PDK unit rescale "
            "or parameter spelling -- see docs/cli/extract.md, 'Engine'"
        )
    return warnings
