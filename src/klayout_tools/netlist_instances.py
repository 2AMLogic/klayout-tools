"""Derived per-instance view of the netlist ``klt netlist`` just produced.

xschem's written netlist wraps a device instance's parameters onto ``+``
continuation lines and emits the device multiplier twice, as ``mult=N`` and
``m=N`` -- both properties are the PDK symbol's own ``format=`` attribute
(sky130 ``nfet_01v8.sym`` embeds the literal breaks and the pair), so ``klt``
cannot fix them by changing what it writes without rewriting a vendor's
netlist bytes. What it can do is offer a faithful, structured view of what it
just wrote, so a reviewer or a downstream tool reads resolved per-instance
attributes instead of re-deriving them from wrapped text (issue #5687).

The two multiplier forms are **not** redundant: ``m=`` is SPICE's own
subcircuit-instance multiplier and carries the drive, while sky130's device
subcircuits declare ``mult`` without forwarding it to the inner primitive --
``mult`` instead de-rates the Monte Carlo mismatch sigma
(``AGAUSS(0,1.0,1)/sqrt(l*w*mult)`` under ``MC_MM_SWITCH``). Dropping either
silently corrupts a simulation (1x drive, or mismatch overstated by
``sqrt(N)``), so this module only reports the pair -- and whether the two
agree -- and never suggests a form is removable (that judgement, and the PDK
model text it needs, is issue #5779's lint).

Kept out of :mod:`klayout_tools.netlist` to hold that file at its size
ceiling (the same reason :mod:`klayout_tools.netlist_diagnostics` exists).
"""

from __future__ import annotations

from typing import Any

from .netlist_paths import fold_continuations

#: How many instance entries the payload's ``instances`` array keeps. Above
#: this the array truncates and the sibling ``instances_truncated`` flag is
#: set (the ``diagnostics.*_truncated`` convention), so a large deck cannot
#: balloon the JSON payload unboundedly.
INSTANCES_CAP = 1000


def instances_payload(data: bytes, *, cap: int = INSTANCES_CAP) -> dict[str, Any]:
    """The payload block derived from ``data``, the netlist text just produced.

    ``data`` is the regenerated netlist's bytes (in ``--check`` mode, the side
    ``--check`` actually compared). Returns ``{"instances": [...],
    "instances_truncated": bool}`` -- a top-level ``instances`` **array** (one
    object per device instance card, in file order) plus the truncation flag,
    so ``instances`` is ``[]``, never absent, for a deck with no devices.
    """
    entries = parse_instance_cards(data.decode("utf-8", errors="replace"))
    truncated = len(entries) > cap
    return {"instances": entries[:cap], "instances_truncated": truncated}


def parse_instance_cards(text: str) -> list[dict[str, Any]]:
    """Parse ``text`` (a SPICE netlist) into one entry per device instance.

    ``+`` continuation lines are folded onto their entry via the shared
    :func:`klayout_tools.netlist_paths.fold_continuations` -- the same
    primitive ``--check``'s layout normalization uses -- so a parameter
    emitted on a continuation line lands in its instance's ``params``. What
    remains after folding is an instance card unless it starts with ``+``
    (an orphan continuation with no entry to fold onto), ``*`` (a comment,
    which the fold leaves byte-intact) or ``.`` (a SPICE control card:
    ``.subckt``, ``.param``, ``.include``, ...).
    """
    folded = fold_continuations(text.encode("utf-8", errors="replace"))
    entries: list[dict[str, Any]] = []
    for raw in folded.split(b"\n"):
        card = raw.decode("utf-8", errors="replace").strip()
        if not card or card.startswith(("*", "+", ".")):
            continue
        entry = _parse_card(card)
        if entry is not None:
            entries.append(entry)
    return entries


def _parse_card(card: str) -> dict[str, Any] | None:
    """One instance card -> its payload entry; ``None`` for a non-instance.

    ``name`` is the first token; ``model`` is the token immediately before
    the first ``key=value`` token (SPICE's positional prefix -- nodes -- ends
    where named parameters begin, which is how xschem's ``format=`` output
    shapes every device card); ``params`` is every ``key=value`` on the
    folded card, verbatim strings, last one winning on a duplicate key. A
    card with no ``key=value`` token (a bare resistor, a source) yields an
    empty ``params`` and a ``null`` ``model`` -- it is still an instance.
    """
    tokens = card.split()
    name = tokens[0]
    if not name[0].isalpha():
        return None
    params, model = _split_params(tokens[1:])
    return {
        "name": name,
        "card": card,
        "model": model,
        "params": params,
        "multiplier": {
            "m": params.get("m"),
            "mult": params.get("mult"),
            "agree": _agree(params.get("m"), params.get("mult")),
        },
    }


def _split_params(tokens: list[str]) -> tuple[dict[str, str], str | None]:
    """Split a card's positional prefix from its ``key=value`` tail.

    Returns ``(params, model)``: ``params`` maps every token from the first
    one containing ``=`` (tokens after it without ``=`` are skipped -- they
    are positional stragglers, not values of a named parameter); ``model`` is
    the token immediately before that first one, or ``None`` when the whole
    card is positional.
    """
    for index, token in enumerate(tokens):
        if "=" not in token:
            continue
        params: dict[str, str] = {}
        for tail in tokens[index:]:
            key, sep, value = tail.partition("=")
            if sep and key:
                params[key] = value
        return params, tokens[index - 1] if index >= 1 else None
    return {}, None


def _agree(m: str | None, mult: str | None) -> bool | None:
    """Whether the two multiplier forms agree, tri-state.

    ``True`` when both are present and numerically equal, ``False`` when both
    are present and differ, ``None`` when fewer than two are present **or**
    either value is not numeric (an ``expr(...)``, a bare word, a
    SPICE-suffixed ``6u``) -- a consumer must be able to tell "disagree" from
    "not applicable", and a non-numeric value is the latter, never a crash.
    """
    if m is None or mult is None:
        return None
    m_value, mult_value = _numeric(m), _numeric(mult)
    if m_value is None or mult_value is None:
        return None
    return m_value == mult_value


def _numeric(value: str) -> float | None:
    """Parse a plain numeric value; ``None`` when it is not one.

    ``float`` decides: ``6``, ``6.0``, ``1e3`` parse; anything else is
    ``None``. A SPICE suffix is a scale factor, not notation -- ``6u`` is
    ``6e-6`` -- so stripping the suffix to compare mantissas would call
    ``mult=6 m=6m`` agreed while the drive is wrong by 1000x, the same
    unit-suffixed-literal failure ``pdk_models.py`` documents from the
    ``.option scale=1.0u`` incident (#1396). A suffixed literal therefore
    takes the same ``None`` path as an ``expr(...)``: ``agree`` reports
    "not applicable" rather than guessing a scale it cannot verify (proper
    suffix arithmetic is more machinery than this purely-local card view
    needs). Anything else non-numeric (``expr(...)``, a bare node name) is
    ``None`` too.
    """
    try:
        return float(value)
    except ValueError:
        return None
