"""``klt pex``: extract a parasitic-annotated netlist from a routed layout,
re-run the schematic testbenches against it per corner, and report a
per-corner, per-spec-row schematic-vs-extracted delta -- Phase 1a of Epic
#709 ("PEX-aware post-layout sim flow for klt").

Pure library: :func:`run_pex` returns plain Python data (a ``dict`` of
JSON-serialisable primitives) and never prints, mirroring ``extract.py`` /
``sim.py``. Serialisation and human-readable formatting live in the CLI
command module (``cli/pex_cmd.py``).

## What this productizes

`.claude/skills/design-extraction/SKILL.md`'s "Post-layout re-verification
workflow" and ``docs/cli/sim.md``'s "Post-layout verification
(``netlist_source``)" section already document a *manual* two-step version
of this workflow: run ``klt extract`` on the layout, then re-run the same
``klt sim`` testbench request against the extracted netlist with
``netlist_source: "extracted"``, and diff the two runs' ``measurements[]``
by hand. ``klt pex`` is that workflow productized into one command: given a
routed layout and one or more of those existing testbench requests, it
drives extraction itself (:func:`~klayout_tools.extract.run_extract` with
``parasitics=True``), re-runs each testbench twice (schematic netlist, then
extracted netlist) via :func:`~klayout_tools.sim.run_sim`, and emits a
per-corner, per-spec-row delta report -- no new testbench format, no
hand-diffing.

## Reusing the existing testbench convention -- the DUT ``.include`` swap

A ``klt sim`` request's ``netlist`` field is a single file (see
``sim.py``'s "Netlist convention" docstring section): it does not separate
"the DUT" from "the stimulus". ``docs/cli/extract.md``'s "Verified
compatible with ``klt sim``'s netlist convention" section documents the
resolution already in use: an extracted netlist is a bare DUT with no
stimulus, so *"a thin testbench ``.include``s the extracted file,
instantiates the ``.subckt``, and adds the sources, and that testbench is
the ``klt sim`` ``netlist``"* -- verified end to end by
``tests/test_extract.py``'s ``test_extracted_netlist_feeds_klt_sim_
unmodified``. This module leans on that same convention for the
*schematic* side too: a testbench request's ``netlist`` file is expected to
``.include``/``.inc`` a separate schematic-equivalent DUT file (hand-written
or generator-produced, sharing the extracted netlist's
``.SUBCKT <top> <pins...> ... .ENDS <top>`` interface) rather than inlining
the DUT's own devices directly. ``klt pex`` re-runs each testbench schematic-
side completely unmodified (reusing the existing testbench file exactly as
authored -- the literal AC #2 requirement: "reusing the existing testbench
conventions, no parallel testbench format"), and for the extracted side,
rewrites *only* that one ``.include``/``.inc`` line to point at the
extraction's own written netlist -- every source, load, and measurement
card in the testbench is untouched. See :func:`_find_dut_include` /
:func:`_rewrite_dut_include`. A testbench with no ``.include``/``.inc``
directive at all cannot be re-pointed this way and is refused up front
(:class:`PexError`) -- see those functions' docstrings for the extraction
mechanics.

## Envelope shape: matches the provisional shape #871 already wired into `klt signoff`

Issue #871 (Phase 2b of epic #706, merged before this issue) taught ``klt
signoff``'s ``_classify``/``_check_passed``/``_detail``
(``signoff.py``) to recognise a *provisional*, Curator-proposed ``pex``
envelope shape -- a top-level ``delta`` list (per-corner, per-spec-row
schematic-vs-extracted comparisons) plus a ``reference_netlist`` field --
before this module existed, because Epic #706's own post-layout T1 item
(item 7) needed something to grade against. This module's real
:func:`run_pex` output matches that shape exactly (``delta[]`` entries
carry ``spec_row``/``corner_id``/``schematic_value``/``extracted_value``/
``delta_pct``/``status``; ``status`` is ``"pass"`` only when every ``delta``
row passed), so ``signoff.py``'s existing recognition logic needs no
change -- see ``docs/cli/pex.md`` for the full, ratified contract and
``docs/cli/signoff.md``'s "Item 7 is kind-restricted" section for the
now-reconciled note.

**Scope-mismatch note (Curator-flagged, issue #801):** the provisional
shape's own worked examples (``docs/design-evidence-tiers.md``,
``docs/cli/signoff.md``) invoke ``klt pex extracted.spice schematic.spice
--format json`` -- i.e. *two already-produced netlists* as input. This
issue's own Acceptance Criteria (and Epic #709's Phase 1a goal) instead
take a **routed layout plus a testbench set** as input, since ``klt pex``
must run extraction itself as part of the command, not merely diff two
netlists someone else already produced and simulated. This module follows
the AC (layout + testbenches in), per the issue's explicit resolution of
that discrepancy -- see the issue's PR description for the full note.

## What ``--parasitics`` does and does not model, inherited unchanged

``klt pex`` always extracts with ``parasitics=True`` (first-order lumped
RC -- one series R per net terminal plus one ground C per net, from the
deck's curated sheet-resistance/capacitance table; see
``extract.py``'s ``PARASITIC_MODEL_SCOPE``). It inherits that model's scope
and limits unchanged: quasi-static only, and by default a single lumped
element per net (issue #760's vertical-overlap coupling is always included;
issue #976's ``--critical-net`` lateral coupling and issue #977's
``--distributed-rc`` multi-segment ladder are both opt-in, forwarded
straight through to ``klt extract`` -- see this function's own
``critical_nets``/``distributed_rc`` docstring paragraphs). Neither opt-in
is on by default, so an unmodified caller sees the exact same model as
before either existed. ``result["extraction"]["model"]`` echoes
``PARASITIC_MODEL_SCOPE`` verbatim so a reader of the delta report does not
have to cross-reference ``extract.py`` to know what the "extracted" side of
each row's comparison does and does not account for.
"""

