"""ABC mapping settings shared by synthesis, arithmetic trials and verification."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ._paths import _reject_unknown_keys

FastMode = Literal["native", "inline"] | None


@dataclass(frozen=True)
class AbcMapping:
    fast: bool
    script_name: str | None
    script_bytes: bytes | None

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

    def record(self, response: dict[str, Any], provenance: dict[str, Any]) -> None:
        disclosure = self.report()
        response["synthesis"] = disclosure
        if disclosure["abc_script"] is not None:
            provenance["abc_script"] = disclosure["abc_script"]

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


def resolve_mapping(
    request: dict[str, Any], request_path: str, error_cls: type[Exception]
) -> AbcMapping | None:
    if "synthesis" not in request:
        return None
    spec = request["synthesis"]
    if not isinstance(spec, dict):
        raise error_cls("request.synthesis must be an object")
    _reject_unknown_keys(
        spec, ("abc_fast", "abc_script"), request_path, "synthesis", error_cls
    )
    fast = spec.get("abc_fast", False)
    if not isinstance(fast, bool):
        raise error_cls("request.synthesis.abc_fast must be a boolean")
    if "abc_script" not in spec:
        return AbcMapping(fast, None, None)
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
    return AbcMapping(False, os.path.relpath(path, directory), data)


def synth_lines(top: str, fast: FastMode) -> list[str]:
    if fast is None:
        return [f"synth -top {top}"]
    flag = "-fast" if fast == "native" else "-script +strash;dretime;map"
    return [f"synth -noabc -top {top}", f"abc {flag}", "opt -fast"]


def liberty_command(
    liberty: str,
    fast: FastMode,
    script_text: str | None,
    *,
    constrained: bool,
    delay_ps: int | None,
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
            commands += f";buffer;upsize{target};dnsize{target};stime,-p"
        script_text = "+" + commands
    command = f"abc -liberty {liberty}"
    if script_text is not None:
        command += f" -script {script_text}"
    return command


def _identity(report: Any) -> Any:
    if report is None:
        return {"abc_fast": False, "abc_script_sha256": None}
    if not isinstance(report, dict):
        return report
    script = report.get("abc_script")
    if not isinstance(report.get("abc_fast"), bool) or "abc_script" not in report:
        return report
    if script is not None and not (
        isinstance(script, dict) and isinstance(script.get("sha256"), str)
    ):
        return report
    return {
        "abc_fast": report.get("abc_fast"),
        "abc_script_sha256": script.get("sha256")
        if isinstance(script, dict)
        else script,
    }


def mapping_check(committed: Any, mapping: AbcMapping | None) -> dict[str, Any]:
    expected = _identity(committed)
    actual = _identity(mapping.report() if mapping is not None else None)
    return {
        "field": "synthesis",
        "expected": expected,
        "actual": actual,
        "match": expected == actual,
    }
