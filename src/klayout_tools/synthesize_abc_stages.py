"""Checkpoint generic gates and bound the generic ABC invocation."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import env_provenance
from .synthesize_abc import (
    AbcMapping,
    FastMode,
    generic_abc,
    record_mapping,
    synth_lines,
)


class SynthesizeError(Exception):
    """A request, engine or artifact error during synthesis."""


@dataclass(frozen=True)
class StagedMapping:
    frontend: str
    generic: str
    tail: str
    netlist: Path
    stats: Path
    timeout_s: float

    def check_checkpoint(self) -> None:
        for path in (self.netlist, self.stats, self.netlist.with_suffix(".il")):
            try:
                if path.is_file() and path.stat().st_size:
                    continue
            except OSError as exc:
                raise SynthesizeError(
                    f"could not inspect pre-ABC checkpoint '{path}': {exc}"
                ) from exc
            raise SynthesizeError(f"missing or empty pre-ABC checkpoint '{path}'")


class AbcTimeout(SynthesizeError):
    """Only the generic ABC invocation exceeded its own wall-clock budget."""

    def __init__(self, stages: StagedMapping, log: str):
        super().__init__(f"generic ABC exceeded {stages.timeout_s:g}s")
        self.stages = stages
        self.log = log

    def report(
        self,
        top: str,
        repo_root: str | None,
        provenance: dict[str, Any],
        mapping: AbcMapping | None,
        *,
        arithmetic: dict[str, Any] | None,
        verify_equivalence: bool,
        restructure_timing: bool,
    ) -> dict[str, Any]:
        from .synthesize import (
            SCHEMA_VERSION,
            _read_produced_netlist,
            _read_stats,
            _summarize_warnings,
            _yosys_version,
        )

        stages = self.stages
        _read_produced_netlist(str(stages.netlist))
        stats = _read_stats(str(stages.stats), top)
        counts = stats.get("num_cells_by_type")
        if not isinstance(counts, dict) or (not counts and stats["num_cells"]):
            raise SynthesizeError(f"missing pre-ABC cell counts in '{stages.stats}'")
        partial = {
            "stage": "pre_abc",
            "netlist_path": env_provenance.repo_relative_path(
                str(stages.netlist), repo_root=repo_root
            ),
            "stats_path": env_provenance.repo_relative_path(
                str(stages.stats), repo_root=repo_root
            ),
            "cell_counts": counts,
            "abc_timeout_s": stages.timeout_s,
        }
        skipped = {"status": "not_run", "reason": "abc_timeout"}
        response = {
            "schema_version": SCHEMA_VERSION,
            "engine": "yosys",
            "engine_version": _yosys_version(),
            "hdl_toplevel": top,
            "parameters": provenance["parameters"],
            "defines": provenance["defines"],
            "run_id": stages.netlist.parent.name,
            "status": "inconclusive",
            "reason": "abc_timeout",
            "partial": partial,
            "provenance": provenance,
            "warnings": _summarize_warnings(self.log),
            "arithmetic": arithmetic,
            "equivalence": skipped if verify_equivalence else None,
            "restructuring": skipped if restructure_timing else None,
        }
        if "yosys_timeout_s" in provenance:
            response["yosys_timeout_s"] = provenance["yosys_timeout_s"]
        record_mapping(mapping, response, provenance)
        return response


def write_mapping(
    script_path: str,
    frontend: list[str],
    tail: list[str],
    top: str,
    fast: FastMode,
    timeout_s: float | None,
    *,
    repo_root: str | None,
    pdk_root: str | None,
    run_frontend: list[str] | None = None,
    run_tail: list[str] | None = None,
) -> str | StagedMapping:
    from .synthesize import _publish_script, _script_path_text

    if timeout_s is None:
        run_lines = None
        if run_frontend is not None:
            run_lines = run_frontend + synth_lines(top, fast) + (run_tail or [])
        return _publish_script(
            script_path,
            frontend + synth_lines(top, fast) + tail,
            pdk_root,
            run_lines=run_lines,
        )

    script = Path(script_path)
    netlist = script.parent / f"{top}_pre_abc.v"
    stats = script.parent / f"{top}_pre_abc.json"

    def path_text(path: Path, repo: str | None) -> str:
        return _script_path_text(str(path), repo_root=repo)

    def script_lines(
        active_frontend: list[str], active_tail: list[str], repo: str | None
    ):
        before = path_text(netlist.with_suffix(".il"), repo)
        after = path_text(script.parent / f"{top}_post_abc.il", repo)
        pre = [
            *active_frontend,
            f"synth -noabc -top {top}",
            f"write_rtlil {before}",
            f"write_verilog -noattr {path_text(netlist, repo)}",
            f"tee -q -o {path_text(stats, repo)} stat -json -top {top}",
        ]
        generic = [f"read_rtlil {before}", generic_abc(fast), f"write_rtlil {after}"]
        return pre, generic, [f"read_rtlil {after}", "opt -fast", *active_tail]

    commit_pre, commit_generic, commit_tail = script_lines(frontend, tail, repo_root)
    run_triple = None
    if run_frontend is not None and repo_root is not None:
        run_triple = script_lines(run_frontend or [], run_tail or [], None)
    pre_script = _publish_script(
        str(script.with_suffix(".pre_abc.ys")),
        commit_pre,
        pdk_root,
        run_lines=run_triple[0] if run_triple else None,
    )
    generic_script = _publish_script(
        str(script.with_suffix(".generic_abc.ys")),
        commit_generic,
        pdk_root,
        run_lines=run_triple[1] if run_triple else None,
    )
    tail_script = _publish_script(
        script_path,
        commit_tail,
        pdk_root,
        run_lines=run_triple[2] if run_triple else None,
    )
    return StagedMapping(
        pre_script, generic_script, tail_script, netlist, stats, timeout_s
    )


def run_mapping(
    scripts: StagedMapping | str, *, cwd: str | None = None
) -> tuple[str, str]:
    """Return the mapped-output script and the logs from the executed stages."""
    from .synthesize import _run_yosys

    if isinstance(scripts, str):
        return scripts, _run_yosys(scripts, cwd=cwd)

    frontend_log = _run_yosys(scripts.frontend, cwd=cwd)
    scripts.check_checkpoint()
    try:
        generic_log = _run_yosys(scripts.generic, timeout_s=scripts.timeout_s, cwd=cwd)
    except SynthesizeError as exc:
        if not isinstance(exc.__cause__, subprocess.TimeoutExpired):
            raise
        output = exc.__cause__.stdout or ""
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        raise AbcTimeout(scripts, frontend_log + "\n" + output) from exc
    tail_log = _run_yosys(scripts.tail, cwd=cwd)
    return scripts.tail, "\n".join([frontend_log, generic_log, tail_log])
