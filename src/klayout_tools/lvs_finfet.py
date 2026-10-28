"""FinFET (ASAP7) support for ``klt lvs`` (issue #2813, part of #2761).

``klt extract --deck asap7`` reports geometry-counted ``NFIN`` per device
(:mod:`klayout_tools.extract_finfet`). A FinFET compare that silently dropped
``NFIN`` would degrade into a planar ``W``/``L`` compare, so the pieces below
exist to make that impossible:

* :func:`validate_finfet_request` -- refuses every request shape/option that
  would relax, remove or bypass the exact ``NFIN`` comparison;
* :func:`make_finfet_reference_reader` -- a ``NetlistSpiceReader`` whose ``M``
  cards build the *same* device classes the extractor builds
  (``extract_finfet.finfet_device_class``), carrying a validated ``NFIN``;
* :func:`configure_finfet_comparison` -- ``NFIN`` compared exactly, ``L`` to
  an absolute :data:`L_TOLERANCE_UM`, on both sides;
* :func:`apply_finfet_bulk` -- the opt-in, disclosed comparison-side bulk
  policy (``options.finfet_bulk``): ASAP7 standard cells draw no taps while
  the reference CDL ties NMOS bulk to ``VSS`` and PMOS bulk to ``VDD``.

No symmetric-stack / series-order normalisation is performed: the cells whose
drawn stack topology differs from the reference are reported as mismatches
and documented as unsupported (``docs/cli/lvs.md``). In particular
``NetlistComparer.join_symmetric_nets`` is never used -- it over-merges ICG
cells and segfaulted KLayout 0.30.12.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import klayout.db as kdb

#: Absolute tolerance (um) on ``L``. Reference ``l=20n`` is parsed to metres
#: and rescaled to um, which differs from the extractor's value only by float
#: noise; 1e-5 um (0.01 nm) is far below any real gate-length difference.
L_TOLERANCE_UM = 1e-5

#: ``options.finfet_bulk`` polarities, and the device-class-name prefix that
#: selects them (ASAP7 classes are ``nmos_<vt>`` / ``pmos_<vt>``).
BULK_POLARITIES = ("nmos", "pmos")

#: Options that would relax/remove the exact NFIN compare, or that rewrite the
#: devices on one side only.
_REFUSED_OPTIONS = (
    ("compare_parameters", "can disable the NFIN comparison"),
    (
        "parameter_tolerance",
        "snaps parameters, which would relax the exact NFIN comparison",
    ),
    (
        "combine_devices",
        "re-combines devices; FinFET fingers are always merged by the extractor",
    ),
    (
        "combine_devices_per_circuit",
        "re-combines devices; FinFET fingers are always merged by the extractor",
    ),
)


def _error(message: str) -> Exception:
    from .lvs import LvsError

    return LvsError(message)


def is_finfet_layout(layout_spec: Mapping[str, Any]) -> bool:
    from .extract_finfet import is_finfet_deck_name

    return is_finfet_deck_name(layout_spec.get("deck"))


def parse_finfet_bulk(options: Mapping[str, Any]) -> dict[str, str] | None:
    """``options.finfet_bulk`` -> ``{"nmos": <layout net>, "pmos": <layout
    net>}`` (either key optional) or ``None`` when omitted/``null``."""
    value = options.get("finfet_bulk")
    if value is None:
        return None
    if (
        not isinstance(value, dict)
        or not value
        or any(key not in BULK_POLARITIES for key in value)
        or any(not isinstance(name, str) or not name.strip() for name in value.values())
    ):
        raise _error(
            "options.finfet_bulk must be a non-empty JSON object mapping "
            f"{' and/or '.join(repr(p) for p in BULK_POLARITIES)} to the "
            "layout net name the device bulk is asserted to sit on, e.g. "
            '{"nmos": "VSS", "pmos": "VDD"}'
        )
    return {key: value[key].strip() for key in BULK_POLARITIES if key in value}


def validate_finfet_request(
    layout_spec: Mapping[str, Any],
    reference_spec: Mapping[str, Any],
    options: Mapping[str, Any],
    engine: str,
) -> None:
    """Refuse every request that would compare a FinFET layout without the
    exact ``NFIN`` compare. Called only for a FinFET ``layout.deck``."""
    deck = layout_spec.get("deck")
    if engine != "klayout":
        raise _error(
            f"layout.deck '{deck}' is a FinFET extraction deck; only "
            "engine 'klayout' compares NFIN exactly (the netgen engine would "
            "compare planar W/L)"
        )
    if "netlist" in layout_spec:
        raise _error(
            f"layout.deck '{deck}' is a FinFET extraction deck; klt lvs "
            "supports it only with inline extraction (layout.file) -- a "
            "pre-extracted layout.netlist is read by the planar reader, which "
            "would drop NFIN"
        )
    form = reference_spec.get("form", "plain-element")
    if form not in ("plain-element", "gate-level-verilog"):
        raise _error(
            f"layout.deck '{deck}' is a FinFET extraction deck; "
            f"reference.form {form!r} is not supported (use 'plain-element' "
            "CDL/SPICE M cards carrying nfin=, or 'gate-level-verilog' with "
            "reference.library to expand library cells from the PDK CDL)"
        )
    for key in ("device_map", "device_bulk"):
        if reference_spec.get(key):
            raise _error(
                f"layout.deck '{deck}' is a FinFET extraction deck; "
                f"reference.{key} is not supported"
            )
    for name, why in _REFUSED_OPTIONS:
        if options.get(name):
            raise _error(
                f"layout.deck '{deck}' is a FinFET extraction deck; "
                f"options.{name} is not supported because it {why}"
            )


def _checked_nfin(value: Any, card: str) -> float:
    if value is None:
        raise _error(f"FinFET device card {card} has no nfin= parameter")
    try:
        nfin = float(value)
    except (TypeError, ValueError):
        raise _error(
            f"FinFET device card {card}: nfin={value!r} is not a number"
        ) from None
    if not math.isfinite(nfin) or nfin <= 0 or nfin != int(nfin):
        raise _error(
            f"FinFET device card {card}: nfin={value!r} must be a positive "
            "integer fin count"
        )
    return nfin


def _checked_positive(params: Mapping[str, Any], key: str, card: str) -> float:
    value = params.get(key)
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise _error(
            f"FinFET device card {card} has no valid {key.lower()}= parameter"
        ) from None
    if not math.isfinite(number) or number <= 0:
        raise _error(
            f"FinFET device card {card}: {key.lower()}={value!r} must be positive"
        )
    return number


def make_finfet_reference_reader() -> kdb.NetlistSpiceReader:
    """A ``NetlistSpiceReader`` that builds FinFET device classes for ``M``
    cards. ``L``/``W`` arrive SI-scaled (metres) and are converted to um, the
    unit the extractor reports; ``NFIN`` is validated, never defaulted or
    rounded; ``FINGERS`` is 1 per card (extraction metadata, not a fin-count
    multiplier). Model identity is preserved (lower-cased, like the
    extractor's class names)."""
    import klayout.db as kdb

    from .extract_finfet import finfet_device_class

    # One class object per model for the whole read: Netlist.combine_devices
    # groups by class identity, and device_class_by_name is a slow linear
    # lookup on a large library netlist.
    registry: dict[str, Any] = {}

    class _FinFETReferenceDelegate(kdb.NetlistSpiceReaderDelegate):
        def element(self, circuit, element, name, model, value, nets, params):
            if element != "M":
                return super().element(
                    circuit, element, name, model, value, nets, params
                )
            card = f"M{name} (model {model!r}) in circuit {circuit.name!r}"
            if not model:
                raise _error(f"FinFET device card {card} names no model")
            if len(nets) != 4:
                raise _error(
                    f"FinFET device card {card} has {len(nets)} nets; "
                    "a 4-terminal d g s b MOSFET is required"
                )
            length = _checked_positive(params, "L", card)
            width = _checked_positive(params, "W", card)
            nfin = _checked_nfin(params.get("NFIN"), card)
            netlist = circuit.netlist()
            cls = registry.get(model.lower())
            if cls is None:
                cls = finfet_device_class(model.lower())
                netlist.add(cls)
                registry[model.lower()] = cls
            device = circuit.create_device(cls, name)
            for terminal, net in zip(("D", "G", "S", "B"), nets, strict=True):
                device.connect_terminal(cls.terminal_id(terminal), net)
            device.set_parameter("L", length * 1e6)
            device.set_parameter("W", width * 1e6)
            device.set_parameter("NFIN", nfin)
            device.set_parameter("FINGERS", 1.0)
            return True

    return kdb.NetlistSpiceReader(_FinFETReferenceDelegate())


def combine_reference_fingers(netlist: kdb.Netlist) -> None:
    """Merge exactly-parallel reference devices with the extractor's own
    combiner, so both sides count fins the same way."""
    netlist.combine_devices()


def configure_finfet_comparison(*netlists: kdb.Netlist) -> list[str]:
    """Compare ``NFIN`` exactly and ``L`` to :data:`L_TOLERANCE_UM` for every
    FinFET device class on every given netlist. Returns the configured class
    names; raises when a class named like a FinFET model lacks ``NFIN`` on any
    side (which would make the compare planar)."""
    import klayout.db as kdb

    configured: set[str] = set()
    for netlist in netlists:
        for device_class in netlist.each_device_class():
            if not isinstance(device_class, kdb.DeviceClassMOS4Transistor):
                continue
            if not device_class.has_parameter("NFIN"):
                raise _error(
                    f"device class '{device_class.name}' carries no NFIN "
                    "parameter; refusing a planar W/L compare of a FinFET layout"
                )
            # W is derived from NFIN for a FinFET (nfin x fin pitch) and is not
            # an independent size: only NFIN and L are compared. Clearing the
            # primary flag keeps the comparer's device ordering consistent
            # with the explicit comparison below.
            for name in ("W", "AS", "AD", "PS", "PD"):
                if device_class.has_parameter(name):
                    device_class.enable_parameter(name, False)
            device_class.equal_parameters = kdb.EqualDeviceParameters(
                device_class.parameter_id("NFIN"), 0.0, 0.0
            ) + kdb.EqualDeviceParameters(
                device_class.parameter_id("L"), L_TOLERANCE_UM, 0.0
            )
            configured.add(device_class.name)
    return sorted(configured)


def apply_finfet_bulk(
    circuit: kdb.Circuit, spec: Mapping[str, str] | None
) -> list[dict[str, Any]]:
    """Apply ``options.finfet_bulk`` to the *layout* circuit and return one
    disclosure record per requested polarity.

    Per polarity, the bulk terminals of every device whose class name starts
    with that polarity must land on exactly one net (the synthesized
    substrate or drawn well); that net is joined with the single net the
    caller named. Absent or ambiguous (several same-named) target nets, and
    bulk terminals spread over several nets, are request errors -- never a
    silent tie. Geometry is untouched; only the comparison netlist changes.
    """
    records: list[dict[str, Any]] = []
    if not spec:
        return records
    available = sorted({net.name for net in circuit.each_net() if net.name})
    for polarity, target_name in spec.items():
        devices = [
            device
            for device in circuit.each_device()
            if device.device_class().name.lower().startswith(polarity)
            and device.device_class().has_terminal("B")
        ]
        record: dict[str, Any] = {
            "polarity": polarity,
            "net": target_name,
            "device_count": len(devices),
            "device_classes": sorted({d.device_class().name for d in devices}),
            "bulk_net": None,
            "status": "no_devices",
        }
        if devices:
            bulk_nets = {}
            for device in devices:
                net = device.net_for_terminal(device.device_class().terminal_id("B"))
                if net is None:
                    raise _error(
                        f"options.finfet_bulk['{polarity}']: a {polarity} "
                        f"device ({device.expanded_name()}) has an unconnected "
                        "bulk terminal; refusing to tie it by policy"
                    )
                bulk_nets[net.cluster_id if net.cluster_id else id(net)] = net
            if len(bulk_nets) > 1:
                names = sorted(n.expanded_name() for n in bulk_nets.values())
                raise _error(
                    f"options.finfet_bulk['{polarity}']: the {polarity} bulk "
                    f"terminals land on {len(bulk_nets)} different nets "
                    f"({', '.join(names)}); the bulk policy only ties a single "
                    "unambiguous bulk net -- draw taps or fix the layout"
                )
            bulk_net = next(iter(bulk_nets.values()))
            named = [n for n in circuit.each_net() if n.name == target_name]
            # The bulk net itself may already carry the name (a labeled well);
            # the join partner is then the *other* net of that name, if any.
            others = [
                n
                for n in named
                if n.cluster_id != bulk_net.cluster_id
                or (n.expanded_name() != bulk_net.expanded_name())
            ]
            if not named:
                shown = ", ".join(available) or "(none)"
                raise _error(
                    f"options.finfet_bulk['{polarity}']: layout net "
                    f"'{target_name}' not found in circuit '{circuit.name}' "
                    f"(named nets: {shown})"
                )
            if len(others) > 1:
                raise _error(
                    f"options.finfet_bulk['{polarity}']: layout net name "
                    f"'{target_name}' is ambiguous ({len(others)} distinct "
                    "nets besides the bulk net carry it); fix the supply "
                    "fragmentation first -- the bulk policy never merges "
                    "several nets by name"
                )
            record["bulk_net"] = bulk_net.expanded_name()
            if not others:
                record["status"] = "already_connected"
            else:
                circuit.join_nets(others[0], bulk_net)
                record["status"] = "joined"
        records.append(record)
    return records


def bulk_disclosures(
    records: list[dict[str, Any]], category: str
) -> list[dict[str, Any]]:
    """``severity: "warning"`` ``mismatches[]`` entries, one per polarity, so
    a ``"match"`` reached under the policy is never silent."""
    from .lvs_mismatch import _mismatch

    entries = []
    for record in records:
        if record["status"] == "joined":
            action = (
                f"joined the {record['polarity']} bulk net "
                f"'{record['bulk_net']}' onto layout net '{record['net']}'"
            )
        elif record["status"] == "already_connected":
            action = (
                f"found the {record['polarity']} bulk already on layout net "
                f"'{record['net']}' (no join needed)"
            )
        else:
            action = f"found no {record['polarity']} devices to tie"
        entries.append(
            _mismatch(
                category,
                "warning",
                f"options.finfet_bulk {action} on {record['device_count']} "
                "device(s) before comparing -- the bulk connectivity is a "
                "caller assertion, not geometry read from the layout",
                "layout",
                details=dict(record),
            )
        )
    return entries


def slice_spice_to_top(text: str, top: str | None) -> str:
    """Restrict a multi-cell SPICE/CDL library to ``top`` and the subcircuits
    it (transitively) names, dropping every other ``.SUBCKT`` block.

    A standard-cell library holds hundreds of cells, and reading all of them
    through the Python reader delegate costs tens of seconds per run for a
    compare that looks at one. Lines outside ``.SUBCKT``/``.ENDS`` (comments,
    ``.GLOBAL``, ...) are kept verbatim. The text is returned unchanged when
    ``top`` is absent or not defined in ``text`` (so the normal "top circuit
    not found" error path still applies), or when the blocks are not
    well-formed.
    """
    if not top:
        return text
    lines = text.splitlines(keepends=True)
    blocks: dict[str, list[str]] = {}
    order: list[tuple[str, list[str]]] = []  # (name or "", lines)
    current: list[str] | None = None
    current_name = ""
    outside: list[str] = []
    for line in lines:
        stripped = line.strip()
        upper = stripped.upper()
        if current is None:
            if upper.startswith(".SUBCKT"):
                parts = stripped.split()
                if len(parts) < 2:
                    return text
                current_name = parts[1].upper()
                current = [line]
            else:
                outside.append(line)
        else:
            current.append(line)
            if upper.startswith(".ENDS"):
                blocks[current_name] = current
                order.append((current_name, current))
                current = None
    if current is not None or top.upper() not in blocks:
        return text
    wanted: set[str] = set()
    pending = [top.upper()]
    while pending:
        name = pending.pop()
        if name in wanted:
            continue
        wanted.add(name)
        tokens = {tok.upper() for line in blocks[name][1:] for tok in line.split()}
        pending.extend(other for other in blocks if other in tokens and other != name)
    kept = [line for name, block in order if name in wanted for line in block]
    return "".join(outside + kept)


def prepare_finfet_request(
    layout_spec: Mapping[str, Any],
    reference_spec: Mapping[str, Any],
    options: Mapping[str, Any],
    engine: str,
) -> tuple[bool, dict[str, str] | None]:
    """Request-time FinFET handling for ``run_lvs``: returns ``(is_finfet,
    bulk policy)``. Refuses every unsupported shape/option for a FinFET deck
    and an ``options.finfet_bulk`` on a non-FinFET deck."""
    finfet = is_finfet_layout(layout_spec)
    if finfet:
        validate_finfet_request(layout_spec, reference_spec, options, engine)
    bulk = parse_finfet_bulk(options)
    if bulk is not None and not finfet:
        raise _error("options.finfet_bulk requires a FinFET layout.deck (e.g. asap7)")
    return finfet, bulk


def prepare_finfet_comparison(
    finfet: bool,
    layout_netlist: kdb.Netlist,
    layout_circuit: kdb.Circuit,
    reference_netlist: kdb.Netlist,
    bulk: Mapping[str, str] | None,
) -> list[dict[str, Any]]:
    """Comparison-time FinFET setup (a no-op returning ``[]`` for a planar
    deck): merge reference fingers with the extractor's combiner, apply the
    opt-in bulk policy to the layout circuit, and configure exact ``NFIN``
    on both sides. Returns the bulk-policy disclosure records."""
    if not finfet:
        return []
    combine_reference_fingers(reference_netlist)
    records = apply_finfet_bulk(layout_circuit, bulk)
    configure_finfet_comparison(layout_netlist, reference_netlist)
    return records


def sliced_reference_file(
    finfet: bool,
    top: str | None,
    read_path: str,
    read_text: str,
    tmp_path: str | None,
) -> tuple[str, str, str | None]:
    """For a FinFET reference, write ``read_text`` restricted to ``top`` (see
    :func:`slice_spice_to_top`) to a temporary file; otherwise return the
    inputs unchanged. Returns ``(read_path, read_text, tmp_path)``."""
    if not finfet:
        return read_path, read_text, tmp_path
    sliced = slice_spice_to_top(read_text, top)
    if sliced is read_text:
        return read_path, read_text, tmp_path
    import tempfile

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".spice", delete=False, encoding="utf-8"
    ) as tmp:
        tmp.write(sliced)
    return tmp.name, sliced, tmp.name


def reference_reader(finfet: bool, default_factory: Any) -> kdb.NetlistSpiceReader:
    """The FinFET (nfin-preserving) reader, else ``default_factory()``."""
    return make_finfet_reference_reader() if finfet else default_factory()


def finfet_bulk_replay_options(echoed: Mapping[str, Any]) -> dict[str, Any]:
    """``{"finfet_bulk": {...}}`` when the committed report echoed a policy."""
    value = echoed.get("finfet_bulk")
    return {"finfet_bulk": dict(value)} if isinstance(value, dict) else {}
