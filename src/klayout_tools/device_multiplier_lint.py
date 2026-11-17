"""Device-multiplier lint (issue #5779): does a device instance's vendor
``mult`` parameter agree with the SPICE ``m=`` drive multiplier?

sky130's device subcircuits **declare** a vendor ``mult`` parameter and
never forward it to the inner primitive as a device multiplier, so the
drive multiplier comes only from SPICE's own ``m=`` on the instance. The
two forms are a non-redundant pair: ``m=`` supplies drive, ``mult``
de-rates mismatch (it appears in the subckt's mismatch sigma denominator,
e.g. ``AGAUSS(0,1.0,1)/sqrt(l*w*mult)``), so an instance carrying
``mult=6`` and no ``m=`` simulates a **1x** device, and ``m=N`` with
``mult`` absent or unequal means drive and mismatch de-rating disagree.

The lint checks that the **pair agrees** on instances of subcircuits that
declare ``mult`` in their ``.subckt`` header but never forward it to drive
(no ``m=`` card in the body referencing ``mult``). It never suggests
dropping either form: ``mult`` is live in a mismatch Monte Carlo run even
though it is inert nominally.

Pure text-in/findings-out: :func:`lint_instances` takes folded instance
cards plus ``{name: subckt text}`` and returns findings; no I/O of its
own (``sim_environment_report.device_multiplier_environment`` does the
reading and reuses the shared ``sim_staging`` include-closure resolver).
Detection is text-driven -- no hard-coded PDK subckt-name list.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from ._spice_number import parse as parse_spice_number
from ._spice_text import logical_lines
from ._spice_tokens import split_spice_tokens

#: Finding ``kind``: ``mult=N`` (N>1) with no ``m=`` -- the device
#: simulates 1x drive.
DRIVE_1X = "drive_1x"

#: Finding ``kind``: ``m=`` present but unequal to ``mult`` (or ``mult``
#: absent) on a declaring-not-forwarding subcircuit -- drive and mismatch
#: de-rating disagree.
DRIVE_MISMATCH_DISAGREEMENT = "drive_mismatch_disagreement"

#: A value referencing ``mult`` inside a body card's ``m=`` parameter --
#: the only spelling that counts as forwarding it to drive.
_MULT_REF_RE = re.compile(r"\bmult\b", re.IGNORECASE)

#: Instances listed by name in the unverifiable note before it falls back
#: to a count.
_MAX_LISTED = 5


def instance_cards(text: str) -> list[str]:
    """Subcircuit-instance cards of one netlist text: logical cards
    (comments stripped, ``+`` continuations folded) whose first token
    starts with ``X``."""
    return [line for line in logical_lines(text) if line[0].lower() == "x"]


def collect_subckts(texts: Iterable[str]) -> dict[str, str]:
    """``{lower-cased name: subckt text}`` across netlist/model texts.

    A subckt's text is its ``.subckt`` header line (parameters included)
    plus its own body cards: nested ``.subckt`` bodies are excluded (they
    are collected as their own entries -- an inner ``m={mult}`` forwards
    the inner subckt's parameter, not the outer's). First definition wins
    when a name repeats across the closure.
    """
    collected: dict[str, str] = {}
    for text in texts:
        stack: list[tuple[str, str, list[str]]] = []
        for line in logical_lines(text):
            word = line.split(None, 1)[0].lower()
            if word == ".subckt":
                tokens = line.split()
                if len(tokens) < 2:
                    continue
                stack.append((tokens[1].lower(), line, []))
            elif word == ".ends":
                if not stack:
                    continue
                name, header, body = stack.pop()
                collected.setdefault(name, "\n".join([header, *body]))
            elif stack:
                stack[-1][2].append(line)
    return collected


def lint_instances(
    instance_cards: list[str], subckts: dict[str, str]
) -> list[dict[str, Any]]:
    """Findings for instance cards of subcircuits that declare ``mult``
    but never forward it to drive (the acceptance-criteria pure function:
    cards in, findings out, no I/O)."""
    return _lint(instance_cards, subckts)[0]


def instance_report(
    instance_cards: list[str], subckts: dict[str, str]
) -> dict[str, Any]:
    """:func:`lint_instances` findings plus the non-finding disclosures:
    a ``note`` when instances could not be checked (their subcircuit text
    is absent from the scanned closure) and ``unevaluated`` entries when a
    ``m=``/``mult=`` value is not a plain number. ``findings`` is always
    present."""
    findings, unverified, unevaluated = _lint(instance_cards, subckts)
    report: dict[str, Any] = {"findings": findings}
    if unverified:
        listed = [f"{e['instance']} ({e['subckt']})" for e in unverified[:_MAX_LISTED]]
        if len(unverified) > _MAX_LISTED:
            listed.append(f"+{len(unverified) - _MAX_LISTED} more")
        report["note"] = (
            f"{len(unverified)} instance(s) pass m=/mult= to a subcircuit whose "
            "text is not in the scanned include closure (models supplied "
            f"outside it): {', '.join(listed)}. Their vendor-mult/m= pair was "
            "not cross-checked."
        )
    if unevaluated:
        report["unevaluated"] = unevaluated
    return report


def _lint(
    instance_cards: list[str], subckts: dict[str, str]
) -> tuple[list[dict[str, Any]], list[dict[str, str]], list[dict[str, str]]]:
    """``(findings, unverified, unevaluated)`` behind both public entries."""
    findings: list[dict[str, Any]] = []
    unverified: list[dict[str, str]] = []
    unevaluated: list[dict[str, str]] = []
    for card in instance_cards:
        tokens = split_spice_tokens(card)
        if not tokens or tokens[0][0].lower() != "x":
            continue
        params = _card_params(tokens)
        if "m" not in params and "mult" not in params:
            continue
        target = _target_subckt(tokens)
        subckt_text = subckts.get((target or "").lower())
        if subckt_text is None:
            unverified.append({"instance": tokens[0], "subckt": target or ""})
            continue
        header, _, body = subckt_text.partition("\n")
        if not _header_declares_mult(header) or _body_forwards_mult(body):
            continue
        m_raw = params.get("m")
        mult_raw = params.get("mult")
        m_value = _param_number(m_raw)
        mult_value = _param_number(mult_raw)
        for name, raw, value in (("m", m_raw, m_value), ("mult", mult_raw, mult_value)):
            if raw is not None and value is None:
                unevaluated.append({"instance": tokens[0], "param": name, "value": raw})
        comparable = (m_raw is None or m_value is not None) and (
            mult_raw is None or mult_value is not None
        )
        if not comparable:
            continue
        if m_raw is None:
            if mult_value is not None and mult_value > 1:
                findings.append(
                    _finding(
                        DRIVE_1X,
                        tokens[0],
                        target or "",
                        m=None,
                        mult=mult_raw,
                        m_value=None,
                        mult_value=mult_value,
                    )
                )
            continue
        effective = 1.0 if mult_value is None else mult_value
        if m_value != effective:
            findings.append(
                _finding(
                    DRIVE_MISMATCH_DISAGREEMENT,
                    tokens[0],
                    target or "",
                    m=m_raw,
                    mult=mult_raw,
                    m_value=m_value,
                    mult_value=effective,
                )
            )
    return findings, unverified, unevaluated


def _finding(
    kind: str,
    instance: str,
    subckt: str,
    *,
    m: str | None,
    mult: str | None,
    m_value: float | None,
    mult_value: float | None,
) -> dict[str, Any]:
    """One finding: the instance echo plus note text that names both
    forms' roles (never suggesting either is removable)."""
    m_text = _number_text(m, m_value)
    mult_text = _number_text(mult, mult_value)
    if kind == DRIVE_1X:
        note = (
            f"the vendor mult={mult_text} passed to '{subckt}' multiplies "
            "nothing on this instance's drive: the subcircuit declares mult "
            "but never forwards it to the device, and the instance carries "
            "no m=, so the device simulates 1x. mult still de-rates mismatch "
            "in a Monte Carlo run (the subcircuit's mismatch expressions "
            "read it), so carry the drive on m= and keep the pair equal "
            "(m=N with mult=N for an N-times device)."
        )
    else:
        if mult is None:
            pair = (
                "vendor mult absent (the subcircuit's mismatch expressions "
                "default it to 1)"
            )
        else:
            pair = f"vendor mult={mult_text}"
        note = (
            f"drive m={m_text} and {pair} disagree on '{subckt}': the "
            "subcircuit de-rates mismatch by mult (it is never forwarded to "
            f"drive), so this instance simulates {m_text}x drive with a "
            f"{mult_text}x device's mismatch de-rating. Keep the pair equal "
            "(m=N with mult=N for an N-times device)."
        )
    return {
        "instance": instance,
        "subckt": subckt,
        "kind": kind,
        "m": m,
        "mult": mult,
        "note": note,
    }


