"""Arithmetic-architecture selection subsystem for ``klt synthesize``
(issue #1722): substitute a generated prefix adder for Yosys's own ``$add``
expansion, and -- in ``auto`` mode -- pick the one that measures best.

Split out of ``synthesize.py`` (issue #2532) as a self-contained subsystem --
the per-trial engine-knob bundle (:class:`_EngineOptions`), request
validation (:func:`_resolve_arithmetic`), the three-stage
probe/generate-and-prove/measure-and-select driver
(:func:`_run_arithmetic_selection`) and its constituent stages
(:func:`_probe_add_widths`, :func:`_parse_yosys_param_int`,
:func:`_prepare_adder_candidate`, :func:`_verify_generated_adder`,
:func:`_measure_candidate`, :func:`_candidate_measurement`) plus the
selection rule itself (:func:`_select_arithmetic_candidate`). This mirrors
the shape of the earlier ``extract_parasitics.py`` (#1572),
``lvs_netgen.py`` (#1803), ``lvs_mismatch.py`` (#1721) and
``gen_compose_routing.py`` (#1717) splits: a cohesive, low-coupling
subsystem relocated **verbatim** out of a file that had grown past the point
one module should hold request/response orchestration *and* a second,
self-contained architecture-exploration engine.

The subsystem's whole interface back into ``synthesize.py`` is two calls in
:func:`~klayout_tools.synthesize.run_synthesize`
(:func:`_resolve_arithmetic` then :func:`_run_arithmetic_selection`), plus
:func:`_candidate_measurement`, which that function reuses to shape the
winning architecture's *real* (non-trial) measurement.

Dependency surface mirrors ``lvs_netgen.py``'s discipline: this module calls
back into ``synthesize.py`` for :class:`~klayout_tools.synthesize.
SynthesizeError`, the script/artifact helpers
(:func:`~klayout_tools.synthesize._run_yosys`,
:func:`~klayout_tools.synthesize._write_script`,
:func:`~klayout_tools.synthesize._write_constr`,
:func:`~klayout_tools.synthesize._write_text_file`,
:func:`~klayout_tools.synthesize._read_produced_netlist`,
:func:`~klayout_tools.synthesize._read_produced_abc_timing`,
:func:`~klayout_tools.synthesize._read_stats`,
:func:`~klayout_tools.synthesize._read_sta_timing`,
:func:`~klayout_tools.synthesize._report_path`) and the
``DEFAULT_ADDER_LABEL``/``DEFAULT_ADDER_MIN_WIDTH``/``_ADDER_MODES``
constants -- each imported *inside* the handful of functions that use them,
never at module scope, so this module never has a load-time dependency back
on ``synthesize.py``; only ``synthesize.py`` depends on this module at
import time. ``klt equiv``'s :func:`~klayout_tools.equiv.run_equiv` /
:class:`~klayout_tools.equiv.EquivError` are reached the same way
(through ``synthesize.py`` rather than straight from ``equiv.py``) so that
``monkeypatch.setattr(synthesize, "run_equiv", …)`` keeps reaching the adder
equivalence gate exactly as it did before the split.

``synthesize.py`` in turn imports this module's names back at module scope,
preserving ``klayout_tools.synthesize.<name>`` as a working import path for
every name the test suite (``tests/test_synthesize_arithmetic.py``,
``tests/test_synthesize.py``) and any other caller used before this split.
``ADDER_ARCHITECTURES`` (``arith_gen.ARCHITECTURES`` under this module's
own alias) moves here with its only users and is re-exported the same way.

This is a **private implementation module, not part of the public API** --
the public entry point remains
:func:`klayout_tools.synthesize.run_synthesize`.
"""

from __future__ import annotations

import json
import os
from typing import Any

from .arith_gen import (
    ARCHITECTURES as ADDER_ARCHITECTURES,
)
from .arith_gen import (
    ArithGenError,
    emit_techmap_verilog,
    generate_adder,
    normalize_architecture,
)


