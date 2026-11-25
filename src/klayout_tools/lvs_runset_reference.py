"""Request validation and ``subckt-call`` reference conversion for the
``klayout-runset`` LVS engine (issue #4805).

Split out of ``lvs_runset`` to keep that module under its file-size ceiling.
``lvs_runset`` imports this lazily (it needs ``RUNSET_ENGINE`` back)."""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Sequence
from typing import Any

from .lvs_base import LvsError
from .lvs_runset_sg13g2_cards import PASSIVE_CARD_SHAPES, sg13g2_model_name

#: ``reference.form`` values the runset engine accepts.
_RUNSET_REFERENCE_FORMS = ("plain-element", "subckt-call")


def refuse_unsupported(
    request: dict[str, Any], layout_spec: dict[str, Any], ref_spec: dict[str, Any]
) -> None:
    from .lvs_runset import RUNSET_ENGINE

    for field in ("hints", "parameter_tolerance"):
        if request.get(field) is not None:
            raise LvsError(
                f"request.{field} is only supported for engine 'klayout' -- "
                f"the {RUNSET_ENGINE} engine delegates the whole compare to "
                "the PDK's own runset"
            )
    # ``reference.form: "subckt-call"`` (+ ``deck``/``device_map``) converts the
    # reference with the "klayout" engine's own conversion before the runset
    # sees it; every other ``reference.*`` key stays refused.
    ref_form = ref_spec.get("form", "plain-element")
    if ref_form not in _RUNSET_REFERENCE_FORMS:
        raise LvsError(
            f"reference.form {ref_form!r} is not supported by engine "
            f"'{RUNSET_ENGINE}' (supported: "
            f"{', '.join(repr(f) for f in _RUNSET_REFERENCE_FORMS)})"
        )
    ref_allowed: tuple[str, ...] = ("netlist", "top", "form")
    if ref_form == "subckt-call":
        ref_allowed += ("deck", "device_map")
    if "device_map" in ref_spec and not isinstance(ref_spec["device_map"], dict):
        raise LvsError("request.reference.device_map must be a JSON object")
    for side, spec, allowed, why in (
        ("layout", layout_spec, ("file", "top"), "the runset does its own extraction"),
        (
            "reference",
            ref_spec,
            ref_allowed,
            "the reference must be a SPICE netlist in the runset's own dialect, "
            'or reference.form "subckt-call" to convert one',
        ),
    ):
        extra = [k for k in spec if k not in allowed]
        if extra:
            raise LvsError(
                f"{side}.{extra[0]} is not supported by engine '{RUNSET_ENGINE}' "
                f"({why}; only {side}.{', '.join(allowed)} apply)"
            )
    if "file" not in layout_spec:
        raise LvsError(
            f"engine '{RUNSET_ENGINE}' requires layout.file (a GDS/OASIS stream)"
        )
    if "netlist" not in ref_spec:
        raise LvsError(f"engine '{RUNSET_ENGINE}' requires reference.netlist")


def _drop_capacitor_placeholder(text: str, placeholder_classes: dict[str, str]) -> str:
    """Remove the positional ``0`` value from converted capacitor cards.

    The ``"klayout"`` engine ignores the placeholder, but a PDK runset's own
    SPICE reader takes it literally: against the real sky130 ``sky130.lvs`` the
    card ``C1 a b 0 <model> A= P=`` reads ``0`` as an extra net and drops
    ``A``/``P`` (live-verified, issue #4817), while ``C1 a b <model> A= P=``
    compares ``match``."""
    classes = {
        name.lower()
        for name, kind in placeholder_classes.items()
        if kind == "capacitor"
    }
    if not classes:
        return text
    lines = []
    for line in text.split("\n"):
        tokens = line.split()
        if (
            len(tokens) > 4
            and tokens[0][:1] in "Cc"
            and tokens[3] == "0"
            and tokens[4].lower() in classes
        ):
            line = " ".join(tokens[:3] + tokens[4:])
        lines.append(line)
    return "\n".join(lines)


_SUBCKT_RE = re.compile(r"^(\s*\.(?:subckt|ends)\s+)(\S+)", re.I)


def rename_top_subckt(text: str, old: str, new: str) -> str:
    """Rename the ``.subckt old`` header (and its ``.ends old``) to ``new``;
    only the exact, case-insensitive top name matches (issue #5402)."""
    out = []
    for line in text.split("\n"):
        m = _SUBCKT_RE.match(line)
        if m and m.group(2).lower() == old.lower():
            line = m.group(1) + new + line[m.end() :]
        out.append(line)
    return "\n".join(out)


