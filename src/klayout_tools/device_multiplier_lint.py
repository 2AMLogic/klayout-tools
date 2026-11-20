"""Device-multiplier lint (issue #5779): does a device instance's vendor
``mult`` parameter agree with the SPICE ``m=`` drive multiplier?

Some subcircuits declare ``mult`` without forwarding it to the primitive's
``m=``. On these instances SPICE's ``m=`` supplies drive. What ``mult``
does in a Monte Carlo run depends on the model. For example, sky130's
``nfet_01v8`` uses it in the mismatch sigma denominator ``sqrt(l*w*mult)``.
The lint checks the pair on declaring, non-forwarding subcircuits and
recommends keeping both forms equal.

Pure text-in/findings-out: :func:`lint_instances` takes folded instance
cards (bare, or tagged with their closure-file label -- issue #6684) plus
``{name: subckt text}`` and returns findings; no I/O of its own
(:func:`sim_environment_report.device_multiplier_environment` does the
reading and reuses the shared ``sim_staging`` include-closure resolver).
Detection is text-driven -- no hard-coded PDK subckt-name list.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from ._spice_number import parse as parse_spice_number
from ._spice_text import circuit_lines, logical_lines
from ._spice_tokens import split_card_assignments, split_spice_tokens

#: Finding ``kind``: ``mult=N`` (N>1) with no ``m=`` -- the device
#: simulates 1x drive.
DRIVE_1X = "drive_1x"

#: Finding ``kind``: ``m=`` present but unequal to ``mult`` (or ``mult``
#: absent) on a declaring-not-forwarding subcircuit.
DRIVE_MISMATCH_DISAGREEMENT = "drive_mismatch_disagreement"

#: A value referencing ``mult`` inside a body card's ``m=`` parameter --
#: the only spelling that counts as forwarding it to drive.
_MULT_REF_RE = re.compile(r"\bmult\b", re.IGNORECASE)

#: Instances listed by name in the unverifiable note before it falls back
#: to a count.
_MAX_LISTED = 5

#: The ``.subckt`` names enclosing a card, lower-cased, outermost first;
#: ``()`` is the deck's top level (issue #6684). A nested ``.subckt`` is
#: private to its parent, so this is what a card needs to resolve the
#: callee its own enclosing subcircuits make visible.
Scope = tuple[str, ...]

#: An instance card optionally paired with the repo-relative label of the
#: closure file it was read from (issue #6684), and optionally its
#: :data:`Scope`: ``(card, label)`` or ``(card, label, scope)``. A bare
#: ``str`` card -- or a ``None`` label -- is a card of the top-level
#: netlist, whose findings keep the pre-#6684 shape (no ``file`` key) and
#: whose uncheckable pairs keep their ``note``/``unevaluated``
#: disclosures; those disclosures are deliberately scoped to the netlist
#: the caller wrote, so a library file's parametric internals (an
#: ``X… mult={mult}`` wrapper inside a PDK ``.subckt`` body) cannot add
#: noise to a run that merely includes it. A card without a scope resolves
#: its callee in the flat first-definition-wins view; one with a scope
#: resolves it the way the simulator does (:meth:`SubcktTable.resolve`).
SourcedCard = tuple[str, str | None] | tuple[str, str | None, Scope]


def instance_cards(text: str) -> list[str]:
    """Subcircuit-instance cards of one netlist text: logical cards
    (comments stripped, ``+`` continuations folded) whose first token
    starts with ``X``, outside control blocks."""
    return [
        line for line in circuit_lines(logical_lines(text)) if line[0].lower() == "x"
    ]


# Only calls between members of this foundry primitive library are exempt.
_SKY130_PRIMITIVES = "sky130_fd_pr__"


class SubcktTable(dict[str, str]):
    """:func:`collect_subckts`' ``{lower-cased name: subckt text}`` view,
    which also remembers the scope each definition was written in.

    The flat mapping keeps its first-definition-wins contract for callers
    that only know a name. :attr:`scoped` and :meth:`resolve` serve a card
    that knows where it sits: two files may each define a private ``leaf``
    inside their own DUT, and the call inside each must reach its own.
    """

    def __init__(self) -> None:
        super().__init__()
        #: ``{(enclosing scope, name): text}``; first definition wins.
        self.scoped: dict[tuple[Scope, str], str] = {}

    def resolve(self, name: str, scope: Scope | None) -> str | None:
        """The subcircuit text a card in ``scope`` reaches for ``name``.

        ``None`` scope is the flat view. Otherwise the search runs from the
        card's innermost enclosing subcircuit out to the top level, as the
        simulator does: a nested definition shadows a global one inside its
        parent, and is not visible outside it (``None`` when nothing is).
        """
        if scope is None:
            return self.get(name)
        for depth in range(len(scope), -1, -1):
            text = self.scoped.get((scope[:depth], name))
            if text is not None:
                return text
        return None


# Each open definition holds its header and its own body cards. Nested
# definitions close into the table separately.
_OpenSubckts = list[tuple[str, list[str]]]


def _collect_line(
    word: str, line: str, stack: _OpenSubckts, table: SubcktTable
) -> None:
    if word == ".subckt":
        tokens = line.split(None, 2)
        if len(tokens) >= 2:
            stack.append((tokens[1].lower(), [line]))
    elif word == ".ends":
        if stack:
            name, lines = stack.pop()
            definition = "\n".join(lines)
            table.setdefault(name, definition)
            table.scoped.setdefault(
                (tuple(parent for parent, _ in stack), name), definition
            )
    elif stack:
        stack[-1][1].append(line)


def collect_subckts(texts: Iterable[str]) -> SubcktTable:
    """Definitions in independent texts, with a fresh outer scope for each."""
    table = SubcktTable()
    for text in texts:
        stack: _OpenSubckts = []
        for line in circuit_lines(logical_lines(text)):
            _collect_line(line.split(None, 1)[0].lower(), line, stack, table)
    return table


def scan_closure(
    netlist: str, sources: dict[str, tuple[str, str | None]]
) -> tuple[list[tuple[str, str | None, Scope]], SubcktTable]:
    """Read a staged deck in include-site order using one subcircuit stack.

    Keys are the unique names assigned by staging. Each value is the staged
    text and its physical source label. Only includes already in this map
    contribute cards or definitions. Report cards in the original file order.
    """
    table = SubcktTable()
    stack: _OpenSubckts = []
    by_file: dict[str, list[tuple[str, str | None, Scope]]] = {k: [] for k in sources}
    seen: set[tuple[str, Scope]] = set()
    active: set[str] = set()

    def visit(name: str) -> None:
        site = (name, tuple(parent for parent, _ in stack))
        if name not in sources or name in active or site in seen:
            return
        seen.add(site)
        active.add(name)
        text, label = sources[name]
        for line in circuit_lines(logical_lines(text)):
            parts = line.split(None, 1)
            word = parts[0].lower()
            if word in (".include", ".inc") and len(parts) == 2:
                # Staging rewrites local targets to quoted, flat filenames.
                visit(parts[1].strip('"'))
            else:
                _collect_line(word, line, stack, table)
                if word.startswith("x"):
                    scope = tuple(parent for parent, _ in stack)
                    by_file[name].append((line, label, scope))
        active.remove(name)

    visit(netlist)
    return [card for cards in by_file.values() for card in cards], table


def lint_instances(
    instance_cards: Iterable[str | SourcedCard], subckts: dict[str, str]
) -> list[dict[str, Any]]:
    """Findings for instance cards of subcircuits that declare ``mult``
    but never forward it to drive (the acceptance-criteria pure function:
    cards in, findings out, no I/O). Cards may be bare strings (the
    top-level netlist's) or ``(card, file_label)`` pairs from the scanned
    include closure (issue #6684); a closure card's finding carries the
    additive ``file`` field. A ``(card, file_label, scope)`` entry, as
    :func:`scan_closure` yields, also resolves its callee from the
    card's enclosing subcircuits outward."""
    return _lint(instance_cards, subckts)[0]


def instance_report(
    instance_cards: Iterable[str | SourcedCard], subckts: dict[str, str]
) -> dict[str, Any]:
    """:func:`lint_instances` findings plus the non-finding disclosures:
    a ``note`` when instances could not be checked (their subcircuit text
    is absent from the scanned closure) and ``unevaluated`` entries when a
    ``m=``/``mult=`` value is not a plain number. Both disclosures cover
    the top-level netlist's own cards only (issue #6684): closure-file
    cards yield findings, not library-internal chatter. ``findings`` is
    always present."""
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
    instance_cards: Iterable[str | SourcedCard], subckts: dict[str, str]
) -> tuple[list[dict[str, Any]], list[dict[str, str]], list[dict[str, str]]]:
    """``(findings, unverified, unevaluated)`` behind both public entries."""
    findings: list[dict[str, Any]] = []
    unverified: list[dict[str, str]] = []
    unevaluated: list[dict[str, str]] = []
    for entry in instance_cards:
        card, source, scope = _unpack(entry)
        _lint_card(card, source, scope, subckts, findings, unverified, unevaluated)
    return findings, unverified, unevaluated


def _unpack(entry: str | SourcedCard) -> tuple[str, str | None, Scope | None]:
    """``(card, closure-file label, scope)`` of one entry; ``None`` for a
    part the entry does not carry."""
    if isinstance(entry, str):
        return entry, None, None
    if len(entry) == 3:
        return entry
    return entry[0], entry[1], None


def _lint_card(
    card: str,
    source: str | None,
    scope: Scope | None,
    subckts: dict[str, str],
    findings: list[dict[str, Any]],
    unverified: list[dict[str, str]],
    unevaluated: list[dict[str, str]],
) -> None:
    """Fold one card's decision into the shared result lists: skip
    non-instance and parameterless cards, note a checkable instance whose
    subcircuit text is missing from the closure, else run the rule table
    (only for a subcircuit that declares ``mult`` and never forwards it).
    ``source`` is the card's closure-file label (``None`` for the
    top-level netlist): it reaches the finding as ``file``, and only a
    top-level card can land in ``unverified``. ``scope`` picks the
    callee the card's enclosing subcircuits make visible."""
    tokens = split_spice_tokens(card)
    if not tokens or tokens[0][0].lower() != "x":
        return
    params, target = _card_params(tokens)
    if "m" not in params and "mult" not in params:
        return
    name = (target or "").lower()
    # Vendored sky130 primitive models instantiate other members of the
    # foundry's primitive library (e.g. FET parasitics). Restrict this
    # exception to internal calls. A parameterized DUT calling the very
    # same primitive is still a user of the model.
    if (
        source is not None
        and scope
        and scope[-1].startswith(_SKY130_PRIMITIVES)
        and name.startswith(_SKY130_PRIMITIVES)
    ):
        return
    if scope is not None and isinstance(subckts, SubcktTable):
        subckt_text = subckts.resolve(name, scope)
    else:
        subckt_text = subckts.get(name)
    if subckt_text is None:
        if source is None and _pair_could_yield_finding(params):
            unverified.append({"instance": tokens[0], "subckt": target or ""})
        return
    header, _, body = subckt_text.partition("\n")
    header_mult = _header_mult_token(header)
    if header_mult is None or _body_forwards_mult(body):
        return
    _compare_pair(
        tokens[0], target or "", params, header_mult, findings, unevaluated, source
    )


def _pair_could_yield_finding(params: dict[str, str]) -> bool:
    """Could this ``m=``/``mult=`` pair yield a finding under the rule
    table, whatever the subcircuit does? Pairs that are clean no matter
    what -- no ``m=`` with ``mult=1``, or an equal ``m=N``/``mult=N``
    pair -- cannot, so an instance whose subcircuit text is missing is
    noted only when the pair is worth cross-checking. Without this gate
    the note would fire on every sky130 run whose models come through
    ``models.pdk`` rather than a netlist ``.include`` (the common case),
    naming instances that are unconditionally clean."""
    m_raw = params.get("m")
    mult_raw = params.get("mult")
    if m_raw is None:
        mult_value = _param_number(mult_raw)
        return mult_value is not None and mult_value > 1
    m_value = _param_number(m_raw)
    mult_value = _param_number(mult_raw)
    if m_value is None or (mult_raw is not None and mult_value is None):
        # An expression value compares to nothing, so no finding can fire.
        return False
    return m_value != (1.0 if mult_value is None else mult_value)


def _compare_pair(
    instance: str,
    subckt: str,
    params: dict[str, str],
    header_mult: str | None,
    findings: list[dict[str, Any]],
    unevaluated: list[dict[str, str]],
    source: str | None = None,
) -> None:
    """The rule table for one instance of a subcircuit that declares
    ``mult`` but never forwards it to drive: disclose unevaluated values,
    then flag a pair that disagrees. ``unevaluated`` is a top-level-only
    disclosure (``source is None``); findings fire for closure cards too,
    tagged with ``source`` as their ``file``. An omitted instance
    ``mult=`` takes the header's declared default (``header_mult``), so
    the comparison stays text-driven end to end; an explicit instance
    ``mult=`` always wins over the header."""
    m_raw = params.get("m")
    mult_raw = params.get("mult")
    m_value = _param_number(m_raw)
    mult_value = _param_number(mult_raw)
    if source is None:
        for name, raw, value in (("m", m_raw, m_value), ("mult", mult_raw, mult_value)):
            if raw is not None and value is None:
                unevaluated.append({"instance": instance, "param": name, "value": raw})
    comparable = (m_raw is None or m_value is not None) and (
        mult_raw is None or mult_value is not None
    )
    if not comparable:
        return
    if m_raw is None:
        if mult_value is not None and mult_value > 1:
            findings.append(
                _finding(
                    DRIVE_1X,
                    instance,
                    subckt,
                    m=None,
                    mult=mult_raw,
                    m_value=None,
                    mult_value=mult_value,
                    file=source,
                )
            )
        return
    if mult_raw is None:
        declared = _param_number(header_mult)
        effective = 1.0 if declared is None else declared
    elif mult_value is None:
        # Unreachable today: ``comparable`` above already returned the
        # nonnumeric-mult case. Restated so the checker sees the
        # invariant (a present ``mult=`` implies a numeric value).
        return
    else:
        effective = mult_value
    if m_value != effective:
        findings.append(
            _finding(
                DRIVE_MISMATCH_DISAGREEMENT,
                instance,
                subckt,
                m=m_raw,
                mult=mult_raw,
                m_value=m_value,
                mult_value=effective,
                file=source,
            )
        )


def _finding(
    kind: str,
    instance: str,
    subckt: str,
    *,
    m: str | None,
    mult: str | None,
    m_value: float | None,
    mult_value: float | None,
    file: str | None = None,
) -> dict[str, Any]:
    """One finding: the instance echo plus note text that names both
    forms' roles (never suggesting either is removable). ``file`` -- the
    scanned-closure label of the file the card came from -- is present
    only on a closure-file finding (issue #6684); a top-level netlist
    finding keeps the exact pre-#6684 shape."""
    m_text = _number_text(m, m_value)
    mult_text = _number_text(mult, mult_value)
    if kind == DRIVE_1X:
        drive = (
            f"'{subckt}' declares mult but does not forward mult={mult_text} "
            "to device drive. With no m= on this instance, the device simulates 1x."
        )
    else:
        pair = "mult absent" if mult is None else f"mult={mult_text}"
        drive = (
            f"The multiplier settings on '{subckt}' disagree (m={m_text}, {pair}). "
            f"The instance simulates {m_text}x drive."
        )
    note = (
        f"{drive} Depending on the model, mult can also scale Monte Carlo mismatch. "
        "Keep m=N and mult=N for an N-times device."
    )
    finding: dict[str, Any] = {
        "instance": instance,
        "subckt": subckt,
        "kind": kind,
        "m": m,
        "mult": mult,
        "note": note,
    }
    if file is not None:
        finding["file"] = file
    return finding


def _number_text(raw: str | None, value: float | None) -> str:
    """Report spelling of a multiplier: the parsed value when it parsed,
    else the raw token (an unevaluated value never reaches a finding)."""
    if value is not None:
        return f"{value:g}"
    return raw or ""


def _header_mult_token(header: str) -> str | None:
    """The ``mult`` token declared in the ``.subckt`` header -- the value an
    omitted instance ``mult=`` resolves to. ``None`` when the header does
    not declare the parameter. Uses the shared tokenizer (not a bare
    ``split()``) so the spaced ``mult = 1`` spelling reads the same as
    ``mult=1``."""
    params, _ = _card_params(split_spice_tokens(header))
    return params.get("mult")


def _body_forwards_mult(body: str) -> bool:
    """Does any body card's ``m=`` reference ``mult``? That -- and only
    that -- is forwarding it to drive; ``mult`` appearing in a mismatch
    expression (``sqrt(l*w*mult)``) is not."""
    for line in body.splitlines():
        first = line.split(None, 1)[0]
        if first.startswith("."):
            continue
        params, _ = _card_params(split_spice_tokens(line))
        if _MULT_REF_RE.search(params.get("m", "")):
            return True
    return False


def _card_params(tokens: list[str]) -> tuple[dict[str, str], str | None]:
    """Case-insensitive SPICE parameters and the preceding model name."""
    assignments, model = split_card_assignments(tokens[1:])
    return {name.lower(): value for name, value in assignments}, model


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
