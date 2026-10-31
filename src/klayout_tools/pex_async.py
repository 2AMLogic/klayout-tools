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
ASYNC_SCHEMA_VERSION = 2

#: ``PexError.code`` values this module raises.
MANIFEST_INVALID_CODE = "pex_manifest_invalid"
FINGERPRINT_MISMATCH_CODE = "resume_fingerprint_mismatch"
LEG_MISSING_CODE = "pex_attach_leg_missing"
MANIFEST_INCOMPLETE_CODE = "pex_manifest_incomplete"
ASYNC_UNSUPPORTED_CODE = "pex_async_unsupported"
MANIFEST_NO_REQUEST_CODE = "pex_manifest_no_request"


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


def load_request(manifest_path: str) -> dict[str, Any]:
    """The ``klt pex`` request a ``--submit-only`` run recorded in its
    manifest, for a bare ``--attach`` (issue #4692). A manifest without one
    (written before schema 2) is refused by name."""
    from .pex import PexError

    manifest = AsyncLegs._load(manifest_path)
    request = manifest.get("request")
    if not isinstance(request, dict):
        raise PexError(
            f"--attach: manifest '{manifest_path}' (schema_version "
            f"{manifest.get('schema_version')}) records no request, so a bare "
            "--attach cannot rebuild the run; re-run `klt pex --attach` with "
            "the original layout, --deck and testbench arguments, or submit "
            "again to write a schema-2 manifest",
            code=MANIFEST_NO_REQUEST_CODE,
        )
    return request


class AsyncLegs:
    """Routes every pex ``run_sim`` leg: blocking (``mode=None``), submit
    (``"submit"``) or attach (``"attach"``)."""

    def __init__(
        self,
        *,
        submit_only: bool,
        attach: str | None,
        work_dir: str,
        request: Mapping[str, Any] | None = None,
    ) -> None:
        self._request = dict(request) if request is not None else None
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
        self._extracted_netlist_sha256: str | None = None
        self._request_sha256: dict[str, str] = {}
        # Issue #4686: a re-run of an interrupted --submit-only replays the
        # legs the earlier run recorded instead of submitting them again.
        if self.mode == "attach" or (
            self.submitting and os.path.isfile(self.manifest_path or "")
        ):
            self._manifest = self._load(self.manifest_path, flag=self._flag())
            self._recorded = {
                (leg["testbench"], leg["side"]): leg for leg in self._manifest["legs"]
            }
            if self.submitting:
                self._legs = list(self._manifest["legs"])

    def _flag(self) -> str:
        return "--submit-only" if self.submitting else "--attach"

    @staticmethod
    def _load(path: str | None, *, flag: str = "--attach") -> dict[str, Any]:
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
                f"{flag}: cannot read pex submit manifest '{path}': {exc!r}",
                code=MANIFEST_INVALID_CODE,
            ) from exc
        return manifest

    def verify(
        self, *, extracted_netlist_sha256: str | None, request_sha256: Mapping[str, str]
    ) -> None:
        """Refuse a manifest recorded for different inputs than this
        invocation extracted/loaded (the sim ``--attach`` precedent), and an
        ``--attach`` of a manifest whose submit never finished."""
        from .pex import PexError

        self._extracted_netlist_sha256 = extracted_netlist_sha256
        self._request_sha256 = dict(request_sha256)
        if not self._manifest:
            return
        recorded = {
            tb["label"]: tb["request_sha256"] for tb in self._manifest["testbenches"]
        }
        if self._manifest[
            "extracted_netlist_sha256"
        ] != extracted_netlist_sha256 or recorded != dict(request_sha256):
            raise PexError(
                f"{self._flag()}: manifest '{self.manifest_path}' was submitted for "
                "different inputs (extracted netlist or testbench requests "
                "changed); re-run the same klt pex command that produced it, "
                "or submit again into a fresh --outdir",
                code=FINGERPRINT_MISMATCH_CODE,
            )
        if self.mode == "attach" and not self._manifest.get("complete", True):
            missing = [
                f"{label}/{side}"
                for label in request_sha256
                for side in ("schematic", "extracted")
                if (label, side) not in self._recorded
            ]
            raise PexError(
                f"--attach: manifest '{self.manifest_path}' is incomplete -- the "
                "--submit-only run that wrote it was interrupted before every "
                "leg was submitted (missing legs: "
                f"{', '.join(missing) or 'unknown'}); re-run the same "
                "`klt pex --submit-only` command to finish submitting",
                code=MANIFEST_INCOMPLETE_CODE,
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
        replayed = self._recorded.get((testbench, side))
        if replayed is not None:
            # Already submitted by an earlier, interrupted run: no new job.
            return replayed
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
        # Issue #4686: persist before the next leg, so a caller killed
        # mid-submit leaves a manifest a re-run can pick up.
        self._write(complete=False)
        return report

    def _write(self, *, complete: bool) -> None:
        payload = {
            "schema_version": ASYNC_SCHEMA_VERSION,
            "complete": complete,
            "extracted_netlist_sha256": self._extracted_netlist_sha256,
            "testbenches": [
                {"label": label, "request_sha256": sha}
                for label, sha in self._request_sha256.items()
            ],
            "legs": self._legs,
        }
        if self._request is not None:
            payload["request"] = self._request
        path = self.manifest_path or ""
        tmp_path = f"{path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
        os.replace(tmp_path, path)

    def finish_submit(
        self,
        *,
        extracted_netlist_sha256: str | None,
        request_sha256: Mapping[str, str],
    ) -> dict[str, Any]:
        """Write the final (``complete``) manifest and return the
        ``submitted`` status payload."""
        self._extracted_netlist_sha256 = extracted_netlist_sha256
        self._request_sha256 = dict(request_sha256)
        self._write(complete=True)
        return {
            "schema_version": ASYNC_SCHEMA_VERSION,
            "status": "submitted",
            "manifest_path": self.manifest_path,
            "leg_count": len(self._legs),
            "legs": self._legs,
        }
