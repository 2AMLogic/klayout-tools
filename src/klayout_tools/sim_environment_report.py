"""``environment``-block contributors for ``klt sim``'s corner-sweep report
(issue #5600).

:func:`klayout_tools.sim._run_sim_corners` assembles its report's
``environment`` block from a long ordered sequence of contributions. Most of
them already came from small, individually testable ``*_environment(...)``
helpers -- :func:`~klayout_tools.sim_portable.portable_environment`,
:func:`~klayout_tools.sim_param_file.param_file_environment` and friends --
but eleven were still written inline in the sweep function's epilogue, as a
flat append-point that every feature needing to disclose something extended
in place. This module holds those eleven, in the same shape the pattern
already used. The corner coverage helper shares the PDK's per-flavour
classification with the sweep report (#5663).

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
from dataclasses import dataclass
from typing import Any

from . import _warn, device_multiplier_lint, env_provenance, sim_provenance
from . import sim_staging as sim_staging
from ._batch_common import apply_unrun_signal
from ._provenance import sha256_file
from .pdk import PdkNotFoundError, list_corners
from .sim_model_digests import ModelInputDigests

__all__ = [
    "ModelInputDigests",
    "NetlistDigests",
    "backend_environment",
    "budget_environment",
    "corner_coverage_environment",
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


def _corner_process_sections(corner_points: list[Any]) -> list[str]:
    """List process sections in sweep order for coverage and Monte Carlo reports."""
    return [
        name
        for point in corner_points
        for name in (
            point.process_sections
            if point.process_sections is not None
            else ([point.process] if point.process else [])
        )
    ]


def corner_coverage_environment(
    request: dict[str, Any], corner_points: list[Any]
) -> dict[str, Any]:
    """Report typical and unknown device flavours at requested non-typical corners.

    Use :func:`klayout_tools.pdk.list_corners`'s classification (#3794, #5663).
    Each section lists proven typical flavours in ``unmoved_devices`` and
    inconclusive ones in ``unknown_devices``. Omit empty lists, empty sections,
    and the whole fragment when no PDK data or reportable sections remain.
    """
    models = request.get("models") or {}
    if not (models.get("pdk") or models.get("pdk_root")):
        return {}
    process_sections = list(dict.fromkeys(_corner_process_sections(corner_points)))
    if not process_sections:
        return {}
    try:
        report = list_corners(variant=models.get("pdk"), root=models.get("pdk_root"))
    except PdkNotFoundError:
        return {}
    by_corner = {entry["corner"]: entry for entry in report["corners"]}
    entries: list[dict[str, Any]] = []
    for name in process_sections:
        corner = by_corner.get(name)
        if corner is None:
            continue
        devices = [
            device
            for section in corner["sections"]
            for device in section.get("devices", [])
        ]
        if not devices:
            continue
        entry = _corner_coverage_entry(name, devices)
        if entry is not None:
            entries.append(entry)
    if not entries:
        return {}
    return {"corner_coverage": {"pdk": report["pdk"], "sections": entries}}


def _corner_coverage_entry(
    name: str, devices: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """One ``corner_coverage.sections[]`` row, or ``None`` when it must be
    omitted (the typical section, or nothing to report either way)."""
    unmoved = [d["flavour"] for d in devices if d["skew"] == "typical"]
    unknown = [d["flavour"] for d in devices if d["skew"] == "unknown"]
    if name == "typical" or not (unmoved or unknown):
        return None
    entry: dict[str, Any] = {"process": name}
    if unmoved:
        entry["unmoved_devices"] = unmoved
    if unknown:
        entry["unknown_devices"] = unknown
    return entry


@dataclass(frozen=True)
class NetlistDigests:
    """The netlist byte digests a report cites, captured once, before the
    sweep dispatches any corner (issue #5569).

    ``environment.netlist_sha256``, the ``.include`` closure and
    ``provenance.input.content_hash`` all pin the bytes the job was staged
    with. Re-reading the file at report assembly -- minutes to hours after a
    ``batch`` submit -- would let an edit in between change what the report
    claims to pin, or crash the epilogue on a file deleted meanwhile.

    ``closure`` is the :func:`netlist_closure_environment` fragment, so the
    ``netlist_closure``/``netlist_closure_error`` disclosure is unchanged;
    only the moment of capture moved. ``sha256`` is ``None`` when the file
    could not be hashed at capture time.
    """

    sha256: str | None
    closure: dict[str, Any]

    @classmethod
    def capture(
        cls, request: dict[str, Any], netlist_path: str, *, repo_root: str | None
    ) -> NetlistDigests:
        return cls(
            sha256=sha256_file(netlist_path),
            closure=netlist_closure_environment(
                request, netlist_path, repo_root=repo_root
            ),
        )

    def environment(
        self,
        attach_job_id: str | None,
        resume: bool,
        remote_environment: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """``environment.netlist_sha256`` and ``netlist_sha256_scope``.

        ``client-at-collection`` describes the request resolved by an attach
        or a batch resume that only collected previously dispatched jobs.
        """
        collection_only = attach_job_id is not None
        if not collection_only and resume and remote_environment is not None:
            jobs = remote_environment.get("fleet", [remote_environment])
            # A missing shard report or timestamp leaves dispatch unknown.
            collection_only = bool(jobs) and all(
                job is not None
                and job.get("client_submitted_at", False) is None
                and job.get("client_launched_at", False) is None
                for job in jobs
            )
        return {
            "netlist_sha256": self.sha256,
            "netlist_sha256_scope": (
                "client-at-collection" if collection_only else "staged"
            ),
        }

    def pin_provenance_input(self, provenance: dict[str, Any]) -> None:
        """Re-pin ``provenance.input.content_hash`` to the captured digest.

        ``build_provenance`` hashes the path it is handed at call time; this
        makes the block equal ``sha256:<environment.netlist_sha256>`` by
        construction, even when the file changed (or vanished) mid-run --
        ``klt signoff --manifest``'s staleness gate reads the former. A
        no-op when nothing was captured, leaving the hash-the-path value.
        """
        block = provenance.get("input")
        if block is not None and self.sha256 is not None:
            block["content_hash"] = f"sha256:{self.sha256}"


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
    request: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the remote or batch environment for the simulation report.

    ``apply_unrun_signal`` preserves the worker identity and adds resume
    details when corners were lost. It uses the retained checkpoint, when
    available, to point a later ``--resume`` at the missing corners.
    A declared PDK revision is compared with the workers' observed stamps
    in the sibling ``pdk_revision_comparison`` report field.
    """
    if remote_environment is None:
        return {}
    return {
        "remote": apply_unrun_signal(
            remote_environment,
            corners_lost,
            artifacts_dir,
            checkpoint_path,
        ),
        **sim_provenance.pdk_revision_comparison_environment(
            remote_environment, request
        ),
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

    Instance cards are read from the netlist and included DUT files (issue
    #6684). Included calls between sky130 primitive-library devices are
    model internals. Other calls inherit their include site's scope. The shared
    ``sim_staging`` resolver supplies the full closure for checking each
    card, and a card resolves its callee from its own enclosing
    subcircuits outward, so a net carrying
    ``mult=6`` and no ``m=`` against a subcircuit that declares ``mult``
    but never forwards it is named instead of silently simulating 1x. A
    finding from a closure file carries that file's repo-relative label as
    ``file``; a top-level finding keeps the pre-#6684 shape, and the
    ``note``/``unevaluated`` disclosures cover the netlist's own cards
    only (a PDK library's parametric internals must not add noise).
    Additive/optional: ``{}`` -- the key absent -- when there is
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
    root_name = "netlist.cir"
    sources, not_scanned = _multiplier_sources(
        root_name, netlist_path, netlist_text, request, repo_root
    )
    cards, subckts = device_multiplier_lint.scan_closure(root_name, sources)
    report = device_multiplier_lint.instance_report(cards, subckts)
    has_content = (
        bool(report["findings"]) or "note" in report or "unevaluated" in report
    )
    if not has_content:
        return {}
    if not_scanned:
        report["not_scanned"] = not_scanned
    if report["findings"]:
        _warn.warn(
            f"warning: {len(report['findings'])} device-multiplier finding(s) "
            "for this netlist; see environment.device_multiplier.findings"
        )
    return {"device_multiplier": report}


def _multiplier_sources(
    root_name: str,
    netlist_path: str,
    netlist_text: str,
    request: dict[str, Any],
    repo_root: str | None,
) -> tuple[dict[str, tuple[str, str | None]], list[dict[str, str]]]:
    """Pair each staged text with its original report label. Rewritten include
    targets are the edges between these texts, including in nested fragments."""
    sources: dict[str, tuple[str, str | None]] = {root_name: (netlist_text, None)}
    try:
        staged = sim_staging.stage_netlist(
            netlist_path,
            netlist_staged_name=root_name,
            roots=sim_staging.host_resolved_roots(request or {}),
        )
    except sim_staging.IncludeStagingError:
        return sources, [{"target": "<include closure>", "reason": "unresolved"}]

    not_scanned = [
        {"target": target, "reason": reason}
        for target, reason in staged.host_resolved_detail
    ]
    if staged.netlist.content is not None:
        sources[root_name] = (staged.netlist.content, None)
    netlist_real = os.path.realpath(netlist_path)
    for item in staged.includes:
        path = item.source_path or item.local_path
        assert path is not None
        if os.path.realpath(path) == netlist_real:
            continue
        info = env_provenance.repo_relative_path(path, repo_root=repo_root)
        label = info.get("path") or (
            f"<{info.get('scope', 'external')}>/{os.path.basename(path)}"
        )
        text = item.content
        if text is None:
            try:
                with open(path, encoding="utf-8") as handle:
                    text = handle.read()
            except (OSError, UnicodeDecodeError):
                not_scanned.append({"target": label, "reason": "unreadable"})
                continue
        sources[item.staged_name] = (text, label)
    return sources, not_scanned
