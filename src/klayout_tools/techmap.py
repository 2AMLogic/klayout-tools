"""Python wiring for `native/techmap`'s standalone `klt-techmap` binary --
Epic #704 ("RTL synthesis for `klt`"), Phase 2c, issue #875.

Pure library: :func:`run_techmap` returns plain Python data (a ``dict`` of
JSON-serialisable primitives) and never prints, mirroring ``synthesize.py``/
``equiv.py``.

``native/techmap/`` (issue #874, `docs/design/synth-techmap-stage-contract.md`
issue #873) implements Liberty-driven technology mapping as a standalone Rust
binary (`klt-techmap <request.json>`) -- deliberately not (yet) a `klt`
Python-extension/`pyo3` surface, following `native/statime/`'s own "engine and
CLI first, production integration later" precedent (see that crate's own
``Cargo.toml`` comment). This module is that follow-on production
integration's *first* piece: it invokes the real compiled binary as a
subprocess (never re-implements the mapping algorithm in Python) and wires
``klt equiv``'s (:func:`klayout_tools.equiv.run_equiv`) acceptance gate in
one stage later than Phase 1b's own `klt synthesize --verify-equivalence`
gate (:func:`klayout_tools.synthesize._verify_synthesis_equivalence`) --
between the **pre-mapping generic netlist** and the **mapped output** from
the techmap stage, rather than between RTL and a Yosys/abc-orchestrated
netlist.

**Acceptance gate**: :func:`run_techmap`'s ``verify_equivalence`` flag (the
CLI's ``--verify-equivalence``, default ``False``/additive -- unchanged
behaviour for every existing caller, exactly the same opt-in posture
``synthesize.py``'s own flag uses) gates the just-produced
``mapped_netlist_path`` through ``klt equiv`` against
``generic_netlist_verilog_path`` -- the same generic netlist the mapping was
computed from, re-emitted as self-contained behavioral Verilog by
``native/techmap/src/verilog.rs::write_generic`` (issue #875's own addition
to that crate). A non-``"equivalent"`` verdict (``"counterexample"`` or
``"inconclusive"``) -- or an :class:`~klayout_tools.equiv.EquivError`, e.g. a
**sequential** design (this MVP's ``klt equiv`` is combinational-only, see
``docs/cli/equiv.md``'s "Scope" section; a generic netlist containing `$dff`
cells cannot use this flag today, exactly the same restriction Phase 1b's own
gate already has) -- is a hard :class:`TechmapError`, never a silent warning:
a mapped netlist this gate cannot prove faithful to the generic netlist it
was mapped from is not "done".
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any

from ._paths import _load_request_json, _resolve_relative, validate_request_shape
from .equiv import EquivError, run_equiv

#: Bumped only on a non-additive (breaking) change to this command's own
#: JSON shape -- see docs/json-contract.md. Mirrors the Rust binary's own
#: `klt.synth.techmap.response/1` `schema_version`, currently `1`.
SCHEMA_VERSION = 1

#: `native/techmap/`'s crate directory, relative to this file -- three
#: levels up from `src/klayout_tools/techmap.py` is the repo root.
_TECHMAP_CRATE_DIR = os.path.normpath(
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "..", "native", "techmap"
    )
)

_REQUIRED_REQUEST_FIELDS = ("generic_netlist", "liberty")


class TechmapError(Exception):
    """Raised when a technology-mapping run cannot be completed at all: a
    missing/malformed request, an unbuilt ``klt-techmap`` binary, a
    subprocess failure (malformed generic netlist, an unrealised generic
    cell, unreadable/unparseable liberty -- exit code 1 per
    `docs/design/synth-techmap-stage-contract.md` section 6), non-JSON
    output from the binary, or (when ``verify_equivalence`` is set) a
    failed/inconclusive equivalence check against the pre-mapping generic
    netlist.

    The CLI turns this into a clean stderr message + exit code 1, matching
    every other request-taking ``klt`` verb's "no ran-but-found-problems
    outcome of its own" posture.
    """


def load_request(request_path: str) -> dict[str, Any]:
    """Read and minimally validate a ``klt-techmap`` request JSON file.

    Raises :class:`TechmapError` if the file is missing/unreadable, not
    valid JSON, or missing a required top-level field (``generic_netlist``,
    ``liberty``) -- mirrors ``klt synthesize``/``klt equiv``'s own
    ``load_request``. Does not require a ``schema`` field, matching those
    same commands (user-authored input, never emitted by this tool).
    """
    request = _load_request_json(request_path, TechmapError)
    return validate_request_shape(
        request,
        "request file",
        error_cls=TechmapError,
        required_fields=_REQUIRED_REQUEST_FIELDS,
    )


def _binary_path() -> str:
    """The compiled `klt-techmap` binary path -- release build preferred
    (matching `tests/corpus/techmap/compare.py`'s own `_ensure_binary`
    convention), falling back to a debug build.

    Raises :class:`TechmapError` with a clear, actionable message (mirroring
    ``mom.py``'s/``congestion.py``'s own "extension not installed" errors)
    if neither exists -- this module never silently shells out to `cargo
    build` from production code (a multi-second first-call latency spike a
    caller of ``run_techmap`` would not expect); building is a deliberate,
    explicit step, same posture the `pyo3` extensions take toward
    `maturin develop`.
    """
    release = os.path.join(_TECHMAP_CRATE_DIR, "target", "release", "klt-techmap")
    if os.path.isfile(release):
        return release
    debug = os.path.join(_TECHMAP_CRATE_DIR, "target", "debug", "klt-techmap")
    if os.path.isfile(debug):
        return debug
    raise TechmapError(
        "the klt-techmap binary is not built -- from a repo checkout, run "
        "`cargo build --release` inside native/techmap/ (or `cargo build` "
        "for a debug build)"
    )


def run_techmap(
    request_path: str,
    *,
    verify_equivalence: bool = False,
    equiv_timeout_s: float | None = None,
) -> dict[str, Any]:
    """Run the technology-mapping stage declared by the request at
    ``request_path`` -- a ``klt.synth.techmap.request/1`` document
    (`docs/design/synth-techmap-stage-contract.md` section 6:
    ``generic_netlist``, ``liberty``, optional ``constraints.clock_period_ns``).

    Invokes the real, compiled ``klt-techmap`` binary (:func:`_binary_path`)
    as a subprocess -- never re-implements the mapping algorithm in Python.
    Returns the binary's own ``klt.synth.techmap.response/1`` JSON verbatim
    (``top``, ``status``, ``instance_count``, ``area_um2``,
    ``instance_counts_by_type``, ``timing``, ``mapped_netlist_path``,
    ``generic_netlist_verilog_path``), plus an additive ``equivalence`` key
    (``None`` unless ``verify_equivalence`` is set).

    ``verify_equivalence`` (the CLI's ``--verify-equivalence`` flag; default
    ``False``, additive/opt-in -- unchanged behaviour for every existing
    caller) gates the produced ``mapped_netlist_path`` through ``klt equiv``
    (:func:`klayout_tools.equiv.run_equiv`) against
    ``generic_netlist_verilog_path`` -- see this module's own docstring,
    "Acceptance gate". ``equiv_timeout_s`` (the CLI's ``--equiv-timeout-s``)
    overrides ``klt equiv``'s own default proof timeout; ``None`` leaves
    :data:`klayout_tools.equiv.DEFAULT_TIMEOUT_S` in effect.

    Raises :class:`TechmapError` for anything that prevents a mapped
    netlist from being produced at all -- see :class:`TechmapError`'s own
    docstring.

    Generated artifacts (the equivalence-check request/script/log, when
    ``verify_equivalence`` is set) are written to ``.klt/techmap/`` next to
    the request file -- the same "next to the input" convention
    ``klt synthesize``'s ``.klt/synthesize/`` and ``klt equiv``'s own
    ``.klt/equiv/`` already use.
    """
    request = load_request(request_path)
    request_dir = os.path.dirname(os.path.abspath(request_path))

    generic_netlist = request["generic_netlist"]
    if not isinstance(generic_netlist, str) or not generic_netlist:
        raise TechmapError("request.generic_netlist must be a non-empty string")
    liberty = request["liberty"]
    if not isinstance(liberty, str) or not liberty:
        raise TechmapError("request.liberty must be a non-empty string")

    liberty_path = _resolve_relative(liberty, request_dir)
    if not os.path.isfile(liberty_path):
        raise TechmapError(f"liberty file not found: {liberty}")

    binary = _binary_path()

    try:
        completed = subprocess.run(
            [binary, os.path.abspath(request_path)],
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise TechmapError(f"could not launch klt-techmap: {exc}") from exc

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise TechmapError(
            f"klt-techmap exited with code {completed.returncode}: "
            f"{detail or 'no output captured'}"
        )

    try:
        response = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise TechmapError(
            f"klt-techmap produced output that is not valid JSON: {exc}"
        ) from exc

    equivalence = None
    if verify_equivalence:
        equivalence = _verify_techmap_equivalence(
            request_dir=request_dir,
            techmap_response=response,
            liberty_path=liberty_path,
            timeout_s=equiv_timeout_s,
        )

    response["equivalence"] = equivalence
    return response


def _verify_techmap_equivalence(
    *,
    request_dir: str,
    techmap_response: dict[str, Any],
    liberty_path: str,
    timeout_s: float | None,
) -> dict[str, Any]:
    """The ``verify_equivalence`` gate :func:`run_techmap` calls after a
    successful mapping run: reuses :func:`klayout_tools.equiv.run_equiv`'s
    existing request contract, with ``gold`` set to the pre-mapping generic
    netlist's own behavioral-Verilog re-emission
    (``techmap_response["generic_netlist_verilog_path"]``, no ``liberty`` --
    it is self-contained RTL, not a standard-cell netlist) and ``gate`` set
    to the mapped netlist ``klt-techmap`` just produced
    (``techmap_response["mapped_netlist_path"]``, ``liberty`` attached so
    its standard-cell instances resolve as real logic, mirroring
    :func:`klayout_tools.synthesize._verify_synthesis_equivalence`'s
    identical reasoning one stage earlier in the pipeline).

    Returns a small summary dict (attached to :func:`run_techmap`'s own
    ``equivalence`` field) on an ``"equivalent"`` verdict. Raises
    :class:`TechmapError` -- a hard failure, never a silent warning -- for
    every other outcome: a proven ``"counterexample"`` (the mapped netlist
    diverges from the generic netlist it was mapped from), an
    ``"inconclusive"`` verdict (a solver/process timeout -- never treated as
    a pass), or an :class:`~klayout_tools.equiv.EquivError` (e.g. a
    sequential generic netlist, outside this MVP's combinational-only
    ``klt equiv`` scope).
    """
    top = techmap_response["top"]
    output_dir = os.path.join(request_dir, ".klt", "techmap")
    try:
        os.makedirs(output_dir, exist_ok=True)
    except OSError as exc:
        raise TechmapError(
            f"could not create output directory '{output_dir}': {exc}"
        ) from exc

    equiv_request_path = os.path.join(output_dir, f"equiv_request_{top}.json")
    equiv_request = {
        "gold": {
            "sources": [techmap_response["generic_netlist_verilog_path"]],
            "top": top,
        },
        "gate": {
            "sources": [techmap_response["mapped_netlist_path"]],
            "top": top,
            "liberty": liberty_path,
        },
    }
    try:
        with open(equiv_request_path, "w", encoding="utf-8") as handle:
            json.dump(equiv_request, handle, indent=2)
    except OSError as exc:
        raise TechmapError(
            f"could not write equivalence-check request '{equiv_request_path}': {exc}"
        ) from exc

    try:
        equiv_report = run_equiv(equiv_request_path, timeout_s=timeout_s)
    except EquivError as exc:
        raise TechmapError(
            f"equivalence check against the pre-mapping generic netlist could "
            f"not be completed: {exc}"
        ) from exc

    status = equiv_report["status"]
    if status != "equivalent":
        raise TechmapError(_equivalence_failure_message(status, equiv_report))

    return {
        "status": status,
        "engine": equiv_report["engine"],
        "engine_version": equiv_report["engine_version"],
        "timeout_s": equiv_report["timeout_s"],
        "elapsed_s": equiv_report["elapsed_s"],
        "artifacts": equiv_report["artifacts"],
    }


def _equivalence_failure_message(status: str, equiv_report: dict[str, Any]) -> str:
    """An actionable :class:`TechmapError` message for a non-``"equivalent"``
    ``klt equiv`` verdict against a just-mapped netlist -- mirrors
    ``synthesize.py``'s own ``_equivalence_failure_message`` exactly, one
    stage later in the pipeline."""
    log_path = equiv_report.get("artifacts", {}).get("log_path")
    if status == "counterexample":
        counterexample = equiv_report.get("counterexample") or {}
        diverging = counterexample.get("diverging_outputs") or []
        confirmed = counterexample.get("confirmed_by_simulation")
        detail = (
            f"diverging outputs: {', '.join(diverging)}"
            if diverging
            else "no diverging outputs reported"
        )
        message = (
            "mapped netlist is NOT equivalent to its pre-mapping generic "
            f"netlist (klt equiv reported 'counterexample'; {detail}; "
            f"confirmed_by_simulation={confirmed})"
        )
    else:
        diagnostics = equiv_report.get("diagnostics") or []
        detail = diagnostics[0]["message"] if diagnostics else "no diagnostic detail"
        message = (
            "equivalence check against the pre-mapping generic netlist did "
            f"not reach a verdict (klt equiv reported '{status}'): {detail}"
        )
    if log_path:
        message += f" -- see {log_path} for the full proof"
    return message
