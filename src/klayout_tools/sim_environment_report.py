"""``environment``-block contributors for ``klt sim``'s corner-sweep report
(issue #5600).

:func:`klayout_tools.sim._run_sim_corners` assembles its report's
``environment`` block from a long ordered sequence of contributions. Most of
them already came from small, individually testable ``*_environment(...)``
helpers -- :func:`~klayout_tools.sim._portable_environment`,
:func:`~klayout_tools.sim._corner_coverage_environment`,
:func:`~klayout_tools.sim_param_file.param_file_environment` and friends --
but eleven were still written inline in the sweep function's epilogue, as a
flat append-point that every feature needing to disclose something extended
in place. This module holds those eleven, in the same shape the pattern
already used:

* each takes only the two-to-five values it actually needs, never the
  sweep function's ambient locals;
* each returns the ``environment`` *fragment* it owns -- ``{}`` when the
  request did not declare that kind of input, so the caller stays a plain
  ``environment.update(...)`` with no conditional of its own;
* key insertion order is the caller's, unchanged -- these helpers never see
  the ``environment`` dict itself.

**These keys are the JSON contract** (``docs/cli/sim.md``,
``docs/json-contract.md``). Every one is additive/optional in the sense that
it appears only when the request declared the corresponding input -- so the
failure mode to protect against is a key that silently *stops* appearing, not
one that changes shape. Keep the present/absent condition of each helper
identical to what it documents.
"""

from __future__ import annotations

import os
import sys
from typing import Any

from . import device_multiplier_lint, env_provenance
from . import sim_staging as sim_staging
from ._batch_common import apply_unrun_signal

__all__ = [
    "backend_environment",
    "budget_environment",
    "device_multiplier_environment",
    "fail_fast_probe_environment",
    "monte_carlo_environment",
    "netlist_closure_environment",
    "netlist_source_environment",
    "orphaned_environment",
    "remote_entry_environment",
    "resume_environment",
    "timeout_preflight_environment",
]


def netlist_closure_environment(
    request: dict[str, Any],
    netlist_path: str,
    *,
    repo_root: str | None,
) -> dict[str, Any]:
    """``environment.netlist_closure`` / ``.netlist_closure_error`` (issue #2799).

    ``netlist_sha256`` covers only the top-level file, so also pin the
    resolved ``.include``/``.inc`` closure (same resolver the off-host
    backends stage with). Additive; an unresolvable closure is *disclosed*
    -- as ``netlist_closure_error`` -- rather than failing the report.

    Exactly one of the two keys is always present.
    """
    try:
        return {
            "netlist_closure": sim_staging.netlist_closure(
                request, netlist_path, repo_root=repo_root
            )
        }
    except sim_staging.IncludeStagingError as exc:
        return {"netlist_closure_error": str(exc)}


def netlist_source_environment(netlist_source: Any) -> dict[str, Any]:
    """``environment.netlist_source``.

    Additive/optional: only present when the request declares it, so
    existing consumers of a request that omits ``netlist_source`` see an
    unchanged ``environment`` block (see ``docs/cli/sim.md``).
    """
    if netlist_source is None:
        return {}
    return {"netlist_source": netlist_source}


def remote_entry_environment(
    remote_environment: dict[str, Any] | None,
    *,
    corners_lost: int,
    artifacts_dir: str,
    checkpoint_path: str | None,
) -> dict[str, Any]:
    """``environment.remote`` -- additive/optional (``remote``/``batch`` only).

    See ``docs/cli/sim.md``. ``checkpoint_path`` is the retained checkpoint
    (``None`` when the sweep did not retain one), which
    :func:`~klayout_tools._batch_common.apply_unrun_signal` uses to point a
    later ``--resume`` at the corners this run never got back.

    The value under ``"remote"`` is whatever ``apply_unrun_signal`` returns:
    ``remote_environment`` itself when no unrun signal applies, else a new
    dict carrying the additive ``unrun_units``/``resumable``/``shard_map``
    fields. Either way a later reader of ``environment["remote"]`` -- notably
    :func:`~klayout_tools.sim_batch_features.pop_remote_diagnostics` -- sees
    exactly what it saw when this was inline.
    """
    if remote_environment is None:
        return {}
    return {
        "remote": apply_unrun_signal(
            remote_environment,
            corners_lost,
            artifacts_dir,
            checkpoint_path,
        )
    }


def backend_environment(backend_provenance: dict[str, Any]) -> dict[str, Any]:
    """``environment.backend``: how the executing backend was decided.

    Always present -- the only one of this module's keys that is not
    conditional on a declared request field.
    """
    return {"backend": backend_provenance}


def monte_carlo_environment(monte_carlo_info: dict[str, Any] | None) -> dict[str, Any]:
    """``environment.monte_carlo``.

    Additive/optional: only present when the request declares ``monte_carlo``
    -- the seed contract's request-level echo, per ``docs/cli/sim.md``'s
    "Monte Carlo" section. Per-sample seed values live on each corner
    (``corners[].monte_carlo``), not here.
    """
    if monte_carlo_info is None:
        return {}
    return {"monte_carlo": monte_carlo_info}


def budget_environment(
    budget_s: float | None,
    *,
    elapsed_s: float,
    exceeded: bool,
    corners_skipped: int,
) -> dict[str, Any]:
    """``environment.budget``: the wall-clock budget as applied.

    Additive/optional: only present when the request declares
    ``options.wall_clock_budget_s`` (or ``--budget-s`` overrides it) -- see
    :func:`~klayout_tools.sim.run_sim`'s docstring and ``docs/cli/sim.md``'s
    "Wall-clock budget" section. ``exceeded``/``corners_skipped`` are always
    reported here, never left for a caller to infer from scanning
    ``corners[].diagnostics`` itself.
    """
    if budget_s is None:
        return {}
    return {
        "budget": {
            "wall_clock_budget_s": budget_s,
            "elapsed_s": elapsed_s,
            "exceeded": exceeded,
            "corners_skipped": corners_skipped,
        }
    }


