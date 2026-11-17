"""Transient-noise handling for ``klt sim`` (issues #2963, #2985, #5772).

ngspice's ``trnoise``/``trrandom`` sources are the only noise a ``.tran``
contains. This module owns everything ``klt sim`` says or does about them:

* scanning the netlist closure for such sources and the additive
  ``environment.transient_noise`` / ``environment.monte_carlo`` disclosures
  built from the scan;
* ``options.transient_noise_seed`` (#5772): validation, the ``setseed N``
  control line, the checkpoint-fingerprint key and the off-host echo check.

Split out of ``sim`` so the per-file size ratchet
(``scripts/check_file_size_baseline.py``) holds; ``sim`` re-imports the names
it calls. The two ``sim`` helpers the scan needs are imported lazily to avoid
an import cycle.
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any

from . import sim_steps
from .sim_errors import SimError

_NOISE_SOURCE_RE = re.compile(r"\b(trnoise|trrandom)\s*\(", re.IGNORECASE)

_UNSEEDED_NOISE_NOTE = (
    "trnoise()/trrandom() sources are not governed by monte_carlo.seed "
    "(ngspice seeds them from the process id): re-running this request "
    "redraws their noise, and samples run in separate batch jobs or hosts "
    "may share a noise stream."
)


def validate_seed(value: Any, engine: str) -> int | None:
    """Validate ``options.transient_noise_seed`` (issue #5772).

    The value becomes ngspice's ``setseed N`` control command, emitted first
    in every corner deck's ``.control`` block -- measured on ngspice-47 to be
    the one placement that pins a ``trrandom`` stream through a ``klt sim``
    run (``.spiceinit`` and ``.options seed=`` are both too early or
    otherwise ignored; see docs/cli/sim.md's #3957 section). It does **not**
    make ``trnoise`` reproducible -- no known route does -- so the option's
    echo and scope statement live on ``environment.transient_noise``.

    ``None``/absent normalises to ``None`` -- no ``setseed`` line, decks
    byte-identical to before this issue. Anything else must be an integer (a
    bool is a JSON ``true``/``false``, not the integer ngspice's command
    takes). ``engine: "xyce"`` refuses a declared value outright -- the
    command has no Xyce equivalent and the Xyce deck has no ``.control``
    block to carry it -- rather than silently accepting it with no effect.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise SimError(
            "options.transient_noise_seed must be an integer (the value "
            "ngspice's `setseed` control command takes), got " + json.dumps(value)
        )
    if engine == "xyce":
        raise SimError(
            "options.transient_noise_seed is not supported for engine "
            "'xyce': it emits `setseed` in the ngspice `.control` block, "
            "which the Xyce deck does not have; use engine 'ngspice' or "
            "drop the option"
        )
    return value


def seed_lines(seed: int | None) -> list[str]:
    """The ``setseed N`` command line for a corner deck's ``.control`` block
    (issue #5772), or no lines when the request declared no seed -- decks
    stay byte-identical to pre-#5772 then.

    Emitted as the block's **first** command, before the ``pre_osdi`` lines,
    the ``alterparam``/``reset`` prefix, the ``save`` card and every supply
    ``alter``: the measured configuration (issues #3957/#5772, ngspice-47)
    is ``setseed N`` first in ``.control``; ``.spiceinit`` placement is
    measured not to pin the stream (the interpreter sources it before the
    deck is parsed), and the prefix commands themselves draw nothing from
    the RNG. This makes a ``trrandom`` stream replayable; it does **not**
    make ``trnoise`` replayable -- no known route does."""
    return [] if seed is None else [f"setseed {seed}"]


def key_in_fingerprint(payload: dict[str, Any], seed: int | None) -> None:
    """Issue #5772: the seed changes every corner deck's bytes (the
    ``setseed`` card), so a checkpoint written under another seed -- or none
    -- never applies. Keyed in off ``None`` only, so an existing checkpoint
    stays valid for a request that omits the field."""
    if seed is not None:
        payload["transient_noise_seed"] = seed


def check_transient_noise_seed_echo(
    request: dict[str, Any], report: dict[str, Any]
) -> None:
    """Fail loudly when an off-host report did not honour
    ``options.transient_noise_seed`` (issue #5772).

    A runner that predates the field drops it silently: its corners run with
    unpinned noise streams while the caller believes them seed-keyed. The
    collected inner report's ``environment.transient_noise`` block is the
    runner's own disclosure, so a block that carries no ``seed`` -- or a
    different one -- while the request declared the option is that silent
    drop, turned into a named :class:`SimError`. A report with no -- or an
    empty -- ``transient_noise`` block is not checked: the run had no
    qualifying transient-noise source, and the option is then inert by
    design.
    """
    declared = (request.get("options") or {}).get("transient_noise_seed")
    if declared is None:
        return
    block = (report.get("environment") or {}).get("transient_noise") or {}
    if not block:
        return
    echoed = block.get("seed")
    if echoed != declared:
        raise SimError(
            "off-host runner did not honour options.transient_noise_seed: "
            f"declared {json.dumps(declared)}, runner echoed "
            f"{json.dumps(echoed)} (the runner is likely older than this "
            "client and dropped the option)"
        )


def _noise_sources_in_text(netlist_text: str) -> list[tuple[str, str]]:
    """``(lower-cased name, keyword)`` of every independent ``V*``/``I*``
    source declaring a ``trnoise``/``trrandom`` waveform (issue #2963),
    including inside ``.subckt`` bodies. Lexical, via
    ``sim._logical_netlist_lines``."""
    from .sim import _logical_netlist_lines

    found: list[tuple[str, str]] = []
    for line in _logical_netlist_lines(netlist_text):
        if line[0].lower() not in ("v", "i"):
            continue
        match = _NOISE_SOURCE_RE.search(line)
        if match:
            found.append((line.split(None, 1)[0].lower(), match.group(1).lower()))
    return found


def _scan_noise_sources(
    netlist_path: str, *, request: dict[str, Any], repo_root: str | None
) -> tuple[set[tuple[str, str]], list[dict[str, str]]]:
    """``(sources, not_scanned)``: every ``trnoise``/``trrandom`` source
    declared in the netlist's scanned ``.include`` closure (issues #2963,
    #2985), and what part of the closure could not be scanned."""
    from .sim import _mismatch_scan_closure

    files, not_scanned = _mismatch_scan_closure(request, netlist_path, repo_root)
    sources: set[tuple[str, str]] = set()
    for _label, path in files:
        try:
            with open(path, encoding="utf-8") as handle:
                sources.update(_noise_sources_in_text(handle.read()))
        except (OSError, UnicodeDecodeError):
            not_scanned.append({"target": _label, "reason": "unreadable"})
    return sources, not_scanned


def _unseeded_noise_report(
    netlist_path: str, *, request: dict[str, Any], repo_root: str | None
) -> dict[str, Any] | None:
    """Additive ``environment.monte_carlo`` fields for transient-noise
    sources the ``monte_carlo.seed`` contract does not govern (issue #2963),
    or ``None`` when no scanned file declares one (shape unchanged)."""
    sources, not_scanned = _scan_noise_sources(
        netlist_path, request=request, repo_root=repo_root
    )
    if not sources:
        return None
    report: dict[str, Any] = {
        "unseeded_sources": [
            {"name": name, "keyword": keyword} for name, keyword in sorted(sources)
        ],
        "unseeded_sources_note": _UNSEEDED_NOISE_NOTE,
    }
    if not_scanned:
        report["unseeded_sources_not_scanned"] = not_scanned
    return report


def _analysis_runs_tran(analysis: dict[str, Any]) -> bool:
    """Does the resolved analysis run a transient (scalar ``tran`` or an
    ``analysis_steps[]`` tran step)?"""
    if sim_steps.is_steps_analysis(analysis):
        return any(
            str(step.get("kind", "")).lower() == "tran"
            for step in analysis.get("steps", [])
        )
    return str(analysis.get("kind", "")).lower() == "tran"


def _reproducibility_note(kinds: set[str], *, seed: int | None) -> str:
    """The issue #5772 reproducibility statement for
    ``environment.transient_noise``, composed from the source kinds actually
    found and whether the request pinned the stream.

    Per-kind, measured (issues #3957/#5772): ``trnoise`` is not replayable
    through any known ngspice seeding route; ``trrandom`` is replayable only
    via a ``.control`` ``setseed`` command, which
    ``options.transient_noise_seed`` emits. The note must never claim a
    ``trnoise`` stream is pinnable, and never claim ``trrandom`` is inherently
    unpinnable -- the two half-truths the note replaces (the #2963 wording
    said both sources were unseedable).
    """
    sentences: list[str] = []
    if "trnoise" in kinds:
        sentences.append(
            "trnoise streams are not replayable through any known ngspice "
            "seeding route (measured, issues #3957/#5772): re-running this "
            "request redraws them."
        )
    if "trrandom" in kinds:
        if seed is not None:
            sentences.append(
                f"trrandom streams are replayable via a `.control` `setseed` "
                f"command; this run emitted `setseed {seed}` first in each "
                "corner's `.control` block, so its trrandom measurements "
                "replay at this seed."
            )
        else:
            sentences.append(
                "trrandom streams are replayable only via a `.control` "
                "`setseed` command; this run declares no "
                "options.transient_noise_seed, so its trrandom stream is not "
                "reproducible either."
            )
    if seed is not None and "trnoise" in kinds:
        sentences.append(
            "options.transient_noise_seed pins the trrandom stream only; it "
            "does not make the trnoise stream reproducible."
        )
    return " ".join(sentences)


def transient_noise_environment(
    netlist_path: str,
    *,
    request: dict[str, Any],
    analysis: dict[str, Any],
    engine: str,
    repo_root: str | None,
    transient_noise_seed: int | None = None,
) -> dict[str, Any]:
    """Additive ``environment.transient_noise`` (issue #2985): what noise an
    ngspice transient contains -- only the declared ``trnoise``/``trrandom``
    sources, no intrinsic device or resistor noise. ``{}`` (field absent)
    unless the engine is ngspice, a transient runs and a scanned file
    declares such a source.

    Issue #5772: the block also carries the reproducibility disclosure
    (``reproducibility_note``, keyed on the kinds actually found) on every
    qualifying run -- the #2963 note only reached runs with a
    ``monte_carlo`` block -- and, when the request declared
    ``options.transient_noise_seed``, the effective seed as ``seed``.
    """
    if engine != "ngspice" or not _analysis_runs_tran(analysis):
        return {}
    sources, not_scanned = _scan_noise_sources(
        netlist_path, request=request, repo_root=repo_root
    )
    if not sources:
        return {}
    report: dict[str, Any] = {
        "engine": engine,
        "sources": [
            {"name": name, "kind": keyword} for name, keyword in sorted(sources)
        ],
        "intrinsic_device_noise": False,
        "intrinsic_resistor_noise": False,
        "reproducibility_note": _reproducibility_note(
            {keyword for _name, keyword in sources}, seed=transient_noise_seed
        ),
    }
    if transient_noise_seed is not None:
        report["seed"] = transient_noise_seed
    if not_scanned:
        report["sources_not_scanned"] = not_scanned
    return {"transient_noise": report}


def with_unseeded_noise(
    monte_carlo_info: dict[str, Any],
    netlist_path: str,
    *,
    request: dict[str, Any],
    repo_root: str | None,
) -> dict[str, Any]:
    """``monte_carlo_info`` plus the issue #2963 unseeded-noise disclosure
    (and its stderr warning) when a scanned file declares such a source."""
    noise_report = _unseeded_noise_report(
        netlist_path, request=request, repo_root=repo_root
    )
    if noise_report is None:
        return monte_carlo_info
    print(f"warning: {_UNSEEDED_NOISE_NOTE}", file=sys.stderr)
    return {**monte_carlo_info, **noise_report}
