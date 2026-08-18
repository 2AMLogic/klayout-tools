"""``klt.layout_plan.request/1``: the plan-JSON contract + a pure reference
validator (issue #1131, Phase B of
``docs/design/netlist-driven-layout-spike.md`` -- read that document's
section 2 ("Proposed declarative plan contract") first; it settles the
request/response shape this module implements).

**Scope: contract + validation only.** This module never generates
geometry, never calls :func:`klayout_tools.gen.generate`/
:func:`klayout_tools.gen_compose.compose`, and never writes a GDS/OASIS
stream. Executing a plan (resolving a group's netlist-derived sizing,
compiling ``rows``/``abutment`` onto ``gen_compose``'s ``"explicit"``
placement, deriving ``connectivity[]`` from the netlist) is Phase C
(the spike's §3/§4), a separate, not-yet-built follow-up. Everything here
answers one question: *is this plan document internally consistent and
does every reference it makes actually resolve* -- nothing more.

**Built on Phase A, not a second netlist reader.** :func:`validate_layout_plan`
takes an already-built netlist digest (:mod:`klayout_tools.netlist_digest`,
issue #1130) rather than parsing SPICE itself -- a plan's device references
are checked against that digest's own ``devices[]`` entries, by
``(name, device_class)`` (see the per-class note below).
:func:`validate_layout_plan_document` is the convenience wrapper that builds
the digest for the caller, from the plan's own ``netlist.path``/``top``/
``form``/``deck``/``device_map`` fields, via
:func:`klayout_tools.netlist_digest.build_netlist_digest`
-- the same ``klt lvs`` reference-side ingestion path Phase A itself reuses,
never a private parse.

**A digest device ``name`` is per-class, so a bare name can be ambiguous
-- and an ambiguous reference is an error, never a silent first match.**
Per :mod:`klayout_tools.netlist_digest`'s own module docstring ("Device
``name`` is per-class"), ``NetlistSpiceReader`` strips the leading SPICE
element-type letter from an instance token (``"M1"`` -> ``"1"``, ``"R1"``
-> ``"1"``), so ``"1"`` can be a legitimate, simultaneous name for one MOS
device, one resistor, and one capacitor in the same circuit -- a real
schematic's ordinary per-element-type SPICE counters make that the common
case, not a contrived one. A validator resolving ``device_groups[].devices``
against a flat ``{d["name"] for d in digest["devices"]}`` set would
therefore report "resolves" for a plan that names the resistor when its
author meant the MOS, which is a false pass on exactly the check this
module exists to perform.

So a ``device_groups[].devices`` entry is **either** the spike's §2 bare
string (``"1"``) **or** a class-qualified object
(``{"name": "1", "device_class": "PFET"}``) -- the same ``name`` + ``class``
pairing ``klt lvs``'s own ``mismatches[].device`` already uses
(``docs/cli/lvs.md``) and the digest's own ``devices[]`` entries already
carry. Resolution rules:

* a **qualified** entry must match a digest device on *both* ``name`` and
  ``device_class`` (an unmatched pair is an application error naming the
  classes that name does have);
* an **unqualified** entry must match exactly one digest device *class* for
  that name -- unique, it resolves (and the response echoes the resolved
  ``device_class``, so a validated plan states which device it bound to);
  ambiguous across two or more classes, it is an application error telling
  the author to qualify it, rather than a coin-flip binding.

A bare-string plan against a single-class netlist -- the spike's own §2
worked example -- therefore keeps working exactly as written; qualification
is only ever *required* where it is actually load-bearing. This is a
strictly additive sharpening of the spike's shape, not a divergence from
it (issue #1131's curator review flagged the same collision from reading
Phase A's merged digest module).

**What is still deferred**: whether every device in one group belongs to
the *same* class, and whether that class is one the group's named
generator can actually draw, are not checked here -- "full shape-class
consistency checking is deferred" per this issue's own acceptance
criteria, and the generator-side device-class mapping does not exist until
Phase C compiles a group into a real ``klt gen`` call.

**Exit-code trichotomy, and why it splits differently from every other
``klt`` verb.** Per ``docs/json-contract.md``, exit code ``2`` is normally
argparse's own territory -- reserved for a CLI usage mistake, produced
before any subcommand handler runs, and never raised by library code. This
module adds **no** ``klt`` subcommand (see "No CLI subcommand" below), so
there is no argparse layer to own that boundary; instead, ``2`` and ``1``
split the space this module *does* own, using the same distinction
``klt gen-compose``'s own validation already draws structurally --

* :class:`LayoutPlanUsageError` (exit ``2``) -- the plan document itself
  does not conform to the ``klt.layout_plan.request/1`` shape: invalid
  JSON syntax, a field with the wrong JSON type, a required field missing,
  or an enum-like field (``netlist.form``, ``device_groups[].topology``,
  ``rows[].align``, ``abutment[].edge``) holding a value outside its known
  literal set. Every one of these is checkable by reading the request
  document alone -- no netlist digest, no generator table, no sibling
  ``device_groups[]`` entry is needed to know the document is malformed.
* :class:`LayoutPlanError` (exit ``1``) -- the document is shaped
  correctly, but a value that is supposed to *reference* something does
  not resolve: a ``device_groups[].devices`` name absent from the netlist
  digest, a ``device_groups[].generator`` naming a generator ``klt gen``
  does not have, a ``device_groups[].topology`` value the named generator
  does not document supporting (see "Topology support" below), a
  ``device_groups[].encloses``/``rows[].order``/``abutment[].a``/``.b`` id
  absent from the request's own ``device_groups[].id`` set, or an
  ``abutment[]`` entry naming the same group for both sides. This is
  exactly the "an unresolvable name is an application error, exit code 1"
  convention this issue asks to mirror from
  :func:`klayout_tools.gen_compose._parse_connectivity`/
  :func:`klayout_tools.gen_compose._validate_block_port` -- generalised
  here to reference sets a plan can point at (the digest, the generator
  table, or a sibling ``device_groups[].id``), not just a same-request
  block/port pair.

:class:`LayoutPlanUsageError` subclasses :class:`LayoutPlanError`, so a
caller that only wants "did this fail" can catch the base class; a caller
that wants the exit code distinguishes with :func:`exit_code_for`.

**Topology support: hard-reject, not flagged-but-accepted.** The spike's
§2 field table names four ``device_groups[].topology`` values
(``"array"``, ``"common_centroid"``, ``"interdigitated"``, ``"single"``)
but is explicit that only two of them, and only for two generators, exist
in ``klt gen`` today: ``mos_array``/``bjt_array``'s own ``params.topology``
accepts exactly ``"array"``/``"common_centroid"`` (``docs/cli/gen.md``).
``"interdigitated"``/``"single"`` are values the spike *proposes*
generators grow support for later (§4 Phase E, deliberately deferred) --
declaring either today, on any generator, cannot possibly compile to a
real ``klt gen`` call. This module chooses to raise
:class:`LayoutPlanError` (exit ``1``) rather than accept the plan with a
``warnings[]`` note, for the same reason ``gen_compose.py`` hard-rejects an
unsupported ``placement.strategy`` instead of silently ignoring it: a
"validated" plan that names a topology no generator can execute is not a
usable increment of work for whatever calls this validator next (a human,
or Phase C once it exists) -- surfacing it as an error a caller must fix
now is more useful than a warning a caller can (and likely will) ignore
until Phase C fails on it anyway. ``diff_pair`` is treated as supporting
only ``"common_centroid"`` even though it accepts no ``params.topology``
of its own -- it always lays out a common-centroid cross-quad pattern
(``docs/cli/gen.md``'s ``diff_pair`` section), so that is the one topology
value consistent with what it actually draws; every other generator this
module recognises documents no ``topology`` concept at all, so any
``device_groups[].topology`` value declared against it is unsupported.

**PDK resolution is deferred to Phase C.** ``request.pdk`` is validated
for shape only (``variant``/``root``, the same
:data:`klayout_tools.gen_compose._ALLOWED_PDK_KEYS` allow-list every other
``klt gen``-family verb already uses) -- this module never calls
:func:`klayout_tools.pdk.find_pdk`. A plan's PDK does not need to resolve
on disk to be a structurally/referentially valid *plan*; it needs to
resolve before Phase C actually calls ``klt gen``/``klt gen compose``,
which is exactly where that check belongs, and keeps this module's own
tests hermetic (no PDK install required to validate a plan document).

**No ``klt`` CLI subcommand is added.** Per this issue's own acceptance
criteria, a library-level validator alone satisfies Phase B --
:func:`validate_layout_plan`/:func:`validate_layout_plan_document`/
:func:`validate_layout_plan_json` are the complete public surface. A
``klt plan validate``-shaped verb would need to decide how a caller
supplies the digest (build it inline from ``request.netlist``, as
:func:`validate_layout_plan_document` already does, or accept a
pre-built one) with no clear win over calling this module directly from
Python -- there is no rendering/formatting value a CLI wrapper would add
that a caller integrating this into a larger pipeline (the whole point of
a "plan" sitting between ingestion and generation) actually needs today.
Revisit this once Phase C exists and a caller wants to validate a plan
from the shell before compiling it.
"""