def orphaned_environment(orphaned: bool) -> dict[str, Any]:
    """``environment.orphaned``.

    Additive/optional: only present when the always-on orphan check actually
    fired -- reported for the benefit of a later ``--resume`` invocation
    inspecting this report; the process that would have read it live is, by
    definition, the one that died.
    """
    if not orphaned:
        return {}
    return {"orphaned": True}


def timeout_preflight_environment(preflight_warning: Any) -> dict[str, Any]:
    """``environment.timeout_preflight_warning`` (issue #1686).

    Additive/optional: only present when the coarse, pre-grid budget
    preflight had something to say (issue #1686, third acceptance criterion)
    -- absent for the common case (an ``options.timeout_s`` that looks
    plausible for the declared ``tran`` window), exactly like ``orphaned``.
    Advisory only -- never blocks the sweep.
    """
    if preflight_warning is None:
        return {}
    return {"timeout_preflight_warning": preflight_warning}


def fail_fast_probe_environment(probe_result: Any) -> dict[str, Any]:
    """``environment.fail_fast_probe`` (issue #1694).

    Additive/optional: only present when
    ``fail_fast_probe``/``options.fail_fast_probe`` opted in *and* the probe
    actually ran and came back conclusive -- reports the measured
    rate/estimate regardless of whether it triggered an abort, so a caller
    can see how close a passing sweep came to the margin, not just learn
    about it after the fact. See ``docs/design/sim-corner-reached-s.md`` and
    ``docs/cli/sim.md``'s "Timeout-budget preflight" section.
    """
    if probe_result is None:
        return {}
    return {"fail_fast_probe": probe_result}


def resume_environment(
    resume: bool,
    *,
    resumed_corners: int,
    checkpoint_path: str | None,
    checkpoint_retained: bool,
    repo_root: str | None,
) -> dict[str, Any]:
    """``environment.resume``: what a resumed sweep reused, and where from.

    Additive/optional: only present when ``options.resume``/``--resume`` was
    requested -- see :func:`~klayout_tools.sim.run_sim`'s docstring.
    ``checkpoint_path`` is normalised like every other path this report
    echoes.
    """
    if not resume:
        return {}
    return {
        "resume": {
            "resumed_corners": resumed_corners,
            "checkpoint_path": env_provenance.repo_relative_path(
                checkpoint_path, repo_root=repo_root
            ),
            "checkpoint_retained": checkpoint_retained,
        }
    }


def device_multiplier_environment(
    netlist_path: str,
    *,
    request: dict[str, Any],
    repo_root: str | None,
) -> dict[str, Any]:
    """``environment.device_multiplier`` (issue #5779): the device-multiplier
    lint's findings for this run's netlist -- warning-only, never a failing
    exit.

    Instance cards of the netlist are checked against the device
    subcircuit texts of the scanned ``.include`` closure (the same shared
    ``sim_staging`` resolver every other disclosure here uses), so a net
    carrying ``mult=6`` and no ``m=`` against a subcircuit that declares
    ``mult`` but never forwards it is named instead of silently simulating
    1x. Additive/optional: ``{}`` -- the key absent -- when there is
    nothing to say (no checkable instance, or every checked pair agrees).
    A closure that cannot be resolved or read is disclosed through the
    lint's ``note`` (instances whose subcircuit text is missing) and
    ``not_scanned``, never an error.
    """
    try:
        with open(netlist_path, encoding="utf-8") as handle:
            netlist_text = handle.read()
    except (OSError, UnicodeDecodeError):
        return {}
    try:
        staged = sim_staging.stage_netlist(
            netlist_path,
            netlist_staged_name="netlist.cir",
            roots=sim_staging.host_resolved_roots(request or {}),
        )
        closure_paths = [item.source_path or item.local_path for item in staged.files]
        not_scanned = [
            {"target": target, "reason": reason}
            for target, reason in staged.host_resolved_detail
        ]
    except sim_staging.IncludeStagingError:
        closure_paths = []
        not_scanned = [{"target": "<include closure>", "reason": "unresolved"}]
    closure_texts: list[str] = [netlist_text]
    for path in closure_paths:
        # Labels follow the scanned-closure convention (issue #2928):
        # repo-relative, never an absolute external path.
        info = env_provenance.repo_relative_path(path, repo_root=repo_root)
        label = info.get("path") or (
            f"<{info.get('scope', 'external')}>/{os.path.basename(path)}"
        )
        try:
            with open(path, encoding="utf-8") as handle:
                closure_texts.append(handle.read())
        except (OSError, UnicodeDecodeError):
            not_scanned.append({"target": label, "reason": "unreadable"})
    cards = device_multiplier_lint.instance_cards(netlist_text)
    subckts = device_multiplier_lint.collect_subckts(closure_texts)
    report = device_multiplier_lint.instance_report(cards, subckts)
    has_content = (
        bool(report["findings"]) or "note" in report or "unevaluated" in report
    )
    if not has_content:
        return {}
    if not_scanned:
        report["not_scanned"] = not_scanned
    if report["findings"]:
        print(
            f"warning: {len(report['findings'])} device-multiplier finding(s) "
            "for this netlist; see environment.device_multiplier.findings",
            file=sys.stderr,
        )
    return {"device_multiplier": report}