class _EngineOptions:
    """The per-library Yosys/ABC knobs a trial synthesis needs to reproduce
    the real run's engine configuration exactly.

    Bundled into one object purely so :func:`_measure_candidate` does not
    take nine positional look-alikes; every field is resolved once, in
    :func:`run_synthesize`, from the same tables the real run uses.
    """

    __slots__ = (
        "liberty_path",
        "cell_library",
        "delay_target_ps",
        "dont_use_globs",
        "tie_cells",
        "constr_inputs",
    )

    def __init__(
        self,
        *,
        liberty_path: str,
        cell_library: str,
        delay_target_ps: int | None,
        dont_use_globs: tuple[str, ...],
        tie_cells: tuple[tuple[str, str], tuple[str, str]] | None,
        constr_inputs: tuple[str, float] | None,
    ) -> None:
        self.liberty_path = liberty_path
        self.cell_library = cell_library
        self.delay_target_ps = delay_target_ps
        self.dont_use_globs = dont_use_globs
        self.tie_cells = tie_cells
        self.constr_inputs = constr_inputs

    @property
    def target_period_ns(self) -> float | None:
        if self.delay_target_ps is None:
            return None
        return self.delay_target_ps / 1000.0


def _resolve_arithmetic(request: dict[str, Any]) -> dict[str, Any] | None:
    """Validate and normalise ``request.arithmetic`` (issue #1722).

    Returns ``None`` when the field is absent -- the unchanged, pre-#1722
    behaviour for every existing request document. Otherwise returns
    ``{"adders", "min_width", "candidates", "verify_adders"}`` with defaults
    filled in. Raises :class:`SynthesizeError` naming the offending field for
    anything malformed; an unknown architecture name is an error, never a
    silent fallback to Yosys's own expansion.
    """
    from .synthesize import (
        _ADDER_MODES,
        DEFAULT_ADDER_MIN_WIDTH,
        SynthesizeError,
    )

    arithmetic = request.get("arithmetic")
    if arithmetic is None:
        return None
    if not isinstance(arithmetic, dict):
        raise SynthesizeError("request.arithmetic must be a JSON object")

    adders = arithmetic.get("adders")
    if adders is None:
        raise SynthesizeError(
            "request.arithmetic.adders is required when request.arithmetic "
            f"is given (one of: {', '.join(_ADDER_MODES + ADDER_ARCHITECTURES)})"
        )
    if not isinstance(adders, str) or not adders:
        raise SynthesizeError("request.arithmetic.adders must be a non-empty string")
    if adders not in _ADDER_MODES:
        try:
            adders = normalize_architecture(adders)
        except ArithGenError as exc:
            raise SynthesizeError(f"request.arithmetic.adders: {exc}") from exc

    min_width = arithmetic.get("min_width", DEFAULT_ADDER_MIN_WIDTH)
    if isinstance(min_width, bool) or not isinstance(min_width, int) or min_width < 2:
        raise SynthesizeError(
            "request.arithmetic.min_width must be an integer >= 2 "
            f"(default {DEFAULT_ADDER_MIN_WIDTH})"
        )

    candidates = arithmetic.get("candidates")
    if candidates is None:
        resolved_candidates = list(ADDER_ARCHITECTURES)
    else:
        if not isinstance(candidates, list) or not candidates:
            raise SynthesizeError(
                "request.arithmetic.candidates must be a non-empty array of "
                "architecture names"
            )
        resolved_candidates = []
        for entry in candidates:
            try:
                name = normalize_architecture(entry)
            except ArithGenError as exc:
                raise SynthesizeError(f"request.arithmetic.candidates: {exc}") from exc
            if name not in resolved_candidates:
                resolved_candidates.append(name)
        if adders != "auto":
            raise SynthesizeError(
                "request.arithmetic.candidates only applies to "
                'arithmetic.adders: "auto" -- an explicit architecture is '
                "already the only candidate"
            )

    verify_adders = arithmetic.get("verify_adders", True)
    if not isinstance(verify_adders, bool):
        raise SynthesizeError("request.arithmetic.verify_adders must be a boolean")

    return {
        "adders": adders,
        "min_width": min_width,
        "candidates": resolved_candidates,
        "verify_adders": verify_adders,
    }


