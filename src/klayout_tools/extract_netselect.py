"""Pattern / all-nets selectors for ``klt extract``'s ``--critical-net`` and
``--parasitics-net`` scoping (issue #2974).

Both flags take exact net names, which is awkward for a regular generated
array (``bl_0 .. bl_N``) where the caller would otherwise have to expand one
flag per net. The selectors here are resolved *against the extracted net
set* -- a glob cannot be expanded at flag-parse time because the nets do not
exist yet -- and the result is merged with any exact names, so everything
downstream (the coupling pass, ground-pass scoping, warnings) keeps working
on the same concrete, escaped-spelling ``frozenset[str]`` it always did.

Matching is shell-style (:func:`fnmatch.fnmatchcase`) against the escaped
spelling ``spice_safe_net_name(_net_identity_name(net))`` -- the spelling
``parasitics.nets[].net`` reports and the exact-name flags compare against.
Anonymous (``\\$<n>``) nets are never swept in implicitly: a glob matches one
only when the glob itself starts with ``\\$``, and ``--critical-net-all``
skips them entirely.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from typing import TYPE_CHECKING, Any

from .extract_parasitics import _net_identity_name, spice_safe_net_name

if TYPE_CHECKING:
    import klayout.db as kdb

    from .decks import ExtractionDeck


@dataclass
class NetSelectors:
    """Requested pattern selectors plus (after :meth:`resolve`) their result."""

    critical_patterns: tuple[str, ...] = ()
    critical_all: bool = False
    critical_excludes: tuple[str, ...] = ()
    parasitics_patterns: tuple[str, ...] = ()
    strict: bool = False
    error: Callable[[str], Exception] = ValueError
    # Filled by `resolve`.
    critical_nets: frozenset[str] | None = None
    parasitics_nets: frozenset[str] | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def has_critical(self) -> bool:
        return bool(self.critical_patterns) or self.critical_all

    def resolve(
        self,
        circuit: kdb.Circuit,
        deck: ExtractionDeck,
        critical_nets: frozenset[str] | None,
        parasitics_nets: frozenset[str] | None,
    ) -> tuple[frozenset[str] | None, frozenset[str] | None]:
        """Merge the exact names with the selector matches for ``circuit``."""
        named: list[str] = []
        anonymous: list[str] = []
        for net in circuit.each_net():
            name = net.expanded_name()
            if name == deck.substrate_net or name.startswith(
                f"{deck.substrate_net}_iso"
            ):
                continue
            if net.cluster_id == 0:
                continue
            escaped = spice_safe_net_name(_net_identity_name(net))
            (anonymous if name.startswith("$") else named).append(escaped)

        if self.has_critical:
            matched = self._select(
                "--critical-net-pattern",
                self.critical_patterns,
                named,
                anonymous,
                select_all=self.critical_all,
                excludes=self.critical_excludes,
            )
            critical_nets = frozenset(critical_nets or ()) | matched
        if self.parasitics_patterns:
            matched = self._select(
                "--parasitics-net-pattern",
                self.parasitics_patterns,
                named,
                anonymous,
            )
            parasitics_nets = frozenset(parasitics_nets or ()) | matched
        self.critical_nets = critical_nets
        self.parasitics_nets = parasitics_nets
        return critical_nets, parasitics_nets

    def _select(
        self,
        flag: str,
        patterns: Sequence[str],
        named: Iterable[str],
        anonymous: Iterable[str],
        *,
        select_all: bool = False,
        excludes: Sequence[str] = (),
    ) -> frozenset[str]:
        named = list(named)
        anonymous = list(anonymous)
        selected: set[str] = set()
        if select_all:
            selected.update(named)
        unmatched: list[str] = []
        for pattern in patterns:
            candidates = anonymous + named if pattern.startswith("\\$") else named
            hits = {name for name in candidates if fnmatchcase(name, pattern)}
            if not hits:
                unmatched.append(pattern)
            selected |= hits
        selected = {
            name
            for name in selected
            if not any(fnmatchcase(name, ex) for ex in excludes)
        }
        problems: list[str] = []
        if unmatched:
            problems.append(
                f"{flag} pattern(s) "
                f"{', '.join(repr(p) for p in unmatched)} match no net in this "
                "layout"
            )
        if select_all and not selected:
            problems.append(
                "--critical-net-all selects no net in this layout "
                "(after --critical-net-exclude)"
            )
        for problem in problems:
            if self.strict:
                raise self.error(f"{problem} (--strict-net-patterns)")
            self.warnings.append(
                f"{problem} -- nothing was scoped for "
                "them. See docs/cli/extract.md's '--critical-net' section."
            )
        return frozenset(selected)

    def echo(self) -> dict[str, Any]:
        """Additive ``parasitics`` report fields for the requested selectors."""
        out: dict[str, Any] = {}
        if self.critical_patterns:
            out["critical_net_patterns"] = list(self.critical_patterns)
        if self.critical_all:
            out["critical_net_all"] = True
            out["critical_net_excludes"] = list(self.critical_excludes)
        if self.parasitics_patterns:
            out["parasitics_net_patterns"] = list(self.parasitics_patterns)
        return out