_SG13G2_RF_CARDS = {"INDUCTOR", "SG13_HV_SVARICAP"}


def _sg13g2_rf_card(tokens: list[str], positional: list[str]) -> str:
    name = positional[-1]
    prefix, count = PASSIVE_CARD_SHAPES[name.upper()]
    terminals = len(positional) - 1
    if terminals != count:
        raise LvsError(f"SG13G2 {name} requires {count} terminals, got {terminals}")
    return " ".join(
        [prefix + tokens[0][1:], *(t for t in tokens[1:] if t.lower() != "params:")]
    )


def _sg13g2_reference_cards(
    text: str, source: str, binding: Any, explicit: frozenset[str]
) -> tuple[str, set[int]]:
    """Write native element cards and preserve hierarchical or unresolved X calls."""
    from ._spice_text import fold_spice_continuations
    from .lvs_runset_sg13g2_cards import native_card, native_primitive_names
    from .netlist_normalize import _binding_known, _split_params, _tokenize
    from .netlist_normalize_scan import recovery_subckt_names

    primitives = _SG13G2_RF_CARDS | native_primitive_names()
    defined = recovery_subckt_names(text, source, primitives)
    lines = fold_spice_continuations(text.split("\n"))
    kept: set[int] = set()
    enclosing = ""
    for i, line in enumerate(lines):
        head = line.lstrip().split(None, 2)
        if not head:
            continue
        if head[0].lower() == ".subckt" and len(head) > 1:
            enclosing = head[1].upper()
        elif head[0].lower() == ".ends":
            enclosing = ""
        elif head[0][:1].lower() == "x":
            tokens = _tokenize(line)
            positional, params = _split_params(tokens[1:])
            name = positional[-1] if positional else ""
            model = name.upper()
            if model not in primitives or name in explicit:
                continue
            if model in _SG13G2_RF_CARDS and _binding_known(name, binding):
                continue
            # PDK unit fixtures may give a top the same name as the primitive
            # it contains. Calls to a separate defined circuit are hierarchy.
            if model in defined and model != enclosing:
                kept.add(i)
                continue
            card = (
                _sg13g2_rf_card(tokens, positional)
                if model in _SG13G2_RF_CARDS
                else native_card(tokens, positional, params)
            )
            if card is None:
                # Leave the original X call visible to the unresolved-call scan.
                kept.add(i)
            else:
                lines[i] = card
    return "\n".join(lines), kept


def _protect_passthrough_calls(
    text: str,
    passthrough: set[str],
    deck: str | None,
    device_map: Any,
    *,
    sg13g2_reference: str | None = None,
) -> tuple[str, dict[str, str]]:
    """Issue #5402: hide ``X`` cards that call a ``passthrough`` subcircuit
    no curated/``device_map`` binding claims (curated wins) behind comment
    markers, so the conversion leaves them for the runset's own reader."""
    from .netlist_normalize import (
        _binding_known,
        _build_subckt_map,
        _split_params,
        _tokenize,
    )

    binding, explicit = _build_subckt_map(deck, device_map)
    kept: set[int] = set()
    if sg13g2_reference is not None:
        text, kept = _sg13g2_reference_cards(text, sg13g2_reference, binding, explicit)
    saved: dict[str, str] = {}
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line.strip()[:1] not in ("X", "x"):
            continue
        positional, _ = _split_params(_tokenize(line)[1:])
        name = positional[-1] if positional else ""
        if i in kept or (
            name.upper() in passthrough and not _binding_known(name, binding)
        ):
            marker = f"* __klt_passthrough_{i}__"
            saved[marker] = line
            lines[i] = marker
    return "\n".join(lines), saved