def _number_text(raw: str | None, value: float | None) -> str:
    """Report spelling of a multiplier: the parsed value when it parsed,
    else the raw token (an unevaluated value never reaches a finding)."""
    if value is not None:
        return f"{value:g}"
    return raw or ""


def _card_params(tokens: list[str]) -> dict[str, str]:
    """``{lower-cased name: raw value}`` of a card's ``name=value``
    tokens. One token may be a brace-wrapped pair list (``{w=1 mult=6}``);
    values keep their own braces (``m={mult * 2}`` stays whole)."""
    params: dict[str, str] = {}
    for token in tokens[1:]:
        _absorb_param(token, params)
    return params


def _absorb_param(token: str, params: dict[str, str]) -> None:
    body = token
    while len(body) >= 2 and body[0] == "{" and body[-1] == "}":
        body = body[1:-1].strip()
        if " " in body and "=" in body:
            for part in body.split():
                _absorb_param(part, params)
            return
    if "=" not in body:
        return
    name, _, value = body.partition("=")
    name = name.strip().lower()
    if name:
        params.setdefault(name, value.strip())


def _target_subckt(tokens: list[str]) -> str | None:
    """The subcircuit an X-card instantiates: its last positional token
    (every ``name=value`` token is a parameter)."""
    positional = [token for token in tokens[1:] if "=" not in token]
    return positional[-1] if positional else None


def _header_declares_mult(header: str) -> bool:
    """Does the ``.subckt`` header declare a ``mult`` parameter?"""
    return "mult" in _card_params(header.split())


def _body_forwards_mult(body: str) -> bool:
    """Does any body card's ``m=`` reference ``mult``? That -- and only
    that -- is forwarding it to drive; ``mult`` appearing in a mismatch
    expression (``sqrt(l*w*mult)``) is not."""
    for line in body.splitlines():
        first = line.split(None, 1)[0]
        if first.startswith("."):
            continue
        for name, value in _card_params(split_spice_tokens(line)).items():
            if name == "m" and _MULT_REF_RE.search(value):
                return True
    return False


def _param_number(raw: str | None) -> float | None:
    """Numeric value of a ``m=``/``mult=`` token: outer braces/quotes
    stripped, then the shared SPICE-number parser (``None`` when the value
    is an expression or parameter reference -- nothing to compare)."""
    if raw is None:
        return None
    value = raw.strip()
    while len(value) >= 2 and (
        (value[0] == "{" and value[-1] == "}")
        or (value[0] == "(" and value[-1] == ")")
        or (value[0] == value[-1] and value[0] in "'\"")
    ):
        value = value[1:-1].strip()
    return parse_spice_number(value)
