"""Shared OpenROAD-subprocess helpers used by the ``klt`` verb modules that
drive OpenROAD as a subprocess: ``place_and_route.py``'s own place/route/CTS/
in-flow-STA stages, and ``post_route_sta.py``'s standalone post-route timing
verb (issue #1099).

``_run_openroad``, ``_count_violations``, and ``_openroad_version`` were
previously defined identically (modulo docstring length, and -- for
``_run_openroad`` -- which ``*Error`` class it raised) in both modules; they
now live here as the single source of truth, following the same precedent
as ``_paths.py`` hosting ``load_request``'s shared file-read/JSON-parse
boilerplate (issue #642).

``_run_openroad`` is the one genuine behavioral difference between the two
former copies: ``place_and_route.py`` needs to raise ``PlaceAndRouteError``
on a launch failure, ``post_route_sta.py`` needs ``PostRouteStaError``. That
is preserved here via a caller-supplied ``error_cls`` parameter, the same
pattern ``_paths.py``'s own ``_load_request_json``/``_load_spec_json`` use
for the identical problem.
"""

from __future__ import annotations

import json
import re
import subprocess
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ._provenance import sha256_file
from .env_provenance import find_repo_root, repo_relative_path

_OPENROAD_VERSION_RE = re.compile(r"OpenROAD\s+(\S+)")

#: Slack magnitude (ns) at or above which a reported value is OpenSTA's own
#: *unconstrained-design sentinel* rather than a measurement (issue #1865).
#:
#: OpenSTA reports the worst slack of a design with no constrained
#: startpoint/endpoint as its internal infinity, which reaches this repo's
#: responses -- via OpenROAD's ``-metrics`` dump -- as ``1e+39``. That value
#: is a *positive number*, so a naive downstream gate reading
#: ``worst_slack_ns >= 0`` concludes "timing closed" on a design that was
#: never timed at all. The threshold is deliberately far below the sentinel
#: (and far above any physically meaningful slack: ``1e29`` ns is roughly
#: 3e21 years) so it keeps matching if a future OpenSTA build reports its
#: infinity as ``1e+30`` instead.
_UNCONSTRAINED_SLACK_NS = 1e29


def _is_unconstrained_slack(value: float | None) -> bool:
    """Whether ``value`` is OpenSTA's unconstrained-design sentinel rather
    than a real slack measurement (issue #1865) -- see
    :data:`_UNCONSTRAINED_SLACK_NS`. ``None`` (a metric the run never
    populated at all) is **not** the sentinel: absence is a different thing
    from "reported, but not a measurement"."""
    if value is None:
        return False
    return abs(value) >= _UNCONSTRAINED_SLACK_NS


def _timing_status(values: Iterable[float | None]) -> str | None:
    """Classify a set of reported slack values as ``"constrained"`` /
    ``"unconstrained"`` / ``None`` (issue #1865).

    This is the mechanical signal that lets a caller distinguish "genuinely
    timed, zero negative slack" from "never had a constrained path to
    measure", without special-casing ``1e+39`` by value in every consumer:

    - ``None`` -- no slack metric was reported at all in this scope (e.g. a
      ``klt place-and-route`` stage whose own OpenROAD reports populate no
      timing key). Nothing to classify; not a claim either way.
    - ``"unconstrained"`` -- **any** reported value is the sentinel. The
      conservative reading on purpose: a gate should require
      ``timing_status == "constrained"`` before trusting *any* slack number
      in the same scope.
    - ``"constrained"`` -- every reported value is a real measurement.
    """
    reported = [value for value in values if value is not None]
    if not reported:
        return None
    if any(_is_unconstrained_slack(value) for value in reported):
        return "unconstrained"
    return "constrained"


@dataclass(frozen=True)
class _OpenRoadResult:
    """Captured process output plus this invocation's retained evidence."""

    returncode: int
    stdout: str
    stderr: str
    engine_log: dict[str, Any]

    @contextmanager
    def diagnostics(self, error_cls: type[Exception]) -> Iterator[None]:
        """Preserve the caller's diagnosis, including missing metrics/output."""
        try:
            yield
        except error_cls as exc:
            raise error_cls(_log_error_context(str(exc), self.engine_log)) from exc


def _log_error_context(message: str, record: dict[str, Any]) -> str:
    return f"{message} -- openroad invocation: {json.dumps(record, sort_keys=True)}"


def _captured_text(value: str | bytes | None) -> str:
    """Decode only for diagnostics/parsing; retained files keep original bytes."""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


def _log_path(path: Path | None) -> dict[str, Any]:
    root = find_repo_root()
    if path is not None and root is None:
        return {"path": None, "scope": "external"}
    return repo_relative_path(path, repo_root=root)


def _retention_error(record: dict[str, Any], artifact: str, exc: OSError) -> None:
    # str(exc) can include an absolute filename; keep the new diagnostic
    # paths subject to the same privacy rule as successful artifact paths.
    record["retention_errors"].append(
        {"artifact": artifact, "error": f"{type(exc).__name__}: {exc.strerror}"}
    )


def _write_log_artifact(
    directory: Path, name: str, content: str | bytes | None, record: dict[str, Any]
) -> dict[str, Any]:
    path = directory / name
    data = content.encode("utf-8") if isinstance(content, str) else content or b""
    try:
        with path.open("xb") as handle:
            handle.write(data)
    except OSError as exc:
        _retention_error(record, name, exc)
        return _log_path(None)
    return _log_path(path)