def convert_reference(
    reference: str, request: dict[str, Any], work_dir: str, deck_file: str
) -> str:
    """Write the ``subckt-call`` conversion of ``reference`` into ``work_dir``
    and return its path (``reference`` itself for any other form).

    Reuses the ``"klayout"`` engine's conversion, so the schematic MIM call
    ``X.. <pdk>__cap_mim_m3_1 W= L=`` becomes the plain card the runset's own
    extraction pairs with (``C.. a b <pdk>__model__cap_mim A= P=``)."""
    from .lvs_runset import _deck_device_list
    from .netlist_normalize import (
        NormalizeError,
        convert_reference_netlist,
        quote_spice_includes,
    )

    ref_spec = request["reference"]
    layout_top = request["layout"].get("top")
    ref_top = ref_spec.get("top")
    rename = bool(layout_top and ref_top and layout_top != ref_top)
    subckt_call = ref_spec.get("form", "plain-element") == "subckt-call"
    if not subckt_call and not rename:
        return reference
    try:
        with open(reference, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError as exc:
        raise LvsError(
            f"could not read reference netlist '{reference}': {exc}"
        ) from exc
    # The converted copy lives in work_dir: keep relative .include paths valid.
    text = quote_spice_includes(text, os.path.dirname(os.path.abspath(reference)))
    converted = text
    if subckt_call:
        deck, device_map = ref_spec.get("deck"), ref_spec.get("device_map")
        text, saved = _protect_passthrough_calls(
            text,
            _deck_device_list(deck_file),
            deck,
            device_map,
            sg13g2_reference=reference if is_sg13g2_runset(deck_file) else None,
        )
        try:
            result = convert_reference_netlist(text, deck=deck, device_map=device_map)
            converted = _drop_capacitor_placeholder(
                result.text, result.placeholder_value_classes
            )
            for marker, original in saved.items():
                converted = converted.replace(marker, original)
        except NormalizeError as exc:
            raise LvsError(
                f"could not convert subckt-call reference netlist '{reference}' to "
                f"plain-element form: {exc}"
            ) from exc
    if rename:
        # The runset pairs tops by name: give the reference top the layout's.
        converted = rename_top_subckt(converted, ref_top, layout_top)
    out = os.path.join(work_dir, "reference_converted.sp")
    with open(out, "w", encoding="utf-8") as handle:
        handle.write(converted)
    return out


def is_sg13g2_runset(deck_file: str) -> bool:
    """Whether ``deck_file`` is IHP's native ``sg13g2.lvs`` runset."""
    return os.path.basename(deck_file) == "sg13g2.lvs"


def reference_spice_reader(deck_file: str | None) -> Any:
    """The ``NetlistSpiceReader`` the census and the unresolved-call scan
    re-read the converted reference with: the plain one, except for IHP's
    ``sg13g2.lvs``, whose own reader keeps cards the plain one refuses
    (:func:`tolerant_spice_reader`, issue #7855). Every other deck's reading
    is unchanged."""
    import klayout.db as kdb

    if deck_file and is_sg13g2_runset(deck_file):
        return tolerant_spice_reader()
    return kdb.NetlistSpiceReader()


_VALUE_ELEMENTS = ("C", "R", "L")


def _stand_in_card(card: str, element: str) -> tuple[str, str]:
    """``(card, element)`` to re-parse a valueless ``R``/``C``/``L`` card the
    plain parse refused: the retries of IHP's ``CustomReader#parse_element``
    (``rule_decks/custom_reader.lvs``), in its order. A varactor is read as an
    ``M`` card, a four-net ``inductor3`` as an ``M`` card with a stand-in
    ``L=0``, anything else with a stand-in ``C=0``/``R=0``/``L=0``."""
    low = card.lower()
    if element == "C" and "varicap" in low:
        return card, "M"
    if element == "L" and "inductor3" in low:
        return f"{card} L=0", "M"
    return f"{card} {element}=0", element


def _create_generic_device(
    circuit: Any,
    name: str,
    model: str,
    nets: Sequence[Any],
    registry: dict[str, Any],
) -> None:
    """Add ``name`` to ``circuit`` as a device of a class named ``model`` with
    one terminal per net: the shape KLayout's own ``C``/``R``/``L`` handler
    refuses (a three-net ``L``, a four-net ``C``). One class object per
    ``model`` (``registry``); a card with another net count than the class's
    first card raises, so the reference stays unreadable rather than guessed."""
    import klayout.db as kdb

    from .netlist_class_registry import shared_device_class

    def make_class() -> kdb.DeviceClass:
        device_class = kdb.DeviceClass()
        device_class.name = model
        for i in range(len(nets)):
            device_class.add_terminal(
                kdb.DeviceTerminalDefinition(f"T{i + 1}", f"Terminal {i + 1}")
            )
        return device_class

    device_class = shared_device_class(circuit.netlist(), model, make_class, registry)
    terminals = device_class.terminal_definitions()
    if len(terminals) != len(nets):
        raise RuntimeError(
            f"'{model}' is used with {len(nets)} nets after a "
            f"{len(terminals)}-net card of the same class"
        )
    device = circuit.create_device(device_class, name)
    for net, terminal in zip(nets, terminals, strict=True):
        device.connect_terminal(terminal.name, net)


def tolerant_spice_reader() -> Any:
    """A ``NetlistSpiceReader`` that keeps the valueless ``R``/``C``/``L``
    cards ``sg13g2.lvs``'s own reader keeps (issue #7855).

    For the native model-last shapes, the model determines the terminal
    count. Read the net names directly so a numeric substrate such as ``0``
    stays connected. Other valueless cards get the deck's stand-in retry
    (:func:`_stand_in_card`). A card KLayout's own handler still
    refuses (:func:`_create_generic_device`: the RF inductor and varactor
    cards) becomes a device of a generic class named by its model, which is
    how the deck's ``process_device`` treats them too. Any other failure
    still raises, so a reference that genuinely cannot be read stays
    ``None`` / ``[]``."""
    import klayout.db as kdb

    registry: dict[str, kdb.DeviceClass] = {}

    class _Delegate(kdb.NetlistSpiceReaderDelegate):
        def parse_element(self, s: str, element: str) -> Any:
            if element not in _VALUE_ELEMENTS:
                return super().parse_element(s, element)
            parts = self.parse_element_components(s, self.variables())
            if parts.strings:
                *nets, model = parts.strings
                model = sg13g2_model_name(model)
                params = parts.parameters
                value = params.get(element, 0.0)
                # CDL can also put a value in the third field and supply
                # the substrate or model separately. Keep its ordinary parse.
                if (
                    PASSIVE_CARD_SHAPES.get(model) == (element, len(nets))
                    and not {"$SUB", "SUB", "MODEL"}.intersection(params)
                    and isinstance(value, (int, float))
                ):
                    card = kdb.ParseElementData()
                    card.net_names, card.model_name = nets, model
                    card.parameters, card.value = params, value
                    return card
            try:
                return super().parse_element(s, element)
            except RuntimeError:
                return super().parse_element(*_stand_in_card(s, element))

        def element(
            self,
            circuit: kdb.Circuit,
            element: str,
            name: str,
            model: str,
            value: float,
            nets: Sequence[kdb.Net],
            params: dict,
        ) -> bool:
            try:
                return super().element(
                    circuit, element, name, model, value, nets, params
                )
            except RuntimeError:
                if element not in _VALUE_ELEMENTS or not model:
                    raise
            _create_generic_device(circuit, name, model, nets, registry)
            return True

    return kdb.NetlistSpiceReader(_Delegate())


def _converted_call_names(declared: dict[str, Any], deck_file: str) -> set[str]:
    """Upper-cased callees the runset's own reader is known to turn into
    devices, which the unresolved-call scan therefore must not report: the
    classes either side declares plus the deck's ``DEVICE_LIST``.

    The SG13G2 reader converts no ``X`` card (issue #7836): declaring a class
    cannot prove a particular call to it survived, so nothing is exempt."""
    if is_sg13g2_runset(deck_file):
        return set()
    from .lvs_runset import _deck_device_list

    return {
        str(n).upper() for side in declared.values() for n in side or []
    } | _deck_device_list(deck_file)


def _unresolved_reference_calls(
    reference_path: str,
    top: str | None,
    declared: dict[str, Any],
    deck_file: str,
    find_unresolved: Callable[..., list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Issue #4519: ``reference.unresolved_subckt_call`` findings for ``X``
    calls the runset neither converted nor could resolve.

    The reference is re-read with the plain SPICE reader (hierarchy intact);
    for ``sg13g2.lvs`` one that also keeps the valueless native ``R``/``C``/
    ``L`` cards the runset's own reader keeps (:func:`reference_spice_reader`,
    issue #7855: a raise on one used to hide every unresolved call in the
    file). A callee the runset itself converts
    (:func:`_converted_call_names`: its ``declared`` device classes plus the
    deck's ``DEVICE_LIST``) is not reported. ``[]`` when the reference cannot
    be re-read: the runset's own verdict stands."""
    import klayout.db as kdb

    netlist = kdb.Netlist()
    try:
        netlist.read(reference_path, reference_spice_reader(deck_file))
    except Exception:
        return []
    return find_unresolved(
        netlist,
        _converted_call_names(declared, deck_file),
        form="spice",
        top=top,
        reference_path=reference_path,
    )
