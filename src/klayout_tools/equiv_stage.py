"""Resumable-run stage commit records of :mod:`klayout_tools.equiv` (#2280).

Modeled on ``sim.py``'s checkpoint/resume machinery (issue #473), adapted to
what a Yosys proof can checkpoint: whole *stages*, not per-unit results. See
:mod:`klayout_tools.equiv`'s docstring, "Resumable runs", for the full
contract -- in particular why a stage record is structurally incapable of
carrying a verdict unless every validation here passes. Shared by the
combinational path (``equiv.py``) and the ``"yosys-sequential"`` engine
(``equiv_sequential.py``); split out of ``equiv.py`` (#4032) with behaviour
and JSON output unchanged.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any

from ._provenance import _combined_content_hash, _content_hash

#: ``equiv_status``'s own exact success line (verified live against the
#: pinned Yosys v0.67 this repo's own CI installs, `equiv_status.cc`'s
#: `unproven_equiv_cells.empty()` branch) -- every `$equiv` cell was proven.
EQUIV_ALL_PROVEN_RE = re.compile(r"Equivalence successfully proven!")


#: One `equiv_status` "unproven cell" report line, e.g.
#: `  Unproven $equiv $auto$equiv_make.cc:295:find_same_wires$10212:
#:  \_560_.B_gold \_560_.B_gate` -- the trailing group holds the gold/gate
#: signals the unproven `$equiv` cell pairs (see
#: `_parse_unproven_equiv_signals`).
UNPROVEN_EQUIV_RE = re.compile(r"^\s*Unproven \$equiv \S+: (.+)$", re.MULTILINE)

#: The two classified outcomes stage 1 can commit. Anything else (a process
#: timeout, a Yosys error, zero `$equiv` cells) is either an error the run
#: raises or a `partial: true` record -- never a resumable commit.
STAGE1_ALL_PROVEN = "all_proven"
STAGE1_UNPROVEN_CELLS = "unproven_cells"
_STAGE1_CLASSIFICATIONS = (STAGE1_ALL_PROVEN, STAGE1_UNPROVEN_CELLS)


def stage_record_path(output_dir: str, stage: int) -> str:
    """Where stage ``stage``'s commit record lives: ``stage<N>.commit.json``
    directly under ``output_dir`` (the same ``.klt/equiv/`` directory the
    stage's own script/log/netlist artifacts already go to), so a resumable
    run's on-disk footprint stays in one place -- the same convention
    ``sim._checkpoint_path`` follows for ``.klt/sim/checkpoint.json``.
    """
    return os.path.join(output_dir, f"stage{stage}.commit.json")


def stage_fingerprint(
    *,
    gold: dict[str, Any],
    gate: dict[str, Any],
    port_map: dict[str, str],
    effective_timeout_s: float,
    engine: str,
    induction_depth: int,
    sim_backend: str,
) -> str:
    """A SHA-256 fingerprint of everything that determines what a
    sequential run's stages actually compute -- the basis for deciding
    whether an on-disk stage record still applies to *this* request.

    Mirrors ``sim._checkpoint_fingerprint``'s own rule: content-hashes the
    inputs (not just their paths -- an edited-in-place file at the same
    path must invalidate the record) alongside every run parameter that
    reaches the proof. Deliberately *path-independent* (the same property
    ``_combined_content_hash`` documents for itself): the same request
    checked out at a different path -- or pulled back from a remote host
    with the artifacts (``docs/guides/remote-evidence-runs.md``) -- is
    resumable, which a path-keyed fingerprint would silently forbid.
    ``timeout_s`` is normalized through ``float()`` so the JSON-encoded
    ``60`` and ``60.0`` (a request integer vs. a ``--timeout-s`` float)
    fingerprint identically -- they are the same budget, and a resume must
    not discard a valid record over encoding.

    A fingerprint mismatch means "the request changed since the stage
    committed" -- the record is discarded (with a
    ``resume_stage_record_discarded`` diagnostic, never silently), exactly
    like ``sim._load_checkpoint``'s mismatched checkpoint.
    """
    payload: dict[str, Any] = {
        "engine": engine,
        "gold": {
            "top": gold["top"],
            "sources_sha256": _combined_content_hash(gold["sources"]),
            "liberty_sha256": (
                _content_hash(gold["liberty"]) if gold.get("liberty") else None
            ),
        },
        "gate": {
            "top": gate["top"],
            "sources_sha256": _combined_content_hash(gate["sources"]),
            "liberty_sha256": (
                _content_hash(gate["liberty"]) if gate.get("liberty") else None
            ),
        },
        "port_map": port_map,
        "timeout_s": float(effective_timeout_s),
        "induction_depth": induction_depth,
        "sim_backend": sim_backend,
    }
    # Issue #3466: only fingerprinted when set, so existing records stay valid.
    for label, side in (("gold", gold), ("gate", gate)):
        if side.get("parameters"):
            payload[label]["parameters"] = side["parameters"]
    blob = json.dumps(payload, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def write_stage_record(path: str, record: dict[str, Any]) -> None:
    """Atomically write a stage record: temp file + ``os.replace`` -- the
    same never-partially-written discipline ``sim._Checkpoint._flush_locked``
    uses, so a reader (this one or a future one) can never observe a
    half-written record. ``partial`` defaults to an explicit JSON ``false``
    when the caller does not state it: the committing path in
    :func:`_run_sequential` writes classified records (always committed),
    while :func:`write_partial_stage_record` is the only caller that
    passes ``partial: True`` explicitly.
    """
    payload = dict(record)
    payload.setdefault("partial", False)
    tmp_path = f"{path}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
        os.replace(tmp_path, path)
    except OSError:
        # Best-effort, exactly like the stage log write above it: a record
        # write failure must never fail an otherwise-successful proof. The
        # cost of losing one is only that the next `--resume` re-runs the
        # stage -- never a wrong verdict.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def write_partial_stage_record(path: str, fingerprint: str, *, reason: str) -> None:
    """Best-effort write of a ``partial: true`` stage record documenting a
    stage attempt that died or failed *before* reaching a classified
    outcome (process timeout, Yosys error, zero `$equiv` cells).

    This is the producer behind issue #2280's `partial: true` marking: an
    interrupted run's on-disk trace of "stage 1 was attempted here and did
    not complete" is explicitly partial, and the loader below refuses any
    record whose ``partial`` is not exactly JSON ``false`` -- so a partial
    record can never satisfy the resume, no matter what else it contains.
    Never overwrites an already-committed (``partial: false``) record: a
    later, killed re-run's partial marker must not destroy the one valid
    commit an earlier run left. Never raises (see
    :func:`write_stage_record` for the failure posture).
    """
    try:
        with open(path, encoding="utf-8") as handle:
            existing = json.load(handle)
        if isinstance(existing, dict) and existing.get("partial") is False:
            return
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    write_stage_record(
        path,
        {
            "stage": 1,
            "fingerprint": fingerprint,
            # The marker this function exists for: an attempt that never
            # reached a classified outcome is explicitly partial, and the
            # loader refuses any record whose `partial` is not exactly
            # JSON false -- so this artifact can never satisfy the resume,
            # no matter what else it contains.
            "partial": True,
            "classification": None,
            "blacklist": [],
            "refinements": 0,
            "reason": reason,
        },
    )


def _stage_record_shape_error(data: Any, fingerprint: str) -> str | None:
    """The structural-validation half of :func:`load_committed_stage1`:
    the reason a parsed record must be discarded, or ``None`` when every
    structural check passes. Factored out so the loader itself stays under
    the repo's complexity ratchet (`scripts/check_complexity_baseline.py`,
    issue #2034) -- each check here is one of the ways a partial or
    fabricated artifact is prevented from satisfying the resume's verdict
    check (issue #2280).
    """
    if not isinstance(data, dict):
        return "record is not a JSON object"
    if data.get("partial") is not False:
        return "record is marked partial (or carries no partial marker)"
    if data.get("stage") != 1:
        return "record is not for stage 1"
    if data.get("fingerprint") != fingerprint:
        return (
            "fingerprint mismatch -- the request changed since this stage "
            "record was committed"
        )
    if data.get("classification") not in _STAGE1_CLASSIFICATIONS:
        return "record carries no valid stage-1 classification"
    blacklist = data.get("blacklist")
    if not isinstance(blacklist, list) or not all(
        isinstance(name, str) and name for name in blacklist
    ):
        return "record's blacklist is not a list of wire names"
    refinements = data.get("refinements")
    if (
        not isinstance(refinements, int)
        or isinstance(refinements, bool)
        or (refinements < 0)
    ):
        return "record's refinement count is not a non-negative integer"
    return None


def _stage_log_corroborates(log_text: str, classification: str) -> bool:
    """Whether the committed stage log's own bytes corroborate the recorded
    classification: an ``all_proven`` record requires Yosys's own
    ``Equivalence successfully proven!`` line; an ``unproven_cells`` record
    requires unproven-``$equiv`` report lines and the success line's
    absence. The log, not the record, is the primary artifact -- a
    fabricated or truncated record whose log disagrees is discarded (issue
    #2280's negative-control property)."""
    if classification == STAGE1_ALL_PROVEN:
        return bool(EQUIV_ALL_PROVEN_RE.search(log_text))
    return not EQUIV_ALL_PROVEN_RE.search(log_text) and bool(
        UNPROVEN_EQUIV_RE.search(log_text)
    )


def load_committed_stage1(
    record_path: str,
    fingerprint: str,
    *,
    log_path: str,
    netlist_path: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """Read and fully validate stage 1's commit record for a ``--resume``
    run. Returns ``(record, None)`` when the record is committed, intact,
    fingerprint-matched, and corroborated by its own committed log bytes;
    ``(None, None)`` when no record exists at all (the ordinary first-run
    shape -- nothing was discarded, so no diagnostic is owed); and
    ``(None, reason)`` when a record exists but is rejected -- the caller
    surfaces ``reason`` as a ``resume_stage_record_discarded`` warning and
    re-runs the stage.

    Every rejection below is issue #2280's negative-control property made
    structural: the only verdict-bearing field a record carries is its
    ``classification`` (an ``all_proven`` classification *is* the final
    ``"equivalent"`` verdict for the resumed run), so each check exists to
    make sure nothing but a fully-corroborated commit can reach it -- see
    :func:`_stage_record_shape_error` (the structural checks) and
    :func:`_stage_log_corroborates` (the log-bytes check). An
    ``unproven_cells`` record additionally requires the committed stage-1
    netlist (stage 2's own input) to still be on disk.
    """
    if not os.path.isfile(record_path):
        return None, None
    try:
        with open(record_path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError, ValueError):
        return None, "record is unreadable or not valid JSON"
    shape_error = _stage_record_shape_error(data, fingerprint)
    if shape_error is not None:
        return None, shape_error
    if not os.path.isfile(log_path):
        return None, "the committed stage log artifact is missing"
    try:
        with open(log_path, encoding="utf-8") as handle:
            log_text = handle.read()
    except OSError:
        return None, "the committed stage log artifact is unreadable"
    if not _stage_log_corroborates(log_text, data["classification"]):
        return None, (
            "the committed stage log does not corroborate the recorded "
            f"classification {data['classification']!r}"
        )
    if data["classification"] == STAGE1_UNPROVEN_CELLS and not os.path.isfile(
        netlist_path
    ):
        return None, "the committed stage netlist artifact (stage 2's input) is missing"
    return data, None
