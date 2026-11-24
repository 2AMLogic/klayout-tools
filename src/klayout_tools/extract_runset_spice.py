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

**By default, measured parameters are not carried onto the call.** KLayout
reports a device's geometry in micron units (``A=2`` um^2, ``P=6`` um for the
``pnpMPA`` testcase) while the PDK subcircuit declares its own parameters in SI
(``.param a=2p p=6u``), and the parameter *names* differ per class and per
PDK. Guessing a unit rescale per device family is exactly the per-PDK
workaround that disqualified ``tests/helpers/extract_klayout_oracle.py`` as
the basis for this engine, so the ``X`` call is written parameter-free and the
subcircuit's own declared defaults apply. This is **disclosed, never silent**:
every dropped parameter is named per class in ``warnings[]`` (issue #695's
rule), and ``devices[].params`` in the JSON report still carries every
measured value the report has a field for.

**``--device-param`` carries them when the caller says how** (issue #5513):
``CLASS:PARAM=NAME[:SUFFIX]`` appends ``NAME=<value><SUFFIX>`` to every call of
``CLASS``, where ``<value>`` is the measured KLayout parameter, unscaled. The
caller states the (name, unit suffix) pair -- a PDK runset's own writer does
the same, one name-keyed suffix per parameter and no arithmetic -- so nothing
here is a built-in per-PDK table. Only what is *still* uncarried is then
disclosed, and a netlist that drops nothing carries no disclosure at all.
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Mapping
from typing import Any

from .extract_netlist_target import _finish_netlist_write, _resolve_netlist_target
from .extract_runset_flags import RUNSET_ENGINE
from .extract_spef import ExtractError

#: ``{device class: {KLayout parameter: (SPICE parameter name, unit suffix)}}``
#: -- the shape :func:`.extract_runset_flags.parse_device_params` produces.
DeviceParams = Mapping[str, Mapping[str, tuple[str, str]]]


def _x_call_of(device: Any) -> tuple[Any, list[Any]] | None:
    """``(device class, connected nets)`` for a device written as an ``X``
    call, or ``None`` for one left to KLayout's own writer: a class with no
    name, or a device with a dangling terminal, which cannot become an ``X``
    call without a missing node. The one predicate the writer and the
    pre-write survey share, so what is reported dropped is what is dropped."""
    device_class = device.device_class()
    if not device_class.name:
        return None
    terminals = list(device_class.terminal_definitions())
    nets = [device.net_for_terminal(t.id()) for t in terminals]
    if not terminals or any(net is None for net in nets):
        return None
    return device_class, nets


def _runset_spice_writer_delegate(
    kdb: Any, device_models: Mapping[str, str], device_params: DeviceParams
) -> Any:
    """``kdb.NetlistSpiceWriterDelegate`` writing every device as an ``X``
    subcircuit call onto its own class name (or ``device_models``' override),
    carrying the parameters ``device_params`` declares for its class."""

    class _RunsetSpiceWriterDelegate(kdb.NetlistSpiceWriterDelegate):
        def write_device(self, device: Any) -> None:
            call = _x_call_of(device)
            if call is None:
                super().write_device(device)
                return
            device_class, nets = call
            subckt = device_models.get(device_class.name, device_class.name)
            carried = "".join(
                f" {name}={device.parameter(param):.12g}{suffix}"
                for param, (name, suffix) in sorted(
                    device_params.get(device_class.name, {}).items()
                )
            )
            name = self.format_name(device.expanded_name())
            pins = " ".join(self.net_to_string(net) for net in nets)
            self.emit_line(f"X{name} {pins} {subckt}{carried}")

    return _RunsetSpiceWriterDelegate()


def _uncarried_parameters(
    netlist: Any, device_params: DeviceParams
) -> dict[str, set[str]]:
    """Per device class written as an ``X`` call, the measured parameter names
    the call will **not** carry -- empty when every class carries everything.

    Surveyed before the write rather than recorded during it, because the
    netlist header has to say whether anything is dropped before the first
    card is written."""
    uncarried: dict[str, set[str]] = {}
    for circuit in netlist.each_circuit():
        for device in circuit.each_device():
            call = _x_call_of(device)
            if call is None:
                continue
            name = call[0].name
            left = {p.name for p in call[0].parameter_definitions()}
            left -= set(device_params.get(name, ()))
            if left:
                uncarried.setdefault(name, set()).update(left)
    return uncarried


def _check_device_params(netlist: Any, device_params: DeviceParams) -> list[str]:
    """Validate ``--device-param`` against the classes the runset actually
    extracted. A parameter its class does not define is an
    :class:`ExtractError` -- a typo would otherwise leave a device silently at
    its default size. A class the netlist has no such device class for cannot
    be checked and carries nothing, so that is returned as a warning."""
    classes = {c.name: c for c in netlist.each_device_class()}
    warnings: list[str] = []
    for class_name in sorted(device_params):
        device_class = classes.get(class_name)
        if device_class is None:
            warnings.append(
                f"--device-param declares class '{class_name}', but the runset's "
                "extracted netlist has no device class of that name (classes: "
                f"{', '.join(sorted(classes)) or '(none)'}), so nothing was "
                "carried for it"
            )
            continue
        defined = sorted(p.name for p in device_class.parameter_definitions())
        unknown = sorted(set(device_params[class_name]) - set(defined))
        if unknown:
            raise ExtractError(
                f"--device-param names {', '.join(unknown)} for class "
                f"'{class_name}', which it does not define (it defines: "
                f"{', '.join(defined) or '(none)'}). Parameter names are "
                "case-sensitive and are the ones devices[].params reports"
            )
    return warnings


def _device_params_echo(device_params: DeviceParams) -> dict[str, Any]:
    """The report's additive ``device_params`` field -- present only when a
    declaration was given, so a report without one is byte-identical to one
    produced before ``--device-param`` existed (issue #5513)."""
    if not device_params:
        return {}
    return {
        "device_params": {
            class_name: {
                param: {"name": name, "suffix": suffix}
                for param, (name, suffix) in sorted(params.items())
            }
            for class_name, params in sorted(device_params.items())
        }
    }


def _netlist_description(
    deck_basename: str, uncarried: Mapping[str, set[str]], declared: bool
) -> str:
    """The netlist header. Only the *first* description line is comment-
    prefixed by ``NetlistSpiceWriter``; every continuation line carries its own
    ``* `` or ngspice reads it as a card (same convention as ``extract.py``'s
    sub-circuit header). The no-parameters line is written only when a card
    actually drops something (issue #5513)."""
    description = (
        f"extracted by klt extract --engine {RUNSET_ENGINE} --deck-file "
        f"{deck_basename}\n"
        "* no parasitics: this engine extracts devices and connectivity only"
    )
    if uncarried and not declared:
        description += (
            "\n* no device parameters: each device is an X call onto its "
            "class's subcircuit with no parameters, so the subcircuit's own "
            "declared defaults apply -- the measured geometry is in the "
            "report's devices[].params, and warnings[] names it per class"
        )
    elif uncarried:
        description += (
            "\n* some device parameters are not carried: an X call carries "
            "only what --device-param declared for its class, so the "
            "subcircuit's own declared defaults apply for the rest -- "
            "warnings[] names them per class"
        )
    return description


def _prepare_merged_node_names(circuit: Any) -> list[dict[str, Any]]:
    """Give the SPICE writer and the appended R/C cards one node namespace.

    KLayout prints a comma as a pipe but escapes a literal pipe. Dots in
    reports become underscores. Resolve these differences on the actual nets
    before writing and reserve names case-insensitively, including duplicates.
    The saved LVS database keeps the original labels and cluster IDs.
    """
    from .extract_parasitics import spice_safe_net_name

    candidates = []
    for net in circuit.each_net():
        raw = net.expanded_name()
        candidate = re.sub(
            r"[^a-zA-Z0-9_$,]",
            lambda match: f"_{ord(match[0]):x}_",
            raw.replace(".", "_").replace("|", ","),
        )
        if candidate.startswith(","):
            candidate = "n_" + candidate
        candidates.append((net, raw, candidate))
    # An existing spelling has priority over a label that normalizes onto it.
    candidates.sort(key=lambda row: (row[1] != row[2], row[0].cluster_id))
    reserved = {candidate.casefold() for _, _, candidate in candidates}
    used = set()
    aliases = []
    for net, raw, candidate in candidates:
        node = candidate
        if candidate.casefold() in used:
            suffix = 1
            while f"{candidate}_{suffix}".casefold() in reserved:
                suffix += 1
            node = f"{candidate}_{suffix}"
            reserved.add(node.casefold())
        used.add(node.casefold())
        if node != raw:
            net.name = node
            aliases.append(
                {
                    "runset_cluster_id": net.cluster_id,
                    "original": raw,
                    "node": spice_safe_net_name(node),
                }
            )
    return aliases


def _write_netlist(
    *,
    netlist: Any,
    path: str,
    output: str | None,
    no_netlist: bool,
    deck_basename: str,
    models: Mapping[str, str],
    device_params: DeviceParams,
    uncarried: Mapping[str, set[str]],
) -> tuple[str | None, str | None]:
    """Write ``netlist`` as SPICE through the X-call delegate; return
    ``(reported netlist_path, netlist_sha256)``, both ``None`` under
    ``no_netlist``."""
    import klayout.db as kdb

    from .extract_netlist_write import write_spice_netlist
    from .extract_progress import stage

    netlist_path, scratch_dir = _resolve_netlist_target(path, output, no_netlist)
    writer = kdb.NetlistSpiceWriter(
        _runset_spice_writer_delegate(kdb, models, device_params)
    )
    writer.use_net_names = True
    description = _netlist_description(deck_basename, uncarried, bool(device_params))
    try:
        with stage("netlist_write"):
            write_spice_netlist(netlist, netlist_path, writer, description)
    except Exception as exc:  # one error envelope
        if scratch_dir is not None:
            shutil.rmtree(scratch_dir, ignore_errors=True)
        raise ExtractError(f"could not write netlist '{netlist_path}': {exc}") from exc
    with stage("netlist_hash"):
        return _finish_netlist_write(netlist_path, scratch_dir)


def _dropped_parameter_warnings(
    uncarried: Mapping[str, set[str]],
    device_models: Mapping[str, str],
    device_params: DeviceParams,
) -> list[str]:
    """One warning per device class whose measured parameters the written
    ``X`` call does not carry (issue #695: naming them is the contract). A
    class with every parameter declared is not in ``uncarried``, so it gets
    none."""
    warnings: list[str] = []
    for class_name in sorted(uncarried):
        params = ", ".join(sorted(uncarried[class_name]))
        subckt = device_models.get(class_name, class_name)
        carried = ", ".join(sorted(device_params.get(class_name, ())))
        lead = (
            f"carrying only the --device-param declarations for {carried}"
            if carried
            else "with no parameters"
        )
        warnings.append(
            f"device class '{class_name}' is written as an X call onto "
            f"subcircuit '{subckt}' {lead}: the measured "
            f"{params} {'is' if len(uncarried[class_name]) == 1 else 'are'} not "
            "carried onto the card, so the subcircuit's own declared defaults "
            "apply (every measured value this report has a field for is in "
            "devices[].params). The "
            f"{RUNSET_ENGINE} engine does not guess a per-PDK unit rescale "
            "or parameter spelling -- see docs/cli/extract.md, 'Engine'"
        )
    return warnings
