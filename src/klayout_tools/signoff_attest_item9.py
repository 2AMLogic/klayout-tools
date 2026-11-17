"""Item 9 (testbenches shipped) inventory extensions (issue #4848).

Split out of :mod:`klayout_tools.signoff_attest` (file-size ratchet). Three
additive pieces on top of the original ``testbenches`` path list plus one
top-level ``cold_start``:

* a ``testbenches`` entry may be ``{"path", "cold_start"}`` so a failure
  names the bench it belongs to;
* ``unbenched_rows`` declares spec rows that have no bench, with an owner issue;
* a start/finish progress line per command run, written to stderr only.

Plus (issue #5463) the ``documented_argv`` disclosure on a cold start and the
item's reproducibility-pin checks, which live here for the same reason. Plus
(issue #5701) a bench entry's ``sources`` -- the implementation modules the
bench is spread across -- hashed as ``role: "source"`` ``present`` checks
attributed to the bench(es) that declared them.

The validators take the command/path helpers as arguments so this module need
not import :mod:`signoff_attest` (which imports it).
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from ._jsonnum import is_int


def _error(message: str) -> Exception:
    """An ``AttestationError`` (imported lazily: ``signoff_attest`` imports us)."""
    from klayout_tools.signoff_attest import AttestationError

    return AttestationError(message)


#: Item 9's accepted inventory keys (``signoff_attest._ITEM_KEYS[9]``).
ITEM9_KEYS = frozenset({"testbenches", "cold_start", "pins", "unbenched_rows"})


#: The emitted-check fields a validation-only cold start adds (issue #5463).
DOCUMENTED_KEYS = ("reproduces", "documented_argv")


def documented_spec(
    raw: Mapping[str, Any], field: str, accepted: bool
) -> dict[str, Any]:
    """The validated ``documented_argv`` of a command spec (issue #5463).

    ``documented_argv`` is the full invocation that *reproduces* a bench whose
    declared ``argv`` only validates it -- the honest cold start for a bench
    measured by a corner grid or a Monte Carlo run on a batch fleet costs
    hours of fleet budget, so producers declare a cheap proxy instead. It is
    recorded, never executed, and ``argv`` still runs and still gates.

    Only an item-9 ``cold_start`` accepts it (``accepted``). Anywhere else the
    key is a typo or a misunderstanding, and ``_command_spec`` ignores unknown
    keys, so it is refused rather than silently dropped."""
    if "documented_argv" not in raw:
        return {}
    if not accepted:
        raise _error(
            f"'{field}.documented_argv' is only accepted on an item 9 "
            "cold_start -- it records the invocation that reproduces a bench "
            "the declared argv only validates"
        )
    argv = raw["documented_argv"]
    if (
        not isinstance(argv, list)
        or not argv
        or not all(isinstance(arg, str) and arg for arg in argv)
    ):
        raise _error(
            f"'{field}.documented_argv' must be a non-empty list of non-empty strings"
        )
    return {"documented_argv": list(argv)}


def documented_fields(spec: Mapping[str, Any]) -> dict[str, Any]:
    """The check fields a spec carrying ``documented_argv`` emits: the command
    that ran only ``"validates"`` the bench, and the invocation that
    reproduces it. Empty otherwise, so an ordinary check is unchanged."""
    argv = spec.get("documented_argv")
    if not argv:
        return {}
    return {"reproduces": "validates", "documented_argv": list(argv)}


def carried_check(
    committed: Mapping[str, Any], spec: Mapping[str, Any]
) -> dict[str, Any]:
    """A committed ``local_only`` check carried by ``--check`` /
    ``--skip-local-only``, with the disclosure fields taken from the *current*
    inventory: what a command reproduces is a property of what is declared
    now, so a committed value that no longer matches is drift, not evidence."""
    kept = {
        key: value for key, value in committed.items() if key not in DOCUMENTED_KEYS
    }
    return {**kept, "local_only": True, "skipped": True, **documented_fields(spec)}


def pin_checks(pins: Any) -> list[dict[str, Any]]:
    """Item 9's reproducibility pins: the PDK name + revision (or the explicit
    ``{"none": true, "reason": "..."}`` no-PDK form -- an absent ``pdk`` never
    means "no PDK"), and at least one tool version. A missing or empty pin is
    a failed check, not a malformed inventory -- the inventory is well-formed,
    it just does not yet say how to reproduce the run."""
    pins = pins if isinstance(pins, Mapping) else {}
    pdk_value = pdk_pin(pins.get("pdk"))
    tools = pins.get("tools")
    tools_value = None
    if (
        isinstance(tools, Mapping)
        and tools
        and all(
            isinstance(key, str) and key and isinstance(value, str) and value
            for key, value in tools.items()
        )
    ):
        tools_value = {key: tools[key] for key in sorted(tools)}
    return [
        {
            "check": "pin",
            "role": "pdk",
            "passed": pdk_value is not None,
            "value": pdk_value,
        },
        {
            "check": "pin",
            "role": "tools",
            "passed": tools_value is not None,
            "value": tools_value,
        },
    ]


def _validated_sources(
    sources: Any,
    field: str,
    normalize_path: Callable[[Any, str, str], str],
    root: str,
) -> list[str]:
    """The validated ``sources`` of one bench entry (issue #5701): a
    non-empty list of repo-relative paths, each normalized like any other
    inventory path, none repeated within the entry. The same source may be
    named by several benches -- dedup with attribution happens in
    :func:`validate_into`."""
    if not isinstance(sources, list) or not sources:
        raise _error(
            f"'{field}.sources' must be a non-empty list of repo-relative paths"
        )
    normalized: list[str] = []
    for member in sources:
        path = normalize_path(member, f"{field}.sources", root)
        if path in normalized:
            raise _error(f"'{field}.sources' lists '{path}' more than once")
        normalized.append(path)
    return normalized


def split_testbenches(
    inventory: Mapping[str, Any],
    root: str,
    normalize_path: Callable[[Any, str, str], str],
    command_spec: Callable[..., dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Reduce object-form ``testbenches`` entries to plain paths.

    Returns ``(inventory_with_plain_paths, {normalized_path: bench_record})``,
    where a bench record carries the entry's ``cold_start`` spec and its
    normalized ``sources`` paths (issue #5701). String entries pass through
    untouched, so an old-form inventory is returned as an equal mapping and
    validates exactly as before. A malformed container is left for the
    generic path-list validator to refuse."""
    entries = inventory.get("testbenches")
    if not isinstance(entries, list):
        return dict(inventory), {}
    paths: list[Any] = []
    records: dict[str, dict[str, Any]] = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            paths.append(entry)
            continue
        field = f"testbenches[{index}]"
        unknown = sorted(set(entry) - {"path", "cold_start", "sources"})
        if unknown:
            raise _error(
                f"'{field}' has unknown field(s): {', '.join(unknown)} "
                "(accepted: cold_start, path, sources)"
            )
        if "path" not in entry:
            raise _error(f"'{field}' must declare 'path'")
        path = normalize_path(entry["path"], "testbenches", root)
        paths.append(entry["path"])
        record: dict[str, Any] = {}
        if "cold_start" in entry:
            record["cold_start"] = command_spec(
                entry["cold_start"], f"{field}.cold_start", root, documented_argv=True
            )
        if "sources" in entry:
            record["sources"] = _validated_sources(
                entry["sources"], field, normalize_path, root
            )
        if record:
            # A duplicate path is refused by the generic list validator.
            records.setdefault(path, record)
    return {**inventory, "testbenches": paths}, records


def _source_benches(
    bench_records: Mapping[str, Any], benches: Sequence[str]
) -> dict[str, list[str]]:
    """The deduped ``{source_path: [bench_path, ...]}`` attribution map
    (issue #5701): one entry per unique source, keys and bench lists sorted,
    so a source named by several benches is hashed once yet still names all
    of them. A path that is already a bench is refused -- its bytes are
    pinned as the bench either way, and a merged-role check would be
    complexity for a rare tree."""
    source_benches: dict[str, list[str]] = {}
    for bench, record in sorted(bench_records.items()):
        for source in record.get("sources", ()):
            source_benches.setdefault(source, []).append(bench)
    for source in source_benches:
        if source in benches:
            raise _error(
                f"'{source}' is declared both as a testbench and as a source "
                "-- its bytes are already pinned as the testbench; list it "
                "once, as a testbench, not also as another bench's source"
            )
    return {source: source_benches[source] for source in sorted(source_benches)}


def validate_into(
    normalized: dict[str, Any],
    inventory: Mapping[str, Any],
    bench_records: Mapping[str, Any],
    root: str,
    command_spec: Callable[..., dict[str, Any]],
) -> None:
    """Item 9's normalised non-path fields. Every bench needs a cold start:
    its own, or the top-level one. A bench's ``sources`` are collected into
    ``source_benches`` (issue #5701); ``bench_cold_starts`` keeps its
    ``{path: spec}`` shape, records without a ``cold_start`` contributing
    none."""
    benches = normalized["testbenches"]
    normalized["bench_cold_starts"] = {
        path: record["cold_start"]
        for path, record in bench_records.items()
        if "cold_start" in record
    }
    normalized["source_benches"] = _source_benches(bench_records, benches)
    normalized["unbenched_rows"] = validated_unbenched_rows(inventory)
    normalized["pins"] = inventory.get("pins")
    if "cold_start" in inventory:
        normalized["cold_start"] = command_spec(
            inventory["cold_start"], "cold_start", root, documented_argv=True
        )
        return
    missing = [
        path for path in benches if "cold_start" not in bench_records.get(path, {})
    ]
    if missing:
        raise _error(
            "item 9 inventory must declare 'cold_start' -- the invocation "
            "that reproduces the claimed measurements from a clean checkout "
            "(top-level, or on every testbench entry; no cold_start for: "
            f"{', '.join(missing)})"
        )


def validated_unbenched_rows(inventory: Mapping[str, Any]) -> list[dict[str, Any]]:
    """``unbenched_rows``: ``[{"row": str, "issue": int}]`` (optional)."""
    raw = inventory.get("unbenched_rows")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise _error("'unbenched_rows' must be a list of {row, issue}")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw):
        field = f"unbenched_rows[{index}]"
        if not isinstance(entry, Mapping) or set(entry) != {"row", "issue"}:
            raise _error(f"'{field}' must be an object with 'row' and 'issue'")
        row, issue = entry["row"], entry["issue"]
        if not isinstance(row, str) or not row.strip():
            raise _error(f"'{field}.row' must be a non-empty string")
        if not is_int(issue) or issue <= 0:
            raise _error(f"'{field}.issue' must be a positive integer")
        if row in seen:
            raise _error(f"unbenched row '{row}' is declared more than once")
        seen.add(row)
        rows.append({"row": row, "issue": issue})
    return rows


