"""Request validation and ``subckt-call`` reference conversion for the
``klayout-runset`` LVS engine (issue #4805).

Split out of ``lvs_runset`` to keep that module under its file-size ceiling.
``lvs_runset`` imports this lazily (it needs ``RUNSET_ENGINE`` back)."""

from __future__ import annotations

import os
import re
from typing import Any

#: ``reference.form`` values the runset engine accepts.
_RUNSET_REFERENCE_FORMS = ("plain-element", "subckt-call")


def refuse_unsupported(
    request: dict[str, Any], layout_spec: dict[str, Any], ref_spec: dict[str, Any]
) -> None:
    from .lvs import LvsError
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


def _protect_passthrough_calls(
    text: str, passthrough: set[str], deck: str | None, device_map: Any
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

    binding = _build_subckt_map(deck, device_map)[0]
    saved: dict[str, str] = {}
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line.strip()[:1] not in ("X", "x"):
            continue
        positional, _ = _split_params(_tokenize(line)[1:])
        name = positional[-1] if positional else ""
        if name.upper() in passthrough and not _binding_known(name, binding):
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
    from .lvs import LvsError
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
            text, _deck_device_list(deck_file), deck, device_map
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