def _run_arithmetic_selection(
    *,
    config: dict[str, Any],
    output_dir: str,
    resolved_sources: list[str],
    hdl_toplevel: str,
    engine_options: _EngineOptions,
    equiv_timeout_s: float | None,
    repo_root: str | None = None,
) -> tuple[dict[str, Any], tuple[str, ...], str | None]:
    """Resolve ``request.arithmetic`` into a concrete adder substitution.

    Three stages, all of which can end in "substitute nothing" without
    failing the run:

    1. **Probe.** One extra Yosys pass (:func:`_probe_add_widths`) dumps the
       elaborated design as JSON and reads the ``Y_WIDTH`` of every ``$add``
       cell. Nothing at or above ``min_width`` means there is no wide adder
       to lever on -- reported as ``status: "no-wide-adders"``, with the
       run continuing exactly as it would have without the field.
    2. **Generate + prove.** Each candidate architecture is generated at
       every probed width and (unless ``verify_adders`` is ``false``) proven
       equivalent to a behavioural ``a + b + cin`` of the same width via
       ``klt equiv``. A candidate with an unproven adder is **disqualified**,
       never silently kept -- the issue's own equivalence gate.
    3. **Measure + select.** In ``"auto"`` mode each surviving candidate,
       plus Yosys's own default expansion, gets a full trial synthesis in its
       own artifacts directory, and the winner is the smallest one meeting
       ``constraints.clock_period_ns`` (see
       :func:`_select_arithmetic_candidate` for the complete rule, including
       what happens when none does). With an explicit architecture there is
       nothing to select, so no trials are run at all -- the one requested
       architecture is substituted directly and measured by the real run.

    Returns ``(report, adder_sources, techmap_path)``: the response's
    ``arithmetic`` field, the generated Verilog files the real synthesis
    script must ``read_verilog``, and the ``techmap -map`` rule file it must
    apply (both empty/``None`` when nothing is substituted).
    """
    from .synthesize import DEFAULT_ADDER_LABEL, SynthesizeError

    mode = config["adders"]
    arith_dir = os.path.join(output_dir, "arith")
    report: dict[str, Any] = {
        "mode": "auto" if mode == "auto" else "explicit",
        "requested": mode,
        "min_width": config["min_width"],
        "status": "ok",
        "reason": None,
        "adder_widths": [],
        "target_period_ns": engine_options.target_period_ns,
        "selected_architecture": DEFAULT_ADDER_LABEL,
        "candidates": [],
        "selected_measured": None,
    }

    if mode == DEFAULT_ADDER_LABEL:
        report["status"] = "not-requested"
        report["reason"] = (
            'arithmetic.adders: "default" leaves Yosys\'s own $add expansion '
            "in place -- nothing was substituted or measured"
        )
        return report, (), None

    widths = _probe_add_widths(
        resolved_sources=resolved_sources,
        hdl_toplevel=hdl_toplevel,
        output_dir=arith_dir,
        min_width=config["min_width"],
    )
    report["adder_widths"] = widths
    if not widths:
        report["status"] = "no-wide-adders"
        report["reason"] = (
            f"no $add cell of width >= {config['min_width']} survives "
            f"elaboration of '{hdl_toplevel}' -- nothing was substituted"
        )
        return report, (), None

    architectures = list(config["candidates"]) if mode == "auto" else [mode]
    prepared: dict[str, dict[str, Any]] = {}
    for architecture in architectures:
        prepared[architecture] = _prepare_adder_candidate(
            architecture=architecture,
            widths=widths,
            arith_dir=arith_dir,
            verify_adders=config["verify_adders"],
            equiv_timeout_s=equiv_timeout_s,
        )

    if mode != "auto":
        entry = prepared[mode]
        if entry["disqualified_reason"] is not None:
            raise SynthesizeError(
                f"arithmetic.adders: '{mode}' could not be substituted: "
                f"{entry['disqualified_reason']}"
            )
        report["selected_architecture"] = mode
        report["candidates"] = [
            {
                "architecture": mode,
                "prefix_cells": entry["prefix_cells"],
                "logic_levels": entry["logic_levels"],
                "max_fanout": entry["max_fanout"],
                "adder_equivalence": entry["adder_equivalence"],
                "disqualified_reason": None,
                "measured": None,
            }
        ]
        return report, tuple(entry["sources"]), entry["techmap_path"]

    rows: list[dict[str, Any]] = []
    default_row = {
        "architecture": DEFAULT_ADDER_LABEL,
        "prefix_cells": None,
        "logic_levels": None,
        "max_fanout": None,
        "adder_equivalence": [],
        "disqualified_reason": None,
        "measured": _measure_candidate(
            label=DEFAULT_ADDER_LABEL,
            trial_dir=os.path.join(arith_dir, DEFAULT_ADDER_LABEL),
            resolved_sources=resolved_sources,
            hdl_toplevel=hdl_toplevel,
            engine_options=engine_options,
            adder_sources=(),
            adder_techmap_path=None,
            repo_root=repo_root,
        ),
    }
    rows.append(default_row)

    for architecture in architectures:
        entry = prepared[architecture]
        row: dict[str, Any] = {
            "architecture": architecture,
            "prefix_cells": entry["prefix_cells"],
            "logic_levels": entry["logic_levels"],
            "max_fanout": entry["max_fanout"],
            "adder_equivalence": entry["adder_equivalence"],
            "disqualified_reason": entry["disqualified_reason"],
            "measured": None,
        }
        if entry["disqualified_reason"] is None:
            row["measured"] = _measure_candidate(
                label=architecture,
                trial_dir=os.path.join(arith_dir, architecture),
                resolved_sources=resolved_sources,
                hdl_toplevel=hdl_toplevel,
                engine_options=engine_options,
                adder_sources=tuple(entry["sources"]),
                adder_techmap_path=entry["techmap_path"],
                repo_root=repo_root,
            )
        rows.append(row)

    report["candidates"] = rows
    winner, reason = _select_arithmetic_candidate(rows, engine_options.target_period_ns)
    report["reason"] = reason
    if winner is None:
        report["status"] = "no-candidate"
        report["selected_architecture"] = DEFAULT_ADDER_LABEL
        return report, (), None

    report["selected_architecture"] = winner["architecture"]
    if winner["architecture"] == DEFAULT_ADDER_LABEL:
        return report, (), None
    entry = prepared[winner["architecture"]]
    return report, tuple(entry["sources"]), entry["techmap_path"]


