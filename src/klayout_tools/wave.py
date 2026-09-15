"""Python wiring for `native/wave`'s standalone `klt-wave` binary --
Epic #1585 ("`klt wave` -- waveform query surface for functional-
verification traces"), Phase 2c, issue #1601.

Pure library: :func:`run_wave_build`/:func:`run_wave_query` return plain
Python data (a ``dict`` of JSON-serialisable primitives) and never print,
mirroring ``techmap.py``/``synthesize.py``/``equiv.py``.

``native/wave/`` (issues #1599/#1600, `docs/design/waveform-query-contract-
spike.md` sections 4-5) implements both `klt wave` verbs -- `build`
(VCD/FST trace -> indexed FST store) and `query` (one or more query ops
against a built store) -- as a standalone Rust binary (`klt-wave
<build|query> <request.json>`), following `native/techmap/`'s own
"deliberately a plain `cargo build`/`cargo test` binary crate -- no `pyo3`/
`maturin` wiring" precedent (decision record section 13: "native-Rust-
first... Plain `cargo build`/`cargo test` binary crate"). This module is
the production integration that makes both verbs reachable as `klt wave
build`/`klt wave query`: it invokes the real compiled binary as a
subprocess (never re-implements the FST/VCD engine in Python) via
:func:`_binary_path`, the exact `techmap.py::_binary_path()`
degrade-cleanly pattern (decision record section 14): a host without a
Rust toolchain (or one that has not yet run `cargo build --release`
inside `native/wave/`) gets a clear, actionable :class:`WaveError`, never
an opaque stack trace or a hard install-time failure -- and, per the
contract spike's section 16 Curator checklist, `pyproject.toml` does not
declare `klt-wave-native` as a dependency at all (neither required nor an
optional group): like `native/techmap/`, this crate has no `pyo3`/
`maturin` wiring and no `pyproject.toml` of its own, so there is nothing
for `uv`/`pip` to resolve -- the "build it yourself with `cargo build
--release`" instruction *is* the degrade-cleanly path, exactly as it
already is for `klt techmap`.

Both verbs' binary output is invoked with the Rust CLI's own
``--format json`` (regardless of what this module's own caller asked
for -- ``klt wave``'s CLI layer, ``cli/wave_cmd.py``, renders ``--format
text`` itself from the parsed dict, the same split `techmap_cmd.py` uses)
so this module always has a real JSON document to parse, never scraping
the binary's human-readable text summary.

**`provenance.klt_version` override.** The Rust binary's own response sets
``provenance.klt_version`` to its own crate version (`CARGO_PKG_VERSION`,
e.g. ``"0.1.0"`` -- an internal, unpublished crate version, not meaningful
to an agent asking "which `klt` build produced this"). This module
overrides that field with the *installed `klayout-tools` package's own*
version (:func:`klayout_tools._provenance._klt_version`) before returning
either response -- the same "Python wrapper layer... may override that
field with the installed `klayout-tools` package's own version" `build.rs`
and `main.rs` (issue #1600/#1599) both anticipate in their own docstrings,
one stage ahead of this module landing. Left unchanged (the crate's own
version) only in the unlikely case the installed package's version cannot
be resolved at all -- never overwritten with ``None``.
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any

from ._provenance import _klt_version

#: `native/wave/`'s crate directory, relative to this file -- two levels up
#: from `src/klayout_tools/wave.py` is `src/`, three levels up is the repo
#: root -- mirrors `techmap.py::_TECHMAP_CRATE_DIR`'s own computation.
_WAVE_CRATE_DIR = os.path.normpath(
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "..", "native", "wave"
    )
)

#: Exit code `klt-wave query` uses for "every op ran, at least one declared
#: predicate was not satisfied" (`docs/design/waveform-query-contract-
#: spike.md` section 5, "Exit codes") -- a normal, non-error outcome (this
#: module still returns the parsed response for it), distinct from a run
#: that failed to execute at all.
QUERY_EXIT_UNSATISFIED = 3


class WaveError(Exception):
    """Raised when a `klt wave build`/`klt wave query` run cannot be
    completed at all: an unbuilt `klt-wave` binary (:func:`_binary_path`),
    a subprocess launch failure, a non-zero exit that is not `query`'s own
    "unsatisfied predicate" code 3 (`build`'s failure modes: unreadable/
    malformed trace, an unresolvable `trace.format`, a named `clock.signal`/
    `reset.signal`/`signals` entry absent from the trace, an empty
    `signals` array, an unwritable `store.path`; `query`'s failure modes: a
    malformed request, an unreadable/corrupt store, an op naming a signal
    absent from the store, cycle addressing against a store with no clock
    declared, a predicate on an op that does not define one, or an empty
    `ops` array -- see `docs/design/waveform-query-contract-spike.md`
    sections 4-5's own exit-code tables), or non-JSON output from the
    binary.

    The CLI turns this into a clean stderr message + exit code 1, matching
    every other request-taking `klt` verb's "no ran-but-found-problems
    outcome of its own" posture (`techmap_cmd.py`'s identical pattern).
    """


def _binary_path() -> str:
    """The compiled `klt-wave` binary path -- release build preferred,
    falling back to a debug build -- mirroring
    `techmap.py::_binary_path()`'s exact convention (itself matching
    `tests/corpus/techmap/compare.py`'s `_ensure_binary`).

    Raises :class:`WaveError` with a clear, actionable message if neither
    exists -- this module never silently shells out to `cargo build` from
    production code (a multi-second first-call latency spike a caller of
    `run_wave_build`/`run_wave_query` would not expect); building is a
    deliberate, explicit step.
    """
    release = os.path.join(_WAVE_CRATE_DIR, "target", "release", "klt-wave")
    if os.path.isfile(release):
        return release
    debug = os.path.join(_WAVE_CRATE_DIR, "target", "debug", "klt-wave")
    if os.path.isfile(debug):
        return debug
    raise WaveError(
        "the klt-wave binary is not built -- from a repo checkout, run "
        "`cargo build --release` inside native/wave/ (or `cargo build` "
        "for a debug build)"
    )


def _invoke(
    binary: str, mode: str, request_path: str
) -> subprocess.CompletedProcess[str]:
    """Run `klt-wave <mode> <request_path> --format json`, raising
    :class:`WaveError` if the process cannot even be launched (mirrors
    `techmap.py`'s identical `OSError` handling around `subprocess.run`)."""
    try:
        return subprocess.run(
            [binary, mode, os.path.abspath(request_path), "--format", "json"],
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise WaveError(f"could not launch klt-wave: {exc}") from exc


def _error_detail(completed: subprocess.CompletedProcess[str]) -> str:
    """The best available human-readable detail for a failed `klt-wave`
    run: the `message` field of its own `{"schema_version", "error":
    {"command", "message"}}` JSON error envelope on stderr
    (`docs/design/waveform-query-contract-spike.md` section 2) when
    present and parseable, otherwise the raw stderr/stdout text."""
    stderr = (completed.stderr or "").strip()
    try:
        payload = json.loads(stderr)
        message = payload["error"]["message"]
        if isinstance(message, str) and message:
            return message
    except (json.JSONDecodeError, KeyError, TypeError):
        pass
    return stderr or (completed.stdout or "").strip() or "no output captured"


def _parse_response(
    completed: subprocess.CompletedProcess[str], command: str
) -> dict[str, Any]:
    """Decode `completed.stdout` as the command's own JSON response,
    raising :class:`WaveError` (mirroring `techmap.py`'s identical
    `json.JSONDecodeError` handling) if it is not valid JSON -- should not
    happen for a `klt-wave` run that reached exit 0/3 and was invoked with
    `--format json`, but this module never trusts a subprocess's stdout
    blindly."""
    try:
        response = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise WaveError(
            f"klt-wave {command} produced output that is not valid JSON: {exc}"
        ) from exc
    _override_klt_version(response)
    return response


def _override_klt_version(response: dict[str, Any]) -> None:
    """Replace `response["provenance"]["klt_version"]` (the `klt-wave`
    binary's own `CARGO_PKG_VERSION`) with the installed `klayout-tools`
    package's own version, when resolvable -- see this module's own
    docstring, "`provenance.klt_version` override"."""
    provenance = response.get("provenance")
    if not isinstance(provenance, dict):
        return
    package_version = _klt_version()
    if package_version:
        provenance["klt_version"] = package_version


def run_wave_build(request_path: str) -> dict[str, Any]:
    """Run `klt wave build` against the `klt.wave_build.request/1` document
    at ``request_path`` (`docs/design/waveform-query-contract-spike.md`
    section 4: `trace`, optional `clock`/`reset`, optional `signals`
    allow-list, `store`).

    Invokes the real, compiled `klt-wave` binary (:func:`_binary_path`) as
    a subprocess -- never re-implements the VCD/FST ingest in Python.
    Returns the binary's own `klt.wave_build.response/1` JSON verbatim
    (`trace`, `store`, `clock`, `reset`, `timescale`, `time_range`,
    `signal_count`, `value_change_count`, `provenance`), with
    `provenance.klt_version` overridden (see this module's own docstring).

    Raises :class:`WaveError` for any run that did not build a store at
    all (exit 1 or 2 -- `build` has no pass/fail concept of its own beyond
    "did a queryable store come out", contract section 4's own "No exit
    code 3" note) or produced unparseable output.
    """
    binary = _binary_path()
    completed = _invoke(binary, "build", request_path)
    if completed.returncode != 0:
        raise WaveError(
            f"klt-wave build exited with code {completed.returncode}: "
            f"{_error_detail(completed)}"
        )
    return _parse_response(completed, "build")


def run_wave_query(request_path: str) -> dict[str, Any]:
    """Run `klt wave query` against the `klt.wave_query.request/1` document
    at ``request_path`` (`docs/design/waveform-query-contract-spike.md`
    section 5: `store`, one or more `ops`).

    Invokes the real, compiled `klt-wave` binary (:func:`_binary_path`) as
    a subprocess -- never re-implements the query engine in Python.
    Returns the binary's own `klt.wave_query.response/1` JSON verbatim
    (`store`, `status`, `results`, `provenance`), with
    `provenance.klt_version` overridden (see this module's own docstring),
    for **both** a fully-satisfied run (exit 0, `status: "ok"`) and one
    where every op ran but at least one declared `predicate` was not
    satisfied (exit :data:`QUERY_EXIT_UNSATISFIED`, `status:
    "unsatisfied"`) -- the latter is a normal, informative outcome (the
    same way `klt drc` finding violations is a successful run), not an
    error; callers that want a hard pass/fail gate should check
    `response["status"]` themselves, mirroring `klt drc_cmd.py`'s own
    `EXIT_VIOLATIONS` split.

    Raises :class:`WaveError` for any run that failed to execute at all
    (exit 1 or 2 -- a malformed request, an unreadable/corrupt store, an
    op naming a signal absent from the store, cycle addressing against a
    store built with no clock declared, a predicate on an op that does not
    define one, or an empty `ops` array; see contract section 5's own
    partial-failure design -- one bad op fails the whole batch, never a
    silent per-op skip) or produced unparseable output.
    """
    binary = _binary_path()
    completed = _invoke(binary, "query", request_path)
    if completed.returncode not in (0, QUERY_EXIT_UNSATISFIED):
        raise WaveError(
            f"klt-wave query exited with code {completed.returncode}: "
            f"{_error_detail(completed)}"
        )
    return _parse_response(completed, "query")
