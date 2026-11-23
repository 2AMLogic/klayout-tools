"""Fingerprint and persist completed simulation corners for resumable sweeps."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from collections.abc import Sequence
from typing import Any

from . import sim_analysis_overrides as _sao
from ._provenance import sha256_file
from .sim_corner_point import CornerPoint

DEFAULT_MC_SEED_SCOPE = "per_corner"
DEFAULT_MC_SEED_MODE = "derived"
DEFAULT_SAVE_MODE = "all"


def _checkpoint_path(artifacts_dir: str) -> str:
    """Where a resumable sweep's checkpoint lives: a single ``checkpoint.json``
    directly under ``artifacts_dir`` -- the same directory ``keep_artifacts``
    already writes per-corner logs/rawfiles into (see ``run_sim``'s
    docstring), so a resumable run's on-disk footprint stays in one place
    regardless of ``keep_artifacts``.
    """
    return os.path.join(artifacts_dir, "checkpoint.json")


def restore_checkpoint(
    checkpoint_path: str, fingerprint: str
) -> tuple[dict[str, dict[str, Any]], str | None, _Checkpoint]:
    """Restore matching completed corners and the checkpoint to extend."""
    pre_completed, loaded_engine_version = _load_checkpoint(
        checkpoint_path, fingerprint
    )
    checkpoint = _Checkpoint(checkpoint_path, fingerprint)
    checkpoint.completed = dict(pre_completed)
    checkpoint.engine_version = loaded_engine_version
    return pre_completed, loaded_engine_version, checkpoint


def _key_in_initial_conditions(
    payload: dict[str, Any], corner_points: Sequence[CornerPoint]
) -> None:
    """Issue #3881: a start state changes the transient result, so a changed
    map must not reuse a stored one. Keyed in only when present, leaving every
    legacy fingerprint untouched."""
    ics = {
        point.corner_id: point.initial_conditions
        for point in corner_points
        if point.initial_conditions
    }
    if ics:
        payload["initial_conditions"] = ics


def _key_in_mc_mismatch_scale(
    payload: dict[str, Any], corner_points: Sequence[CornerPoint]
) -> None:
    """Issue #3942: corner IDs carry neither seeds nor the sigma scale, so a
    resume under another ``monte_carlo.mismatch_scale`` would reuse samples
    drawn at the old scale. Keyed in only when declared."""
    mc_scales = sorted(
        {
            (point.mc_seed["mismatch_param"], point.mc_seed["mismatch_scale"])
            for point in corner_points
            if point.mc_seed and "mismatch_scale" in point.mc_seed
        }
    )
    if mc_scales:
        payload["mc_mismatch_scale"] = mc_scales


def _key_in_mc_seed_options(
    payload: dict[str, Any], corner_points: Sequence[CornerPoint]
) -> None:
    """Issues #2899/#4173: corner IDs carry no seeds, so a resume under another
    ``monte_carlo.seed_scope`` / ``seed_mode`` would reuse samples drawn from
    other seeds. Each is keyed in off its default only, so an existing
    checkpoint stays valid for a request that omits the field."""
    for field, default, key in (
        ("seed_scope", DEFAULT_MC_SEED_SCOPE, "mc_seed_scope"),
        ("seed_mode", DEFAULT_MC_SEED_MODE, "mc_seed_mode"),
    ):
        values = {
            point.mc_seed[field]
            for point in corner_points
            if point.mc_seed and field in point.mc_seed
        } - {default}
        if values:
            payload[key] = sorted(values)


def _checkpoint_fingerprint(
    *,
    netlist_path: str,
    models_lib: str | None,
    analysis: dict[str, Any],
    measurements_spec: list[dict[str, Any]],
    corner_points: list[CornerPoint],
    timeout_s: float,
    engine: str,
    osdi_preload: tuple[str, ...] = (),
    corner_section_libs: tuple[str, ...] = (),
    save_mode: str = DEFAULT_SAVE_MODE,
    save_vectors: tuple[str, ...] = (),
    complex_vectors: tuple[str, ...] = (),
    ngspice_options: dict[str, Any] | None = None,
    stimulus_lines: tuple[str, ...] = (),
) -> str:
    """A SHA-256 fingerprint of everything that determines what a sweep's
    corners actually run -- the basis for deciding whether an on-disk
    checkpoint still applies to *this* request.

    Content-hashes the netlist/model library (not just their paths -- an
    edited-in-place file at the same path must invalidate the checkpoint,
    per this issue's own acceptance criteria) alongside the analysis,
    measurement cards, engine (not ``timeout_s``: #5410), and the exact list of
    expanded corner IDs (covers the whole corner matrix, including any
    Monte Carlo expansion, since two requests that expand to a different
    corner list are not resumable against each other's progress). A
    fingerprint mismatch means "resume against a request file that has
    changed since the checkpoint was written" -- the checkpoint is treated
    as if it did not exist, never silently reused against a since-modified
    matrix (see ``run_sim``'s docstring).
    """
    payload: dict[str, Any] = {
        "netlist_sha256": sha256_file(netlist_path),
        "models_lib_sha256": sha256_file(models_lib),
        "analysis": analysis,
        "measurements_spec": measurements_spec,
        "engine": engine,
        "corner_ids": [point.corner_id for point in corner_points],
    }
    if osdi_preload:
        # Issue #2513: a rebuilt `.osdi` changes every device it defines, so
        # it invalidates a checkpoint exactly like an edited model library.
        # Only keyed in when declared, so a request without the option keeps
        # its pre-#2513 fingerprint (and its existing checkpoint) unchanged.
        payload["osdi_preload_sha256"] = [sha256_file(p) for p in osdi_preload]
    if save_mode != DEFAULT_SAVE_MODE:
        # Issue #2732: the saved set decides which measurements resolve, so
        # a checkpoint written under one mode never applies to the other.
        # Only keyed in off the default, so an existing checkpoint stays
        # valid for a request that omits the option.
        payload["save_mode"] = save_mode
    if save_vectors:
        # Issue #3211: the saved vector set decides which measurements resolve.
        payload["save_vectors"] = list(save_vectors)
    if complex_vectors:
        # Issue #3337: the declared vectors decide what the corner returns.
        payload["complex_vectors"] = list(complex_vectors)
    if ngspice_options:
        # Issue #3111: solver options change which solves converge, so a
        # checkpoint written under other options never applies. Only keyed
        # in when declared, leaving existing fingerprints untouched.
        payload["ngspice_options"] = ngspice_options
    _key_in_mc_seed_options(payload, corner_points)
    _key_in_mc_mismatch_scale(payload, corner_points)
    if stimulus_lines:
        # Issue #2977: the declared stimulus decides every corner's drive, so
        # an edited schedule must invalidate the checkpoint. Only keyed in
        # when declared, so existing checkpoints stay valid.
        payload["stimulus"] = list(stimulus_lines)
    if any(point.nodeset for point in corner_points):
        # Issue #3036: an initial guess can change which solution a
        # multi-equilibrium circuit lands on, so a changed hint must not reuse
        # a stored result. The corner IDs above do not carry it. Only keyed
        # in when a hint exists, leaving every legacy fingerprint untouched.
        payload["nodeset"] = {
            point.corner_id: point.nodeset for point in corner_points if point.nodeset
        }
    _key_in_initial_conditions(payload, corner_points)
    if windows := _sao.windows_by_corner(corner_points):
        payload["analysis_overrides"] = windows  # issue #4970
    if corner_section_libs:
        # Issue #2522: a corner's per-section libraries determine its device
        # models exactly as `models.lib` does, so editing one must invalidate
        # the checkpoint too. Only keyed in when the request declares them, so
        # a bundle without per-section libraries keeps its pre-#2522
        # fingerprint (and its existing checkpoint) unchanged.
        payload["corner_section_libs_sha256"] = [
            sha256_file(p) for p in corner_section_libs
        ]
    blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _load_checkpoint(
    path: str, fingerprint: str
) -> tuple[dict[str, dict[str, Any]], str | None]:
    """Read a checkpoint file, returning ``({corner_id: corner_report},
    engine_version)`` -- both empty/``None`` when the file is missing,
    unreadable, not valid JSON, or its own fingerprint does not match
    ``fingerprint`` (see :func:`_checkpoint_fingerprint`). Never raises: a
    corrupt or stale checkpoint degrades to "nothing completed yet", the
    same outcome as no checkpoint at all, rather than aborting the sweep.
    """
    if not os.path.isfile(path):
        return {}, None
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}, None
    if not isinstance(data, dict) or data.get("fingerprint") != fingerprint:
        return {}, None
    completed = data.get("completed")
    if not isinstance(completed, dict):
        return {}, None
    return completed, data.get("engine_version")


class _Checkpoint:
    """Incrementally persists completed corner reports to a JSON file so an
    interrupted sweep (wall-clock budget hit, orphaned parent, plain crash)
    can resume without recomputing corners that already finished.

    Only constructed when the request enables ``options.resume``/``--resume``
    (see ``run_sim``) -- writing/reading a checkpoint is an explicit opt-in,
    mirroring ``options.keep_artifacts``, so a default sweep leaves no extra
    file behind. Every :meth:`record` call flushes the *entire* completed set
    to disk immediately (via a temp-file-plus-``os.replace`` atomic write,
    never a partially-written file a concurrent resume could read) -- a
    corner report is small, and a sweep is corner-count-bounded, so this
    stays cheap relative to the ``ngspice`` run it follows. Thread-safe: both
    ``local-parallel``'s bounded dispatch window and ``_run_sharded``'s
    per-shard threads call :meth:`record` concurrently.
    """

    @property
    def fingerprint(self) -> str:
        return self._fingerprint

    def __init__(self, path: str, fingerprint: str) -> None:
        self._path = path
        self._fingerprint = fingerprint
        self._lock = threading.Lock()
        self.completed: dict[str, dict[str, Any]] = {}
        self.engine_version: str | None = None

    def record(
        self, corner_id: str, report: dict[str, Any], engine_version: str | None
    ) -> None:
        with self._lock:
            self.completed[corner_id] = report
            if engine_version is not None:
                self.engine_version = engine_version
            self._flush_locked()

    def clear(self) -> None:
        """Remove the checkpoint file once a sweep finishes with nothing
        skipped -- a fully-completed sweep has nothing left to resume, and a
        stale file left behind is a pointless leftover (its fingerprint
        guard already makes it harmless to a future *different* request,
        but there is no reason to keep it around either)."""
        with self._lock:
            self.completed = {}
            try:
                os.remove(self._path)
            except OSError:
                pass

    def _flush_locked(self) -> None:
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        tmp_path = f"{self._path}.tmp"
        payload = {
            "fingerprint": self._fingerprint,
            "engine_version": self.engine_version,
            "completed": self.completed,
        }
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        os.replace(tmp_path, self._path)
