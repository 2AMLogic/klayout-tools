"""One-sided ``input_voltage`` / ``output_voltage`` groups in a merge (issue #7809).

``input_voltage(NAME)`` and ``output_voltage(NAME)`` are optional library-level
groups. A public release can split one corner over several cell-group files
where only some of them declare the groups (a later re-release of one group
drops them), and ``liberty_merge`` would otherwise refuse the set because the
header field is "present in A, missing in B".

The rule, per kind (``input_voltage`` / ``output_voltage``):

* a kind that **no member declares** is nothing to reconcile;
* a kind that **some members declare and the others lack entirely** is unioned
  by name: the merged header carries the groups once, from the first member
  that declares them (appended after the first header when the first member
  lacks them), and the lacking members are not compared against them;
* members that **each declare** the kind are compared like any other header
  field, so a same-named group with a different body, a different group name,
  or a missing sibling group still refuses;
* a member that lacks the kind must not reference a unioned group's name
  (``input_voltage : NAME;`` in its cells): its text would be valid only by
  accident of the union, so the merge refuses instead of guessing.

Everything here returns a refusal message and leaves raising to
``liberty_merge`` (as ``liberty_header_diff`` does), so no import cycle forms.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .liberty_header_diff import header_mismatch
from .liberty_merge_syntax import _depth1_groups, _find_attr_reference

#: The optional library-level groups a merge may union.
KINDS = ("input_voltage", "output_voltage")


@dataclass
class _Declared:
    """One member's groups of one kind."""

    #: Field-map slice: ``kind(NAME)`` keys (plus repeated-group occurrence keys).
    fields: dict[str, str]
    #: ``(unquoted NAME, raw group text)`` per declaration, in source order.
    groups: list[tuple[str, str]]


@dataclass
class VoltageUnion:
    """Reconciles the voltage groups of every merged member, in listed order."""

    #: Merged-header text to append after the first member's header.
    extra: list[str] = field(default_factory=list)
    _first: dict[str, _Declared] = field(default_factory=dict)
    _ref: dict[str, tuple[str, _Declared]] = field(default_factory=dict)
    _members: list[tuple[str, str, set[str]]] = field(default_factory=list)

    def first(self, path: str, header: str, fields: dict[str, str], text: str) -> None:
        """Record the first member, whose header is the merged header."""
        self._first = _declared(header, fields)
        self._ref = {kind: (path, found) for kind, found in self._first.items()}
        self._members.append((path, text, set(self._first)))

    def reconcile(
        self,
        first_path: str,
        first: dict[str, str],
        path: str,
        other: dict[str, str],
        header: str,
        text: str,
    ) -> tuple[dict[str, str], dict[str, str], str | None]:
        """``(first, other, refusal)``: the two field maps without the groups
        that are unioned rather than compared, or a refusal message."""
        mine = _declared(header, other)
        self._members.append((path, text, set(mine)))
        drop_first: set[str] = set()
        drop_other: set[str] = set()
        for kind in KINDS:
            theirs = mine.get(kind)
            if theirs is None:
                if kind in self._first:
                    drop_first.update(self._first[kind].fields)
                continue
            if kind in self._first:
                continue  # both declare: the ordinary field comparison decides
            known = self._ref.get(kind)
            if known is None:
                self._ref[kind] = (path, theirs)
                self.extra.extend(raw for _, raw in theirs.groups)
            elif clash := header_mismatch(
                known[0], known[1].fields, "", path, theirs.fields, ""
            ):
                return first, other, clash
            drop_other.update(theirs.fields)
        return _without(first, drop_first), _without(other, drop_other), None

    def dangling(self) -> str | None:
        """A refusal when a member lacking a unioned group references its name."""
        for kind, (ref_path, found) in self._ref.items():
            names = {name for name, _ in found.groups}
            for path, text, declared in self._members:
                if kind in declared or kind not in text:
                    continue
                if name := _find_attr_reference(text, {kind}, names):
                    return (
                        f"'{path}' references {kind} '{name}' but declares no such "
                        f"group, and the merged header would take it from "
                        f"'{ref_path}' (cannot merge cell groups)"
                    )
        return None


def _without(fields: dict[str, str], drop: set[str]) -> dict[str, str]:
    return {key: value for key, value in fields.items() if key not in drop}


def _declared(header: str, fields: dict[str, str]) -> dict[str, _Declared]:
    """The kinds ``header`` declares as groups, with their fields and raw text.

    Two independent scans must agree on how many groups a kind has (the field
    map also holds complex attributes such as ``input_voltage (x);``); a kind
    where they disagree is left out, so the ordinary comparison refuses it."""
    raw: dict[str, list[tuple[str, str]]] = {}
    for kind, name, start, stop in _depth1_groups(header, KINDS):
        raw.setdefault(kind, []).append((name, header[start:stop]))
    found = {}
    for kind, groups in raw.items():
        sliced = {k: v for k, v in fields.items() if k.startswith(f"{kind}(")}
        if len(sliced) == len(groups):
            found[kind] = _Declared(sliced, groups)
    return found
