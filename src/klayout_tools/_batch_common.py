"""Helpers shared by the batch-offload modules (``netlist_batch``,
``extract_batch``, ``place_and_route_batch``, ``sim_batch``,
``signoff_generator_batch``).

Error strings and ``details`` keys built here are part of the JSON contract
(``docs/json-contract.md``); change them here, once.
"""

from __future__ import annotations

import math
import os
import sys
import tarfile
import uuid
from collections.abc import Sequence
from typing import Any, TypeVar

# ``ExtractError``/``NetlistError`` share a ``code``/``job_id``/``details``
# keyword shape that ``type[Exception]`` cannot express, so construction goes
# through ``Any`` while the return type stays the caller's own class.
_E = TypeVar("_E", bound=Exception)

BATCH_TREE_ROOT_FILENAME = "tree_root.txt"
BATCH_JOB_FAILED_CODE = "batch_job_failed"
BATCH_JOB_TIMEOUT_CODE = "batch_job_timeout"
BATCH_POLL_TIMEOUT_CODE = "batch_poll_timeout"


def _ctor(err_cls: type[_E]) -> Any:
    return err_cls


def make_job_id(kind: str) -> str:
    """A fresh ``klt-<kind>-<12 hex>`` job id, in the ``[A-Za-z0-9._-]+``
    alphabet the launch script accepts (it becomes an S3 prefix and an EC2
    tag), recognisable by ``kind`` in the bucket."""
    return f"klt-{kind}-{uuid.uuid4().hex[:12]}"


def scrub_tarinfo(info: tarfile.TarInfo) -> tarfile.TarInfo:
    """No submitting-host user or group names, and a zeroed mtime so the same
    input tree always yields the same archive bytes."""
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.mtime = 0
    return info


def read_runner_roots(collected: str) -> list[str]:
    """The runner-side stage roots recorded in ``tree_root.txt``: absolute
    paths without a trailing ``/``, longest first so a resolved path is
    rewritten before a prefix of it."""
    try:
        with open(
            os.path.join(collected, BATCH_TREE_ROOT_FILENAME), encoding="utf-8"
        ) as handle:
            lines = [line.strip().rstrip("/") for line in handle.read().splitlines()]
    except OSError:
        return []
    return sorted(
        {line for line in lines if line.startswith("/")}, key=len, reverse=True
    )


def job_timeout_seconds(timeout_s: float) -> int:
    """``--timeout-s`` plus the per-job overhead ``klt sim`` adds, so the inner
    ``timeout`` always fires before the harness's outer one. Never capped here;
    the fleet ceiling is each module's own concern."""
    from . import sim_batch

    return int(math.ceil(sim_batch._default_batch_job_timeout_s(1, timeout_s)))


def rewrite_runner_paths(value: Any, runner_roots: list[str], local_root: str) -> Any:
    """``value`` with every string under a runner scratch root re-rooted at
    ``local_root`` (the directory a local run would have written)."""
    local_root = local_root.rstrip("/")
    if isinstance(value, str):
        for root in runner_roots:
            if value == root or value.startswith(root + "/"):
                return local_root + value[len(root) :]
        return value
    if isinstance(value, list):
        return [rewrite_runner_paths(item, runner_roots, local_root) for item in value]
    if isinstance(value, dict):
        return {
            key: rewrite_runner_paths(item, runner_roots, local_root)
            for key, item in value.items()
        }
    return value


