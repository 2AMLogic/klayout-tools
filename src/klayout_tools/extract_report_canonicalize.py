"""Canonical comparison and connectivity hashes for serialized extract reports."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

# `\$<n>` -- the escaped spelling `spice_safe_net_name` gives every anonymous
# net (issue #1162) wherever a net name enters the JSON report
# (`nets[].name`, `devices[].nets[...]`, `parasitics.nets[].net`/`hub_net`,
# etc.) -- the bookkeeping spelling issue #1559's field-class table
# (`docs/cli/extract.md`) documents as not a stable contract across builds.
_ANONYMOUS_NET_NAME_RE = re.compile(r"^\\\$\d+$")
# The unanchored-at-the-end form of the above: the longest `\$<digits>` prefix
# of a string leaf, which is the only prefix :func:`_rewrite_net_name` can
# possibly rename (issue #3568).
_ANONYMOUS_NET_PREFIX_RE = re.compile(r"^\\\$\d+")


def _net_attachment_index(devices: list[Any]) -> dict[str, list[str]]:
    """``{net name: [<device>.<terminal>, ...]}`` for every string-named net a
    ``devices[]`` terminal touches, built in one pass so
    :func:`_net_attachment_key` is a dict lookup instead of a rescan of every
    device per net (issue #3568). Duplicates are kept: one device with two
    terminals on the same net contributes two entries."""
    index: dict[str, list[str]] = {}
    for device in devices:
        if not isinstance(device, dict):
            continue
        for terminal, terminal_net in (device.get("nets") or {}).items():
            if isinstance(terminal_net, str):
                index.setdefault(terminal_net, []).append(
                    f"{device.get('name')}.{terminal}"
                )
    return index


def _net_attachment_key(
    net_name: str,
    devices: list[Any],
    index: Mapping[str, list[str]] | None = None,
) -> str:
    """The sorted ``<device>.<terminal>`` attachment list for ``net_name``,
    read from this same report's already-built ``devices[]`` array -- the
    structural identity issue #1559 normalizes an anonymous net's unstable
    ``\\$<n>`` spelling to, mirroring the "resolve by structural identity,
    not the raw counter" convention in
    :func:`klayout_tools.extract_parasitics._net_identity_name`
    and the extraction driver's ``net_id``-keyed lookups
    for the *live* ``kdb.Net``/``kdb.Device`` objects, applied here instead
    to the already-serialized JSON report. Returns ``""`` for a net with no
    device terminal at all (real floating geometry, or a synthesized
    ground/substrate net) -- nothing to disambiguate by, so the caller
    leaves that net's original spelling untouched rather than fabricate a
    key that cannot be trusted to be unique.

    ``index`` (from :func:`_net_attachment_index` over the same ``devices``)
    makes the call O(attachments of this net) rather than O(all terminals).
    """
    if index is not None and isinstance(net_name, str):
        return ",".join(sorted(index.get(net_name, ())))
    attachments = sorted(
        f"{device.get('name')}.{terminal}"
        for device in devices
        if isinstance(device, dict)
        for terminal, terminal_net in (device.get("nets") or {}).items()
        if terminal_net == net_name
    )
    return ",".join(attachments)


def _anonymous_net_name_map(report: Mapping[str, Any]) -> dict[str, str]:
    """Build the ``{raw \\$<n> spelling: canonical structural name}`` map
    this report's own ``nets[]``/``devices[]`` arrays imply -- every
    anonymous net whose attachment list (see :func:`_net_attachment_key`) is
    non-empty maps to a name derived from that list; every other net (named
    or unresolvably anonymous) is absent from the map, i.e. left unchanged
    by :func:`_rewrite_net_name`.
    """
    nets = report.get("nets")
    devices = report.get("devices")
    if not isinstance(nets, list) or not isinstance(devices, list):
        return {}
    name_map: dict[str, str] = {}
    index = _net_attachment_index(devices)
    for entry in nets:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not _ANONYMOUS_NET_NAME_RE.match(name):
            continue
        attachment_key = _net_attachment_key(name, devices, index)
        if attachment_key:
            name_map[name] = f"$anon<{attachment_key}>"
    return name_map


def _rewrite_net_name(value: Any, name_map: Mapping[str, str]) -> Any:
    """Canonicalize a single report string that may *be* an anonymous net
    name, or may *start with* one followed by a suffix this module mints
    from it (a parasitics leg/hub/segment net, e.g. ``\\$3__t0``) -- returns
    ``value`` unchanged for anything else (not a string, or a string not
    built from a key in ``name_map``).

    The suffix case requires a boundary check (the character immediately
    after the matched prefix must not be alphanumeric) so that renaming
    ``\\$3`` never also matches the unrelated net ``\\$31`` by accident.
    """
    if not isinstance(value, str):
        return value
    canonical = name_map.get(value)
    if canonical is not None:
        return canonical
    # Every ``name_map`` key is ``\\$<digits>``, so the only key a leaf can
    # start with (past the boundary rule) is its longest ``\\$<digits>``
    # prefix: any shorter digit prefix is followed by a digit, i.e. an
    # alphanumeric, and would be rejected below (issue #3568).
    if not name_map or not value.startswith("\\$"):
        return value
    match = _ANONYMOUS_NET_PREFIX_RE.match(value)
    if match is None:
        return value
    raw_name = match.group()
    canonical_name = name_map.get(raw_name)
    if canonical_name is None:
        return value
    boundary = value[len(raw_name) : len(raw_name) + 1]
    if boundary and boundary.isalnum():
        return value
    return canonical_name + value[len(raw_name) :]


def _canonicalize_extract_report_for_rerun_diff(
    report: Mapping[str, Any],
) -> dict[str, Any]:
    """Return a deep copy of an extract ``--format json`` report with every
    field ``docs/cli/extract.md``'s field-class table (issue #1559)
    classifies as **bookkeeping** -- extractor-internal identifiers with no
    meaning outside the one run that produced them -- canonicalized so
    ``klt extract --check <report> --rerun`` stops reporting bookkeeping-only
    differences as ``status: "drifted"``:

    - every ``net_id`` (KLayout's per-run cluster counter) is stripped
      recursively. Only top-level net rows lose ``structural_id``, so
      pre-field committed reports stay comparable (#5735);
    - every anonymous ``\\$<n>`` net-name spelling is rewritten to the
      structural identity :func:`_anonymous_net_name_map` derives for it
      (that net's own sorted ``<device>.<terminal>`` attachment list), so
      two builds that assign the *same* net a *different* placeholder
      number compare equal;
    - ``nets[]``/``parasitics.nets[]`` -- whose extraction-time sort key is
      itself derived from the (possibly still-anonymous, pre-canonicalization)
      name -- are re-sorted by ``(canonical name, structural attachment
      key)``, so a pure reordering artifact of that unstable sort key does
      not itself register as drift. The structural attachment key (the same
      sorted ``<device>.<terminal>`` list :func:`_anonymous_net_name_map`
      derives an anonymous net's canonical name from) is also needed as a
      *tiebreaker* among two or more entries that legitimately share one
      ``name``/``net`` (e.g. several distinct, un-strapped islands with the
      identical layout label, issue #765/#811, which is exactly what
      ``net_id`` used to disambiguate before this function stripped it) --
      without it, two same-named entries would sort ambiguously and the
      list order between committed/fresh could itself register as
      (spurious) drift.

    A genuine content change is never swallowed by this: a differing
    ``resistance_ohm``/``capacitance_ff``/device count/pin connection etc.
    survives both the ``net_id`` strip (a different field) and the name
    rewrite (canonicalization is a pure relabeling, not a value change), so
    :func:`~klayout_tools._report_verify.diff_verdict_fields` still reports
    it.

    Shared by evidence comparison, normalized hashing and the two dicts
    :func:`klayout_tools.extract.rerun_extract_report` diffs --
    the *embedded* ``fresh`` report in its response is always the real,
    un-normalized one, so a consumer inspecting it still sees the tool's
    actual current output verbatim.
    """
    name_map = _anonymous_net_name_map(report)
    normalized = _strip_net_id_and_rewrite_names(report, name_map)

    nets = normalized.get("nets")
    if isinstance(nets, list):
        for entry in nets:
            if isinstance(entry, dict):
                entry.pop("structural_id", None)
    devices = normalized.get("devices")
    if isinstance(nets, list) and isinstance(devices, list):
        index = _net_attachment_index(devices)
        normalized["nets"] = sorted(
            nets,
            key=lambda entry: (
                (
                    entry.get("name", ""),
                    _net_attachment_key(entry.get("name", ""), devices, index),
                )
                if isinstance(entry, dict)
                else ("", "")
            ),
        )

    parasitics = normalized.get("parasitics")
    if isinstance(parasitics, dict):
        parasitics_nets = parasitics.get("nets")
        if isinstance(parasitics_nets, list):
            parasitics["nets"] = sorted(
                parasitics_nets,
                key=lambda entry: (
                    (
                        entry.get("net", ""),
                        _parasitics_net_attachment_key(entry),
                    )
                    if isinstance(entry, dict)
                    else ("", "")
                ),
            )

    return normalized


def _normalized_netlist_sha256(devices: list[Any], nets: list[Any]) -> str:
    """Issue #3407: SHA-256 hex digest of this run's flat ``devices[]`` /
    ``nets[]`` connectivity with KLayout's anonymous-net numbering
    normalized away -- the same ``net_id``/``structural_id`` strip,
    ``\\$<n>`` structural rewrite and ``(name, attachment key)`` re-sort
    :func:`_canonicalize_extract_report_for_rerun_diff` applies, serialized
    as sorted-key compact JSON. Unlike ``netlist_sha256`` (the written
    ``.spice`` bytes, which embed the ``$N`` counter) it is stable across
    ``klayout`` builds that only renumber anonymous nets; any real
    connectivity, device-parameter or net-name change alters it. Covers the
    flat netlist only (not ``--subcircuit`` decks or parasitics output).
    """
    canonical = _canonicalize_extract_report_for_rerun_diff(
        {"devices": devices, "nets": nets}
    )
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _parasitics_net_attachment_key(entry: Mapping[str, Any]) -> str:
    """The sorted ``<device>.<terminal>`` attachment list a single
    ``parasitics.nets[]`` entry's own ``terminals[]`` already names --
    :func:`_canonicalize_extract_report_for_rerun_diff`'s sort tiebreaker
    for two entries sharing one ``net`` (see that function's docstring),
    built directly from the entry itself rather than re-deriving it from
    the top-level ``devices[]`` array the way :func:`_net_attachment_key`
    does for a top-level ``nets[]`` entry."""
    terminals = entry.get("terminals")
    if not isinstance(terminals, list):
        return ""
    return ",".join(
        sorted(
            f"{terminal.get('device')}.{terminal.get('terminal')}"
            for terminal in terminals
            if isinstance(terminal, dict)
        )
    )


def _strip_net_id_and_rewrite_names(value: Any, name_map: Mapping[str, str]) -> Any:
    """Strip per-run net IDs and rewrite anonymous names throughout the report."""
    if isinstance(value, dict):
        return {
            key: _strip_net_id_and_rewrite_names(val, name_map)
            for key, val in value.items()
            if key != "net_id"
        }
    if isinstance(value, list):
        return [_strip_net_id_and_rewrite_names(item, name_map) for item in value]
    return _rewrite_net_name(value, name_map)
