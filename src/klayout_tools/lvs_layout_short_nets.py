"""``hints.layout_short_nets`` for ``klt lvs`` (issue #5632).

The layout-side dual of ``hints.short_nets`` (:mod:`klayout_tools.lvs_short_nets`):
a caller-asserted, warning-disclosed merge of named **layout** nets on the
layout's selected top circuit, applied before the comparer is built. Kept out
of :mod:`klayout_tools.lvs`, which is size-ratcheted at its baseline ceiling;
:mod:`klayout_tools.lvs` imports nothing from here at module level, and this
module never imports it back -- the import graph stays acyclic.
"""

from __future__ import annotations

from typing import Any

#: Issue #5632: ``hints.layout_short_nets`` joined named layout nets into one
#: before comparing (always a warning; one entry per applied group). Defined
#: here, not in ``lvs`` -- same self-contained-category convention
#: ``lvs_mos_options.CATEGORY_DEVICE_BULK_IGNORED`` uses -- so this module
#: stays import-cycle-free.
CATEGORY_HINTS_LAYOUT_SHORT_NETS_APPLIED = "hints.layout_short_nets_applied"


def parse_layout_short_nets(hints: dict[str, Any], engine: str) -> Any:
    """Resolve ``hints.layout_short_nets`` into the ``{"nets": [...]}`` spec
    ``run_lvs`` applies to the layout side, or ``None`` when the key is
    absent (issue #5632).

    A non-object value, or a present object naming no non-empty ``nets``
    list, is a request error -- never a silent no-op (the same "a typo'd
    hint should be visible" discipline ``hints.short_nets`` applies). An
    explicit value on an engine other than ``"klayout"`` is rejected for the
    same reason every other ``hints`` hook is: the merge is applied through
    ``klayout.db``'s ``Circuit.join_nets()`` on the in-memory layout
    netlist, which the netgen engine never builds its compare from.

    Per-group name resolution against the selected top circuit happens at
    apply time (:func:`apply_layout_short_nets`), where the circuit is in
    scope -- the same two-stage split ``options.combine_devices``'s
    device-class list uses.
    """
    from .lvs import LvsError

    if "layout_short_nets" not in hints:
        return None
    spec = hints["layout_short_nets"]
    if not isinstance(spec, dict):
        raise LvsError(
            "hints.layout_short_nets must be a JSON object with a 'nets' key"
        )
    net_groups = spec.get("nets")
    if not net_groups:
        raise LvsError(
            "hints.layout_short_nets must declare a non-empty 'nets' key "
            "(layout net-name groups to merge)"
        )
    if not isinstance(net_groups, list) or not all(
        isinstance(group, list) for group in net_groups
    ):
        raise LvsError(
            "hints.layout_short_nets.nets must be a list of layout net-name "
            "groups, each with two or more entries"
        )
    if engine != "klayout":
        raise LvsError(
            "hints.layout_short_nets is only supported for engine 'klayout' "
            "-- it is applied through klayout.db's Circuit.join_nets on the "
            "in-memory layout netlist, the same hook request.hints is "
            "rejected for on the netgen engine (see docs/cli/lvs.md, "
            '"Engine")'
        )
    return spec


def apply_layout_short_nets(spec: Any, layout_circuit: Any) -> list[dict[str, Any]]:
    """Join every ``hints.layout_short_nets.nets`` group into one net on the
    layout's selected top circuit and return the disclosure entries (issue
    #5632).

    ``spec`` is :func:`parse_layout_short_nets`'s result (``None`` when the
    hint is absent -- the overwhelmingly common case, a clean no-op so a
    request that does not use the hook is byte-identical to before it
    existed). Names resolve by exact ``Circuit.net_by_name()`` match only,
    mirroring ``hints.short_nets``: a layout net's node names are
    caller-authored, so there is no SPICE-reader-upper-casing ambiguity to
    paper over. A name that does not resolve is a
    :class:`~klayout_tools.lvs.LvsError`, never a silent no-op.

    Must run before ``options.flatten_layout``, ``options.combine_devices*``
    and the gate-level power-only prune -- the same constraint
    :func:`klayout_tools.lvs_finfet.apply_finfet_bulk_hierarchy` records for
    a layout-side ``join_nets`` (flattening merges sub-cell nets by
    connectivity, so a tie made here survives it).
    """
    if not spec:
        return []
    from .lvs import LvsError
    from .lvs_mismatch import _mismatch

    entries: list[dict[str, Any]] = []
    net_groups = spec["nets"]
    for group in net_groups:
        if (
            not isinstance(group, list)
            or len(group) < 2
            or not all(isinstance(name, str) and name for name in group)
        ):
            raise LvsError(
                "hints.layout_short_nets.nets entries must be a list of two "
                "or more layout net names to merge into one net -- got "
                f"{group!r}"
            )
        nets = []
        for name in group:
            net = layout_circuit.net_by_name(name)
            if net is None:
                raise LvsError(
                    f"hints.layout_short_nets.nets: layout net '{name}' not "
                    f"found on circuit '{layout_circuit.name}'"
                )
            nets.append(net)
        primary = nets[0]
        for other in nets[1:]:
            # A group naming the same already-merged net twice (e.g. a later
            # group's own join already folded it in) is a harmless no-op,
            # not a double-join error -- same treatment as `hints.short_nets`.
            if other is not primary:
                layout_circuit.join_nets(primary, other)
        from .extract import spice_safe_net_name

        merged_net = spice_safe_net_name(primary.expanded_name())
        entries.append(
            _mismatch(
                CATEGORY_HINTS_LAYOUT_SHORT_NETS_APPLIED,
                "warning",
                f"hints.layout_short_nets merged {len(group)} layout net(s) "
                f"({', '.join(group)}) into one net '{merged_net}' on layout "
                f"circuit '{layout_circuit.name}' before comparing -- this "
                "tie was asserted by the request, not read from the layout, "
                "so a 'match' reached through it does not prove the layout "
                "connects these nets on its own (see docs/cli/lvs.md, "
                "'hints.layout_short_nets_applied')",
                "layout",
                details={
                    "members": list(group),
                    "merged_net": merged_net,
                    "circuit": layout_circuit.name,
                },
            )
        )
    return entries
