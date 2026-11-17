"""The ``save_all_supersedes`` request-level diagnostic (issue #5622).

Split out of `sim.py` deliberately: `sim.py` sits at its
`file-size-baseline.json` ceiling, and the per-file size ratchet
(`scripts/check_file_size_baseline.py`) lets a feature grow the file only by
paying the same number of lines down, so the diagnostic lives here and
`sim.py` keeps only the preflight call site.
"""

from __future__ import annotations

import re
from typing import Any

_COMMENT_SPLIT_RE = re.compile(r";|\s\$")


def preflight(
    netlist_path: str,
    engine: str,
    save_mode: str,
    save_vectors: tuple[str, ...],
    want_waveforms: bool,
) -> dict[str, Any] | None:
    """Request-level warning when the generated ``save all`` supersedes a
    ``.save`` card of the netlist body while waveform capture is on (issue
    #5622). Never raises; reports presence only, so a ``.save`` reachable only
    through ``.include``/``.lib`` is a documented miss.

    Gated on every condition that makes the widening real *and* costly: a
    top-level body ``.save`` that actually restricts (a card whose only
    argument is ``all``, or a bare ``.save``, restricted nothing, so the
    generated ``save all`` superseded nothing); ``save_mode`` ``"all"``
    (``"netlist"`` emits no card); no ``save_vectors`` (those emit
    ``save <named vectors>``, not ``save all``); and ``waveforms`` on -- a
    measurements-only request that supersedes a ``.save`` is #2521's intended
    rescue, and its cost is not in the rawfile.
    """
    from .sim import DEFAULT_SAVE_MODE  # lazy: sim imports this module

    if (
        engine == "xyce"
        or save_mode != DEFAULT_SAVE_MODE
        or save_vectors
        or not want_waveforms
    ):
        return None
    try:
        with open(netlist_path, encoding="utf-8", errors="replace") as handle:
            cards = _body_save_cards(handle.read())
    except Exception:  # advisory only
        return None
    restricting = [
        card
        for card in cards
        if [arg for arg in card.split()[1:] if arg.lower() != "all"]
    ]
    if not restricting:
        return None
    return {
        "severity": "warning",
        "code": "save_all_supersedes",
        "message": (
            f"the netlist body restricts the saved signal set ({restricting[0]}) but "
            "the request widens it: the generated 'save all' supersedes the "
            'body\'s .save card(s) (options.save_mode defaults to "all"), so '
            "with options.waveforms on the waveform.raw artifact holds every "
            "node of the flattened design and can be orders of magnitude "
            "larger than the body's own .save implies. Set options.save_mode "
            'to "netlist" to honor the body\'s .save, or options.save_vectors '
            "to name the signals to keep; leave both unset to keep 'save all' "
            "deliberately"
        ),
        "save_cards": len(restricting),
        "source": "netlist_save_cards",
    }


def _body_save_cards(netlist_text: str) -> list[str]:
    """Top-level ``.save`` cards of a netlist body: ``+`` continuations
    joined, comments dropped, ``.control`` ... ``.endc`` blocks and
    ``.subckt`` bodies skipped -- the same lexical scan ``sim.py`` applies
    to the body; not followed into ``.include``/``.lib`` files, and a
    ``.save`` inside a ``.lib`` section body counts as top level, as in
    ``sim._netlist_declarations``."""
    cards: list[str] = []
    depth = 0
    in_control = False
    for line in _logical_lines(netlist_text):
        word = line.split(None, 1)[0].lower()
        if word == ".control":
            in_control = True
        elif word == ".endc":
            in_control = False
        elif not in_control:
            if word == ".subckt":
                depth += 1
            elif word == ".ends":
                depth = max(0, depth - 1)
            elif not depth and word == ".save":
                cards.append(line)
    return cards


def _logical_lines(netlist_text: str) -> list[str]:
    """Netlist lines with ``+`` continuations joined and full-line ``*`` and
    inline ``;``/``$`` comments removed -- the same lexical pass as
    ``sim._logical_netlist_lines``."""
    logical: list[str] = []
    for raw in netlist_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("*"):
            continue
        line = _COMMENT_SPLIT_RE.split(line, maxsplit=1)[0].strip()
        if not line:
            continue
        if line.startswith("+"):
            if logical:
                logical[-1] += " " + line[1:].strip()
            continue
        logical.append(line)
    return logical
