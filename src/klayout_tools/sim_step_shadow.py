"""Name refusals for one `analysis_steps[]` step's measurements.

A step measurement's `name` is not just a response key: `klt sim` emits it
as an *ngspice vector name* in that step's plot (`sim_steps`'s
`_flatten_step_measurement` binds the bare local name before copying it to
`<step>__<local>`). Three distinct collisions follow from that, and all
three are refused at request resolution -- before any deck is written,
hence identically on every backend:

- a name that is not a legal vector name at all;
- a name declared twice in one step (the second `let` silently wins);
- a name that an *earlier* sibling already bound, read back by a later card
  as `v(<name>)` (issue #5446) -- the subtle one, documented on
  :func:`refuse_sibling_vector_shadow`.

They live here rather than inline in `sim_steps._resolve_step_measurements`
so that one function stays a flattening loop and the naming contract has a
single home.
"""

from __future__ import annotations

import re
from typing import Any

from .sim_errors import SimError

#: A legal step-measurement name: it is referenced as ``<step>.<name>`` and
#: becomes an ngspice vector name, so it is an identifier, not an expression.
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: A voltage-family signal accessor and its argument list: ``v(a)``,
#: ``v(a,b)``, ``vdb(out)``, ``vm(x1.n)``. Deliberately only the voltage
#: half of ``sim_steps._TOKEN_RE``'s ``signal`` group -- an ``i(<src>)`` read
#: resolves to the vector ``<src>#branch``, which a measurement name
#: (constrained to :data:`_NAME_RE`) can never collide with.
_VOLTAGE_READ_RE = re.compile(r"\bv[a-z]{0,2}\s*\(([^()]*)\)", re.IGNORECASE)


def validate_step_measurement_name(
    spec: dict[str, Any],
    local: Any,
    earlier: dict[str, str],
    step: dict[str, Any],
) -> None:
    """Refuse every name collision a step measurement can cause.

    ``earlier`` maps the lower-cased name of each measurement already
    resolved in this step to the name as written, and is *not* updated here
    -- the caller adds the current name after this returns, so each check
    sees only genuinely earlier siblings.
    """
    where = f"step {step['name']!r}"
    if not isinstance(local, str) or not _NAME_RE.match(local):
        raise SimError(
            f"{where}: measurement name {local!r} must match "
            "[A-Za-z_][A-Za-z0-9_]* (it is referenced as "
            f"'{step['name']}.<name>' and becomes an ngspice vector name)"
        )
    if local.lower() in earlier:
        raise SimError(
            f"{where}: measurement name {local!r} is declared more than once "
            "(compared case-insensitively)"
        )
    refuse_sibling_vector_shadow(spec, local, earlier, step)


def voltage_read_nodes(spec: dict[str, Any]) -> set[str]:
    """The lower-cased node names a measurement reads through a
    voltage-family accessor, across both of a step measurement's forms
    (``spice`` card and ``expr``).

    Read off the request text, before ``sim_steps._substitute_refs`` runs --
    a ``v(...)`` argument is passed through to the deck untouched, so what is
    written here is exactly the vector name ngspice will look up.
    """
    nodes: set[str] = set()
    for field in ("spice", "expr"):
        text = spec.get(field)
        if not isinstance(text, str):
            continue
        for match in _VOLTAGE_READ_RE.finditer(text):
            for argument in match.group(1).split(","):
                argument = argument.strip().lower()
                if argument:
                    nodes.add(argument)
    return nodes


def refuse_sibling_vector_shadow(
    spec: dict[str, Any],
    local: str,
    earlier: dict[str, str],
    step: dict[str, Any],
) -> None:
    """Issue #5446: refuse a step whose measurement reads ``v(<N>)`` where
    ``<N>`` names an *earlier* measurement in the same step.

    ngspice writes a measurement's result into the step's plot as a vector
    named after the measurement, so a measurement named after a deck node
    replaces that node's waveform with a length-1 scalar. Every later card
    in the step that reads the node then fails with ``find(AT) : out of
    interval`` -- reported against the *later* card, which is correct,
    naming neither the earlier measurement nor the node. An ``expr`` is
    worse: it reads the one-point scalar and can report a wrong value with
    ``status: pass``.

    Only earlier siblings are compared. A card reading ``v(a)`` while named
    ``a`` itself reads the node before it binds, and a *later* sibling named
    after a node an *earlier* card read is likewise harmless -- so this
    refuses only requests that are already broken.
    """
    if not earlier:
        return
    for node in sorted(voltage_read_nodes(spec)):
        shadowed = earlier.get(node)
        if shadowed is None:
            continue
        raise SimError(
            f"step {step['name']!r}: measurement {local!r} reads v({node}), "
            f"but measurement {shadowed!r} earlier in the same step is named "
            f"after that node: ngspice stores a measurement's result in the "
            f"step's plot as a vector named after the measurement, so "
            f"{shadowed!r} replaces that node's waveform with a one-point "
            f"scalar and this card fails with 'out of interval' (or, for an "
            f"'expr', reports a wrong value). Rename measurement "
            f"{shadowed!r} to something that is not a node name."
        )
