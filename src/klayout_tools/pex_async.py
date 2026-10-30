"""Async submit/attach for ``klt pex`` on the ``batch`` backend (issue #4619).

A pex run is several ``run_sim`` calls -- a schematic leg and an extracted leg
per testbench -- so no single job id can address it. ``--submit-only`` runs
each leg with ``run_sim(submit_only=True)`` and records what every leg
returned (its ``job_id``, or the ``batch-shards.json`` path of a sharded leg)
in a ``pex-submit.json`` manifest under the run's artifact root.
``--attach MANIFEST`` re-runs the same ``klt pex`` command, but hands each
leg's recorded value to ``run_sim(attach_job_id=...)`` instead of simulating,
so the rest of the run -- extraction, diagnostics, ``_build_delta_rows`` --
produces the normal report unchanged.
"""

import json
import os
from collections.abc import Callable, Mapping, Sequence
from typing import Any

#: File name of the submit manifest, written under the pex artifact root.
MANIFEST_NAME = "pex-submit.json"

#: Schema of the manifest and of the ``submitted`` status payload.
ASYNC_SCHEMA_VERSION = 1

#: ``PexError.code`` values this module raises.
MANIFEST_INVALID_CODE = "pex_manifest_invalid"
FINGERPRINT_MISMATCH_CODE = "resume_fingerprint_mismatch"
LEG_MISSING_CODE = "pex_attach_leg_missing"
ASYNC_UNSUPPORTED_CODE = "pex_async_unsupported"


def validate_async_flags(
    *,
    submit_only: bool,
    attach: str | None,
    backend: str | None,
    resume: bool | None,
    measure_command: Sequence[str] | None,
    extract_only: bool,
    device_only_leg: bool,
) -> None:
    """Refuse every combination the async forms cannot represent, by name,
    before anything is extracted or written."""
    from .pex import PexError

    if not submit_only and attach is None:
        return
    flag = "--submit-only" if submit_only else "--attach"
    reason = None
    if submit_only and attach is not None:
        reason = (
            "--submit-only and --attach are mutually exclusive: attach collects "
            "an already-submitted run, submit-only launches a new one"
        )
    elif backend is not None and backend != "batch":
        reason = f"{flag} requires backend 'batch' (got '{backend}')"
    elif resume:
        reason = f"{flag} cannot be combined with --resume"
    elif measure_command is not None:
        reason = f"{flag} cannot be combined with --measure-command"
    elif extract_only:
        reason = f"{flag} cannot be combined with --extract-only (nothing is simulated)"
    elif device_only_leg:
        reason = f"{flag} cannot be combined with --device-only-leg"
    if reason is not None:
        raise PexError(reason, code=ASYNC_UNSUPPORTED_CODE)


class AsyncLegs:
    """Routes every pex ``run_sim`` leg: blocking (``mode=None``), submit
    (``"submit"``) or attach (``"attach"``)."""

    def __init__(
        self,
        *,
        submit_only: bool,
        attach: str | None,
        work_dir: str,
    ) -> None:
        self.mode = "submit" if submit_only else "attach" if attach else None
        self.submitting = self.mode == "submit"
        self.manifest_path = (
            os.path.join(work_dir, MANIFEST_NAME)
            if self.submitting
            else os.path.abspath(attach)
            if attach
            else None
        )
        self._legs: list[dict[str, Any]] = []
        self._recorded: dict[tuple[str, str], dict[str, Any]] = {}
        self._manifest: dict[str, Any] = {}
        if self.mode == "attach":
            self._manifest = self._load(self.manifest_path)
            self._recorded = {
                (leg["testbench"], leg["side"]): leg for leg in self._manifest["legs"]
            }

    @staticmethod
    def _load(path: str | None) -> dict[str, Any]:
        from .pex import PexError

        try:
            with open(path or "", encoding="utf-8") as handle:
                manifest = json.load(handle)
            legs = manifest["legs"]
            for leg in legs:
                leg["testbench"], leg["side"], leg["attach"]  # noqa: B018
            manifest["testbenches"], manifest["extracted_netlist_sha256"]  # noqa: B018
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise PexError(
                f"--attach: cannot read pex submit manifest '{path}': {exc!r}",
                code=MANIFEST_INVALID_CODE,
            ) from exc
        return manifest

    def verify(
        self, *, extracted_netlist_sha256: str | None, request_sha256: Mapping[str, str]
    ) -> None:
        """Attach only: refuse a manifest recorded for different inputs than
        this invocation extracted/loaded (the sim ``--attach`` precedent)."""
        from .pex import PexError

        if self.mode != "attach":
            return
        recorded = {
            tb["label"]: tb["request_sha256"] for tb in self._manifest["testbenches"]
        }
        if self._manifest[
            "extracted_netlist_sha256"
        ] != extracted_netlist_sha256 or recorded != dict(request_sha256):
            raise PexError(
                f"--attach: manifest '{self.manifest_path}' was submitted for "
                "different inputs (extracted netlist or testbench requests "
                "changed); re-run the same klt pex command that produced it, "
                "or submit again",
                code=FINGERPRINT_MISMATCH_CODE,
            )

    def run(
        self,
        run_sim: Callable[..., dict[str, Any]],
        request_path: str,
        *,
        testbench: str,
        side: str,
        artifacts_dir: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """One leg. Blocking mode is exactly the plain ``run_sim`` call."""
        from .pex import PexError
        from .sim import SimError

        if self.mode is None:
            return run_sim(request_path, artifacts_dir=artifacts_dir, **kwargs)
        if self.mode == "attach":
            leg = self._recorded.get((testbench, side))
            if leg is None:
                raise PexError(
                    f"--attach: manifest '{self.manifest_path}' has no "
                    f"{side}-side leg for testbench '{testbench}'",
                    code=LEG_MISSING_CODE,
                )
            return run_sim(
                request_path,
                artifacts_dir=artifacts_dir,
                attach_job_id=leg["attach"],
                **kwargs,
            )
        try:
            report = run_sim(
                request_path, artifacts_dir=artifacts_dir, submit_only=True, **kwargs
            )
        except SimError as exc:
            # A leg that cannot be submitted is fatal here: unlike a blocking
            # run there is no half-report to keep.
            raise PexError(
                f"testbench '{testbench}': {side}-side submit failed: {exc}",
                code=exc.code,
            ) from exc
        sharded = bool(report.get("sharded"))
        self._legs.append(
            {
                "testbench": testbench,
                "side": side,
                "sharded": sharded,
                "attach": report["manifest_path"] if sharded else report["job_id"],
                "unit_count": report.get("unit_count"),
            }
        )
        return report

    def finish_submit(
        self,
        *,
        extracted_netlist_sha256: str | None,
        request_sha256: Mapping[str, str],
    ) -> dict[str, Any]:
        """Write the manifest and return the ``submitted`` status payload."""
        payload = {
            "schema_version": ASYNC_SCHEMA_VERSION,
            "extracted_netlist_sha256": extracted_netlist_sha256,
            "testbenches": [
                {"label": label, "request_sha256": sha}
                for label, sha in request_sha256.items()
            ],
            "legs": self._legs,
        }
        with open(self.manifest_path or "", "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
        return {
            "schema_version": ASYNC_SCHEMA_VERSION,
            "status": "submitted",
            "manifest_path": self.manifest_path,
            "leg_count": len(self._legs),
            "legs": self._legs,
        }