from __future__ import annotations

import copy
import json
import os
import re
from collections.abc import Sequence
from typing import Any

from ._paths import _resolve_relative
from .extract import ExtractError, run_extract
from .sim import SimError, load_request, run_sim

__all__ = ["PexError", "run_pex"]

#: Bumped only on a non-additive (breaking) change to this command's own
#: JSON shape -- see docs/json-contract.md.
SCHEMA_VERSION = 1

#: A `.include`/`.inc` directive line, ngspice's two spellings, either
#: quoted or bare -- matches `_find_dut_include`'s "thin testbench" swap
#: point. Case-insensitive: ngspice itself accepts either case.
_INCLUDE_RE = re.compile(
    r"^(?P<prefix>\s*\.(?:include|inc)\s+)(?P<target>.+?)\s*$", re.IGNORECASE
)


class PexError(Exception):
    """Raised when a `klt pex` run cannot be completed: a bad testbench
    request, a testbench with no `.include`/`.inc` DUT reference, testbenches
    that reference different schematic netlists, or a failure in the
    extraction/simulation steps this command drives (wrapping the original
    :class:`~klayout_tools.extract.ExtractError` /
    :class:`~klayout_tools.sim.SimError`).

    The CLI turns this into a clean stderr message + exit code 1, never a
    traceback -- matching every other `klt` verb's error contract.
    """


