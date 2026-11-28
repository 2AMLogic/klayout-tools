"""ABC mapping settings shared by synthesis, arithmetic trials and verification."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ._paths import reject_unknown_keys
from .synthesize_timeout import validate_budget

FastMode = Literal["native", "inline"] | None

# Yosys combinational primitives after techmap, including the LUT/SOP
# representations ABC can return. Sequential cells have separate grading.
_GENERIC_COMB_CELLS = frozenset(
    """$lut $sop $_BUF_ $_TBUF_ $_NOT_ $_AND_ $_NAND_ $_OR_ $_NOR_ $_XOR_ $_XNOR_
    $_ANDNOT_ $_ORNOT_ $_MUX_ $_NMUX_ $_MUX4_ $_MUX8_ $_MUX16_
    $_AOI3_ $_OAI3_ $_AOI4_ $_OAI4_""".split()
)


def unmapped_comb_cells(counts: dict[str, int]) -> list[str]:
    """Residual generic combinational cell types in the final leaf counts."""
    return sorted(cell for cell in _GENERIC_COMB_CELLS if counts.get(cell))


def check_custom_mapping(
    script_path: str | None, stats: dict[str, Any], error_cls: type[Exception]
) -> None:
    if script_path is None:
        return
    counts = stats.get("num_cells_by_type")
    if not isinstance(counts, dict):
        raise error_cls(
            "could not verify request.synthesis.abc_script: missing cell counts"
        )
    unmapped = unmapped_comb_cells(counts)
    if unmapped:
        raise error_cls(
            "request.synthesis.abc_script left unmapped combinational cells: "
            + ", ".join(unmapped)
        )


@dataclass(frozen=True)
class AbcMapping:
    fast: bool
    script_name: str | None
    script_bytes: bytes | None
    timeout_s: float | None = None

    def report(self) -> dict[str, Any]:
        script = None
        if self.script_bytes is not None:
            script = {
                "path": self.script_name,
                "sha256": hashlib.sha256(self.script_bytes).hexdigest(),
            }
        reduced_passes = ["synth_internal", "liberty"] if self.fast else []
        if script is not None:
            reduced_passes = ["liberty"]
        return {
            "abc_fast": self.fast,
            "abc_script": script,
            "reduced_passes": reduced_passes,
        }

    def fast_mode(self, supports_option: Callable[[str], bool]) -> FastMode:
        if not self.fast:
            return None
        return "native" if supports_option("-fast") else "inline"

    def stage_script(self, output_dir: str, error_cls: type[Exception]) -> str | None:
        if self.script_bytes is None:
            return None
        # Yosys emits an unquoted ABC 'source' command even when its own -script
        # argument is quoted. Stage the exact bytes under a plain filename.
        target = Path(output_dir) / "abc_mapping.script"
        try:
            target.write_bytes(self.script_bytes)
        except OSError as exc:
            raise error_cls(
                f"could not stage request.synthesis.abc_script: {exc}"
            ) from exc
        return str(target)


DEFAULT_MAPPING = AbcMapping(False, None, None)


def record_mapping(
    mapping: AbcMapping | None, response: dict[str, Any], provenance: dict[str, Any]
) -> None:
    if mapping is None:
        return
    if mapping.timeout_s is not None:
        provenance["abc_timeout_s"] = mapping.timeout_s
    disclosure = mapping.report()
    response["synthesis"] = disclosure
    if disclosure["abc_script"] is not None:
        provenance["abc_script"] = disclosure["abc_script"]


def resolve_mapping(
    request: dict[str, Any], request_path: str, error_cls: type[Exception]
) -> AbcMapping | None:
    if "synthesis" not in request:
        return None
    spec = request["synthesis"]
    if not isinstance(spec, dict):
        raise error_cls("request.synthesis must be an object")
    reject_unknown_keys(
        spec,
        ("abc_fast", "abc_script", "abc_timeout_s"),
        request_path,
        "synthesis",
        error_cls,
    )
    timeout_s = (
        validate_budget(
            spec["abc_timeout_s"], "request.synthesis.abc_timeout_s", error_cls
        )
        if "abc_timeout_s" in spec
        else None
    )
    fast = spec.get("abc_fast", False)
    if not isinstance(fast, bool):
        raise error_cls("request.synthesis.abc_fast must be a boolean")
    if "abc_script" not in spec:
        return AbcMapping(fast, None, None, timeout_s)
    if "abc_fast" in spec:
        raise error_cls(
            "request.synthesis.abc_fast and request.synthesis.abc_script "
            "are mutually exclusive"
        )
    raw = spec["abc_script"]
    if not isinstance(raw, str) or not raw:
        raise error_cls(
            "request.synthesis.abc_script must be a non-empty script file path"
        )
    directory = Path(request_path).absolute().parent
    path = directory / raw
    try:
        if not path.is_file():
            raise error_cls(f"request.synthesis.abc_script is not a file: {raw!r}")
        data = path.read_bytes()
    except (OSError, ValueError) as exc:
        raise error_cls(
            f"could not read request.synthesis.abc_script {raw!r}: {exc}"
        ) from exc
    return AbcMapping(False, os.path.relpath(path, directory), data, timeout_s)


def _stime_flags(stats_dump: bool) -> str:
    """The ``stime`` report flags for :func:`liberty_command`'s inline form:
    ``-p`` (critical-path summary) always, plus ``-d`` (statistics-file
    dump, issue #7010) when the run executes from its own directory so the
    dump is recoverable there."""
    return "-pd" if stats_dump else "-p"


def generic_abc(fast: FastMode) -> str:
    if fast is None:
        return "abc"
    return "abc -fast" if fast == "native" else "abc -script +strash;dretime;map"


def synth_lines(top: str, fast: FastMode) -> list[str]:
    if fast is None:
        return [f"synth -top {top}"]
    return [f"synth -noabc -top {top}", generic_abc(fast), "opt -fast"]


def liberty_command(
    liberty: str,
    fast: FastMode,
    script_text: str | None,
    *,
    constrained: bool,
    delay_ps: int | None,
    stats_dump: bool = False,
) -> str:
    if fast == "native":
        return f"abc -fast -liberty {liberty}"
    if fast == "inline":
        # Yosys 0.68 removed -fast. Reproduce 0.67's ABC_FAST_COMMAND_LIB
        # and ABC_FAST_COMMAND_CTR, including its delay-target substitution.
        target = f",-D,{delay_ps}" if delay_ps is not None else ""
        retime = f";retime,-o{target}" if target else ""
        commands = f"strash;dretime{retime};map{target}"
        if constrained:
            # Issue #7010: `-pd` keeps the log report and additionally dumps
            # ABC's statistics file, recovered when the log line is lost.
            stime = _stime_flags(stats_dump)
            commands += f";buffer;upsize{target};dnsize{target};stime,{stime}"
        script_text = "+" + commands
    elif script_text and script_text.startswith("+"):
        # ABC reserves a leading '+' for inline commands, even in a file path.
        script_text = "./" + script_text
    command = f"abc -liberty {liberty}"
    if script_text is not None:
        command += f" -script {script_text}"
    return command


def _identity(report: Any) -> dict[str, Any] | None:
    match report:
        case {"abc_fast": bool() as fast, "abc_script": None, "reduced_passes": passes}:
            digest = None
        case {
            "abc_fast": False,
            "abc_script": {"path": str() as path, "sha256": str() as digest},
            "reduced_passes": passes,
        } if path and not os.path.isabs(path):
            fast = False
        case _:
            return None
    return {
        "abc_fast": fast,
        "abc_script_sha256": digest,
        "reduced_passes": passes,
    }


def mapping_check(
    committed: dict[str, Any], mapping: AbcMapping | None
) -> dict[str, Any]:
    recorded = committed.get("synthesis", DEFAULT_MAPPING.report())
    expected = _identity(recorded)
    actual = _identity((mapping or DEFAULT_MAPPING).report())
    return {
        "field": "synthesis",
        "expected": recorded if expected is None else expected,
        "actual": actual,
        "match": expected is not None and expected == actual,
    }
