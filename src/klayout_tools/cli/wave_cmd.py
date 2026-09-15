"""``klt wave build`` / ``klt wave query`` commands: serialise a
`native/wave` (`klt-wave` binary, issues #1599/#1600) run as text or JSON --
Epic #1585 Phase 2c, issue #1601.

Output goes through the shared envelope helpers in :mod:`.output`, as with
every other ``klt`` subcommand -- see ``docs/json-contract.md``.

Exit codes (see ``docs/design/waveform-query-contract-spike.md`` sections 4
and 5 for the underlying Rust binary's own tables, and ``docs/cli/wave.md``
for the CLI-facing summary):

``klt wave build``
    0 - store built successfully
    1 - failed to build -- returned by ``emit_error`` as
        ``output.ERROR_EXIT_CODE``

``klt wave query``
    0 - every op ran and every declared ``predicate`` (if any) is satisfied
        (``status: "ok"``)
    1 - failed to run at all -- returned by ``emit_error`` as
        ``output.ERROR_EXIT_CODE``
    3 - every op ran successfully, but at least one declared ``predicate``
        was not satisfied (``status: "unsatisfied"``)

(2 is reserved for argparse usage errors, as with every other ``klt``
subcommand.)
"""

import argparse

from ..wave import WaveError, run_wave_build, run_wave_query
from .output import emit_error, emit_success

#: `klt wave query`'s "ran, but at least one declared predicate was not
#: satisfied" exit code (contract section 5) -- mirrors `drc_cmd.py`'s own
#: `EXIT_VIOLATIONS` naming for the identical 0/3 split.
EXIT_OK = 0
EXIT_UNSATISFIED = 3


def run_build(args: argparse.Namespace) -> int:
    try:
        response = run_wave_build(args.request)
    except WaveError as exc:
        return emit_error("wave build", str(exc), args.format)

    emit_success(response, args.format, _print_build_text)

    return EXIT_OK


def run_query(args: argparse.Namespace) -> int:
    try:
        response = run_wave_query(args.request)
    except WaveError as exc:
        return emit_error("wave query", str(exc), args.format)

    emit_success(response, args.format, _print_query_text)

    return EXIT_OK if response["status"] == "ok" else EXIT_UNSATISFIED


def _print_build_text(resp: dict) -> None:
    print(
        f"store: {resp['store']['path']} "
        f"({resp['store']['size_bytes']} bytes, {resp['store']['content_hash']})"
    )
    print(
        f"trace: {resp['trace']['path']} "
        f"({resp['trace']['format']}, {resp['trace']['size_bytes']} bytes)"
    )
    clock = resp.get("clock")
    if clock is not None:
        period = clock["period_ns"]
        period_str = f"{period}ns" if period is not None else "unknown"
        print(f"clock: {clock['signal']} ({clock['edge']}, period {period_str})")
    reset = resp.get("reset")
    if reset is not None:
        release = reset.get("release")
        release_str = (
            f"{release['time_ns']}ns (cycle {release['cycle']})"
            if release is not None
            else "not observed"
        )
        print(f"reset: {reset['signal']} ({reset['active']}, release {release_str})")
    print(f"timescale: {resp['timescale']['value']} {resp['timescale']['unit']}")
    time_range = resp["time_range"]
    print(
        f"time range: {time_range['from']['time_ns']}ns .. "
        f"{time_range['to']['time_ns']}ns"
    )
    print(
        f"signals: {resp['signal_count']}, value changes: {resp['value_change_count']}"
    )


def _print_query_text(resp: dict) -> None:
    print(f"store: {resp['store']['path']} (status: {resp['status']})")
    for result in resp["results"]:
        op = result.get("op", "?")
        signal = result.get("signal") or result.get("signals")
        line = f"  {op}"
        if signal:
            line += f" {signal}"
        for key in (
            "value",
            "found",
            "count",
            "stuck",
            "diverges",
            "truncated",
        ):
            if key in result:
                line += f" {key}={result[key]}"
        if "satisfied" in result:
            line += " [satisfied]" if result["satisfied"] else " [unsatisfied]"
        print(line)