from __future__ import annotations

import json
import os
from typing import Any

from ._paths import _resolve_relative
from .gen import _GENERATOR_SPECS
from .netlist_digest import build_netlist_digest

#: Contract identifier for the request envelope (spike section 2).
REQUEST_SCHEMA = "klt.layout_plan.request/1"

#: Bumped only on a non-additive (breaking) change to this module's own
#: response JSON shape -- see docs/json-contract.md.
SCHEMA_VERSION = 1

#: ``request.netlist.form`` values -- identical to ``lvs.py``'s own
#: ``_REFERENCE_FORMS``, since a plan's netlist ingestion resolves through
#: the exact same ``klt lvs`` reference-side reader (see the module
#: docstring).
_NETLIST_FORMS = ("plain-element", "subckt-call")

#: Allowed keys in ``request.pdk`` -- mirrors
#: ``klayout_tools.gen_compose._ALLOWED_PDK_KEYS`` (spike section 2): an
#: unrecognised key (e.g. ``name``, a plausible typo for ``variant``) is a
#: usage error rather than a silent fallback to ``$PDK``/the default search
#: order.
_ALLOWED_PDK_KEYS = {"variant", "root"}

#: Allowed keys in ``request.netlist`` -- the same ``path``/``top``/``form``/
#: ``deck``/``device_map`` shape ``klt lvs``'s ``request.reference`` already
#: accepts (issue #1163): an unrecognised key is a usage error rather than a
#: silent drop, mirroring :data:`_ALLOWED_PDK_KEYS`.
_ALLOWED_NETLIST_KEYS = {"path", "top", "form", "deck", "device_map"}