def _strip_quotes(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in ("'", '"'):
        return token[1:-1]
    return token


def _find_dut_include(body_text: str, body_dir: str) -> tuple[int, str]:
    """Locate the first `.include`/`.inc` directive in a testbench body
    (``body_text``, read from the file at ``body_dir``'s sibling) -- the
    "thin testbench `.include`s the DUT" convention this module's docstring
    documents.

    Returns ``(line_index, resolved_dut_path)``: the zero-based line index
    of the directive (for :func:`_rewrite_dut_include`) and the absolute
    path of the file it names (relative targets resolve against
    ``body_dir``, the testbench body file's own directory -- the same
    convention ngspice itself uses for `.include`).

    Raises :class:`PexError` if the body contains no such directive -- a
    flat testbench with the DUT's own devices inlined directly has no
    single swap point this module can re-point at the extracted netlist
    without also parsing/rewriting device cards, which is out of scope (see
    this module's docstring).
    """
    for index, line in enumerate(body_text.splitlines()):
        match = _INCLUDE_RE.match(line)
        if match:
            target = _strip_quotes(match.group("target").strip())
            return index, _resolve_relative(target, body_dir)
    raise PexError(
        "testbench netlist has no `.include`/`.inc` directive -- klt pex "
        "requires the schematic-side testbench to `.include` its DUT "
        "netlist file (the \"thin testbench .include's the extracted "
        'file" convention -- see docs/cli/extract.md\'s "Verified '
        "compatible with klt sim's netlist convention\" and docs/cli/"
        "pex.md), so the DUT can be swapped for the extracted netlist "
        "without touching the rest of the testbench"
    )


def _rewrite_dut_include(body_text: str, line_index: int, new_dut_path: str) -> str:
    """Return ``body_text`` with the `.include`/`.inc` directive at
    ``line_index`` re-pointed at ``new_dut_path`` (always re-quoted) --
    every other line (sources, loads, `.model` cards, measurements) is
    byte-identical to the input.
    """
    lines = body_text.splitlines(keepends=True)
    line = lines[line_index]
    match = _INCLUDE_RE.match(line.rstrip("\n"))
    assert match is not None, "line_index must name a line _find_dut_include matched"
    newline = "\n" if line.endswith("\n") else ""
    lines[line_index] = f'{match.group("prefix")}"{new_dut_path}"{newline}'
    return "".join(lines)


def _index_corner_measurements(
    sim_report: dict[str, Any],
) -> dict[str, dict[str, dict[str, Any]]]:
    """``{corner_id: {measurement_name: measurement_dict}}`` from a `klt sim`
    response's ``corners[]`` -- the per-(corner, spec-row) lookup
    :func:`_build_delta_rows` diffs schematic against extracted with."""
    index: dict[str, dict[str, dict[str, Any]]] = {}
    for corner in sim_report["corners"]:
        index[corner["corner_id"]] = {m["name"]: m for m in corner["measurements"]}
    return index


def _delta_pct(schematic_value: Any, extracted_value: Any) -> float | None:
    if (
        schematic_value is None
        or extracted_value is None
        or not isinstance(schematic_value, (int, float))
        or not isinstance(extracted_value, (int, float))
        or schematic_value == 0
    ):
        return None
    return round(100.0 * (extracted_value - schematic_value) / abs(schematic_value), 3)


def _row_status(
    schematic_measurement: dict[str, Any] | None, extracted_measurement: dict[str, Any]
) -> str:
    """A delta row's own ``status`` -- mirrors `klt sim`'s own per-
    measurement limit evaluation on the *extracted* side (the side item 7
    actually grades): ``"pass"`` unless the extracted measurement itself
    failed or errored, or the schematic side is missing/errored (in which
    case no trustworthy delta exists to report at all -- ``"error"``, never
    a silently-fabricated ``"pass"``)."""
    extracted_status = extracted_measurement["status"]
    schematic_status = (
        schematic_measurement["status"]
        if schematic_measurement is not None
        else "error"
    )
    if (
        schematic_measurement is None
        or schematic_status == "error"
        or extracted_status == "error"
    ):
        return "error"
    if extracted_status == "fail":
        return "fail"
    return "pass"


def _build_delta_rows(
    *,
    spec_row_prefix: str | None,
    schematic_report: dict[str, Any],
    extracted_report: dict[str, Any],
) -> list[dict[str, Any]]:
    schematic_index = _index_corner_measurements(schematic_report)
    rows: list[dict[str, Any]] = []
    for corner in extracted_report["corners"]:
        corner_id = corner["corner_id"]
        schematic_measurements = schematic_index.get(corner_id, {})
        for extracted_measurement in corner["measurements"]:
            name = extracted_measurement["name"]
            spec_row = name if spec_row_prefix is None else f"{spec_row_prefix}.{name}"
            schematic_measurement = schematic_measurements.get(name)
            rows.append(
                {
                    "spec_row": spec_row,
                    "corner_id": corner_id,
                    "schematic_value": (
                        schematic_measurement["value"]
                        if schematic_measurement is not None
                        else None
                    ),
                    "extracted_value": extracted_measurement["value"],
                    "delta_pct": _delta_pct(
                        schematic_measurement["value"]
                        if schematic_measurement is not None
                        else None,
                        extracted_measurement["value"],
                    ),
                    "status": _row_status(schematic_measurement, extracted_measurement),
                }
            )
    return rows


def _prepare_extracted_request(
    *,
    testbench_path: str,
    extracted_netlist_path: str,
    work_dir: str,
    label: str,
) -> str:
    """Build (and write, under ``work_dir``) an "extracted-side" copy of the
    testbench request at ``testbench_path``: identical in every field except
    ``netlist`` (re-pointed at a new DUT-swapped body file, see
    :func:`_rewrite_dut_include`) and ``netlist_source`` (forced to
    ``"extracted"``). Returns the written request file's path, suitable for
    :func:`~klayout_tools.sim.run_sim`.

    Any relative ``models.lib`` the original request declares is resolved
    to an absolute path first -- the written copy lives in ``work_dir``,
    not next to the original testbench file, so a relative reference would
    otherwise resolve against the wrong directory.
    """
    request = load_request(testbench_path)
    request_dir = os.path.dirname(os.path.abspath(testbench_path))

    schematic_body_path = _resolve_relative(request["netlist"], request_dir)
    with open(schematic_body_path, encoding="utf-8") as handle:
        body_text = handle.read()
    body_dir = os.path.dirname(schematic_body_path)
    include_line, _dut_path = _find_dut_include(body_text, body_dir)
    extracted_body_text = _rewrite_dut_include(
        body_text, include_line, extracted_netlist_path
    )

    extracted_body_path = os.path.join(
        work_dir, f"{label}.extracted-dut-testbench.spice"
    )
    with open(extracted_body_path, "w", encoding="utf-8") as handle:
        handle.write(extracted_body_text)

    extracted_request = copy.deepcopy(request)
    models = extracted_request.get("models")
    if isinstance(models, dict) and isinstance(models.get("lib"), str):
        models = dict(models)
        models["lib"] = _resolve_relative(models["lib"], request_dir)
        extracted_request["models"] = models
    extracted_request["netlist"] = extracted_body_path
    extracted_request["netlist_source"] = "extracted"

    extracted_request_path = os.path.join(work_dir, f"{label}.extracted-request.json")
    with open(extracted_request_path, "w", encoding="utf-8") as handle:
        json.dump(extracted_request, handle, indent=2)
    return extracted_request_path


def run_pex(
    layout_path: str,
    testbench_paths: Sequence[str],
    deck_name: str,
    *,
    output: str | None = None,
    top: str | None = None,
    pdk_variant: str | None = None,
    pdk_root: str | None = None,
    artifacts_dir: str | None = None,
    backend: str | None = None,
    critical_nets: Sequence[str] | None = None,
    distributed_rc: bool = False,
    mom_rlc_net: str | None = None,
    mom_rlc_resistance_ohm: float | None = None,
    mom_rlc_capacitance_ff: float | None = None,
    mom_rlc_inductance_nh: float | None = None,
) -> dict[str, Any]:
    """Extract ``layout_path`` (with lumped-RC parasitics) and re-run every
    testbench in ``testbench_paths`` against both the schematic DUT it
    already references and the newly-extracted one, producing a per-corner,
    per-spec-row delta report.

    ``deck_name``/``top``/``pdk_variant``/``pdk_root``/``output`` are passed
    through to :func:`~klayout_tools.extract.run_extract` (``parasitics``
    is always ``True`` here -- ``klt pex`` has no schematic-equivalent-only
    mode; use `klt extract` directly for that). ``critical_nets`` (``klt
    pex --critical-net``, repeatable, issue #976, Epic #709 Phase 2a) is
    also passed straight through to :func:`~klayout_tools.extract.run_extract`
    -- extracts lateral (same-layer, sidewall) coupling capacitance for any
    same-layer net pair naming one of these nets, in addition to Phase 1's
    always-on vertical-overlap coupling (issue #760), so the re-simulated
    extracted-side testbench (and the resulting `delta[]` rows) reflect it.
    ``None``/empty (the default) skips it entirely -- byte-identical to
    before this feature existed. ``distributed_rc`` (``klt pex
    --distributed-rc``, issue #977, Epic #709 Phase 2b) is also passed
    straight through to :func:`~klayout_tools.extract.run_extract` --
    replaces the single-lumped-element star model with a distributed,
    multi-segment RC ladder for every ``critical_nets``-named net (requires
    ``critical_nets`` to be non-empty), so the re-simulated extracted-side
    testbench (and the resulting `delta[]` rows) reflect the finer-grained
    model. ``False`` (the default) skips it entirely -- byte-identical to
    before this feature existed. ``mom_rlc_net``/``mom_rlc_resistance_ohm``/
    ``mom_rlc_capacitance_ff``/``mom_rlc_inductance_nh`` (``klt pex
    --mom-rlc-net <net> --mom-rlc-resistance-ohm <r>
    --mom-rlc-capacitance-ff <c> [--mom-rlc-inductance-nh <l>]``, issue
    #988, Epic #709 Phase 3a) are also passed straight through to
    :func:`~klayout_tools.extract.run_extract` -- substitutes a
    caller-supplied, directly-solved R/L/C for one named net (e.g. from a
    separate ``klt mom`` run against that net's real geometry) in place of
    this extraction's own Phase 1/2 lumped-RC/coupling-C value for that net,
    so the re-simulated extracted-side testbench (and the resulting
    `delta[]` rows) reflect a MoM-grade parasitic on exactly the net a
    caller has singled out as critical. See ``run_extract``'s own
    ``mom_rlc_net`` docstring paragraph for the full substitution/validation
    rules. ``None`` (the default) for all four skips this entirely --
    byte-identical to before this feature existed. ``backend`` is passed
    through to every :func:`~klayout_tools.sim.run_sim` call (schematic and
    extracted side, every testbench) -- see ``docs/cli/sim.md``'s
    "Execution backends". ``artifacts_dir`` overrides where this command
    writes its own generated artifacts (the extracted-side testbench body/
    request copies, see :func:`_prepare_extracted_request`, and each `klt
    sim` call's own ``options.keep_artifacts`` output, namespaced per
    testbench and per side as ``<artifacts_dir>/<testbench label>/
    schematic|extracted/``); it defaults to a ``.klt/pex/`` directory next
    to ``layout_path`` (the same "next to the input" convention `klt
    render`/`klt sim` already use).

    Each testbench's schematic-side run reuses the testbench file
    *completely unmodified* -- `run_sim(testbench_path, ...)`, exactly the
    request a caller already runs today (AC #2's "reusing the existing
    testbench conventions, no parallel testbench format"). The extracted-
    side run re-points only the testbench's `.include`/`.inc` DUT reference
    at the freshly-extracted netlist (see :func:`_prepare_extracted_request`)
    and tags `netlist_source: "extracted"`, reusing `klt sim`'s existing
    field (`docs/cli/sim.md`'s "Post-layout verification") rather than
    inventing a parallel mechanism.

    Every testbench must `.include`/`.inc` the *same* resolved schematic DUT
    file -- `klt pex` reports one `reference_netlist` for the whole run, not
    per testbench; testbenches for the same block conventionally all
    `.include` the same schematic netlist. Raises :class:`PexError` if they
    disagree.

    Returns a dict matching the documented JSON schema (see
    ``docs/cli/pex.md``) -- notably a `delta[]` array plus a
    `reference_netlist` field, the shape issue #871 (Phase 2b of epic #706)
    already taught `klt signoff`'s `_classify()` to recognise as kind
    `"pex"` (see this module's docstring). Raises :class:`PexError` for
    anything that prevents the run from completing at all (bad testbench,
    unresolvable DUT reference, disagreeing schematic references, an
    extraction or simulation failure).
    """
    if not testbench_paths:
        raise PexError("at least one testbench request is required")

    try:
        extract_report = run_extract(
            layout_path,
            deck_name,
            output=output,
            top=top,
            pdk_variant=pdk_variant,
            pdk_root=pdk_root,
            parasitics=True,
            critical_nets=critical_nets,
            distributed_rc=distributed_rc,
            mom_rlc_net=mom_rlc_net,
            mom_rlc_resistance_ohm=mom_rlc_resistance_ohm,
            mom_rlc_capacitance_ff=mom_rlc_capacitance_ff,
            mom_rlc_inductance_nh=mom_rlc_inductance_nh,
        )
    except ExtractError as exc:
        raise PexError(f"extraction failed: {exc}") from exc

    extracted_netlist_path = extract_report["netlist_path"]

    work_dir = artifacts_dir or os.path.join(
        os.path.dirname(os.path.abspath(layout_path)), ".klt", "pex"
    )
    os.makedirs(work_dir, exist_ok=True)

    # Every testbench's schematic- and extracted-side `klt sim` call gets its
    # own `keep_artifacts` output directory, explicitly namespaced under
    # `work_dir` -- `run_sim`'s own default (next to its request file) would
    # otherwise collide: every extracted-side request this module writes
    # lives directly in `work_dir` (see `_prepare_extracted_request`), so two
    # testbenches' extracted-side runs would share one default artifacts
    # directory, and corner-slug subdirectories would overwrite each other's
    # logs/rawfiles whenever a testbench sets `options.keep_artifacts`.

    single_testbench = len(testbench_paths) == 1
    labels: list[str] = []
    dut_paths: list[str] = []

    # First pass: resolve every testbench's `.include`d DUT reference and
    # validate them *before* running any simulation at all (extraction has
    # already run above, but every `klt sim` call is comparatively
    # expensive) -- a testbench with no `.include` directive, or a set of
    # testbenches that disagree on which schematic DUT they reference, is
    # reported as a fast, cheap error rather than only surfacing after every
    # testbench has already been simulated on both sides.
    for index, testbench_path in enumerate(testbench_paths):
        label = f"{index:02d}-{os.path.splitext(os.path.basename(testbench_path))[0]}"
        labels.append(label)

        try:
            request = load_request(testbench_path)
        except SimError as exc:
            raise PexError(f"testbench '{testbench_path}': {exc}") from exc

        request_dir = os.path.dirname(os.path.abspath(testbench_path))
        schematic_body_path = _resolve_relative(request["netlist"], request_dir)
        if not os.path.isfile(schematic_body_path):
            raise PexError(
                f"testbench '{testbench_path}': netlist not found: "
                f"{schematic_body_path}"
            )
        with open(schematic_body_path, encoding="utf-8") as handle:
            body_text = handle.read()
        _include_line, dut_path = _find_dut_include(
            body_text, os.path.dirname(schematic_body_path)
        )
        dut_paths.append(dut_path)

    if len(set(dut_paths)) > 1:
        raise PexError(
            "testbenches reference different schematic DUT netlists "
            f"({sorted(set(dut_paths))!r}) -- klt pex expects every "
            "testbench in one run to `.include` the same block's schematic "
            "netlist, so a single reference_netlist can be reported"
        )
    reference_netlist = dut_paths[0]

    testbenches_summary: list[dict[str, Any]] = []
    delta: list[dict[str, Any]] = []

    for index, testbench_path in enumerate(testbench_paths):
        label = labels[index]
        dut_path = dut_paths[index]

        try:
            schematic_report = run_sim(
                testbench_path,
                artifacts_dir=os.path.join(work_dir, label, "schematic"),
                backend=backend,
            )
        except SimError as exc:
            raise PexError(
                f"testbench '{testbench_path}': schematic-side simulation failed: {exc}"
            ) from exc

        try:
            extracted_request_path = _prepare_extracted_request(
                testbench_path=testbench_path,
                extracted_netlist_path=extracted_netlist_path,
                work_dir=work_dir,
                label=label,
            )
            extracted_report = run_sim(
                extracted_request_path,
                artifacts_dir=os.path.join(work_dir, label, "extracted"),
                backend=backend,
            )
        except SimError as exc:
            raise PexError(
                f"testbench '{testbench_path}': extracted-side simulation failed: {exc}"
            ) from exc

        spec_row_prefix = (
            None
            if single_testbench
            else os.path.splitext(os.path.basename(testbench_path))[0]
        )
        rows = _build_delta_rows(
            spec_row_prefix=spec_row_prefix,
            schematic_report=schematic_report,
            extracted_report=extracted_report,
        )
        delta.extend(rows)
        testbenches_summary.append(
            {
                "request": testbench_path,
                "schematic_netlist": dut_path,
                "corner_count": extracted_report["corner_count"],
                "measurement_names": [
                    m["name"] for m in extracted_report["measurements"]
                ],
            }
        )

    passed = sum(1 for row in delta if row["status"] == "pass")
    failed = sum(1 for row in delta if row["status"] == "fail")
    errored = sum(1 for row in delta if row["status"] == "error")
    if errored:
        status = "error"
    elif failed:
        status = "fail"
    else:
        status = "pass"

    corner_count = len({row["corner_id"] for row in delta})

    parasitics = extract_report.get("parasitics") or {}
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "layout": layout_path,
        "netlist": extracted_netlist_path,
        "reference_netlist": reference_netlist,
        "extraction": {
            "deck": deck_name,
            "device_count": extract_report["device_count"],
            "net_count": extract_report["net_count"],
            "netlist_sha256": extract_report["netlist_sha256"],
            "model": parasitics.get("model"),
            # Additive field (issue #976): echoes `--critical-net` back --
            # `[]` when the flag was never given, byte-identical to before
            # this feature existed.
            "critical_nets": parasitics.get("critical_nets") or [],
            # Additive field (issue #977): echoes `--distributed-rc` back --
            # `False` when the flag was never given, byte-identical to
            # before this feature existed.
            "distributed_rc": bool(parasitics.get("distributed_rc")),
            # Additive field (issue #988, Epic #709 Phase 3a): `None`
            # unless `--mom-rlc-net` was given, in which case it is `klt
            # extract`'s own substitution report (see
            # `run_extract`'s `mom_rlc_net` docstring paragraph) --
            # byte-identical to before this feature existed otherwise.
            "mom_rlc_override": parasitics.get("mom_rlc_override"),
        },
        "testbenches": testbenches_summary,
        "corner_count": corner_count,
        "delta": delta,
        "passed": passed,
        "failed": failed,
        "errored": errored,
        "provenance": extract_report["provenance"],
    }
    return result