def _select_arithmetic_candidate(
    rows: list[dict[str, Any]], target_period_ns: float | None
) -> tuple[dict[str, Any] | None, str | None]:
    """Pick the winning candidate row, and explain the pick when it is not
    the straightforward one.

    The rule, in order:

    1. Candidates that **met** ``constraints.clock_period_ns`` -> the one
       with the smallest ``area_um2`` (the issue's "keeps the one meeting
       ``clock_period_ns`` at least area"), ties broken on delay then on the
       candidate order in :data:`ADDER_ARCHITECTURES`.
    2. No target given -> the fastest candidate, ties broken on area. A
       caller who stated no period asked for the best structure available,
       not for "whatever the default did".
    3. A target given but **nothing met it** -> the fastest candidate, with a
       ``reason`` naming the target and the best delay achieved. This is the
       acceptance criterion's "or the JSON says why none did": the run still
       returns a netlist and the caller can see exactly how far short every
       architecture fell.
    4. No candidate produced a delay number at all (no ``sta`` extension and
       no ABC ``stime`` line) -> the smallest area, with a ``reason`` saying
       the selection was made without timing data.

    Returns ``(None, reason)`` only when every candidate was disqualified by
    the equivalence gate or failed to synthesize.
    """
    eligible = [
        row
        for row in rows
        if row["disqualified_reason"] is None and row["measured"] is not None
    ]
    if not eligible:
        return None, (
            "every arithmetic candidate was disqualified -- see each "
            "candidate's disqualified_reason"
        )

    def _area(row: dict[str, Any]) -> float:
        area = row["measured"]["area_um2"]
        return float("inf") if area is None else area

    def _delay(row: dict[str, Any]) -> float | None:
        return row["measured"]["delay_ns"]

    timed = [row for row in eligible if _delay(row) is not None]

    if not timed:
        winner = min(eligible, key=_area)
        return winner, (
            "no candidate produced a delay measurement (neither the native "
            "sta stage nor ABC's stime report was available) -- selected the "
            "smallest area instead of the fastest structure"
        )

    if target_period_ns is not None:
        meeting = [row for row in timed if _delay(row) <= target_period_ns]
        if meeting:
            winner = min(meeting, key=lambda row: (_area(row), _delay(row)))
            return winner, None
        best = min(timed, key=lambda row: (_delay(row), _area(row)))
        return best, (
            f"no candidate met constraints.clock_period_ns="
            f"{target_period_ns} ns; the fastest was "
            f"'{best['architecture']}' at {_delay(best)} ns -- selected it "
            "anyway as the closest available structure"
        )

    winner = min(timed, key=lambda row: (_delay(row), _area(row)))
    return winner, None