#: Every ``device_groups[].topology`` value the spike's contract names
#: (spike section 2 field table). Membership here is a *shape* check
#: (usage error, exit 2) -- whether a given generator actually supports the
#: value is a separate *reference* check (application error, exit 1), see
#: :data:`_GENERATOR_TOPOLOGY_SUPPORT`.
_TOPOLOGY_VALUES = frozenset({"array", "common_centroid", "interdigitated", "single"})

#: Which ``device_groups[].topology`` values each generator actually
#: supports today, read directly from ``docs/cli/gen.md``'s own generator
#: sections (see the module docstring's "Topology support" note) --
#: ``mos_array``/``bjt_array``'s documented ``params.topology`` enum, and
#: ``diff_pair``'s always-common-centroid layout. Every other generator
#: this module recognises documents no ``topology`` concept at all, so it
#: maps to an empty set (any declared value is unsupported) rather than
#: being omitted -- an omitted key and an empty set behave identically via
#: ``.get(generator, frozenset())``, but listing every generator here
#: explicitly makes "no support yet" a reviewable statement about that
#: generator instead of an absence someone has to infer.
_GENERATOR_TOPOLOGY_SUPPORT: dict[str, frozenset[str]] = {
    "resistor_strip": frozenset(),
    "mos_array": frozenset({"array", "common_centroid"}),
    "res_array": frozenset(),
    "cap_array": frozenset(),
    "guard_ring": frozenset(),
    "diff_pair": frozenset({"common_centroid"}),
    "bjt_array": frozenset({"array", "common_centroid"}),
    "bond_pad": frozenset(),
    "esd_device": frozenset(),
}

#: ``rows[].align`` values (spike section 2 field table).
_ROW_ALIGN_VALUES = frozenset({"bottom", "center", "top"})

#: ``abutment[].edge`` values (spike section 2 field table).
_ABUTMENT_EDGES = frozenset({"top", "bottom", "left", "right"})