def job_failure_parts(
    *,
    job_id: str,
    state: str,
    exit_code: int | None,
    detail: str,
    runner_error: dict[str, Any] | None,
    note: str,
    stderr_tail: str,
    harness_log: str | None,
) -> tuple[str, str, dict[str, Any]]:
    """``(message, code, details)`` for a job that did not finish ``done``.

    ``message`` has no trailing "no local fallback" clause; the caller appends
    its own. ``details`` is empty when the runner's own klt error was
    recovered. ``harness_log`` is the full-log location, or ``None`` to point
    at the job's S3 prefix generically.
    """
    head = f"batch job {job_id} finished {state!r}" + (
        f" (exit {exit_code})" if exit_code is not None else ""
    )
    if detail:
        head += f" ({detail})"
    code: str | None
    if runner_error is not None:
        message = f"{head}: the runner's klt reported: {runner_error['message']}"
        code = runner_error.get("code")
    else:
        message = f"{head} without a usable report"
        if note:
            message += f" [output recovery: {note}]"
        message += (
            f" -- full log: {harness_log}"
            if harness_log
            else " -- see harness.log under the job's S3 prefix"
        )
        if stderr_tail:
            message += f"\nlast of the runner's stderr.log:\n{stderr_tail}"
        code = None
    if code is None:
        code = BATCH_JOB_TIMEOUT_CODE if state == "timeout" else BATCH_JOB_FAILED_CODE
    details: dict[str, Any] = {}
    if runner_error is None:
        details["remote_exit_code"] = exit_code
        details["remote_state"] = state
        if harness_log:
            details["harness_log"] = harness_log
        if stderr_tail:
            details["remote_stderr_tail"] = stderr_tail
    return message, code, details


def resumable(
    environment: dict[str, Any] | None, manifest: Any
) -> dict[str, Any] | None:
    """``environment`` plus the additive "incomplete, resumable" signal
    (issue #4856) when ``manifest`` (a shard manifest) still has shards that
    were submitted but never launched; ``environment`` unchanged otherwise."""
    count = manifest.unlaunched() if manifest is not None else 0
    if not count:
        return environment
    return {
        **(environment or {}),
        "unlaunched_shards": count,
        "resumable": True,
        "shard_map": str(manifest.path),
    }


def with_unrun_signal(
    environment: dict[str, Any] | None, unrun: int, state_path: str | None
) -> tuple[dict[str, Any] | None, bool]:
    """``environment`` plus the additive "incomplete, resumable" signal for
    corners that never ran (``lost_shard``, ``batch_job_interrupted``, ...;
    issue #5364): ``unrun_units`` (count), ``resumable: true`` and -- unless
    :func:`resumable` already set it -- ``shard_map`` (``state_path``, the
    retained shard map or checkpoint). Returns ``(environment, hint_needed)``;
    ``hint_needed`` is False when the unlaunched-shard hint already covers
    the rerun. Unchanged for ``unrun == 0`` or no retained state."""
    if not unrun or not state_path or environment is None:
        return environment, False
    out = {**environment, "unrun_units": unrun, "resumable": True}
    out.setdefault("shard_map", state_path)
    return out, "unlaunched_shards" not in environment


def apply_unrun_signal(
    environment: dict[str, Any] | None,
    unrun: int,
    artifacts_dir: str,
    checkpoint_path: str | None,
) -> dict[str, Any] | None:
    """Issue #5364 wiring for ``run_sim``: when ``unrun`` corners never ran
    (only a ``batch`` run synthesizes them), pick the retained resume state
    (the shard map in ``artifacts_dir``, else ``checkpoint_path``; ``None``
    when no checkpoint was retained), add the :func:`with_unrun_signal`
    fields and print the ``--resume`` hint to stderr once. Returns
    ``environment`` unchanged when nothing applies."""
    if not unrun:
        return environment
    from .sim_batch import BATCH_SHARDS_FILENAME  # circular at import time

    manifest = os.path.join(artifacts_dir, BATCH_SHARDS_FILENAME)
    state = manifest if os.path.exists(manifest) else checkpoint_path
    environment, hint = with_unrun_signal(environment, unrun, state)
    if hint:
        print(
            f"klt sim: {unrun} unit(s) never ran (lost or interrupted batch "
            f"shard); rerun with --resume and the same outdir to finish them "
            f"(state: {state})",
            file=sys.stderr,
        )
    return environment


def resolve_backend_value(
    flag: str | None,
    env_name: str,
    supported: Sequence[str],
    err_cls: type[_E],
) -> str:
    """``--backend`` > ``$<env_name>`` > ``"local"``; an unknown value raises
    ``err_cls`` naming the flag or variable it came from."""
    if flag is not None:
        value, source = flag, "--backend"
    else:
        value = os.environ.get(env_name, "").strip() or "local"
        source = f"${env_name}"
    if value not in supported:
        raise _ctor(err_cls)(
            f"{source} must be one of {', '.join(supported)} (got {value!r})"
        )
    return value