def _probe_add_widths(
    *,
    resolved_sources: list[str],
    hdl_toplevel: str,
    output_dir: str,
    min_width: int,
) -> list[int]:
    """Return the sorted, distinct result widths of the ``$add`` cells the
    elaborated design contains, filtered to ``>= min_width``.

    Runs one extra, cheap Yosys pass (``read_verilog`` -> ``hierarchy`` ->
    ``proc`` -> ``opt_expr``/``opt_clean`` -> ``write_json``) and reads the
    cells straight out of the emitted JSON. This is deliberately **not**
    regexed out of the RTL: a ``+`` in the source can be constant-folded
    away, widened by context, or shared, and only the elaborated netlist
    knows the width Yosys will actually build -- which is the width the
    ``techmap`` rule has to match on.
    """
    from .synthesize import SynthesizeError, _run_yosys

    try:
        os.makedirs(output_dir, exist_ok=True)
    except OSError as exc:
        raise SynthesizeError(
            f"could not create arithmetic output directory '{output_dir}': {exc}"
        ) from exc

    script_path = os.path.join(output_dir, f"probe_{hdl_toplevel}.ys")
    json_path = os.path.join(output_dir, f"probe_{hdl_toplevel}.json")
    lines = [f"read_verilog {path}" for path in resolved_sources]
    lines += [
        f"hierarchy -check -top {hdl_toplevel}",
        "proc",
        "opt_expr",
        "opt_clean",
        f"write_json {json_path}",
    ]
    try:
        with open(script_path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    except OSError as exc:
        raise SynthesizeError(
            f"could not write arithmetic probe script '{script_path}': {exc}"
        ) from exc

    _run_yosys(script_path)

    try:
        with open(json_path, encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise SynthesizeError(
            f"could not read the arithmetic probe output '{json_path}': {exc}"
        ) from exc

    widths: set[int] = set()
    for module in (document.get("modules") or {}).values():
        for cell in (module.get("cells") or {}).values():
            if cell.get("type") != "$add":
                continue
            width = _parse_yosys_param_int(
                (cell.get("parameters") or {}).get("Y_WIDTH")
            )
            if width is not None and width >= min_width:
                widths.add(width)
    return sorted(widths)


def _parse_yosys_param_int(value: Any) -> int | None:
    """Decode a Yosys ``write_json`` cell parameter as an integer.

    Yosys emits parameters either as plain JSON integers or as MSB-first
    binary strings (``"00000000000000000000000000010010"`` for 18) depending
    on the parameter's declared type and width -- both spellings appear in
    the same file. Anything else (``"x"``/``"z"`` bits, a non-numeric string)
    is reported as ``None`` rather than guessed at.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text and set(text) <= {"0", "1"}:
            return int(text, 2)
    return None


def _prepare_adder_candidate(
    *,
    architecture: str,
    widths: list[int],
    arith_dir: str,
    verify_adders: bool,
    equiv_timeout_s: float | None,
) -> dict[str, Any]:
    """Generate one architecture's adder modules (one per probed width) plus
    the shared ``techmap`` rule file, and -- when ``verify_adders`` is set --
    prove each of them equivalent to a behavioural ``a + b + cin``.

    The proof is the issue's step 3 gate, deliberately scoped to the
    *generated adder*, not to the whole substituted design: it is what makes
    "this structure computes addition" a checked claim rather than an assumed
    one, it is cheap (an N-bit adder miter, far cheaper than the whole
    design), and -- unlike a whole-design check -- it works even when the
    design itself is sequential, which the fleet's adder-bound canaries are.

    Never raises for a failed proof: the candidate comes back with a
    ``disqualified_reason``, which the ``auto`` sweep drops from the table
    and an explicit request turns into a hard :class:`SynthesizeError` one
    level up.
    """
    from .synthesize import SynthesizeError, _write_text_file

    candidate_dir = os.path.join(arith_dir, architecture, "rtl")
    try:
        os.makedirs(candidate_dir, exist_ok=True)
    except OSError as exc:
        raise SynthesizeError(
            f"could not create adder output directory '{candidate_dir}': {exc}"
        ) from exc

    sources: list[str] = []
    techmap_entries: dict[int, str] = {}
    equivalence: list[dict[str, Any]] = []
    disqualified: str | None = None
    prefix_cells = 0
    logic_levels = 0
    max_fanout = 0

    for width in widths:
        try:
            built = generate_adder(width=width, architecture=architecture)
        except ArithGenError as exc:
            raise SynthesizeError(
                f"could not generate a {width}-bit '{architecture}' adder: {exc}"
            ) from exc

        module = built["module_name"]
        adder_path = os.path.join(candidate_dir, f"{module}.v")
        reference_path = os.path.join(candidate_dir, f"{module}_ref.v")
        _write_text_file(adder_path, built["verilog"])
        _write_text_file(reference_path, built["reference_verilog"])
        sources.append(adder_path)
        techmap_entries[width] = module
        prefix_cells += built["metrics"]["prefix_cells"]
        logic_levels = max(logic_levels, built["metrics"]["logic_levels"])
        max_fanout = max(max_fanout, built["metrics"]["max_fanout"])

        if not verify_adders:
            continue
        verdict = _verify_generated_adder(
            candidate_dir=candidate_dir,
            module=module,
            reference_module=built["reference_name"],
            adder_path=adder_path,
            reference_path=reference_path,
            width=width,
            timeout_s=equiv_timeout_s,
        )
        equivalence.append(verdict)
        if verdict["status"] != "equivalent" and disqualified is None:
            disqualified = (
                f"the generated {width}-bit '{architecture}' adder was not "
                f"proven equivalent to `a + b + cin` (klt equiv reported "
                f"'{verdict['status']}'"
                + (f": {verdict['detail']}" if verdict["detail"] else "")
                + ")"
            )

    techmap_path = os.path.join(candidate_dir, f"{architecture}_add_techmap.v")
    try:
        _write_text_file(techmap_path, emit_techmap_verilog(techmap_entries))
    except ArithGenError as exc:  # pragma: no cover - widths is non-empty
        raise SynthesizeError(str(exc)) from exc

    return {
        "architecture": architecture,
        "sources": sources,
        "techmap_path": techmap_path,
        "adder_equivalence": equivalence,
        "disqualified_reason": disqualified,
        "prefix_cells": prefix_cells,
        "logic_levels": logic_levels,
        "max_fanout": max_fanout,
    }


def _verify_generated_adder(
    *,
    candidate_dir: str,
    module: str,
    reference_module: str,
    adder_path: str,
    reference_path: str,
    width: int,
    timeout_s: float | None,
) -> dict[str, Any]:
    """Prove one generated adder equivalent to its behavioural reference via
    :func:`klayout_tools.equiv.run_equiv`, returning
    ``{width, status, detail}``.

    A :class:`~klayout_tools.equiv.EquivError` (a missing Yosys, an
    unreadable source) is reported as ``status: "error"`` with the message as
    ``detail`` -- the caller decides whether that disqualifies the candidate
    or fails the run, exactly as it does for a ``"counterexample"``.
    """
    # `run_equiv`/`EquivError` are reached through `synthesize.py` rather
    # than straight from `equiv.py` so that a `monkeypatch.setattr(
    # synthesize, "run_equiv", ...)` still reaches this gate, exactly as it
    # did while this function lived in that module.
    from .synthesize import EquivError, SynthesizeError, run_equiv

    request_path = os.path.join(candidate_dir, f"{module}_equiv_request.json")
    request = {
        "gold": {"sources": [reference_path], "top": reference_module},
        "gate": {"sources": [adder_path], "top": module},
    }
    try:
        with open(request_path, "w", encoding="utf-8") as handle:
            json.dump(request, handle, indent=2)
    except OSError as exc:
        raise SynthesizeError(
            f"could not write adder equivalence request '{request_path}': {exc}"
        ) from exc

    try:
        report = run_equiv(request_path, timeout_s=timeout_s)
    except EquivError as exc:
        return {"width": width, "status": "error", "detail": str(exc)}

    detail = None
    if report["status"] == "counterexample":
        diverging = (report.get("counterexample") or {}).get("diverging_outputs")
        if diverging:
            detail = f"diverging outputs: {', '.join(diverging)}"
    elif report["status"] != "equivalent":
        diagnostics = report.get("diagnostics") or []
        if diagnostics:
            detail = diagnostics[0]["message"]
    return {"width": width, "status": report["status"], "detail": detail}


def _measure_candidate(
    *,
    label: str,
    trial_dir: str,
    resolved_sources: list[str],
    hdl_toplevel: str,
    engine_options: _EngineOptions,
    adder_sources: tuple[str, ...],
    adder_techmap_path: str | None,
    repo_root: str | None = None,
) -> dict[str, Any] | None:
    """Run one full trial synthesis for a candidate architecture and report
    what it measured.

    Uses exactly the engine configuration the real run will use (same
    liberty, same ``-constr``/``-D``/``-dont_use``/``hilomap`` knobs), into
    its own ``.klt/synthesize/<run_id>/arith/<label>/`` directory so every trial's
    script, netlist, stats and ABC log survive as debuggable artifacts --
    "measured, not guessed" is only a real claim if the measurement is
    reproducible afterwards.

    Returns ``None`` when this candidate's trial synthesis fails outright,
    which disqualifies it from selection without failing the whole run: one
    architecture Yosys cannot map is not a reason to abandon the other four.
    """
    from .synthesize import (
        SynthesizeError,
        _read_produced_abc_timing,
        _read_produced_netlist,
        _read_sta_timing,
        _read_stats,
        _report_path,
        _run_yosys,
        _write_constr,
        _write_script,
    )

    try:
        os.makedirs(trial_dir, exist_ok=True)
    except OSError as exc:
        raise SynthesizeError(
            f"could not create arithmetic trial directory '{trial_dir}': {exc}"
        ) from exc

    script_path = os.path.join(trial_dir, f"synth_{hdl_toplevel}.ys")
    netlist_path = os.path.join(trial_dir, f"{hdl_toplevel}_synth.v")
    stats_path = os.path.join(trial_dir, f"{hdl_toplevel}_stats.json")
    constr_path: str | None = None
    abc_log_path: str | None = None
    if engine_options.constr_inputs is not None:
        constr_path = os.path.join(trial_dir, f"{hdl_toplevel}_abc.constr")
        abc_log_path = os.path.join(trial_dir, f"{hdl_toplevel}_abc.log")
        _write_constr(constr_path, *engine_options.constr_inputs)

    _write_script(
        script_path=script_path,
        sources=resolved_sources,
        hdl_toplevel=hdl_toplevel,
        liberty_path=engine_options.liberty_path,
        stats_path=stats_path,
        netlist_path=netlist_path,
        constr_path=constr_path,
        abc_log_path=abc_log_path,
        delay_target_ps=engine_options.delay_target_ps,
        dont_use_globs=engine_options.dont_use_globs,
        tie_cells=engine_options.tie_cells,
        adder_sources=adder_sources,
        adder_techmap_path=adder_techmap_path,
    )

    try:
        _run_yosys(script_path)
        _read_produced_netlist(netlist_path)
        module_stats = _read_stats(stats_path, hdl_toplevel)
        abc_timing = _read_produced_abc_timing(
            abc_log_path, engine_options.delay_target_ps
        )
    except SynthesizeError:
        return None

    measurement = _candidate_measurement(
        instance_count=module_stats["num_cells"],
        area_um2=module_stats["area"],
        sta=_read_sta_timing(netlist_path, engine_options.liberty_path, hdl_toplevel),
        abc_timing=abc_timing,
        target_period_ns=engine_options.target_period_ns,
    )
    measurement["label"] = label
    # Issue #1844: only the values reported back in `candidates[].measured`
    # are normalized -- `netlist_path`/`script_path` above stay the real
    # absolute paths used for this trial's own I/O throughout this function.
    measurement["netlist_path"] = _report_path(netlist_path, repo_root=repo_root)
    measurement["script_path"] = _report_path(script_path, repo_root=repo_root)
    return measurement


def _candidate_measurement(
    *,
    instance_count: int,
    area_um2: float | None,
    sta: dict[str, Any] | None,
    abc_timing: dict[str, Any] | None,
    target_period_ns: float | None,
) -> dict[str, Any]:
    """Normalise one candidate's measured QoR into the comparable triple the
    selection rule uses.

    ``delay_ns`` prefers the native whole-netlist ``sta`` worst path and
    falls back to ABC's own ``stime`` estimate, recording which one it used
    in ``delay_source`` -- the two are **not** interchangeable numbers (see
    this module's docstring and ``docs/cli/synthesize.md``), so a caller
    comparing two runs must check they came from the same source. Every
    candidate in a single sweep is measured the same way, so the *ranking*
    within one report is always consistent even when the extension is
    missing.
    """
    delay_ns: float | None = None
    delay_source: str | None = None
    worst_path = (sta or {}).get("worst_path") if sta else None
    if worst_path and worst_path.get("delay_ns") is not None:
        delay_ns = float(worst_path["delay_ns"])
        delay_source = "sta"
    elif abc_timing and abc_timing.get("critical_path_ps") is not None:
        delay_ns = float(abc_timing["critical_path_ps"]) / 1000.0
        delay_source = "abc_stime"

    meets = None
    if target_period_ns is not None and delay_ns is not None:
        meets = delay_ns <= target_period_ns
    return {
        "instance_count": instance_count,
        "area_um2": area_um2,
        "delay_ns": delay_ns,
        "delay_source": delay_source,
        "meets_constraint": meets,
    }
