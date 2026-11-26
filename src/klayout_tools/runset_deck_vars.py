"""Validation and normalisation of a runset engine's ``deck_vars``, shared by
the ``"klayout-runset"`` engines (issue #7952).

``klt lvs`` (``lvs_runset._parse_deck_vars``) and ``klt extract`` / ``klt pex``
(``extract_runset_flags.parse_deck_vars``) each carried its own copy of the
loop below and of the five ``-rd`` names the engine sets itself, while ``klt
pdk decks`` reads the LVS copy to hide those names from a runset's listed
globals -- so a change to the reserved names had to be made twice for deck
discovery and execution to stay in agreement.

:func:`normalize_deck_vars` is the one loop. Each engine passes its own error
class and diagnostic wording, so a caller still gets its verb's exception and
the text it already parses; the policy -- which names are reserved, which
values are scalars, how a scalar is stringified -- is decided here once.
"""

from __future__ import annotations

from typing import Any

#: ``-rd`` names the engine sets itself; ``deck_vars`` may not override them.
RESERVED_RUNSET_VARS = ("input", "schematic", "report", "top", "topcell")


def normalize_deck_vars(
    raw_vars: Any,
    *,
    error_cls: type[Exception],
    not_object: str,
    reserved: str,
    not_scalar: str,
) -> dict[str, str]:
    """Validate and normalise a ``{name: value}`` ``deck_vars`` mapping into
    ``{name: str}``; a falsey ``raw_vars`` means none were given.

    Every refusal raises ``error_cls``. ``not_object`` is used verbatim when
    ``raw_vars`` is neither falsey nor a dict. ``reserved`` is a
    :meth:`str.format` template for a name in :data:`RESERVED_RUNSET_VARS`,
    with ``{name}`` the offending variable and ``{reserved}`` the joined list.
    ``not_scalar`` is a template for a value that is ``None`` or a container,
    with ``{name}`` the offending variable. Any other value is stringified, a
    ``bool`` lower-cased so the runset's own ``bool_check?`` reads it.
    """
    raw_vars = raw_vars or {}
    if not isinstance(raw_vars, dict):
        raise error_cls(not_object)
    deck_vars: dict[str, str] = {}
    for name, value in raw_vars.items():
        if name in RESERVED_RUNSET_VARS:
            raise error_cls(
                reserved.format(name=name, reserved=", ".join(RESERVED_RUNSET_VARS))
            )
        if isinstance(value, (dict, list)) or value is None:
            raise error_cls(not_scalar.format(name=name))
        deck_vars[name] = str(value).lower() if isinstance(value, bool) else str(value)
    return deck_vars
