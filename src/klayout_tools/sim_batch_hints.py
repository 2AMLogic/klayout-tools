"""Runner-image gap hints for off-host batch diagnostics (issues #4891, #4947),
and the run-level "no unit converged" hint (issue #6720).

Split out of :mod:`klayout_tools.sim_batch` to keep that module under its
per-file size ratchet.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from .pdk_store import sources_revision_token
from .sim_expr_hint import attach_expr_runner_hint

#: ngspice log signatures of a model-library ``expr()`` parameter the engine
#: cannot evaluate; off-host only, where the engine is the runner image's, not
#: the caller's.
_EXPR_UNSUPPORTED_RE = re.compile(
    r"Undefined parameter \[expr\]|Expression err: expr\(", re.IGNORECASE
)
_RUNNER_EXPR_HINT = (
    " The same library may load on a newer local ngspice; compare"
    " environment.remote.runner_klt_version / environment.engine_version and"
    " update the runner image (an infra change, not a client change)."
)

#: A runner image that lacks the requested PDK variant (raised by the worker's
#: PDK resolver, ``pdk.py``); any diagnostic code, since the runner's own
#: ``klt`` chooses it.
_PDK_MISSING_RE = re.compile(r"no supported-layout PDK install", re.IGNORECASE)
_RUNNER_PDK_HINT = (
    " The runner image does not provide this PDK variant; set"
    " options.stage_model_inputs to run with the model files resolved on the"
    " submitting host (see docs/cli/sim.md)."
)


def with_runner_pdk_hint(diagnostic: dict[str, Any]) -> dict[str, Any]:
    """Append the ``options.stage_model_inputs`` pointer to a diagnostic whose
    message reports a missing runner-image PDK install. Additive and
    idempotent; ``code`` and every other field are untouched."""
    message = diagnostic.get("message")
    if (
        isinstance(message, str)
        and _PDK_MISSING_RE.search(message)
        and _RUNNER_PDK_HINT not in message
    ):
        diagnostic["message"] = message + _RUNNER_PDK_HINT
    return diagnostic


def with_runner_expr_hint(diagnostic: dict[str, Any]) -> dict[str, Any]:
    """Append the runner-gap sentence to an ``ngspice_fatal`` diagnostic whose
    message shows an unevaluated ``expr()`` parameter. Idempotent; any other
    diagnostic is returned unchanged."""
    message = diagnostic.get("message")
    if (
        diagnostic.get("code") == "ngspice_fatal"
        and isinstance(message, str)
        and _EXPR_UNSUPPORTED_RE.search(message)
        and _RUNNER_EXPR_HINT not in message
    ):
        diagnostic["message"] = message + _RUNNER_EXPR_HINT
    return diagnostic


def with_runner_hints(diagnostic: dict[str, Any]) -> dict[str, Any]:
    """Apply every runner-image-gap hint that matches ``diagnostic``."""
    return with_runner_pdk_hint(with_runner_expr_hint(diagnostic))


def hint_existing_fatal(diagnostics: list[dict[str, Any]]) -> bool:
    """True when ``diagnostics`` already carries an ngspice deck-abort code;
    in that case also append the runner-gap hint where it applies. The
    missing-PDK hint is applied to every diagnostic regardless of code."""
    from .sim import _NGSPICE_FATAL_CODES

    for existing in diagnostics:
        with_runner_pdk_hint(existing)
    if not any(d.get("code") in _NGSPICE_FATAL_CODES for d in diagnostics):
        return False
    for existing in diagnostics:
        with_runner_expr_hint(existing)
    return True


#: ngspice prints the offending expanded-deck card on the line it reports:
#: ``Error on line 570 : m.x1.m0 d g s b nfet_03v3 w=1.28e-4 l=2.8e-7``.
_CARD_ERR_RE = re.compile(
    r"^[ \t]*Error on line (\d+)(?: or its substitute)?[ \t]*:[ \t]*"
    r"(?:\n[ \t]+)?(\S[^\n]*)$",
    re.IGNORECASE | re.MULTILINE,
)
_MODELNAME_RE = re.compile(r"could not find a valid modelname", re.IGNORECASE)
_ATTRIBUTED_MARK = "ngspice card at expanded-deck line"
_LEGACY_BIN_CARD_ADVICE = "compare this model's w/l against the library's bin cards"


def _worker_stamp_sentence(pdk: dict[str, Any], staged: bool) -> str:
    """The fleet's observed PDK, as one sentence: variant plus stamp (and
    the ``SOURCES`` revision token when parseable), or the absence of a
    readable stamp."""
    sha = pdk.get("stamp_sha256")
    if not (isinstance(sha, str) and sha):
        return f"the fleet resolved PDK {pdk['variant']} but reported no readable stamp"
    token = sources_revision_token(pdk.get("stamp_text"))
    stamp = f"{pdk.get('stamp_scheme') or 'stamp'} {sha}"
    if token:
        stamp += f" (revision {token})"
    if staged:
        stamp += " -- an image stamp; the submitter's staged inputs executed"
    return f"the fleet resolved PDK {pdk['variant']}, {stamp}"


def _worker_comparison_sentence(worker_environment: dict[str, Any]) -> str:
    """``pdk_stamp_comparison`` as text: status, reason when present, and the
    staged-model-inputs basis when the comparison carries one."""
    comparison = worker_environment.get("pdk_stamp_comparison")
    if not (isinstance(comparison, dict) and comparison.get("status")):
        return ""
    text = f"pdk_stamp_comparison: {comparison['status']}"
    if comparison.get("reason"):
        text += f" ({comparison['reason']})"
    if comparison.get("basis"):
        text += f" [basis: {comparison['basis']}]"
    return text


_STAGED_REMEDY = (
    "this job already executed the submitter's staged model inputs, so the "
    "bin cards that failed to select a model are your own -- compare this "
    "model's w/l against your staged library and the digests in "
    "environment.remote.worker_environment.model_inputs"
)
_IMAGE_REMEDY = (
    "the library that failed to select a model is the runner image's, not "
    "yours -- run with options.stage_model_inputs: true to execute your own "
    "model closure on the fleet (docs/cli/sim.md)"
)


def _worker_pdk_clause(worker_environment: Any) -> str | None:
    """The fleet-side attribution tail for a ``modelname`` abort (issue
    #5593): the worker-observed PDK stamp, the stamp comparison, and the
    remedy that removes the divergence -- ``options.stage_model_inputs``
    shipping the submitter's own model closure. ``None`` when the capture is
    not a usable ``ok`` block, so the caller keeps the legacy bin-card
    advice: without a worker stamp there is nothing to attribute, and the
    submitter's own cards are then the only reachable ones."""
    if not isinstance(worker_environment, dict):
        return None
    if worker_environment.get("capture") != "ok":
        return None
    pdk = worker_environment.get("pdk")
    if not isinstance(pdk, dict) or not pdk.get("variant"):
        return None
    staged = pdk.get("models_staged") is True
    parts = [
        _worker_stamp_sentence(pdk, staged),
        _worker_comparison_sentence(worker_environment),
        _STAGED_REMEDY if staged else _IMAGE_REMEDY,
    ]
    return "; ".join(part for part in parts if part)


def attribute_modelname(
    diagnostic: dict[str, Any],
    log_text: str,
    worker_environment: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Name the instance/model behind a ``could not find a valid modelname``
    abort (issue #5138). The fleet outputs carry no expanded deck, but the
    engine log quotes the offending card, so the instance, model and sizing
    are read from it; ngspice's line number indexes the *expanded* deck, not
    the caller's netlist, and is labelled so. Additive and idempotent; never
    raises. Without a quoted card the diagnostic is returned unchanged.

    With a usable ``worker_environment`` block (issue #5593) the trailing
    bin-card advice is replaced by the fleet-side attribution clause
    (:func:`_worker_pdk_clause`): on a batch run the only bin cards the
    submitter can reach are their own, which a local-pass/fleet-fail abort
    has already ruled out."""
    message = diagnostic.get("message")
    if (
        not isinstance(message, str)
        or _ATTRIBUTED_MARK in message
        or not _MODELNAME_RE.search(message)
    ):
        return diagnostic
    cards = list(_CARD_ERR_RE.finditer(log_text))
    modelname = _MODELNAME_RE.search(log_text)
    if modelname is not None:
        cards = [m for m in cards if m.start() < modelname.start()]
    if not cards:
        return diagnostic
    line_no, card = cards[-1].group(1), cards[-1].group(2).split()
    tokens = [t for t in card if "=" not in t]
    params = " ".join(t for t in card if "=" in t)
    if len(tokens) >= 6 and tokens[0][:1].lower() == "m":
        detail = f"instance '{tokens[0]}' model '{tokens[5]}'"
    else:
        detail = f"'{card[0]}'"
    if params:
        detail += f" ({params})"
    tail = _worker_pdk_clause(worker_environment)
    if tail is None:
        tail = _LEGACY_BIN_CARD_ADVICE
    diagnostic["message"] = (
        f"{message}; {_ATTRIBUTED_MARK} {line_no}: {detail} -- the line "
        "number indexes ngspice's expanded deck, not your netlist; " + tail
    )
    return diagnostic


#: Engine logs a collected ``outputs/`` tree may hold (``keep_artifacts`` runs
#: copy each corner's log under ``artifacts/``). Only read to surface the
#: engine's fatal error; never copied wholesale into the JSON.
_BATCH_ENGINE_LOG_NAMES = ("ngspice.log", "xyce.log")

#: Bytes read from the head of an engine log (a fatal include/parse error is
#: printed before the simulation starts).
_BATCH_ENGINE_LOG_HEAD_BYTES = 256 * 1024


def _find_engine_logs(collected: str) -> list[str]:
    """Paths of every engine log under a collected ``outputs/`` tree, sorted."""
    found: list[str] = []
    for root, _dirs, files in os.walk(collected):
        found.extend(
            os.path.join(root, name)
            for name in files
            if name in _BATCH_ENGINE_LOG_NAMES
        )
    return sorted(found)


# --- run-level hint: no batch unit converged (issue #6720) --------------------

CONVERGENCE_ALL_FAILED_CODE = "batch_convergence_all_failed"

#: A unit "failed to converge" when it errored with one of these codes
#: (``timestep_too_small`` is a terminal abort with its own culprit node and is
#: deliberately not part of this hint).
_CONVERGENCE_CODES = frozenset({"singular_matrix", "nonconvergence"})

#: Distinct shard value sets spelled out in the message; ``workers[]`` on the
#: diagnostic always carries every shard.
_MAX_SHARD_GROUPS = 3


def _failed_to_converge(corner: Any) -> bool:
    """An errored unit whose diagnostics include a convergence code."""
    if not isinstance(corner, dict) or corner.get("status") != "error":
        return False
    return any(
        isinstance(d, dict) and d.get("code") in _CONVERGENCE_CODES
        for d in corner.get("diagnostics") or []
    )


def _worker_view(entry: dict[str, Any], fleet_index: int | None) -> dict[str, Any]:
    """What one job's worker reported, read only from that entry's own
    ``worker_environment`` -- never from the client-scoped fields, so an
    absent capture stays absent instead of borrowing the submitter's values."""
    worker = entry.get("worker_environment")
    worker = worker if isinstance(worker, dict) else {}
    comparison = worker.get("engine_build_comparison")
    init = worker.get("ngspice_init")
    return {
        "fleet_index": fleet_index,
        "capture": worker.get("capture"),
        "engine_build_comparison": (
            comparison.get("status") if isinstance(comparison, dict) else None
        ),
        "ngspice_init": (
            {"source": init.get("source"), "sha256": init.get("sha256")}
            if isinstance(init, dict)
            else None
        ),
        "omp_num_threads": worker.get("omp_num_threads"),
    }


def _worker_views(remote: Any) -> list[dict[str, Any]]:
    """One view per job: the single ``environment.remote`` block, or each
    ``fleet[]`` member of a sharded run. ``[]`` when there is no usable block
    (a resumed run, an SSH/older report)."""
    if not isinstance(remote, dict):
        return []
    fleet = remote.get("fleet")
    if isinstance(fleet, list):
        return [_worker_view(m, i) for i, m in enumerate(fleet) if isinstance(m, dict)]
    return [_worker_view(remote, None)]


def _render_values(view: dict[str, Any]) -> str:
    init = view["ngspice_init"] or {}
    fields = (
        ("capture", view["capture"]),
        ("engine_build_comparison.status", view["engine_build_comparison"]),
        ("ngspice_init.source", init.get("source")),
        ("ngspice_init.sha256", init.get("sha256")),
        ("omp_num_threads", view["omp_num_threads"]),
    )
    return ", ".join(
        f"{name}={json.dumps(value, default=str)}" for name, value in fields
    )


def _shard_label(members: list[dict[str, Any]]) -> str:
    return ", ".join("fleet[" + str(m["fleet_index"]) + "]" for m in members)


def _render_workers(views: list[dict[str, Any]]) -> str:
    """The worker-reported values: once for a single job, once per *distinct*
    value set (labelled by the shards sharing it) for a sharded run."""
    if not views:
        return (
            "environment.remote.worker_environment is absent from this report, "
            "so the worker's values are unavailable"
        )
    if views[0]["fleet_index"] is None:
        return f"environment.remote.worker_environment: {_render_values(views[0])}"
    groups: dict[str, list[dict[str, Any]]] = {}
    for view in views:
        values = _render_values(view)
        groups.setdefault(values, []).append(view)
    parts = [
        f"{_shard_label(members)}: {values}"
        for values, members in list(groups.items())[:_MAX_SHARD_GROUPS]
    ]
    if len(groups) > _MAX_SHARD_GROUPS:
        parts.append(
            f"{len(groups) - _MAX_SHARD_GROUPS} more distinct shard value sets "
            "in this diagnostic's workers[]"
        )
    return "environment.remote.fleet[].worker_environment, per shard: " + "; ".join(
        parts
    )


def _render_options(report: dict[str, Any]) -> str:
    environment = report.get("environment")
    options = (
        environment.get("ngspice_options") if isinstance(environment, dict) else None
    )
    if not options:
        return "environment.ngspice_options is not set"
    rendered = json.dumps(options, sort_keys=True, default=str)
    return f"environment.ngspice_options={rendered}"


def attach_convergence_hint(
    report: dict[str, Any], corners: list[dict[str, Any]], *, backend: str | None
) -> None:
    """Additive/optional (issue #6720): when **every** unit of a ``batch`` run
    (at least two) errored on ``singular_matrix``/``nonconvergence``, append
    one warning to the report's top-level ``diagnostics[]`` naming the
    worker-reported fields that can explain a local-versus-batch divergence
    and the one-corner canary. Neutral on the cause: it points at evidence the
    report already carries (each field's *current* value), it does not claim a
    diagnosis. Idempotent; a run with any converging unit, another backend or
    other error codes is left untouched."""
    if backend != "batch" or len(corners) < 2:
        return
    if not all(_failed_to_converge(corner) for corner in corners):
        return
    diagnostics = report.setdefault("diagnostics", [])
    if any(
        isinstance(d, dict) and d.get("code") == CONVERGENCE_ALL_FAILED_CODE
        for d in diagnostics
    ):
        return
    environment = report.get("environment")
    views = _worker_views(
        environment.get("remote") if isinstance(environment, dict) else None
    )
    diagnostics.append(
        {
            "code": CONVERGENCE_ALL_FAILED_CODE,
            "severity": "warning",
            "message": (
                f"all {len(corners)} units of this batch run failed with "
                "singular_matrix/nonconvergence. If the same corner converges "
                "under --backend local, compare what the worker applied: "
                f"{_render_workers(views)}; {_render_options(report)}. "
                "ngspice_init.sha256 is comparable to environment.ngspice_init."
                "sha256 of a --backend local run. To test the worker on one "
                "corner before a full grid, submit a one-corner request with an "
                'explicit backend "batch", options.keep_artifacts true and '
                "--outdir, then read that corner's artifacts.log (docs/cli/"
                'sim.md, "Transient convergence differs between local and '
                'batch").'
            ),
            "unit_count": len(corners),
            "workers": views,
        }
    )


def attach_runner_hints(
    report: dict[str, Any],
    measurements_spec: list[dict[str, Any]],
    corners: list[dict[str, Any]],
    *,
    backend: str | None,
) -> None:
    """Every report-level off-host runner hint, appended to the report's
    top-level ``diagnostics[]`` (the single call ``run_sim`` makes)."""
    attach_expr_runner_hint(report, measurements_spec, corners, backend=backend)
    attach_convergence_hint(report, corners, backend=backend)
