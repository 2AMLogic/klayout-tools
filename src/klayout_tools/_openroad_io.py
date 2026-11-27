"""The script, metrics, and error-text contract around an OpenROAD run,
shared by ``klt place-and-route`` and ``klt sta`` (issue #8051).

``write_script``, ``read_metrics``, and ``format_engine_error`` were each
duplicated -- verbatim, or modulo the error class and the noun a verb uses
for a run -- in ``place_and_route_script.py``/``place_and_route.py`` and
``post_route_sta.py``. They live here as the single source of truth so the
two verbs cannot drift: every caller passes its own ``error_cls`` and
``label``/``context``, so each error string stays byte-identical to its
pre-sharing form. :func:`klayout_tools._openroad_engine.run_openroad` is the
step between the write and the read; these sit in their own module rather
than beside it because ``_openroad_engine.py`` is at its file-size ceiling
(the ratchet, issue #2766) -- the same precedent as
``_openroad_mount_hint.py`` (issue #5753).
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from typing import Any

from ._openroad_engine import OpenRoadResult


def write_script(
    script_path: str, lines: list[str], *, label: str, error_cls: type[Exception]
) -> None:
    """Write the Tcl ``lines`` OpenROAD will run, one per line with a
    trailing newline. A write failure raises ``error_cls`` naming ``label``
    (``"place-and-route"`` / ``"sta"``) so each verb keeps its own error
    type and wording."""
    try:
        with open(script_path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    except OSError as exc:
        raise error_cls(
            f"could not write {label} script '{script_path}': {exc}"
        ) from exc


def format_engine_error(
    completed: OpenRoadResult,
    *,
    context: str,
    hints: Iterable[str | None] = (),
) -> str:
    """The ``openroad <context> failed: <diagnostic>`` message for a failed
    OpenROAD run, each recognized ``hints`` entry appended as a further
    ``--`` clause in the caller's order (``None`` entries are skipped).

    Prefers a bracketed ``[ERROR ...]`` diagnostic OpenROAD itself printed
    -- on either stream, first occurrence -- over a bare ``Error:`` trailer
    line (just ``<script>.tcl, <line> <code>``, no message text) that
    reliably follows it on a Tcl-level failure at ``-exit`` (issue #1079).
    Only when no bracketed diagnostic is present does the last bare
    ``Error:`` line win, on either stream; with neither, the last three
    lines of stderr (or stdout) stand in for the diagnosis.

    ``context`` is the caller's own noun for the run -- ``klt
    place-and-route`` passes ``'<stage>' stage``, ``klt sta`` passes ``"sta
    run"`` -- so the two verbs keep their exact pre-sharing wording.
    """
    bracket_lines: list[str] = []
    bare_error_lines: list[str] = []
    for stream in (completed.stdout or "", completed.stderr or ""):
        for line in stream.splitlines():
            stripped = line.strip()
            if "[ERROR" in stripped:
                bracket_lines.append(stripped)
            elif stripped.startswith("Error:"):
                bare_error_lines.append(stripped)

    if bracket_lines:
        message = f"openroad {context} failed: {bracket_lines[0]}"
    elif bare_error_lines:
        message = f"openroad {context} failed: {bare_error_lines[-1]}"
    else:
        tail_source = (completed.stderr or completed.stdout or "").strip().splitlines()
        tail = " ".join(tail_source[-3:]) if tail_source else "no output captured"
        message = f"openroad {context} exited with code {completed.returncode}: {tail}"
    return message + "".join(f" -- {hint}" for hint in hints if hint is not None)


def read_metrics(
    metrics_path: str,
    *,
    label: str,
    error_cls: type[Exception],
    object_pairs_hook: Any = dict,
) -> dict[str, Any]:
    """Load and validate the ``-metrics`` JSON file a successful OpenROAD
    run leaves at ``metrics_path``. Any problem -- missing, unreadable, not
    JSON, not an object -- raises ``error_cls`` with a message naming
    ``label`` (``"'route' stage"`` / ``"sta"``) so each verb keeps its own
    error type and wording. ``object_pairs_hook`` is forwarded to
    :func:`json.load`."""
    if not os.path.isfile(metrics_path):
        raise error_cls(
            "openroad exited successfully but did not produce the expected "
            f"{label} metrics file '{metrics_path}'"
        )
    try:
        with open(metrics_path, encoding="utf-8") as handle:
            data = json.load(handle, object_pairs_hook=object_pairs_hook)
    except (OSError, UnicodeDecodeError) as exc:
        raise error_cls(
            f"could not read {label} metrics '{metrics_path}': {exc}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise error_cls(
            f"{label} metrics '{metrics_path}' is not valid JSON: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise error_cls(f"{label} metrics '{metrics_path}' must contain a JSON object")
    return data
