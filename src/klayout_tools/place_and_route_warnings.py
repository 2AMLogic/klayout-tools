"""``klt place-and-route``'s top-level ``warnings[]`` composers (issue
#2086's assembly point, extracted from ``place_and_route.py``).

Every producer of the response's ``warnings`` array lives here so the
response assembly stays a single ``extend`` per producer and
:func:`klayout_tools.place_and_route.run_place_and_route` keeps its
complexity baseline. The composers are re-exported from
``place_and_route.py`` under their original names (the ``X as X`` alias
convention that module documents), so the test suite's direct calls and
``monkeypatch`` targets keep resolving.

Producers:

- the power-delivery audit's strings (issue #2086,
  :func:`klayout_tools.place_and_route_power_audit.power_delivery_warnings`)
  and the row-rail fallback caveat (issue #1985), composed by
  :func:`_response_warnings`;
- the antenna-repair skip caveat (:func:`_antenna_repair_warnings`);
- the KLayout layer-map caveat (issue #4481) is composed by
  :func:`klayout_tools.place_and_route_layer_map.layer_map_warnings`, not
  here;
- the Liberty-model caveat (issue #7411,
  :func:`liberty_model_response_warnings` and the stage-loop accumulators):
  when any stage's own Liberty read made OpenSTA **discard a timing model**
  -- ``[WARNING STA-1253]`` (a timing table names a ``lu_table_template``
  the library does not define) followed by ``[WARNING STA-1311]`` (the
  timing group left with no table models) -- one entry names the per-code
  counts and states the consequence. Every stage script reads the same
  deck, so identical lines are deduplicated across stages; a ``--resume``d
  stage never runs in-process, so its lines are recovered from the retained
  ``openroad-logs``.
"""

from __future__ import annotations

import os
from typing import Any

from . import _liberty_warnings
from .place_and_route_power_audit import (
    power_delivery_warnings as power_delivery_warnings,
)
from .place_and_route_power_audit import (
    row_rail_fallback_warning as row_rail_fallback_warning,
)
from .place_and_route_script import (
    _antenna_diode_absent as _antenna_diode_absent,
)


def _antenna_repair_warnings(
    target_stage: str, library: dict[str, Any] | None
) -> list[str]:
    """Warn when a ``route`` run skips antenna repair (``antenna_diode: null``)."""
    if target_stage != "route" or not _antenna_diode_absent(library):
        return []
    return [
        "antenna repair did not run: the effective request.library.antenna_diode is "
        "null (the library has no antenna diode), so repair_antennas "
        "was not called; the antenna check still ran"
    ]


def _response_warnings(
    placed: dict[str, Any],
    *,
    power_requested: bool,
    row_rail_emitted: bool,
    row_rail_layer: str | None,
) -> list[str]:
    """Compose the response's top-level ``warnings`` array for one run.

    Two producers, kept in one place so the response assembly stays a
    single call (and one fewer decision point in the already-baselined
    :func:`klayout_tools.place_and_route.run_place_and_route` --
    ``complexity-baseline.json``, the same reason
    :func:`klayout_tools.place_and_route._validate_pdn_core_fit` is a
    function of its own):

    - the power-delivery audit's own strings (issue #2086,
      :func:`power_delivery_warnings`): what the DEF was measured to
      place, for an omitted ``request.power`` or a supplied one whose
      grid came back incomplete/unverifiable;
    - the row-rail fallback caveat (issue #1985,
      :func:`row_rail_fallback_warning`), appended -- never folded into
      the audit's strings, so each entry stays true to its own evidence:
      the audit's prose is measured from the DEF and already refuses to
      claim "no rails" on the fallback path, while the caveat states the
      structural fact (horizontal rails only, no vertical straps, no
      tapcells, no multi-layer PDN vias) that makes the grid incomplete
      even when every measured count came back nonzero.

    ``row_rail_emitted`` is ``True`` only when the fallback actually ran
    (``request.power`` omitted, ``"route"`` stage reached, a
    :data:`klayout_tools.place_and_route._ROW_RAIL_STRAP` library) -- never
    alongside a real PDN -- so a full-PDN run's ``warnings`` is exactly
    what the audit alone produces.
    """
    warnings = power_delivery_warnings(placed, power_requested=power_requested)
    if row_rail_emitted:
        warnings.append(row_rail_fallback_warning(row_rail_layer))
    return warnings


def engine_log_texts(records: list[dict[str, Any]], output_dir: str) -> list[str]:
    """Issue #7411: the retained stdout/stderr log texts for one resumed
    stage's OpenROAD invocations.

    A ``--resume``d stage never runs in this process, so its own Liberty
    read survives only in the retained ``openroad-logs/<invocation_id>/
    {stdout,stderr}.log`` files under this run's ``output_dir`` (the layout
    ``_openroad_engine._LiveLogs`` owns). An invocation whose logs are
    absent or unreadable contributes nothing rather than failing the run --
    the resumed artifacts are already trusted evidence, and a missing log
    must not turn a recoverable stage into an error.
    """
    texts: list[str] = []
    for record in records:
        invocation_id = record.get("invocation_id")
        if not invocation_id:
            continue
        for stream in ("stdout", "stderr"):
            path = os.path.join(
                output_dir, "openroad-logs", invocation_id, f"{stream}.log"
            )
            try:
                with open(path, encoding="utf-8", errors="replace") as handle:
                    texts.append(handle.read())
            except OSError:
                continue
    return texts


def liberty_warning_lines_after_stage(
    lines: list[str], stdout: str, stderr: str
) -> list[str]:
    """Issue #7411: ``lines`` extended by one live stage's own stream pair
    -- every stage script re-reads the same deck, so identical lines are
    deduplicated and a repeated warning across stages is counted once."""
    return _liberty_warnings.merge_liberty_model_warning_lines(lines, stdout, stderr)


def liberty_warning_lines_from_resumed(
    lines: list[str], records: list[dict[str, Any]], output_dir: str
) -> list[str]:
    """Issue #7411: ``lines`` extended by one resumed stage's retained-log
    warning lines -- the ``--resume`` counterpart of
    :func:`liberty_warning_lines_after_stage`."""
    return _liberty_warnings.merge_liberty_model_warning_lines(
        lines, *engine_log_texts(records, output_dir)
    )


def liberty_model_response_warnings(lines: list[str]) -> list[str]:
    """Issue #7411: the zero-or-one-element ``warnings`` slice for the
    deduplicated Liberty-model warning lines -- ``[]`` on a clean deck, so
    a clean run's ``warnings`` array is byte-identical to a pre-#7411
    run."""
    message = _liberty_warnings.format_liberty_model_warning(
        _liberty_warnings.liberty_model_warning_block(lines)
    )
    return [message] if message is not None else []
