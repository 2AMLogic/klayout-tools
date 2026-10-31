"""Spec validation and parsing for ``klt erc`` (issue #3735).

The dict-in / validated-dict-or-:class:`ErcError`-out half of
``klayout_tools.erc``, split out of it by subsystem (same convention as
``lvs_runset.py`` / ``lvs_combine.py``): every ``_validate_*`` /
``_parse_*`` reader of the ``--spec`` file, the ``_*_KEYS`` tables that
reject unrecognised keys, and :class:`ErcError`. No KLayout geometry work
lives here. ``erc.py`` imports these names back, so
``from klayout_tools.erc import ErcError`` keeps working.
"""

from __future__ import annotations

import math
from typing import Any

from ._jsonnum import is_number
from ._paths import (
    _parse_layer_datatype,
    _reject_unknown_keys,
    _validate_via_entries,
)

#: The stable ``erc_coverage.inapplicable`` reason for the undeclared
#: ``erc.missing_tie`` work when the spec's top-level ``ties_disclosure``
#: (issue #2234) is present -- in place of the default
#: :data:`REASON_NO_TIES_DECLARED` used when ``ties`` is simply omitted with
#: no disclosure at all. Both reasons describe the same "zero ties
#: declared" fact; only this one tells a reader the omission was a
#: considered, disclosed choice rather than an oversight -- see
#: :func:`_validate_ties_disclosure` and `docs/design-evidence-tiers.md`
#: item 11.
REASON_TIES_DISCLOSED_UNEXPRESSIBLE = "ties_disclosed_unexpressible"

#: The stable ``erc_coverage.inapplicable`` reason for the undeclared
#: ``erc.missing_tie`` work when the spec's top-level ``ties_disclosure``
#: declared ``"kind": "tool_limitation"`` (issue #2247) -- the *second*
#: disclosed shape, beside :data:`REASON_TIES_DISCLOSED_UNEXPRESSIBLE`.
#: Same "zero ties declared, deliberately, and here is why" fact; what
#: differs is **where the obstacle lives, and therefore what would clear
#: it**. An unexpressible stream has no tap to name (nothing drawn to
#: narrow, affirm, or bound) -- only different *geometry* clears it. A
#: tool-limitation stream can name its tap perfectly well, but declaring it
#: against the `klt` build the caller is pinned to produces a result they
#: have documented reason to distrust (the issue #2169 class of defect: a
#: build whose tie extraction is not isolated from the primary connectivity
#: graph turns a correct ``ties[]`` declaration into a false
#: ``erc.supply_short``) -- only a different *build* clears it. Collapsing
#: the two would tell a reader to go redraw a layout that is already
#: correct.
REASON_TIES_DISCLOSED_TOOL_LIMITATION = "ties_disclosed_tool_limitation"

#: The stable ``erc_coverage.inapplicable`` reason for the undeclared
#: ``erc.missing_tie`` work when ``ties`` is omitted/empty and no
#: ``ties_disclosure`` was given -- the pre-#2234 behaviour, named as a
#: constant so :data:`REASON_TIES_DISCLOSED_UNEXPRESSIBLE` has a documented
#: counterpart rather than a bare string literal to contrast with.
REASON_NO_TIES_DECLARED = "no_ties_declared"

#: ``ties_disclosure.kind`` (issue #2247): *why* this stream declares no
#: ``ties[]``. Default when the key is omitted, so every pre-#2247 spec
#: keeps the exact meaning it had.
DISCLOSURE_KIND_UNEXPRESSIBLE = "unexpressible"

#: ``ties_disclosure.kind`` (issue #2247): the tap is expressible, but the
#: `klt` build this evidence must be produced on cannot grade a declared tie
#: safely -- see :data:`REASON_TIES_DISCLOSED_TOOL_LIMITATION`.
DISCLOSURE_KIND_TOOL_LIMITATION = "tool_limitation"

#: Every accepted ``ties_disclosure.kind``, mapped to the
#: ``erc_coverage.inapplicable`` reason it records for the undeclared
#: ``erc.missing_tie`` work. A ``kind`` outside this table is a spec error
#: rather than a silent fallback to the default: a typo that quietly
#: downgraded a tool-limitation disclosure to an unexpressible one would
#: misdirect the reader of the report of record, which is the exact
#: conflation these reasons exist to end.
_TIES_DISCLOSURE_COVERAGE_REASON: dict[str, str] = {
    DISCLOSURE_KIND_UNEXPRESSIBLE: REASON_TIES_DISCLOSED_UNEXPRESSIBLE,
    DISCLOSURE_KIND_TOOL_LIMITATION: REASON_TIES_DISCLOSED_TOOL_LIMITATION,
}


#: The keys each part of the ``klt erc`` spec file recognises (issue #2243).
#: Any other key is rejected with its full path rather than ignored -- see
#: ``docs/cli/erc.md``'s "Unrecognised spec keys". Keep these in step with
#: the ``_validate_*``/``_parse_*`` readers below when a spec field is added.
_SPEC_KEYS = (
    "stackup",
    "vias",
    "nets",
    "ties",
    "ties_disclosure",
    "devices",
    "antenna_limits",
)
_STACKUP_KEYS = ("name", "layer", "label_layer", "active_layer", "role")
_NET_KEYS = (
    "name",
    "kind",
    "islands",
    "same_net_as",
    "roles",
    "unlabelled_allowed_boxes",
    "unlabelled_allow_device_connected",
    "extractor_global",
)
_TIE_KEYS = (
    "name",
    "well_layer",
    "tap_layer",
    "connect_to",
    "net",
    "tap_requires",
    "tap_excludes",
    "tap_is_dedicated",
    "tap_boxes",
    "well_boxes",
    "well_complement_of",
    "well_requires",
    "well_excludes",
    "well_requires_boxes",
    "well_excludes_boxes",
    "well_requires_cells",
    "well_excludes_cells",
)
_TIES_DISCLOSURE_KEYS = ("reason", "kind", "undeclared_classes")
_ANTENNA_LIMIT_KEYS = ("layer", "max_ratio", "source")


class ErcError(Exception):
    """Raised when ``klt erc`` cannot run: a bad layout/spec file, a
    malformed stackup/via declaration, an unresolvable top cell, an unknown
    ``--pdk`` name, or a spec whose gate role matches no geometry at all.

    The CLI turns this into a clean stderr message + exit code 1, never a
    traceback -- see ``docs/json-contract.md``.
    """


