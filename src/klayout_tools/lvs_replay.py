"""``klt lvs --check --rerun`` replay helpers, split out of ``lvs.py``
per the per-file size ratchet (issue #5610's compaction knob included).

Each helper turns a committed report's own echo back into the request
fields that reproduce it, so a rerun does not report the echo as drift.
Pure ``dict``-in/``dict``-out; no engine state.
"""

from __future__ import annotations

from typing import Any


def _validate_omit_mismatches(value: Any) -> bool:
    """Optional top-level ``request.omit_mismatches`` (issue #5610, the
    request-document counterpart of ``klt lvs --omit-mismatches``): blank
    the response's ``mismatches[]`` to ``[]`` beside a ``mismatches_omitted:
    true`` echo, keeping every summary field (the tally runs over the full
    list before the blanking, see
    :func:`klayout_tools.lvs_report.assemble_lvs_report`). Omitted/``None``
    is ``False``: the response is unchanged apart from the always-present
    ``mismatches_omitted`` echo. A non-boolean value is a clean
    :class:`LvsError`, the same discipline ``request.portable_paths``
    applies -- never a silent no-op."""
    # Local import: `lvs` imports this module at load time, so a module-level
    # import would be circular (the `lvs_runset.py` pattern).
    from .lvs import LvsError

    if value is None:
        return False
    if not isinstance(value, bool):
        raise LvsError("request.omit_mismatches must be a boolean")
    return value


def _combine_devices_max_attempts_replay_options(
    echoed: dict[str, Any],
) -> dict[str, Any]:
    """``{"combine_devices_max_attempts": <echoed value>}`` when the committed
    report recorded one (issue #2682), else ``{}`` so a legacy report replays
    at the parser's default. The value is passed through unmodified, so a
    malformed recorded budget is rejected by the parser on replay rather than
    silently becoming a valid one."""
    key = "combine_devices_max_attempts"
    return {key: echoed[key]} if key in echoed else {}


def _reference_conversion_echo(
    reference_form: str,
    reference_spec: dict[str, Any],
    pin_order_files: list[str] | None = None,
) -> dict[str, Any]:
    """``{"reference_conversion": {...}}`` for a ``subckt-call`` or
    ``gate-level-verilog`` reference, else ``{}`` (issues #2673, #4353).

    The conversion is a function of the reference file *and* this context, so
    a report that records only the file cannot be replayed: ``--rerun`` would
    re-read the simulation-form SPICE / structural Verilog as plain-element
    SPICE. ``subckt-call`` echoes ``deck``/``device_map``; ``gate-level-
    verilog`` echoes ``library``/``pdk``/``pdk_root`` and the *resolved*
    ``pin_order_files`` (``null`` when omitted). Values are echoed as the
    caller gave them.
    """
    if reference_form == "subckt-call":
        return {
            "reference_conversion": {
                "form": reference_form,
                "deck": reference_spec.get("deck"),
                "device_map": reference_spec.get("device_map"),
            }
        }
    if reference_form == "gate-level-verilog":
        return {
            "reference_conversion": {
                "form": reference_form,
                "library": reference_spec.get("library"),
                "pdk": reference_spec.get("pdk"),
                "pdk_root": reference_spec.get("pdk_root"),
                "pin_order_files": list(pin_order_files) if pin_order_files else None,
            }
        }
    return {}


def _reference_conversion_replay(committed: dict[str, Any]) -> dict[str, Any]:
    """The ``request.reference`` keys that rebuild a committed report's
    ``reference_conversion`` echo (issues #2673, #4353); ``{}`` for a report
    without one (every other form, and any committed before the echo existed
    -- those stay best-effort, as documented)."""
    echoed = committed.get("reference_conversion")
    if not isinstance(echoed, dict):
        return {}
    if echoed.get("form") == "gate-level-verilog":
        spec: dict[str, Any] = {"form": "gate-level-verilog"}
        for key in ("library", "pdk", "pdk_root"):
            if echoed.get(key) is not None:
                spec[key] = echoed[key]
        if isinstance(echoed.get("pin_order_files"), list):
            spec["pin_order_files"] = list(echoed["pin_order_files"])
        return spec
    if echoed.get("form") != "subckt-call":
        return {}
    spec = {"form": "subckt-call"}
    if echoed.get("deck") is not None:
        spec["deck"] = echoed["deck"]
    if isinstance(echoed.get("device_map"), dict):
        spec["device_map"] = dict(echoed["device_map"])
    return spec


def _constant_nets_replay_hints(committed: dict[str, Any]) -> dict[str, Any]:
    """``{"hints": {"constant_nets": {...}}}`` when the committed report
    echoed applied bindings, else ``{}`` (issue #3014)."""
    applied = committed.get("constant_nets_applied")
    if isinstance(applied, dict) and applied:
        return {"hints": {"constant_nets": dict(applied)}}
    return {}


def _omit_mismatches_replay(committed: dict[str, Any]) -> dict[str, Any]:
    """``{"omit_mismatches": True}`` when the committed report echoed the
    ``--omit-mismatches`` compaction, else ``{}`` (issue #5610).

    The replay is what keeps ``--check --rerun`` off the false-drift shape
    issue #1205 already produced once: a compacted committed report
    (``mismatches: []``) diffed against a fresh, uncompacted list reports
    every entry as drift. It follows the ``erc.py`` shape -- read the
    committed report's own ``mismatches_omitted`` response echo and re-assert
    it as a top-level *request* field -- rather than the
    ``_strict_top_level_pin_count_replay_options`` helpers, because this is
    a response echo, not an entry of the ``options`` block (which only
    echoes compare-shaping inputs). Used by both
    :func:`_reconstruct_lvs_request` and
    :func:`_reconstruct_runset_request`, so a runset report replays the
    omission too instead of rerunning uncompacted."""
    return {"omit_mismatches": True} if committed.get("mismatches_omitted") else {}


def _anchor_top_level_pins_replay_options(echoed: dict[str, Any]) -> dict[str, Any]:
    """``{"anchor_top_level_pins": <bool>}`` when the committed report echoed
    an explicit value for it, else ``{}`` (issue #2692).

    Includes ``false``: unlike every other option
    :func:`_reconstruct_lvs_request` replays, omitting this one does **not**
    mean "off" for every reference form -- a ``gate-level-verilog`` request
    that omits it gets anchoring *on* (see
    :func:`_anchor_top_level_pins_enabled`). Dropping an explicit ``false``
    would therefore replay the original compare with anchoring turned on, and
    report the resulting (correct, but different) verdict as drift.
    """
    requested = echoed.get("anchor_top_level_pins")
    return {"anchor_top_level_pins": requested} if isinstance(requested, bool) else {}


def _supply_nets_replay_options(echoed: dict[str, Any]) -> dict[str, Any]:
    names = echoed.get("supply_nets")
    return {"supply_nets": list(names)} if isinstance(names, list) else {}
