"""Item 9 (testbenches shipped) inventory extensions (issue #4848).

Split out of :mod:`klayout_tools.signoff_attest` (file-size ratchet). Three
additive pieces on top of the original ``testbenches`` path list plus one
top-level ``cold_start``:

* a ``testbenches`` entry may be ``{"path", "cold_start"}`` so a failure
  names the bench it belongs to;
* ``unbenched_rows`` declares spec rows that have no bench, with an owner issue;
* a start/finish progress line per command run, written to stderr only.

The validators take the command/path helpers as arguments so this module need
not import :mod:`signoff_attest` (which imports it).
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any


def _error(message: str) -> Exception:
    """An ``AttestationError`` (imported lazily: ``signoff_attest`` imports us)."""
    from klayout_tools.signoff_attest import AttestationError

    return AttestationError(message)


#: Item 9's accepted inventory keys (``signoff_attest._ITEM_KEYS[9]``).
ITEM9_KEYS = frozenset({"testbenches", "cold_start", "pins", "unbenched_rows"})


def split_testbenches(
    inventory: Mapping[str, Any],
    root: str,
    normalize_path: Callable[[Any, str, str], str],
    command_spec: Callable[[Any, str, str], dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Reduce object-form ``testbenches`` entries to plain paths.

    Returns ``(inventory_with_plain_paths, {normalized_path: cold_start_spec})``.
    String entries pass through untouched, so an old-form inventory is
    returned as an equal mapping and validates exactly as before. A malformed
    container is left for the generic path-list validator to refuse."""
    entries = inventory.get("testbenches")
    if not isinstance(entries, list):
        return dict(inventory), {}
    paths: list[Any] = []
    specs: dict[str, dict[str, Any]] = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            paths.append(entry)
            continue
        field = f"testbenches[{index}]"
        unknown = sorted(set(entry) - {"path", "cold_start"})
        if unknown:
            raise _error(
                f"'{field}' has unknown field(s): {', '.join(unknown)} "
                "(accepted: cold_start, path)"
            )
        if "path" not in entry:
            raise _error(f"'{field}' must declare 'path'")
        path = normalize_path(entry["path"], "testbenches", root)
        paths.append(entry["path"])
        if "cold_start" in entry:
            # A duplicate path is refused by the generic list validator.
            specs.setdefault(
                path, command_spec(entry["cold_start"], f"{field}.cold_start", root)
            )
    return {**inventory, "testbenches": paths}, specs


def validate_into(
    normalized: dict[str, Any],
    inventory: Mapping[str, Any],
    bench_specs: Mapping[str, Any],
    root: str,
    command_spec: Callable[[Any, str, str], dict[str, Any]],
) -> None:
    """Item 9's normalised non-path fields. Every bench needs a cold start:
    its own, or the top-level one."""
    benches = normalized["testbenches"]
    normalized["bench_cold_starts"] = dict(bench_specs)
    normalized["unbenched_rows"] = validated_unbenched_rows(inventory)
    normalized["pins"] = inventory.get("pins")
    if "cold_start" in inventory:
        normalized["cold_start"] = command_spec(
            inventory["cold_start"], "cold_start", root
        )
        return
    missing = [path for path in benches if path not in bench_specs]
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
        if isinstance(issue, bool) or not isinstance(issue, int) or issue <= 0:
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
    normalized: Mapping[str, Any], command: Callable[..., dict[str, Any]]
) -> list[dict[str, Any]]:
    """Item 9's command, unbenched-row and (via the caller) pin checks, in a
    deterministic order: top-level cold start, per-bench cold starts sorted by
    path, then unbenched rows sorted by row id."""
    run = with_progress(
        command, lambda role, ident: f"{role} {ident.get('testbench', '')}".strip()
    )
    checks = []
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