def _validate_stackup(spec: dict[str, Any], spec_path: str) -> list[dict[str, Any]]:
    """Validate the ``stackup`` array: fabrication order from the gate
    layer up through every metal role a gate's net may reach.

    ``stackup[0]`` must declare ``"role": "gate"`` -- the polysilicon/gate
    layer that starts the accumulation. Every other entry is an ordinary
    metal role and must not repeat ``role: "gate"``. At least one metal
    role beyond the gate itself is required, or "layer-by-layer
    accumulation" has nothing to accumulate.

    ``stackup[0]`` may additionally set ``"active_layer"`` (issue #1979,
    optional, ``"<layer>/<datatype>"``, same shape as ``label_layer``): the
    diffusion/active layer used to compute *true* gate area as ``poly ∩
    diff`` rather than raw poly-net area -- see ``run_erc``'s gate-region
    computation and ``docs/cli/erc.md``'s "Spec file" section for the full
    rationale (a tie-cell/decap/filler-cell poly resistor has poly area but
    no gate oxide, since it never overlaps diffusion). Only meaningful on
    the gate role itself; rejected on any other entry.
    """
    raw = spec.get("stackup")
    if not isinstance(raw, list) or len(raw) < 2:
        raise ErcError(
            f"spec '{spec_path}' must have a 'stackup' array with at least "
            "two entries: the gate layer (stackup[0], role='gate') and at "
            "least one metal role above it"
        )

    entries: list[dict[str, Any]] = []
    names: list[str] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ErcError(f"spec '{spec_path}': stackup[{i}] must be a JSON object")
        _reject_unknown_keys(entry, _STACKUP_KEYS, spec_path, f"stackup[{i}]", ErcError)
        for key in ("name", "layer"):
            if key not in entry:
                raise ErcError(f"spec '{spec_path}': stackup[{i}] missing {key!r}")
        name = str(entry["name"])
        if name in names:
            raise ErcError(f"spec '{spec_path}': duplicate stackup name {name!r}")
        names.append(name)

        layer = _parse_layer_datatype(
            str(entry["layer"]), spec_path, f"stackup[{i}].layer", ErcError
        )
        label_layer = None
        if entry.get("label_layer") is not None:
            label_layer = _parse_layer_datatype(
                str(entry["label_layer"]),
                spec_path,
                f"stackup[{i}].label_layer",
                ErcError,
            )

        active_layer = None
        if entry.get("active_layer") is not None:
            if i != 0:
                raise ErcError(
                    f"spec '{spec_path}': stackup[{i}].active_layer is only "
                    "valid on stackup[0] (the gate role)"
                )
            active_layer = _parse_layer_datatype(
                str(entry["active_layer"]),
                spec_path,
                f"stackup[{i}].active_layer",
                ErcError,
            )

        role = entry.get("role")
        if role is not None and role != "gate":
            raise ErcError(
                f"spec '{spec_path}': stackup[{i}].role must be omitted or "
                f"'gate' (got {role!r})"
            )
        if i == 0 and role != "gate":
            raise ErcError(
                f'spec \'{spec_path}\': stackup[0] must set "role": "gate" '
                "-- the polysilicon/gate layer, first in fabrication order"
            )
        if i > 0 and role == "gate":
            raise ErcError(
                f"spec '{spec_path}': only stackup[0] may set \"role\": "
                f'"gate" (a second one was found at stackup[{i}])'
            )

        entries.append(
            {
                "name": name,
                "layer": layer,
                "label_layer": label_layer,
                "active_layer": active_layer,
                "role": role,
            }
        )

    return entries