def resolve_batch_config(
    pdk: str, timeout_s: float, err_cls: type[_E], no_fallback: str
) -> Any:
    """:func:`sim_batch._resolve_batch_config` fed a synthetic one-unit
    request, so the fleet config resolves exactly as for ``klt sim``. Its
    ``SimError``/``BatchError`` becomes ``err_cls``."""
    from . import sim_batch
    from .sim import SimError

    try:
        return sim_batch._resolve_batch_config(
            {"models": {"pdk": pdk}}, corner_count=1, timeout_s=timeout_s
        )
    except (SimError, sim_batch.BatchError) as exc:
        raise _ctor(err_cls)(
            f"batch backend: {exc} {no_fallback}", code=getattr(exc, "code", None)
        ) from exc


def transport_error(job_id: str, exc: Any, err_cls: type[_E], no_fallback: str) -> _E:
    """``err_cls`` for a ``sim_batch.BatchError`` raised while submitting,
    launching or polling; the poll timeout says the job may still finish."""
    from . import sim_batch

    if isinstance(exc, sim_batch.BatchPollTimeout):
        return _ctor(err_cls)(
            f"batch job {job_id}: {exc} -- it may still finish on the fleet, but "
            f"no netlist was collected {no_fallback}",
            code=exc.code or BATCH_POLL_TIMEOUT_CODE,
            job_id=job_id,
        )
    return _ctor(err_cls)(
        f"batch job {job_id}: {exc} {no_fallback}", code=exc.code, job_id=job_id
    )


def job_failure(
    *,
    job_id: str,
    status: dict[str, Any],
    runner_error: dict[str, Any] | None,
    note: str,
    stderr_tail: str,
    harness_log: str | None,
    err_cls: type[_E],
    no_fallback: str,
) -> _E:
    """:func:`job_failure_parts` as an ``err_cls`` with ``no_fallback``
    appended to the message."""
    from .sim_exit_reason import status_exit_code

    message, code, details = job_failure_parts(
        job_id=job_id,
        state=str(status.get("state") or ""),
        exit_code=status_exit_code(status),
        detail=str(status.get("detail") or "").strip(),
        runner_error=runner_error,
        note=note,
        stderr_tail=stderr_tail,
        harness_log=harness_log,
    )
    return _ctor(err_cls)(
        f"{message} {no_fallback}", code=code, job_id=job_id, details=details or None
    )


# --------------------------------------------------------------------------- #
# job.json: the shared command and spec assembler
# --------------------------------------------------------------------------- #

#: A ``$EDA_OUTPUT_DIR`` member, quoted for the runner's shell.
_OUT = '"$EDA_OUTPUT_DIR/{}"'