def _retain_openroad_logs(
    script_path: str,
    metrics_path: str,
    *,
    invocation_id: str,
    script_hash: str | None,
    outcome: str,
    returncode: int | None,
    stdout: str | bytes | None,
    stderr: str | bytes | None,
) -> dict[str, Any]:
    """Write only to a newly created directory; retries never overwrite it."""
    directory = Path(script_path).absolute().parent / "openroad-logs" / invocation_id
    record: dict[str, Any] = {
        "invocation_id": invocation_id,
        "script_name": Path(script_path).name,
        "script_path": _log_path(Path(script_path)),
        "script_sha256": script_hash,
        "metrics_path": _log_path(Path(metrics_path)),
        "outcome": outcome,
        "returncode": returncode,
        "directory": _log_path(None),
        "stdout_path": _log_path(None),
        "stderr_path": _log_path(None),
        "metadata_path": _log_path(None),
        "retention_errors": [],
    }
    try:
        directory.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        _retention_error(record, "directory", exc)
        return record
    record["directory"] = _log_path(directory)
    record["stdout_path"] = _write_log_artifact(directory, "stdout.log", stdout, record)
    record["stderr_path"] = _write_log_artifact(directory, "stderr.log", stderr, record)
    record["metadata_path"] = _log_path(directory / "invocation.json")
    record["metadata_path"] = _write_log_artifact(
        directory, "invocation.json", json.dumps(record, indent=2) + "\n", record
    )
    return record


def _run_openroad(
    script_path: str,
    metrics_path: str,
    *,
    error_cls: type[Exception],
    engine_logs: list[dict[str, Any]] | None = None,
) -> _OpenRoadResult:
    """Run ``openroad -no_init -exit -metrics <metrics_path> <script_path>``,
    capturing raw stdout/stderr and retaining both streams separately. The
    result exposes UTF-8 text (replacement decoding) for existing parsers;
    an invalid output byte cannot mask the engine failure or lose its logs.
    Raises ``error_cls`` if the ``openroad``
    binary itself cannot be launched (e.g. not on ``PATH``); a non-zero exit
    from a successfully-launched run is left for the caller to inspect via
    returned result's ``returncode``. No timeout is introduced. If a runner
    supplies a timeout exception, its available partial streams are retained;
    KeyboardInterrupt retains available data and keeps its interruption type.
    Logs are local debugging evidence, not a successful-stage signal."""
    invocation_id = uuid.uuid4().hex
    script_hash = sha256_file(script_path)

    def retain(outcome, returncode, stdout, stderr):
        record = _retain_openroad_logs(
            script_path,
            metrics_path,
            invocation_id=invocation_id,
            script_hash=script_hash,
            outcome=outcome,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )
        if engine_logs is not None:
            engine_logs.append(record)
        return record

    try:
        completed = subprocess.run(
            ["openroad", "-no_init", "-exit", "-metrics", metrics_path, script_path],
            capture_output=True,
        )
    except OSError as exc:
        record = retain("launch_failed", None, None, None)
        raise error_cls(
            _log_error_context(f"could not launch openroad: {exc}", record)
        ) from exc
    except subprocess.TimeoutExpired as exc:
        record = retain("timed_out", None, exc.stdout, exc.stderr)
        raise error_cls(
            _log_error_context(f"openroad timed out: {exc}", record)
        ) from exc
    except KeyboardInterrupt as exc:
        record = retain(
            "interrupted",
            None,
            getattr(exc, "stdout", None),
            getattr(exc, "stderr", None),
        )
        exc.args = (*exc.args, _log_error_context("openroad interrupted", record))
        raise
    record = retain("exited", completed.returncode, completed.stdout, completed.stderr)
    return _OpenRoadResult(
        completed.returncode,
        _captured_text(completed.stdout),
        _captured_text(completed.stderr),
        record,
    )


def _openroad_version() -> str | None:
    """The resolved OpenROAD build string, or ``None`` if unresolvable --
    never raises.

    ``openroad -version`` prints a **bare** version token on its own stdout
    line (e.g. ``26Q3-771-g7cfb2105c9``) -- confirmed live for issue #425's
    own worked example against a real OpenROAD build, distinct from the
    ``OpenROAD <version>`` banner a no-flag/``-no_init`` script invocation
    prints before running. The banner form is matched first (defensively,
    in case a future build changes ``-version``'s own output shape); the
    bare first-token form is the fallback that matches today's real
    behavior.
    """
    try:
        completed = subprocess.run(
            ["openroad", "-version"], capture_output=True, text=True
        )
    except OSError:
        return None
    stdout = completed.stdout.strip()
    if not stdout:
        return None
    banner_match = _OPENROAD_VERSION_RE.search(stdout)
    if banner_match:
        return banner_match.group(1)
    return stdout.split()[0]


def _count_violations(stdout: str, begin: str, end: str) -> int:
    """Count ``"(VIOLATED)"`` lines between ``begin``/``end`` markers in a
    stage's captured stdout -- OpenROAD has no ``*_metric`` proc for
    setup/hold *violation counts* (only the scalar WNS/TNS), so this is the
    documented ``report_*`` stdout-scrape fallback (contract spike section
    5's build/wrap section). Returns ``0`` when the markers aren't found
    (defensive; should not happen for a successful run) or when the report
    found no violating paths."""
    try:
        start_idx = stdout.index(begin) + len(begin)
        stop_idx = stdout.index(end, start_idx)
    except ValueError:
        return 0
    return stdout[start_idx:stop_idx].count("(VIOLATED)")