def _validate_antenna_limits(
    spec: dict[str, Any], spec_path: str, stackup: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """The optional top-level ``antenna_limits`` spec key (issue #4090): a
    caller-supplied per-layer antenna-ratio limit, for a process with no
    built-in ``--pdk`` table. Each entry is ``{"layer": <stackup name>,
    "max_ratio": <finite number > 0>, "source": <non-empty citation>}``.

    ``layer`` must name a non-gate ``stackup`` entry, once. ``source`` is
    required so every limit stays cited, as the built-in tables are. The
    numbers are the caller's responsibility; none are vendored. Omitted or
    ``null`` -> ``[]`` (behaviour unchanged).
    """
    raw = spec.get("antenna_limits")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ErcError(f"spec '{spec_path}': 'antenna_limits' must be an array")
    gate_name = stackup[0]["name"]
    stackup_names = [entry["name"] for entry in stackup]
    seen: set[str] = set()
    entries: list[dict[str, Any]] = []
    for i, entry in enumerate(raw):
        where = f"antenna_limits[{i}]"
        parsed = _validate_antenna_limit_entry(
            entry, spec_path, where, stackup_names, gate_name
        )
        if parsed["layer"] in seen:
            raise ErcError(
                f"spec '{spec_path}': {where}.layer {parsed['layer']!r} "
                "duplicates an earlier antenna_limits entry"
            )
        seen.add(parsed["layer"])
        entries.append(parsed)
    return entries


def _validate_antenna_limit_entry(
    entry: Any,
    spec_path: str,
    where: str,
    stackup_names: list[str],
    gate_name: str,
) -> dict[str, Any]:
    """One ``antenna_limits[]`` entry: object shape, keys, then each field."""
    if not isinstance(entry, dict):
        raise ErcError(f"spec '{spec_path}': {where} must be a JSON object")
    _reject_unknown_keys(entry, _ANTENNA_LIMIT_KEYS, spec_path, where, ErcError)
    for key in _ANTENNA_LIMIT_KEYS:
        if key not in entry:
            raise ErcError(f"spec '{spec_path}': {where} missing {key!r}")
    layer = entry["layer"]
    if not isinstance(layer, str) or layer not in stackup_names:
        raise ErcError(
            f"spec '{spec_path}': {where}.layer {layer!r} is not a stackup "
            f"name (known: {', '.join(stackup_names)})"
        )
    if layer == gate_name:
        raise ErcError(
            f"spec '{spec_path}': {where}.layer {layer!r} is the gate "
            "role, which is never antenna-graded"
        )
    max_ratio = _validate_antenna_max_ratio(entry["max_ratio"], spec_path, where)
    source = entry["source"]
    if not isinstance(source, str) or not source.strip():
        raise ErcError(
            f"spec '{spec_path}': {where}.source must be a non-empty "
            "string citing where the limit comes from"
        )
    return {"layer": layer, "max_ratio": max_ratio, "source": source}


def _validate_antenna_max_ratio(max_ratio: Any, spec_path: str, where: str) -> float:
    if (
        isinstance(max_ratio, bool)
        or not isinstance(max_ratio, (int, float))
        or not math.isfinite(max_ratio)
        or max_ratio <= 0
    ):
        raise ErcError(
            f"spec '{spec_path}': {where}.max_ratio must be a finite "
            f"number > 0 (got {max_ratio!r})"
        )
    return float(max_ratio)


def _validate_vias(
    spec: dict[str, Any], spec_path: str, stackup_names: list[str]
) -> list[dict[str, Any]]:
    return [
        {"name": name, "layer": layer, "between": between}
        for _, _, name, layer, between in _validate_via_entries(
            spec, spec_path, stackup_names, ErcError
        )
    ]


def _parse_same_net_as(
    entry: dict[str, Any], spec_path: str, index: int, name: str
) -> str | None:
    """``nets[].same_net_as`` (optional, issue #2463): the name of another
    ``nets[]`` entry this one is *intentionally* the same electrical net as
    -- a sub-block's own port name and the assembly's name for the same
    deliberate conductor, the shape that otherwise had no expressible form
    (declare both names and ``erc.supply_short`` fires forever on a correct
    layout; declare one and the other name's conductor is uncovered).

    Omitted or ``null`` -> ``None``, the undeclared form, so a generator
    that emits the key unconditionally grades exactly as before. Anything
    but a string naming a *different* declared entry is rejected rather
    than ignored: a typo'd partner name that silently did nothing would
    suppress no false positive, check no tie, and still read as a
    declaration that was honoured -- the unfalsifiable pass shape this
    module refuses everywhere else. The cross-reference itself ("names an
    entry that exists") is checked by :func:`_validate_nets` once every
    name is known, so the key may name an entry declared later in the
    array as well as an earlier one (the relation is symmetric; it belongs
    on whichever entry reads more naturally)."""
    raw = entry.get("same_net_as")
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise ErcError(
            f"spec '{spec_path}': nets[{index}].same_net_as must be the "
            f"non-empty name of another declared nets[] entry (got {raw!r})"
        )
    other = raw.strip()
    if other == name:
        raise ErcError(
            f"spec '{spec_path}': nets[{index}].same_net_as must name a "
            f"different nets[] entry (got {other!r}, its own name)"
        )
    return other


def _validate_nets(
    spec: dict[str, Any], spec_path: str, stackup_names: list[str]
) -> list[dict[str, Any]]:
    """Validate the optional ``nets`` array (issue #861): named nets to
    check for connectivity findings (``erc.unconnected_net``,
    ``erc.multiply_driven_net``, ``erc.supply_short``). Mirrors ``klt
    power``'s ``power_nets`` name-matching convention, plus a ``kind`` used
    to classify a two-net short as a plain multiply-driven net vs. a supply
    short, (issue #2400) an ``islands`` expected electrical-island
    count the ``erc.unconnected_net`` check grades against instead of the
    implicit 1, and (issue #2463) a ``same_net_as`` naming another entry
    this one is intentionally one net with -- see
    :func:`_net_connectivity_findings` for why a declared count and a
    declared tie are the falsifiable forms -- and (issue #2510) the
    ``roles`` this net owns outright, over which the unlabelled remainder is
    measured (:func:`_parse_net_roles`). Omitted or empty -> no
    net-connectivity findings are computed
    (a caller who only wants the floating-gate check need not declare
    this)."""
    raw = spec.get("nets", [])
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise ErcError(f"spec '{spec_path}': 'nets' must be an array")

    entries: list[dict[str, Any]] = []
    names: list[str] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ErcError(f"spec '{spec_path}': nets[{i}] must be a JSON object")
        _reject_unknown_keys(entry, _NET_KEYS, spec_path, f"nets[{i}]", ErcError)
        if "name" not in entry:
            raise ErcError(f"spec '{spec_path}': nets[{i}] missing 'name'")
        name = str(entry["name"]).strip()
        if not name:
            raise ErcError(f"spec '{spec_path}': nets[{i}].name must be non-empty")
        if name in names:
            raise ErcError(f"spec '{spec_path}': duplicate net name {name!r}")
        names.append(name)

        kind = entry.get("kind", "signal")
        if kind not in ("signal", "supply"):
            raise ErcError(
                f"spec '{spec_path}': nets[{i}].kind must be 'signal' or "
                f"'supply' (got {kind!r})"
            )

        roles = _parse_net_roles(entry, spec_path, i, stackup_names)
        extractor_global = _parse_extractor_global(entry, spec_path, i)
        entries.append(
            {
                "name": name,
                # Issue #3745: a net named by the extractor, not by GDS text.
                "extractor_global": extractor_global,
                "kind": kind,
                "islands": _parse_net_islands(entry, spec_path, i),
                # Issue #2463: the declared intentional tie, cross-checked
                # against the declared names below (once they are all known).
                "same_net_as": _parse_same_net_as(entry, spec_path, i, name),
                # Issue #2510: the stackup roles this net owns outright.
                "roles": roles,
                # Issue #2524: the declared carve-out for legitimate
                # unlabelled conductor on those owned roles.
                "unlabelled_allowed_boxes": _parse_unlabelled_allowed_boxes(
                    entry, spec_path, i, roles
                ),
                # Issue #4374: excuse unlabelled clusters that drive a gate.
                "unlabelled_allow_device_connected": (
                    _parse_unlabelled_allow_device_connected(entry, spec_path, i, roles)
                ),
            }
        )

    _validate_same_net_refs(entries, names, spec_path)
    return entries


def _parse_extractor_global(entry: dict[str, Any], spec_path: str, index: int) -> bool:
    """``nets[].extractor_global`` (optional, issue #3745): the net is named
    by the extractor (a PDK deck's global substrate net), not by label text,
    so it is never label-resolved and ``erc.unconnected_net`` cannot grade
    it. Disclosed as ``inapplicable`` in ``erc_coverage`` rather than
    omitted from the spec. Meaningless with any key that presupposes label
    resolution, so those combinations are spec errors."""
    value = entry.get("extractor_global", False)
    if not isinstance(value, bool):
        raise ErcError(
            f"spec '{spec_path}': nets[{index}].extractor_global must be a "
            f"boolean (got {value!r})"
        )
    if value:
        for key in (
            "islands",
            "same_net_as",
            "roles",
            "unlabelled_allowed_boxes",
            "unlabelled_allow_device_connected",
        ):
            if entry.get(key) not in (None, [], {}, False):
                raise ErcError(
                    f"spec '{spec_path}': nets[{index}].extractor_global "
                    f"conflicts with nets[{index}].{key}: an extractor-global "
                    "net is not label-resolved, so it has no islands to count "
                    "or tie"
                )
    return value


def _parse_net_islands(entry: dict[str, Any], spec_path: str, index: int) -> int:
    """``nets[].islands`` (optional, issue #2400): the declared expected
    island count. Default 1 is the pre-#2400 model (one label string == one
    electrical net); a stream whose library PG pin names legitimately land
    on N separate domains declares N so ``erc.unconnected_net`` grades the
    count instead of silently failing every such name. ``islands: 1`` must
    behave exactly like the omitted form.

    Split out of :func:`_validate_nets` to keep that function under the
    repo's C901 complexity ratchet, as :func:`_parse_same_net_as` is."""
    islands = entry.get("islands", 1)
    if isinstance(islands, bool) or not isinstance(islands, int) or islands < 1:
        raise ErcError(
            f"spec '{spec_path}': nets[{index}].islands must be an integer "
            f">= 1 (got {entry.get('islands')!r})"
        )
    return islands


def _parse_net_roles(
    entry: dict[str, Any], spec_path: str, index: int, stackup_names: list[str]
) -> list[str]:
    """``nets[].roles`` (optional, issue #2510): the ``stackup`` role names
    this declared net **owns outright** -- the caller's assertion that every
    conductor shape drawn on each named role is meant to be part of this net
    (a dedicated supply-strap metal, a rail-only layer).

    That assertion is what makes the unlabelled remainder
    (:func:`_unlabelled_state`) measurable at all. A severed rail's
    orphan does not touch the labelled piece -- that is what "severed"
    means -- so nothing in the extracted graph associates it with this net;
    and an ordinary routing role carries many unrelated nets, so "every
    unlabelled shape on the layer" is only this net's orphan when the
    caller says the layer is this net's. The key is therefore opt-in: an
    entry that omits it reports the remainder as ``None`` (not measured),
    never as a guessed ``0``.

    Since issue #2524 that remainder also *gates*
    (``erc.unlabelled_conductor``), which makes this key the single switch
    controlling whether the new rule can fire for a net at all. Declaring it
    is therefore a real assertion with a real consequence -- and where the
    role legitimately carries unlabelled fill, the companion
    ``nets[].unlabelled_allowed_boxes``
    (:func:`_parse_unlabelled_allowed_boxes`) declares that away rather than
    forcing the role to be left unowned and unmeasured.

    Omitted/``null``/``[]`` -> ``[]``, the undeclared form. Anything else
    must be a list of distinct names of declared ``stackup`` entries --
    ``vias`` roles are rejected, since their graph index is not retained
    (see :func:`_island_entry`) and a via sits inside the two stackup shapes
    it joins anyway. A typo is a spec error rather than a silently
    unmeasured role, for the same reason :func:`_parse_same_net_as` rejects
    one."""
    raw = entry.get("roles")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ErcError(
            f"spec '{spec_path}': nets[{index}].roles must be an array of "
            f"stackup role names (got {raw!r})"
        )
    roles: list[str] = []
    for role in raw:
        if not isinstance(role, str) or role not in stackup_names:
            raise ErcError(
                f"spec '{spec_path}': nets[{index}].roles entry {role!r} is "
                f"not a declared stackup role (declared: {stackup_names})"
            )
        if role in roles:
            raise ErcError(
                f"spec '{spec_path}': nets[{index}].roles names {role!r} twice"
            )
        roles.append(role)
    return roles


def _parse_unlabelled_allowed_boxes(
    entry: dict[str, Any], spec_path: str, index: int, roles: list[str]
) -> list[tuple[float, float, float, float]]:
    """``nets[].unlabelled_allowed_boxes`` (optional, issue #2524): the
    regions on this net's owned ``roles`` where unlabelled conductor is
    *expected* -- dummy/fill, a deliberately floating shield, a seal-ring
    fragment -- as the same ``[left, bottom, right, top]`` micrometre box
    list ``ties[].tap_boxes``/``well_boxes`` take.

    This is the escape hatch that makes gating the unlabelled remainder
    (:func:`_unlabelled_state`, ``erc.unlabelled_conductor``) safe. ``roles``
    asserts the caller owns a layer outright; a layer that also carries
    legitimate fill makes that assertion *almost* true, and before this key
    the only two options were an unownable role (no measurement at all) or a
    permanent finding on a correct layout. Structurally it is #2183's
    ``devices[]`` carve-out applied one level up: the caller declares regions
    to subtract before the check runs, and what the declaration actually
    removed is echoed back under ``provenance.net_exclusions`` so a reviewer
    can audit it rather than infer it.

    Exclusion is per *island*, not per area: an unlabelled island survives
    the carve-out unless **all** of its geometry on the owned roles falls
    inside the declared boxes. A fill declaration that happens to overlap a
    real severed orphan therefore cannot silence it -- the deliberate
    conservative direction, since the whole point of the rule is to catch the
    orphan the connectivity graph cannot attribute.

    Omitted/``null``/``[]`` -> ``[]``. Declaring boxes on an entry with no
    ``roles`` is a spec error, not a no-op: there is no measurement for them
    to narrow, so such a declaration would read as honoured while doing
    nothing -- the same unfalsifiable shape :func:`_parse_net_roles` rejects
    a typo'd role name for."""
    boxes = _parse_um_boxes(
        entry, "unlabelled_allowed_boxes", spec_path, index, section="nets"
    )
    if boxes and not roles:
        raise ErcError(
            f"spec '{spec_path}': nets[{index}].unlabelled_allowed_boxes needs "
            "a non-empty nets[].roles to narrow -- without a declared owned "
            "role there is no unlabelled remainder to exclude anything from"
        )
    return boxes


def _parse_unlabelled_allow_device_connected(
    entry: dict[str, Any], spec_path: str, index: int, roles: list[str]
) -> bool:
    """``nets[].unlabelled_allow_device_connected`` (optional, issue #4374,
    default ``false``): when ``true``, unlabelled clusters that drive a gate
    (internal signal nets, ``kind: "device_connected"``) do not gate
    ``erc.unlabelled_conductor``; ``floating`` clusters still do. Like
    ``unlabelled_allowed_boxes`` it narrows the unlabelled measurement, so
    declaring it without ``roles`` is a spec error rather than a silent no-op."""
    value = entry.get("unlabelled_allow_device_connected", False)
    if not isinstance(value, bool):
        raise ErcError(
            f"spec '{spec_path}': nets[{index}].unlabelled_allow_device_connected "
            f"must be a boolean (got {value!r})"
        )
    if value and not roles:
        raise ErcError(
            f"spec '{spec_path}': nets[{index}].unlabelled_allow_device_connected "
            "needs a non-empty nets[].roles to narrow -- without a declared "
            "owned role there is no unlabelled remainder to excuse"
        )
    return value


def _validate_same_net_refs(
    entries: list[dict[str, Any]], names: list[str], spec_path: str
) -> None:
    """Every declared ``nets[].same_net_as`` (issue #2463) must name an
    entry that exists -- checked after the whole array is read, so the key
    may reference a name declared later as well as an earlier one."""
    for i, decl in enumerate(entries):
        other = decl["same_net_as"]
        if other is not None and other not in names:
            raise ErcError(
                f"spec '{spec_path}': nets[{i}].same_net_as names {other!r}, "
                "which is not a declared nets[] entry"
            )
        if other is not None and entries[names.index(other)]["extractor_global"]:
            raise ErcError(
                f"spec '{spec_path}': nets[{i}].same_net_as names {other!r}, "
                "which is declared extractor_global (not label-resolved)"
            )


def _parse_tie_layer_list(
    entry: dict[str, Any], key: str, spec_path: str, index: int
) -> list[tuple[int, int]]:
    """One ``ties[]`` entry's ``["<layer>/<datatype>", ...]`` list under
    ``key`` -- the shared parser behind ``tap_requires`` (issue #2169,
    :func:`_parse_tap_requires`) and the well-side ``well_requires`` /
    ``well_excludes`` (issue #2339, :func:`_parse_well_selection`), which
    take the identical plain-layer-list shape and differ only in what the
    layers *mean*. The layer-list counterpart of :func:`_parse_um_boxes`.

    Omitted/``null`` -> ``[]``. Every entry is validated by
    :func:`~klayout_tools._paths._parse_layer_datatype` rather than coerced:
    a silently-dropped layer would weaken a narrowing the caller believes
    they declared.
    """
    raw = entry.get(key, [])
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise ErcError(
            f"spec '{spec_path}': ties[{index}].{key} must be an array "
            "of '<layer>/<datatype>' strings"
        )
    return [
        _parse_layer_datatype(
            str(value), spec_path, f"ties[{index}].{key}[{j}]", ErcError
        )
        for j, value in enumerate(raw)
    ]


def _parse_cell_names(
    entry: dict[str, Any], key: str, spec_path: str, index: int
) -> list[str]:
    """One ``ties[]`` entry's array of cell names under ``key`` (issue
    #4373: ``well_requires_cells`` / ``well_excludes_cells``). Omitted/``null``
    -> ``[]``. Every entry must be a non-empty string; whether the name exists
    in the stream is checked at run time against the layout, where it is
    known (:func:`~klayout_tools.erc._cells_region`)."""
    raw = entry.get(key, [])
    if raw is None:
        raw = []
    if not isinstance(raw, list) or not all(
        isinstance(name, str) and name for name in raw
    ):
        raise ErcError(
            f"spec '{spec_path}': ties[{index}].{key} must be an array of "
            "non-empty cell-name strings"
        )
    return list(raw)


def _parse_tap_requires(
    entry: dict[str, Any], spec_path: str, index: int
) -> list[tuple[int, int]]:
    """``ties[].tap_requires`` (optional, issue #2169): the extra layers
    intersected into this tie's ``tap_layer`` to derive the real tap --
    ``{"tap_layer": "22/0", "tap_requires": ["32/0"]}`` is ``Comp ∩
    Nplus``. Omitted/``null`` -> ``[]`` (``tap_layer`` alone, the
    pre-#2169 behaviour). Split out of :func:`_validate_ties` to keep that
    function under the repo's C901 complexity ratchet."""
    return _parse_tie_layer_list(entry, "tap_requires", spec_path, index)


def _parse_tap_excludes(
    entry: dict[str, Any], spec_path: str, index: int
) -> list[tuple[int, int]]:
    """``ties[].tap_excludes`` (optional, issue #4194): layers *subtracted*
    from this tie's ``tap_layer`` -- the mirror of :func:`_parse_tap_requires`
    for a tap defined by the **absence** of a marker, e.g. an n-well tap that
    is ``active - pplus`` on a process drawing no n+ implant. Omitted/``null``
    -> ``[]``."""
    return _parse_tie_layer_list(entry, "tap_excludes", spec_path, index)


def _parse_tap_is_dedicated(entry: dict[str, Any], spec_path: str, index: int) -> bool:
    """``ties[].tap_is_dedicated`` (optional, issue #2199): the spec's
    affirmation that ``tap_layer`` names a layer drawn **only** for taps --
    sky130's own ``tap`` (65/44), a PDK tub-contact marker -- so no
    ``tap_requires`` narrowing is needed for the derived tap to be a real
    tap.

    It is not a hint and it changes no geometry: it is the one declarative
    way to say "there is nothing to narrow here", which is what keeps such
    a tie *checked* work instead of the skipped-degenerate classification
    :func:`_degenerate_tie_reasons` otherwise assigns. Omitted/``null`` ->
    ``False``. Anything but a JSON boolean is rejected rather than coerced:
    a truthy ``"false"`` string quietly asserting the opposite of what it
    reads is exactly the silent-pass shape issue #2199 is about.

    Split out of :func:`_validate_ties` to keep that function under the
    repo's C901 complexity ratchet, as :func:`_parse_tap_requires` is.
    """
    raw = entry.get("tap_is_dedicated", False)
    if raw is None:
        return False
    if not isinstance(raw, bool):
        raise ErcError(
            f"spec '{spec_path}': ties[{index}].tap_is_dedicated must be true or false"
        )
    return raw


def _parse_um_boxes(
    entry: dict[str, Any],
    key: str,
    spec_path: str,
    index: int,
    section: str = "ties",
) -> list[tuple[float, float, float, float]]:
    """One spec entry's ``[left, bottom, right, top]`` micrometre box list
    under ``key`` -- the shared parser behind ``ties[].tap_boxes`` (issue
    #2234, :func:`_parse_tap_boxes`), ``ties[].well_boxes`` (issue #2255,
    :func:`_parse_well_boxes`), ``ties[].well_requires_boxes`` /
    ``ties[].well_excludes_boxes`` (issue #2540, :func:`_parse_well_selection`)
    and ``nets[].unlabelled_allowed_boxes`` (issue #2524,
    :func:`_parse_unlabelled_allowed_boxes`), which take the identical
    literal-geometry shape and differ only in what the boxes *mean*.

    ``[left, bottom, right, top]`` is the same tuple
    :func:`~klayout_tools._layout.clip_box` already converts for ``klt
    clip``/``components.py``. Omitted/``null`` -> ``[]``. Every box is
    validated rather than coerced: a 3-element array, a non-number, a bool
    (JSON ``true`` is not a coordinate), or an inverted/degenerate extent is
    a spec error, since a silently-dropped box would weaken an assertion the
    caller believes they made.

    ``section`` names the spec array the entry came from, so the error
    message points at the caller's own key path (``ties[2].tap_boxes[0]`` vs.
    ``nets[1].unlabelled_allowed_boxes[0]``) rather than at whichever array
    happened to be the first user of this parser.
    """
    raw = entry.get(key, [])
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise ErcError(
            f"spec '{spec_path}': {section}[{index}].{key} must be an array of "
            "[left, bottom, right, top] micrometre boxes"
        )
    boxes: list[tuple[float, float, float, float]] = []
    for j, value in enumerate(raw):
        field = f"{section}[{index}].{key}[{j}]"
        if (
            not isinstance(value, list)
            or len(value) != 4
            or not all(is_number(v) for v in value)
        ):
            raise ErcError(
                f"spec '{spec_path}': {field} must be a 4-number "
                "[left, bottom, right, top] array"
            )
        left, bottom, right, top = (float(v) for v in value)
        if left >= right or bottom >= top:
            raise ErcError(
                f"spec '{spec_path}': {field} must have left < right and bottom < top"
            )
        boxes.append((left, bottom, right, top))
    return boxes


def _parse_tap_boxes(
    entry: dict[str, Any], spec_path: str, index: int
) -> list[tuple[float, float, float, float]]:
    """``ties[].tap_boxes`` (optional, issue #2234): a caller *assertion* of
    where the tap geometry is, as a list of ``[left, bottom, right, top]``
    micrometre boxes intersected into ``tap_layer``. Composes with
    ``tap_requires`` (both narrow the same region) but, unlike it, needs no
    PDK marker layer to exist at all: a stream whose taps are genuinely
    drawn but carry no distinguishing implant/marker layer can point
    directly at them instead. Omitted/``null`` -> ``[]`` (no assertion,
    pre-#2234 behaviour, byte-identical). Split out of :func:`_validate_ties`
    to keep that function under the repo's C901 complexity ratchet, as
    :func:`_parse_tap_requires` is.
    """
    return _parse_um_boxes(entry, "tap_boxes", spec_path, index)


def _parse_well_boxes(
    entry: dict[str, Any], spec_path: str, index: int
) -> list[tuple[float, float, float, float]]:
    """``ties[].well_boxes`` (optional, issue #2255): a caller *assertion*
    of the substrate/well region this tie is about, for a block that draws
    no well/tub layer for it to name -- the same ``[left, bottom, right,
    top]`` micrometre box list ``tap_boxes`` takes, and deliberately so, but
    a fundamentally different kind of claim.

    ``tap_boxes`` **narrows** a required, drawn ``tap_layer``: it composes
    with the geometry, and the geometry is still what is measured. This
    **substitutes** for a required drawn layer, so the region it names is
    not corroborated by anything in the stream at all. That is why it is
    valid only with ``well_layer: null`` (:func:`_parse_well_declaration`),
    why it is graded under its own coverage classification rather than
    folded into ``checked_by_assertion``, and why it carries its own
    degeneracy test (:func:`_degenerate_tie_reasons`).
    """
    return _parse_um_boxes(entry, "well_boxes", spec_path, index)


def _parse_well_complement_of(
    entry: dict[str, Any], spec_path: str, index: int
) -> list[tuple[int, int]]:
    """``ties[].well_complement_of`` (optional, issue #3750): the substrate
    half of a tie for a process whose bulk has no drawn well/tub shape -- a
    ``"<layer>/<datatype>"`` string or a non-empty array of them. The tie's
    region is the top-cell extent minus the merged union of those layers
    (see :func:`~klayout_tools.erc._tie_well_region`), so it follows the
    layout instead of going stale like a hand-kept ``well_boxes`` list.

    Omitted/``null`` -> ``[]``. Valid only with ``well_layer: null``
    (:func:`_parse_well_declaration`).
    """
    raw = entry.get("well_complement_of")
    if raw is None:
        return []
    values = [raw] if isinstance(raw, str) else raw
    if not isinstance(values, list) or not values:
        raise ErcError(
            f"spec '{spec_path}': ties[{index}].well_complement_of must be a "
            "'<layer>/<datatype>' string or a non-empty array of them"
        )
    return [
        _parse_layer_datatype(
            str(value), spec_path, f"ties[{index}].well_complement_of[{j}]", ErcError
        )
        for j, value in enumerate(values)
    ]


def _parse_well_selection(
    entry: dict[str, Any], spec_path: str, index: int, drawn_well: bool
) -> dict[str, Any]:
    """The four **well-side class selectors** on one ``ties[]`` entry: which
    merged shapes of a drawn ``well_layer`` this tie is about.

    Two spellings of the same operation, and they compose:

    - ``well_requires`` / ``well_excludes`` (optional, issue #2339) -- marker
      *layers*. A shape is kept when it interacts with geometry on every
      ``well_requires`` layer and with none of the ``well_excludes`` layers.
    - ``well_requires_boxes`` / ``well_excludes_boxes`` (optional, issue
      #2540) -- literal ``[left, bottom, right, top]`` micrometre *boxes*, the
      same shape ``tap_boxes``/``well_boxes`` take. A shape is kept when it
      interacts with the union of ``well_requires_boxes`` and with none of the
      ``well_excludes_boxes`` union.
    - ``well_requires_cells`` / ``well_excludes_cells`` (optional, issue
      #4373) -- *cell names*. A shape is kept when it interacts with the
      ``well_layer`` geometry drawn directly in instances of the
      ``well_requires_cells`` cells and with none of the
      ``well_excludes_cells`` geometry, evaluated in each instance's
      transformed frame (see :func:`~klayout_tools.erc._cells_region`).

    All six exist for the one thing the tap side cannot express: a single
    drawn tub layer carrying two differently-biased well classes (device-body
    wells on one rail, a vertical bipolar's base tub on the other). Each class
    is declared as its own ``ties[]`` entry with its own ``net``.

    The **layer** pair needs a PDK layer that separates the classes, which is
    the ordinary case (a device marker over the special tub) but not the only
    one: a PDK with a single n-tub layer and no per-class marker draws nothing
    that distinguishes them, and on such a stream every marker-driven
    selection keeps every shape or none -- degenerate either way (see
    :data:`REASON_DEGENERATE_WELL_SELECTION`). The **box** pair is the
    literal-geometry escape hatch for exactly that stream, and the well-side
    counterpart of ``tap_boxes`` (issue #2234), which is the same escape hatch
    for a tap with no distinguishing implant. It needs no PDK marker layer to
    exist at all: the caller points at the coordinates of the shapes their
    class owns.

    All four require a **drawn** ``well_layer``: there is nothing to select
    from on a tie whose well region is itself a caller assertion
    (``well_layer: null`` + ``well_boxes``, issue #2255) -- the asserted boxes
    already *are* exactly the region the caller means, box by box, so a second
    narrowing on top of them, layer- or box-driven, would be an unstated claim
    about geometry that corroborates nothing. Rejected rather than silently
    ignored, for the same reason :func:`_parse_um_boxes` rejects a malformed
    box.

    Returns the six parsed values as a dict, merged into the tie entry by
    :func:`_parse_well_declaration`. Split out of that function to keep it
    under the repo's C901 complexity ratchet, as :func:`_parse_tap_requires`
    is split out of :func:`_validate_ties`.
    """
    selection: dict[str, Any] = {
        "well_requires": _parse_tie_layer_list(
            entry, "well_requires", spec_path, index
        ),
        "well_excludes": _parse_tie_layer_list(
            entry, "well_excludes", spec_path, index
        ),
        "well_requires_boxes": _parse_um_boxes(
            entry, "well_requires_boxes", spec_path, index
        ),
        "well_excludes_boxes": _parse_um_boxes(
            entry, "well_excludes_boxes", spec_path, index
        ),
        "well_requires_cells": _parse_cell_names(
            entry, "well_requires_cells", spec_path, index
        ),
        "well_excludes_cells": _parse_cell_names(
            entry, "well_excludes_cells", spec_path, index
        ),
    }
    named = next((key for key, value in selection.items() if value), None)
    if not drawn_well and named is not None:
        raise ErcError(
            f"spec '{spec_path}': ties[{index}].{named} selects among the "
            f"drawn shapes of a well layer, so ties[{index}].well_layer must "
            "name one (got null); an asserted substrate region already names "
            "exactly the region it claims, in 'well_boxes' or "
            "'well_complement_of'"
        )
    return selection


def _parse_well_declaration(
    entry: dict[str, Any], spec_path: str, index: int
) -> dict[str, Any]:
    """One ``ties[]`` entry's well declaration -- the ``well_layer`` /
    ``well_boxes`` keys, of which exactly one is populated (issue #2255), plus
    the four well-side class selectors that ride on the first
    (:func:`_parse_well_selection`).

    Two mutually exclusive forms for *where the well is*, because they are
    different evidence and conflating them would let an assertion hide behind
    drawn geometry:

    - ``"well_layer": "<layer>/<datatype>"`` (every spec before #2255) --
      the drawn well/tub layer, each of whose merged shapes is checked
      independently. ``well_boxes`` alongside it is rejected rather than
      silently applied: a box list over a drawn well is never a statement
      about where the well *is* -- it is either a tap narrowing (``tap_boxes``
      expresses that, better) or a statement about *which of the layer's
      shapes* this entry is about, which since issue #2540 has its own keys
      (``well_requires_boxes``/``well_excludes_boxes``) whose grading says so.
      The error message names both, so the rejection points at a reachable
      state rather than only at the marker-layer selectors a stream with no
      separating marker cannot use.
    - ``"well_layer": null`` + a non-empty ``well_boxes`` -- the
      native-substrate form: nothing is drawn, so the caller names the
      region they are claiming.

    The selectors (``well_requires``/``well_excludes``, issue #2339;
    ``well_requires_boxes``/``well_excludes_boxes``, issue #2540) ride on the
    *drawn* form only: they pick which of that layer's merged shapes this tie
    is about, so a single tub layer holding two differently-biased well
    classes can be declared as two complementary entries instead of one entry
    that reports every well of the other class as untied. They are a
    narrowing, never a substitution, so unlike ``well_boxes`` they compose
    with a drawn ``well_layer`` -- they *require* one.

    ``well_layer: null`` with no ``well_boxes`` is an error, not a tie that
    quietly checks nothing: "there is no well to name and I am not asserting
    one" is *exactly* the state a spec already expresses by omitting the
    ``ties[]`` entry and disclosing it with ``ties_disclosure`` -- and
    unlike this, that route cannot be mistaken for a graded check. The key
    itself stays required (an entry that omits ``well_layer`` altogether
    still raises the same "missing 'well_layer'" error it always has): the
    absence of a drawn well must be *declared*, never inferred from a typo.

    Split out of :func:`_validate_ties` to keep that function under the
    repo's C901 complexity ratchet, as :func:`_parse_tap_requires` is.
    """
    well_boxes = _parse_well_boxes(entry, spec_path, index)
    complement = _parse_well_complement_of(entry, spec_path, index)
    raw = entry["well_layer"]
    declaration: dict[str, Any] = {
        "well_boxes": well_boxes,
        "well_complement_of": complement,
        **_parse_well_selection(entry, spec_path, index, raw is not None),
    }
    if raw is None:
        if well_boxes and complement:
            raise ErcError(
                f"spec '{spec_path}': ties[{index}] declares both well_boxes "
                "and well_complement_of; they are alternative ways to name "
                "the substrate region, so give exactly one"
            )
        if not well_boxes and not complement:
            raise ErcError(
                f"spec '{spec_path}': ties[{index}].well_layer is null, so "
                f"ties[{index}].well_boxes must assert the substrate region "
                "this tie covers (a non-empty array of [left, bottom, right, "
                f"top] micrometre boxes) or ties[{index}].well_complement_of "
                "must name the well layer(s) the substrate is the complement "
                "of; to declare no tie at all, omit the "
                "entry and state why in the top-level 'ties_disclosure'"
            )
        return {"well_layer": None, **declaration}
    if complement:
        raise ErcError(
            f"spec '{spec_path}': ties[{index}].well_complement_of derives a "
            f"substrate region for a tie with no drawn well, so "
            f"ties[{index}].well_layer must be null (got {str(raw)!r}); it "
            "cannot be combined with a drawn well_layer or with well_boxes"
        )
    if well_boxes:
        raise ErcError(
            f"spec '{spec_path}': ties[{index}].well_boxes asserts a substrate "
            f"region for a tie with no drawn well, so ties[{index}].well_layer "
            f"must be null (got {str(raw)!r}); to narrow a drawn well's taps, "
            "use 'tap_boxes', and to select which of its shapes this tie is "
            "about, 'well_requires_boxes'/'well_excludes_boxes' (literal "
            "geometry, for a tub whose classes no drawn layer separates) or "
            "'well_requires'/'well_excludes' (a marker layer that does)"
        )
    return {
        "well_layer": _parse_layer_datatype(
            str(raw), spec_path, f"ties[{index}].well_layer", ErcError
        ),
        **declaration,
    }


def _validate_ties(
    spec: dict[str, Any], spec_path: str, stackup_names: list[str]
) -> list[dict[str, Any]]:
    """Validate the optional ``ties`` array (issue #861): substrate/well
    tie declarations for the ``erc.missing_tie`` check. Each entry names a
    well/tub layer and the tap (contact) layer expected inside it, the
    ``stackup`` role the tap must be wired up to, and the supply net that
    tap must ultimately reach. Omitted or empty -> no missing-tie findings
    are computed.

    ``tap_requires`` (optional array of ``"<layer>/<datatype>"``, issue
    #2169) intersects further layers into the tap, so the spec can express
    the *boolean* a real PDK tap is drawn as -- ``Comp ∩ Nplus`` inside an
    n-well, ``Comp ∩ Pplus`` inside a p-well -- rather than a single layer
    that is either an implant (not a conductor) or a diffusion/cut shared
    with every source/drain in the same well. Same "second plain layer
    field, intersected at use time" shape as ``stackup[0].active_layer``
    (issue #1979), generalised to a list; omitted -> ``tap_layer`` alone,
    unchanged.

    ``tap_is_dedicated`` (optional boolean, issue #2199) is the other way
    to say the same thing for a PDK that draws taps on their own layer:
    nothing needs narrowing because the layer is already tap-only. A tie
    that declares neither is *degenerate* -- see
    :func:`_degenerate_tie_reasons` -- and is graded as skipped rather than
    checked work.

    ``tap_boxes`` (optional array of ``[left, bottom, right, top]``
    micrometre boxes, issue #2234) is a third, caller-*asserted* way: rather
    than narrowing ``tap_layer`` by a PDK marker layer's boolean, the spec
    names the tap geometry directly. For a stream that draws taps with no
    distinguishing marker at all -- neither ``tap_requires`` nor
    ``tap_is_dedicated`` has anything true to narrow or affirm -- this is
    the only way to declare a tie that is not degenerate. Graded under its
    own coverage classification (:func:`_connectivity_coverage`'s
    ``checked_by_assertion``) rather than folded into an ordinary
    geometrically-derived pass, and held to the same falsifiability test as
    every other narrowing form (:func:`_degenerate_tie_reasons`): an
    assertion that removes nothing from the drawn ``tap_layer`` is still
    degenerate.

    ``well_boxes`` (optional array of the same micrometre boxes, issue
    #2255) is the *well*-side counterpart, and the one thing on this entry
    that substitutes for a required field rather than narrowing one: with
    ``"well_layer": null``, it asserts the substrate region of a block that
    draws no well/tub layer at all (a native-substrate, NMOS-in-bulk
    block), which before this could not declare a substrate tie in any
    form. Exactly one of the two well forms may be given -- see
    :func:`_parse_well_declaration` -- and an assertion covering the whole
    top-cell extent is graded as skipped work, not as evidence (see
    :func:`_degenerate_tie_reasons`).

    ``well_requires``/``well_excludes`` (optional arrays of
    ``"<layer>/<datatype>"``, issue #2339) narrow the *drawn* well the way
    ``tap_requires`` narrows the tap, but by **selecting whole merged
    shapes** of ``well_layer`` rather than intersecting it: a shape is kept
    when it interacts with geometry on every ``well_requires`` layer and
    with none of the ``well_excludes`` layers. That is what lets one tub
    layer holding two differently-biased well classes be declared as two
    complementary entries, each with its own ``net``, instead of one entry
    that reports every correctly-tapped well of the other class as
    ``erc.missing_tie``. Valid only with a drawn ``well_layer`` (see
    :func:`_parse_well_selection`), and held to the same falsifiability bar
    as every other narrowing here: a selection keeping every drawn shape, or
    none of them, is graded as skipped work (see
    :func:`_degenerate_tie_reasons`).

    ``well_requires_boxes``/``well_excludes_boxes`` (optional arrays of the
    same ``[left, bottom, right, top]`` micrometre boxes, issue #2540) select
    the same way from literal geometry instead of from a marker layer -- the
    well-side counterpart of ``tap_boxes``, and for the same reason it exists
    on the tap side: the marker-layer form above needs the PDK to draw a layer
    that separates the classes, and a single-tub PDK with no per-class marker
    draws none, leaving a deliberately two-class tub undeclarable in any
    non-degenerate form. They compose with the marker form (all four narrow
    the same selection), ride on the same drawn-``well_layer`` requirement,
    and are graded by the same degeneracy test. Because the *which shapes*
    half of such a tie rests on the caller's word, a tie graded through one is
    additionally named in ``erc_coverage.checked_by_well_assertion`` (see
    :func:`_connectivity_coverage`), which a purely marker-driven selection
    stays out of."""
    raw = spec.get("ties", [])
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise ErcError(f"spec '{spec_path}': 'ties' must be an array")

    entries: list[dict[str, Any]] = []
    names: list[str] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ErcError(f"spec '{spec_path}': ties[{i}] must be a JSON object")
        _reject_unknown_keys(entry, _TIE_KEYS, spec_path, f"ties[{i}]", ErcError)
        for key in ("well_layer", "tap_layer", "connect_to", "net"):
            if key not in entry:
                raise ErcError(f"spec '{spec_path}': ties[{i}] missing {key!r}")

        name = str(entry.get("name", f"tie{i}"))
        if name in names:
            raise ErcError(f"spec '{spec_path}': duplicate tie name {name!r}")
        names.append(name)

        well = _parse_well_declaration(entry, spec_path, i)
        tap_layer = _parse_layer_datatype(
            str(entry["tap_layer"]), spec_path, f"ties[{i}].tap_layer", ErcError
        )

        tap_requires = _parse_tap_requires(entry, spec_path, i)
        tap_excludes = _parse_tap_excludes(entry, spec_path, i)
        tap_is_dedicated = _parse_tap_is_dedicated(entry, spec_path, i)
        tap_boxes = _parse_tap_boxes(entry, spec_path, i)

        connect_to = str(entry["connect_to"])
        if connect_to not in stackup_names:
            raise ErcError(
                f"spec '{spec_path}': ties[{i}].connect_to must name a "
                f"'stackup' entry (got {connect_to!r})"
            )

        net = str(entry["net"]).strip()
        if not net:
            raise ErcError(f"spec '{spec_path}': ties[{i}].net must be non-empty")

        entries.append(
            {
                "name": name,
                **well,
                "tap_layer": tap_layer,
                "tap_requires": tap_requires,
                "tap_excludes": tap_excludes,
                "tap_is_dedicated": tap_is_dedicated,
                "tap_boxes": tap_boxes,
                "connect_to": connect_to,
                "net": net,
            }
        )
    return entries


def _validate_undeclared_classes(
    raw: Any, spec_path: str, tie_names: list[str]
) -> list[str]:
    """Validate ``ties_disclosure.undeclared_classes`` (issue #2541): the
    names of the tie classes this spec discloses it does *not* declare.

    Held to the same "present but malformed is a spec error" bar as every
    other optional key in this module, plus one check the others have no
    analogue for: a name that a ``ties[]`` entry already declares is
    **rejected**, because the same class cannot be both real checked work
    and disclosed-as-undeclarable. That contradiction would otherwise land
    the same ``erc.missing_tie:["<class>"]`` identity in two coverage
    categories at once, which the common coverage contract forbids
    outright -- so catching it here turns an internal invariant failure into
    the spec error it actually is.

    An empty array is rejected for the same reason an empty ``reason`` is:
    it is the unfalsifiable "disclosed nothing" shape the disclosure keys
    exist to rule out. Omitted/``null`` -> ``[]`` (every pre-#2541 spec's
    report stays byte-identical)."""
    if not isinstance(raw, list) or not raw:
        raise ErcError(
            f"spec '{spec_path}': ties_disclosure.undeclared_classes must be "
            "a non-empty array of class names"
        )
    names: list[str] = []
    for i, item in enumerate(raw):
        if not isinstance(item, str) or not item.strip():
            raise ErcError(
                f"spec '{spec_path}': ties_disclosure.undeclared_classes[{i}] "
                "must be a non-empty string"
            )
        name = item.strip()
        if name in names:
            raise ErcError(
                f"spec '{spec_path}': duplicate ties_disclosure."
                f"undeclared_classes entry {name!r}"
            )
        if name in tie_names:
            raise ErcError(
                f"spec '{spec_path}': ties_disclosure.undeclared_classes "
                f"names {name!r}, which ties[] already declares -- a class "
                "cannot be both declared work and a disclosed non-declaration"
            )
        names.append(name)
    return names


def _validate_ties_disclosure(
    spec: dict[str, Any], spec_path: str, tie_names: list[str] | None = None
) -> dict[str, Any] | None:
    """The optional top-level ``ties_disclosure`` spec key (issue #2234): a
    caller's explicit statement that this run declares no tap, and why --
    ``{"reason": "<non-empty string>", "kind": "<kind>"}``. Omitted/``null``
    -> ``None`` (pre-#2234 behaviour, unchanged).

    This changes no geometry and no finding: :func:`run_erc` computes
    exactly the same ``erc_findings`` whether or not it is given. What it
    changes is the ``erc_coverage.inapplicable`` reason recorded for the
    undeclared ``erc.missing_tie`` work when ``ties`` is empty
    (:func:`_ties_disclosure_reason`, in place of
    :data:`REASON_NO_TIES_DECLARED`, see :func:`_connectivity_coverage`), so
    a consumer -- `klt signoff`'s T1 item 11 in particular, see
    `docs/design-evidence-tiers.md` -- can distinguish a considered,
    disclosed omission from "nobody declared ties", which previously
    rendered identically. A non-empty ``ties`` declaration alongside a
    disclosure is not rejected (a spec may express some taps and disclose
    the rest), but the disclosure only ever affects the *undeclared* work's
    reason.

    ``undeclared_classes`` (optional array of names, issue #2541) is what
    gives that undeclared work an identity when ``ties`` is **not** empty.
    Before it, the promise in the paragraph above held only for spec
    *acceptance*: a partial declaration was accepted, but "the undeclared
    work" existed nowhere in the report, so the disclosure's reason token
    never reached ``erc_coverage`` and a considered partial declaration
    rendered byte-identically to one that never considered the second class
    at all. Each name becomes one ``erc_coverage.inapplicable`` entry --
    ``erc.missing_tie:["<name>"]``, carrying the same disclosed reason token
    the kind selects (:func:`_connectivity_coverage`). The names are the
    caller's own vocabulary for the classes they could not declare (e.g.
    ``"p_substrate"``); nothing verifies that such a class exists, for the
    same reason nothing verifies ``reason`` -- but a name that collides with
    a declared ``ties[]`` entry *is* rejected, since that spec contradicts
    itself (:func:`_validate_undeclared_classes`).

    ``kind`` (optional, issue #2247) says which obstacle is being disclosed,
    because the two have different remedies and a reader of the report of
    record should not have to guess which one to go fix:

    - :data:`DISCLOSURE_KIND_UNEXPRESSIBLE` (the default, and the only
      meaning #2234 had) -- there is no tap to name: no narrowing marker, no
      dedicated tap layer, no nameable tap geometry. Cleared by drawing
      something, not by a newer `klt`.
    - :data:`DISCLOSURE_KIND_TOOL_LIMITATION` -- the tap *is* expressible,
      but on the `klt` build this evidence has to be produced with, a
      declared tie is not safely gradeable (issue #2169's unisolated tie
      extraction turning a correct ``ties[]`` into a false
      ``erc.supply_short`` is the reported instance). Cleared by a different
      build, not by different geometry.

    **Neither kind is verified by this run, and neither can be.** A
    disclosure is the caller's word about their own stream or their own
    toolchain; that is exactly why it never moves a finding and why item 11
    stays ``"unmet"`` on both. What the report *does* pin down is the build
    that produced it -- ``provenance.klt_version`` -- so a reader can check
    for themselves whether a disclosed tool limitation applies to the run in
    front of them.

    Validated the same strict way every other optional boolean/string spec
    key in this module is (:func:`_parse_tap_is_dedicated`): present but
    malformed is a spec error, not silently coerced or ignored -- an empty
    or missing ``reason``, or a ``kind`` outside the two above, would be
    exactly the unfalsifiable "disclosed nothing" shape this key exists to
    rule out. ``kind`` is echoed only when the spec actually declared it, so
    every pre-#2247 spec's report stays byte-identical.
    """
    raw = spec.get("ties_disclosure")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ErcError(f"spec '{spec_path}': 'ties_disclosure' must be a JSON object")
    _reject_unknown_keys(
        raw, _TIES_DISCLOSURE_KEYS, spec_path, "ties_disclosure", ErcError
    )
    reason = raw.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ErcError(
            f"spec '{spec_path}': ties_disclosure.reason must be a non-empty string"
        )
    disclosure: dict[str, Any] = {"reason": reason.strip()}
    if "kind" in raw and raw["kind"] is not None:
        kind = raw["kind"]
        if not isinstance(kind, str) or kind not in _TIES_DISCLOSURE_COVERAGE_REASON:
            allowed = ", ".join(sorted(_TIES_DISCLOSURE_COVERAGE_REASON))
            raise ErcError(
                f"spec '{spec_path}': ties_disclosure.kind must be one of "
                f"{allowed} (got {kind!r})"
            )
        disclosure["kind"] = kind
    if raw.get("undeclared_classes") is not None:
        disclosure["undeclared_classes"] = _validate_undeclared_classes(
            raw["undeclared_classes"], spec_path, tie_names or []
        )
    return disclosure