def build_job_command(
    *,
    tree_member: str,
    run: str,
    err_cls: type[_E],
    stdout_target: str | None,
    client_version: str | None = None,
    version_check: str = "enforce",
    preflight_extra: str = "",
    extra_setup: Sequence[str] = (),
    tree_root_subdir: str | None = None,
    extra_tail: str = "",
) -> str:
    """One batch job's ``cmd``: the runner-version preflight, the shared setup
    prologue, ``run``, and the exit-status tail.

    Every byte of the result is the batch contract -- the fleet harness parses
    it (``docs/schemas/batch-job-spec.schema.json``) -- so it is assembled
    here once instead of per verb.

    ``tree_member`` is the input tarball's ``inputs/`` name. The prologue
    resolves ``$EDA_INPUT_DIR``/``$EDA_OUTPUT_DIR``, makes a scratch
    directory, exports a fallback ``HOME`` (the runner can leave it unset,
    which killed xschem's Tcl init outright -- commit ``bf3d1ae10``), unpacks
    the tree and ``cd``s into it.

    ``run`` is the per-verb inner command, already quoted. ``err_cls`` is the
    calling module's error class, raised in place of the preflight's
    ``SimError`` when the submitting client's version cannot be pinned, so
    every verb emits ``docs/json-contract.md``'s error envelope rather than a
    traceback. ``preflight_extra`` is appended to the preflight script (a
    runner-capability gate).

    ``extra_setup`` lines join the prologue just before ``cd "$w"`` (the
    place-and-route unit-request install). ``tree_root_subdir`` records the
    runner's scratch root in :data:`BATCH_TREE_ROOT_FILENAME` for
    :func:`read_runner_roots` -- ``""`` for the tree root itself, a
    tree-relative path for a stage directory under it, ``None`` to record
    none. The filename comes from this module's constant, so the reader and
    every writer can never name different files.

    ``stdout_target`` is the ``$EDA_OUTPUT_DIR`` member ``run``'s stdout goes
    to, with its stderr teed to ``stderr.log`` and the job exiting with
    ``run``'s own status: ``report.json`` for a ``klt <verb>`` job. ``None``
    -- only ``klt sim``'s multi-unit command, which redirects per unit and
    publishes each unit's exit code -- emits neither the redirect nor the
    status capture, and the job exits 0. ``extra_tail`` runs once the status
    is captured, whether or not ``run`` succeeded (the place-and-route
    artifact copy, the generator output publish); it needs a
    ``stdout_target``.
    """
    from . import _provenance, sim_batch

    version = (
        client_version if client_version is not None else _provenance._klt_version()
    )
    try:
        preflight = sim_batch._runner_preflight_script(
            version, enforce=version_check == "enforce"
        )
    except Exception as exc:  # SimError: a client version unsafe to embed
        raise _ctor(err_cls)(str(exc)) from exc
    setup = [
        'EDA_INPUT_DIR=$(cd "$EDA_INPUT_DIR" && pwd -P)',
        'EDA_OUTPUT_DIR=$(cd "$EDA_OUTPUT_DIR" && pwd -P)',
        'w=$(mktemp -d) && export HOME="${HOME:-$(mktemp -d)}"',
        f'tar -xzf "$EDA_INPUT_DIR/{tree_member}" -C "$w"',
        *extra_setup,
        'cd "$w"',
    ]
    if tree_root_subdir is not None:
        at = f"/{tree_root_subdir}" if tree_root_subdir else ""
        setup.append(
            f'printf \'%s\\n%s\\n\' "$w{at}" "$(pwd -P){at}" >'
            + _OUT.format(BATCH_TREE_ROOT_FILENAME)
        )
    if stdout_target is None:
        tail = "; exit 0"
    else:
        run += (
            f" > {_OUT.format(stdout_target)}"
            f" 2> >(tee {_OUT.format(sim_batch.BATCH_STDERR_FILENAME)} >&2)"
        )
        tail = "; rc=$?; wait $! 2>/dev/null"
        tail += f"; {extra_tail}" if extra_tail else ""
        tail += "; exit $rc"
    return preflight + preflight_extra + " && ".join(setup) + " && " + run + tail


def build_job_spec(
    *,
    cmd: str,
    tool: str,
    pdk_variant: str | None,
    tree_member: str,
    tree_label: str,
    tarball_path: str,
    timeout_s: float,
    timeout_seconds: int | None = None,
    extra_inputs: Sequence[Any] = (),
) -> Any:
    """One batch job as a ``sim_batch.BatchJobSpec``: ``cmd`` (from
    :func:`build_job_command`), the input tree, and the job's wall-clock cap.

    The cap defaults to :func:`job_timeout_seconds` -- ``timeout_s`` plus the
    per-job overhead, so the inner ``timeout`` always fires before the
    harness's outer one. Pass ``timeout_seconds`` only where ``timeout_s`` is
    not an inner timeout for the headroom to protect.
    """
    from . import sim_batch

    return sim_batch.BatchJobSpec(
        cmd=cmd,
        tool=tool,
        pdk_variant=pdk_variant,
        timeout_seconds=(
            job_timeout_seconds(timeout_s)
            if timeout_seconds is None
            else timeout_seconds
        ),
        inputs=(
            sim_batch.BatchJobInput(
                name=tree_member, label=tree_label, local_path=tarball_path
            ),
            *extra_inputs,
        ),
    )