def unbenched_row_checks(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """One visible, passing check per declared bench-less row (sorted)."""
    return [
        {
            "check": "unbenched_row",
            "role": "row",
            "row": entry["row"],
            "issue": entry["issue"],
            "passed": True,
        }
        for entry in sorted(rows, key=lambda e: e["row"])
    ]


def with_progress(
    run: Callable[..., dict[str, Any]], label_of: Callable[..., str]
) -> Callable[..., dict[str, Any]]:
    """Wrap a command-check runner so each run writes a start and finish line
    to stderr. Timing stays out of the returned check (reproducible envelope)."""

    def wrapped(spec: Mapping[str, Any], role: str, **identity: Any) -> dict[str, Any]:
        label = label_of(role, identity)
        print(f"attest item 9: running {label} ...", file=sys.stderr, flush=True)
        started = time.monotonic()
        check = run(spec, role, **identity)
        outcome = (
            "skipped"
            if check.get("skipped")
            else ("ok" if check["passed"] else "FAILED")
        )
        print(
            f"attest item 9: {label} {outcome} in {time.monotonic() - started:.1f}s",
            file=sys.stderr,
            flush=True,
        )
        return check

    return wrapped


def item9_checks(
    normalized: Mapping[str, Any],
    command: Callable[..., dict[str, Any]],
    root: str,
    file_check: Callable[..., dict[str, Any]],
) -> list[dict[str, Any]]:
    """Item 9's command, unbenched-row and (via the caller) pin checks, in a
    deterministic order: source ``present`` checks sorted by path (issue
    #5701; the caller has already emitted the bench ``present`` checks, also
    sorted), then the top-level cold start, per-bench cold starts sorted by
    path, then unbenched rows sorted by row id. Each source check carries
    ``testbenches``, the sorted benches that declared it."""
    run = with_progress(
        command, lambda role, ident: f"{role} {ident.get('testbench', '')}".strip()
    )
    checks = [
        {**file_check(root, path, "source"), "testbenches": benches}
        for path, benches in sorted(normalized["source_benches"].items())
    ]
    if "cold_start" in normalized:
        checks.append(run(normalized["cold_start"], "cold_start"))
    for path, spec in sorted(normalized["bench_cold_starts"].items()):
        checks.append(run(spec, "cold_start", testbench=path))
    return checks + unbenched_row_checks(normalized["unbenched_rows"])


def pdk_pin(pdk: Any) -> dict[str, Any] | None:
    """The validated PDK pin value, or ``None`` when it is absent/malformed.

    Either ``{"name", "revision"}`` or the explicit no-PDK form
    ``{"none": true, "reason": "<non-empty>"}`` (issue #4904). The no-PDK form
    carries only ``none`` and ``reason``; mixing in ``name``/``revision`` is
    contradictory and fails. An absent ``pdk`` never means "no PDK".
    """
    if not isinstance(pdk, Mapping):
        return None
    if pdk.get("none") is True:
        reason = pdk.get("reason")
        if (
            isinstance(reason, str)
            and reason.strip()
            and set(pdk) == {"none", "reason"}
        ):
            return {"none": True, "reason": reason}
        return None
    name, revision = pdk.get("name"), pdk.get("revision")
    if isinstance(name, str) and name and isinstance(revision, str) and revision:
        return {"name": name, "revision": revision}
    return None