class LayoutPlanError(Exception):
    """Application error (exit code 1): the plan document is shaped
    correctly, but a reference it makes -- a netlist device name, a
    generator name, a topology value, or a sibling ``device_groups[].id``
    -- does not resolve. See the module docstring's "Exit-code trichotomy"
    note for the full rule distinguishing this from
    :class:`LayoutPlanUsageError`.
    """


class LayoutPlanUsageError(LayoutPlanError):
    """Usage error (exit code 2): the plan document itself is malformed --
    invalid JSON, a field of the wrong type, a required field missing, or
    an enum-like field holding a value outside its known literal set. See
    the module docstring's "Exit-code trichotomy" note.
    """


def exit_code_for(exc: LayoutPlanError) -> int:
    """Map a raised :class:`LayoutPlanError` to the exit code a caller
    (e.g. a future CLI wrapper) should use: ``2`` for
    :class:`LayoutPlanUsageError`, ``1`` for any other
    :class:`LayoutPlanError`.
    """
    return 2 if isinstance(exc, LayoutPlanUsageError) else 1


def _parse_netlist(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise LayoutPlanUsageError("request.netlist must be a JSON object")

    unknown = set(raw) - _ALLOWED_NETLIST_KEYS
    if unknown:
        allowed = ", ".join(sorted(_ALLOWED_NETLIST_KEYS))
        raise LayoutPlanUsageError(
            "request.netlist has unknown field(s): "
            f"{', '.join(sorted(unknown))} -- allowed: {allowed}"
        )

    path = raw.get("path")
    if not isinstance(path, str) or not path:
        raise LayoutPlanUsageError(
            "request.netlist.path is required and must be a non-empty string"
        )

    top = raw.get("top")
    if top is not None and (not isinstance(top, str) or not top):
        raise LayoutPlanUsageError(
            "request.netlist.top must be a non-empty string when given"
        )

    form = raw.get("form", "plain-element")
    if form not in _NETLIST_FORMS:
        allowed = ", ".join(repr(f) for f in _NETLIST_FORMS)
        raise LayoutPlanUsageError(f"request.netlist.form must be one of {allowed}")

    deck = raw.get("deck")
    if deck is not None and (not isinstance(deck, str) or not deck):
        raise LayoutPlanUsageError(
            "request.netlist.deck must be a non-empty string when given"
        )

    device_map = raw.get("device_map")
    if device_map is not None and not isinstance(device_map, dict):
        raise LayoutPlanUsageError("request.netlist.device_map must be a JSON object")

    return {
        "path": path,
        "top": top,
        "form": form,
        "deck": deck,
        "device_map": device_map,
    }


def _parse_pdk(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {"variant": None, "root": None}
    if not isinstance(raw, dict):
        raise LayoutPlanUsageError("request.pdk must be a JSON object")

    unknown = set(raw) - _ALLOWED_PDK_KEYS
    if unknown:
        allowed = ", ".join(sorted(_ALLOWED_PDK_KEYS))
        raise LayoutPlanUsageError(
            "request.pdk has unknown field(s): "
            f"{', '.join(sorted(unknown))} -- allowed: {allowed}"
        )

    variant = raw.get("variant")
    if variant is not None and not isinstance(variant, str):
        raise LayoutPlanUsageError("request.pdk.variant must be a string when given")
    root = raw.get("root")
    if root is not None and not isinstance(root, str):
        raise LayoutPlanUsageError("request.pdk.root must be a string when given")

    return {"variant": variant, "root": root}


def _parse_device_reference(raw: Any, where: str) -> dict[str, str | None]:
    """Normalise one ``device_groups[].devices`` entry to
    ``{"name", "device_class"}``.

    Accepts the spike's §2 bare string (``device_class`` is then ``None``
    -- "resolve it, and require the name to be unambiguous across classes")
    or the class-qualified object form. See the module docstring's "A digest
    device ``name`` is per-class" note for why both exist.
    """
    if isinstance(raw, str):
        if not raw:
            raise LayoutPlanUsageError(f"{where} must be a non-empty string")
        return {"name": raw, "device_class": None}

    if not isinstance(raw, dict):
        raise LayoutPlanUsageError(
            f"{where} must be a device-instance name string, or an object "
            'with a "name" and an optional "device_class"'
        )

    unknown = set(raw) - {"name", "device_class"}
    if unknown:
        raise LayoutPlanUsageError(
            f"{where} has unknown field(s): {', '.join(sorted(unknown))} -- "
            "allowed: device_class, name"
        )

    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise LayoutPlanUsageError(
            f"{where}.name is required and must be a non-empty string"
        )

    device_class = raw.get("device_class")
    if device_class is not None and (
        not isinstance(device_class, str) or not device_class
    ):
        raise LayoutPlanUsageError(
            f"{where}.device_class must be a non-empty string when given"
        )

    return {"name": name, "device_class": device_class}


def _parse_device_groups_shape(raw: Any) -> list[dict[str, Any]]:
    """Structural (usage-only) pass over ``request.device_groups`` -- shape
    and type checks, and ``id`` uniqueness. Reference checks (does a
    ``devices``/``generator``/``topology``/``encloses`` value actually
    resolve) run separately, in :func:`_validate_device_group_references`,
    once every group's ``id`` is known (an ``encloses`` entry can name a
    group declared later in the same array).
    """
    if not isinstance(raw, list):
        raise LayoutPlanUsageError("request.device_groups must be an array")

    groups: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, raw_group in enumerate(raw):
        where = f"request.device_groups[{index}]"
        if not isinstance(raw_group, dict):
            raise LayoutPlanUsageError(f"{where} must be a JSON object")

        group_id = raw_group.get("id")
        if not isinstance(group_id, str) or not group_id:
            raise LayoutPlanUsageError(
                f"{where}.id is required and must be a non-empty string"
            )
        if group_id in seen_ids:
            raise LayoutPlanUsageError(
                f"request.device_groups contains duplicate id '{group_id}'"
            )
        seen_ids.add(group_id)

        entry_where = f"{where} (id '{group_id}')"

        generator = raw_group.get("generator")
        if not isinstance(generator, str) or not generator:
            raise LayoutPlanUsageError(f"{entry_where}.generator is required")

        raw_devices = raw_group.get("devices", [])
        if not isinstance(raw_devices, list):
            raise LayoutPlanUsageError(f"{entry_where}.devices must be an array")
        devices = [
            _parse_device_reference(entry, f"{entry_where}.devices[{i}]")
            for i, entry in enumerate(raw_devices)
        ]

        topology = raw_group.get("topology")
        if topology is not None and (
            not isinstance(topology, str) or topology not in _TOPOLOGY_VALUES
        ):
            allowed = ", ".join(sorted(_TOPOLOGY_VALUES))
            raise LayoutPlanUsageError(
                f"{entry_where}.topology must be one of {allowed} when given"
            )

        encloses = raw_group.get("encloses", [])
        if not isinstance(encloses, list) or not all(
            isinstance(e, str) and e for e in encloses
        ):
            raise LayoutPlanUsageError(
                f"{entry_where}.encloses must be an array of non-empty strings"
            )

        dummy = raw_group.get("dummy")
        if dummy is not None and not isinstance(dummy, dict):
            raise LayoutPlanUsageError(f"{entry_where}.dummy must be a JSON object")

        params = raw_group.get("params")
        if params is not None and not isinstance(params, dict):
            raise LayoutPlanUsageError(f"{entry_where}.params must be a JSON object")

        groups.append(
            {
                "id": group_id,
                "generator": generator,
                "devices": devices,
                "topology": topology,
                "encloses": encloses,
                "dummy": dummy,
                "params": params,
            }
        )

    return groups


def _resolve_device_reference(
    reference: dict[str, str | None],
    classes_by_name: dict[str, set[str]],
    where: str,
) -> dict[str, str]:
    """Resolve one normalised ``devices[]`` entry against the digest's
    ``(name -> {device_class, ...})`` map, returning the fully qualified
    ``{"name", "device_class"}`` pair it binds to.

    Raises :class:`LayoutPlanError` (exit 1) when the name is absent, when
    a declared ``device_class`` does not exist for that name, or when a
    bare name is ambiguous across two or more classes -- see the module
    docstring's "A digest device ``name`` is per-class" note.
    """
    name = reference["name"]
    declared_class = reference["device_class"]

    available = classes_by_name.get(name)
    if not available:
        raise LayoutPlanError(
            f"{where} references netlist device '{name}', which does not "
            "resolve against the ingested netlist digest"
        )

    if declared_class is not None:
        if declared_class not in available:
            raise LayoutPlanError(
                f"{where} references netlist device '{name}' with "
                f"device_class '{declared_class}', which does not resolve "
                "against the ingested netlist digest -- that name's "
                f"device_class(es): {', '.join(sorted(available))}"
            )
        return {"name": name, "device_class": declared_class}

    if len(available) > 1:
        raise LayoutPlanError(
            f"{where} references netlist device '{name}', which is "
            "ambiguous: a digest device name is per-class, and this "
            f"netlist has {len(available)} devices named '{name}' "
            f"({', '.join(sorted(available))}) -- qualify the reference as "
            '{"name": "' + name + '", "device_class": "'
            f"{sorted(available)[0]}" + '"}'
        )

    return {"name": name, "device_class": next(iter(available))}


def _validate_device_group_references(
    groups: list[dict[str, Any]],
    group_ids: set[str],
    classes_by_name: dict[str, set[str]],
) -> None:
    """Reference (application-error) pass over an already shape-checked
    ``device_groups[]`` list -- see the module docstring's "Exit-code
    trichotomy" note. Each group's ``devices`` entries are rewritten in
    place to their fully qualified ``{"name", "device_class"}`` form, so
    the response can echo the class a bare name actually bound to.
    """
    for group in groups:
        entry_where = f"request.device_groups (id '{group['id']}')"

        if group["generator"] not in _GENERATOR_SPECS:
            supported = ", ".join(sorted(_GENERATOR_SPECS))
            raise LayoutPlanError(
                f"{entry_where}.generator '{group['generator']}' is not a "
                f"generator klt gen supports -- supported: {supported}"
            )

        group["devices"] = [
            _resolve_device_reference(
                reference, classes_by_name, f"{entry_where}.devices"
            )
            for reference in group["devices"]
        ]

        if group["topology"] is not None:
            supported_topologies = _GENERATOR_TOPOLOGY_SUPPORT.get(
                group["generator"], frozenset()
            )
            if group["topology"] not in supported_topologies:
                if supported_topologies:
                    detail = (
                        f"'{group['generator']}' supports: "
                        f"{', '.join(sorted(supported_topologies))}"
                    )
                else:
                    detail = (
                        f"generator '{group['generator']}' does not support "
                        "any device_groups[].topology value yet"
                    )
                raise LayoutPlanError(
                    f"{entry_where}.topology '{group['topology']}' is not "
                    f"supported -- {detail}"
                )

        for enclosed_id in group["encloses"]:
            if enclosed_id == group["id"]:
                raise LayoutPlanError(
                    f"{entry_where}.encloses cannot reference its own id"
                )
            if enclosed_id not in group_ids:
                raise LayoutPlanError(
                    f"{entry_where}.encloses references unknown "
                    f"device_groups id '{enclosed_id}'"
                )


def _parse_rows(raw: Any, group_ids: set[str]) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise LayoutPlanUsageError("request.rows must be an array")

    rows: list[dict[str, Any]] = []
    for index, raw_row in enumerate(raw):
        where = f"request.rows[{index}]"
        if not isinstance(raw_row, dict):
            raise LayoutPlanUsageError(f"{where} must be a JSON object")

        order = raw_row.get("order")
        if (
            not isinstance(order, list)
            or not order
            or not all(isinstance(o, str) and o for o in order)
        ):
            raise LayoutPlanUsageError(
                f"{where}.order must be a non-empty array of strings"
            )

        spacing_um = raw_row.get("spacing_um", 0.0)
        if isinstance(spacing_um, bool) or not isinstance(spacing_um, (int, float)):
            raise LayoutPlanUsageError(f"{where}.spacing_um must be a number")
        spacing_um = float(spacing_um)
        if spacing_um < 0:
            raise LayoutPlanUsageError(f"{where}.spacing_um must be >= 0")

        align = raw_row.get("align", "bottom")
        if align not in _ROW_ALIGN_VALUES:
            allowed = ", ".join(sorted(_ROW_ALIGN_VALUES))
            raise LayoutPlanUsageError(f"{where}.align must be one of {allowed}")

        for group_id in order:
            if group_id not in group_ids:
                raise LayoutPlanError(
                    f"{where}.order references unknown device_groups id '{group_id}'"
                )

        rows.append({"order": order, "spacing_um": spacing_um, "align": align})

    return rows


def _parse_abutment(raw: Any, group_ids: set[str]) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise LayoutPlanUsageError("request.abutment must be an array")

    abutment: list[dict[str, Any]] = []
    for index, raw_entry in enumerate(raw):
        where = f"request.abutment[{index}]"
        if not isinstance(raw_entry, dict):
            raise LayoutPlanUsageError(f"{where} must be a JSON object")

        a = raw_entry.get("a")
        b = raw_entry.get("b")
        if not isinstance(a, str) or not a or not isinstance(b, str) or not b:
            raise LayoutPlanUsageError(
                f"{where} must have non-empty string 'a'/'b' fields"
            )

        edge = raw_entry.get("edge")
        if edge not in _ABUTMENT_EDGES:
            allowed = ", ".join(sorted(_ABUTMENT_EDGES))
            raise LayoutPlanUsageError(f"{where}.edge must be one of {allowed}")

        gap_um = raw_entry.get("gap_um", 0.0)
        if isinstance(gap_um, bool) or not isinstance(gap_um, (int, float)):
            raise LayoutPlanUsageError(f"{where}.gap_um must be a number")
        gap_um = float(gap_um)
        if gap_um < 0:
            raise LayoutPlanUsageError(f"{where}.gap_um must be >= 0")

        # Reference checks: does each side actually name a declared group,
        # and -- the one "geometrically plausible" structural check this
        # phase can make without any group's real (not-yet-generated) shape
        # -- is it two *different* groups (see the module docstring; full
        # shape-class consistency is deferred to Phase C/E).
        if a not in group_ids:
            raise LayoutPlanError(
                f"{where}.a references unknown device_groups id '{a}'"
            )
        if b not in group_ids:
            raise LayoutPlanError(
                f"{where}.b references unknown device_groups id '{b}'"
            )
        if a == b:
            raise LayoutPlanError(
                f"{where} references the same device_groups id ('{a}') for "
                "both 'a' and 'b' -- a group cannot abut itself"
            )

        abutment.append({"a": a, "b": b, "edge": edge, "gap_um": gap_um})

    return abutment


def _parse_options(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {"cell_name": None, "output": None}
    if not isinstance(raw, dict):
        raise LayoutPlanUsageError("request.options must be a JSON object")

    cell_name = raw.get("cell_name")
    if cell_name is not None and (not isinstance(cell_name, str) or not cell_name):
        raise LayoutPlanUsageError(
            "request.options.cell_name must be a non-empty string when given"
        )
    output = raw.get("output")
    if output is not None and (not isinstance(output, str) or not output):
        raise LayoutPlanUsageError(
            "request.options.output must be a non-empty string when given"
        )

    return {"cell_name": cell_name, "output": output}


def validate_layout_plan(request: Any, digest: dict[str, Any]) -> dict[str, Any]:
    """Pure validation pass over a ``klt.layout_plan.request/1`` document --
    no generation, no file I/O beyond what the caller already did to build
    ``digest``. Returns the response envelope on success; raises
    :class:`LayoutPlanUsageError`/:class:`LayoutPlanError` otherwise (see
    the module docstring's "Exit-code trichotomy" note).

    ``digest`` is a :func:`klayout_tools.netlist_digest.build_netlist_digest`
    result (or any dict of that shape) -- ``device_groups[].devices``
    entries are resolved against its ``devices[*]``
    ``("name", "device_class")`` pairs, never ``name`` alone (see the module
    docstring). Prefer :func:`validate_layout_plan_document` when the caller
    has not already built the digest.

    The response echoes every ``device_groups[].devices`` entry in its fully
    qualified ``{"name", "device_class"}`` form -- including the class a
    bare-string reference resolved to -- and reports every digest device no
    group claimed in ``unmapped_devices`` (the same shape, always present,
    empty when the plan covers the whole netlist).

    An empty ``device_groups`` array is valid (no groups to place yet, not
    an error) -- the acceptance criteria's own edge case.
    """
    if not isinstance(request, dict):
        raise LayoutPlanUsageError("request must be a JSON object")

    for field in ("netlist", "device_groups"):
        if field not in request:
            raise LayoutPlanUsageError(f"request is missing required field: {field}")

    netlist_spec = _parse_netlist(request["netlist"])
    pdk_spec = _parse_pdk(request.get("pdk"))
    raw_groups = _parse_device_groups_shape(request["device_groups"])
    group_ids = {g["id"] for g in raw_groups}

    digest_devices = digest.get("devices") if isinstance(digest, dict) else None
    if not isinstance(digest_devices, list):
        digest_devices = []
    # `(name -> {device_class, ...})`: a digest device name is unique only
    # *within* a class (netlist_digest's own "Device `name` is per-class"
    # note), so the reference check keys on the pair, never the name alone.
    classes_by_name: dict[str, set[str]] = {}
    for entry in digest_devices:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        device_class = entry.get("device_class")
        if isinstance(name, str) and isinstance(device_class, str):
            classes_by_name.setdefault(name, set()).add(device_class)
    digest_nets = digest.get("nets") if isinstance(digest, dict) else None
    net_count = len(digest_nets) if isinstance(digest_nets, list) else 0
    circuit_name = digest.get("circuit") if isinstance(digest, dict) else None

    _validate_device_group_references(raw_groups, group_ids, classes_by_name)

    rows = _parse_rows(request.get("rows"), group_ids)
    abutment = _parse_abutment(request.get("abutment"), group_ids)
    options = _parse_options(request.get("options"))

    referenced_devices = {
        (d["name"], d["device_class"]) for g in raw_groups for d in g["devices"]
    }
    unmapped_devices = [
        {"name": name, "device_class": device_class}
        for name, device_class in sorted(
            {
                (name, device_class)
                for name, classes in classes_by_name.items()
                for device_class in classes
            }
            - referenced_devices
        )
    ]

    return {
        "schema_version": SCHEMA_VERSION,
        "valid": True,
        "netlist": {
            **netlist_spec,
            "circuit": circuit_name,
            "device_count": len(digest_devices),
            "net_count": net_count,
        },
        "pdk": pdk_spec,
        "device_groups": [
            {
                "id": g["id"],
                "generator": g["generator"],
                "topology": g["topology"],
                "devices": g["devices"],
                "encloses": g["encloses"],
            }
            for g in raw_groups
        ],
        "rows": rows,
        "abutment": abutment,
        "options": options,
        "unmapped_devices": unmapped_devices,
        "warnings": [],
    }


def validate_layout_plan_document(
    request: Any, request_dir: str | None = None
) -> dict[str, Any]:
    """:func:`validate_layout_plan`, but ingesting ``request.netlist``
    itself (via :func:`klayout_tools.netlist_digest.build_netlist_digest`)
    rather than requiring the caller to have already built a digest.

    ``request_dir`` resolves a relative ``request.netlist.path`` (mirrors
    ``klt lvs``'s ``load_request_arg``/``_resolve_relative`` convention);
    defaults to the current working directory, matching every other
    ``klt``-family request loader's own default.

    A netlist that fails to ingest (missing file, unparseable SPICE, an
    unresolvable ``form="subckt-call"`` device name) is a
    :class:`LayoutPlanError` (exit 1, an application error) -- the request
    document itself was well-formed; it is the *referenced netlist file*
    that could not be read, the same class of failure as an unresolvable
    ``device_groups[].devices`` name.
    """
    if not isinstance(request, dict):
        raise LayoutPlanUsageError("request must be a JSON object")
    if "netlist" not in request:
        raise LayoutPlanUsageError("request is missing required field: netlist")

    netlist_spec = _parse_netlist(request["netlist"])
    resolved_path = _resolve_relative(netlist_spec["path"], request_dir or os.getcwd())

    from .lvs import LvsError

    try:
        digest = build_netlist_digest(
            resolved_path,
            top=netlist_spec["top"],
            form=netlist_spec["form"],
            deck=netlist_spec["deck"],
            device_map=netlist_spec["device_map"],
        )
    except LvsError as exc:
        raise LayoutPlanError(f"request.netlist could not be ingested: {exc}") from exc

    return validate_layout_plan(request, digest)


def validate_layout_plan_json(
    raw_json: str, request_dir: str | None = None
) -> dict[str, Any]:
    """:func:`validate_layout_plan_document`, parsing ``raw_json`` (a JSON
    document as text) first. A :class:`json.JSONDecodeError` is wrapped as
    :class:`LayoutPlanUsageError` (exit 2) -- invalid JSON syntax is the
    most basic "the document does not conform to the shape" case this
    module's own usage-error bucket exists for.
    """
    try:
        request = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise LayoutPlanUsageError(f"plan document is not valid JSON: {exc}") from exc
    return validate_layout_plan_document(request, request_dir)
