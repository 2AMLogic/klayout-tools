"""ABC ``stime`` timing recovery for ``klt synthesize`` (issues #807, #4743, #7010).

Split out of ``synthesize.py`` so the per-region attribution
(``timing.regions``) does not grow that file past its size ceiling.

Issue #7010 adds the second recovery half. Engines that run ABC in-process
under a WASI sandbox (the YoWASP ``yowasp-yosys`` wheels) lose ABC's
redirected stdout entirely, so the ``stime -p`` summary this module's log
parser reads never reaches the captured ``abc`` log. ABC's own ``stime -d``
flag still dumps one record per mapped network to a ``stats.txt`` next to
the ABC process's working directory, so :func:`plan_abc_stats_capture`
stages the engine's *own* constrained mapping script (parsed verbatim from
``yosys -p 'help abc'``, so the selected mapping commands are preserved,
``-D`` substitution included) with the trailing ``stime -p`` widened to
``stime -pd``, and the caller runs Yosys from the run directory so the dump
lands beside this invocation's own artifacts.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from typing import Any, NamedTuple

from ._engine_proc import run_in_group

#: ABC's own ``stime -p`` summary line, as it reaches the Yosys log (and,
#: via ``tee -q -o``, the captured ABC log this module parses):
#: ``ABC: WireLoad = "none"  Gates = 297 ( 5.7 %)   Cap = 6.2 ff ( 12.4 %)
#: Area = 1986.91 ( 66.3 %)   Delay = 2485.93 ps  ( 32.3 %)``. Verified live
#: (Yosys 0.68+48, sky130_fd_sc_hd, issue #807).
_ABC_STIME_RE = re.compile(
    r'WireLoad\s*=\s*"(?P<wire_load>[^"]*)".*?Delay\s*=\s*(?P<delay_ps>[\d.]+)\s*ps'
)

#: Yosys's per-region banner in the captured ABC log (verified live against
#: Yosys 0.68+; issue #4743): ``4.1. Extracting gate netlist of module
#: `\top' to `<abc-temp-dir>/input.blif'..``. The most recent one before an
#: ``stime`` line names the module that ABC run mapped. Yosys escapes public
#: names with a leading backslash, which is stripped.
_ABC_MODULE_RE = re.compile(r"Extracting gate netlist of module `\\?([^']+)'")

#: The ``help abc`` section header introducing the default Liberty mapping
#: script Yosys runs when ``-constr`` is passed (verified live against
#: Yosys 0.67+post and the YoWASP 0.68 wheel, issue #7010).
_ABC_CTR_HEADER = "for -liberty/-genlib with -constr:"

#: The filename ABC's ``stime -d`` dump is hardcoded to (ABC's own
#: ``Abc_SclDumpStats`` opens it in the ABC process's working directory, in
#: append mode) -- the run directory the caller executes Yosys from, under
#: :func:`plan_abc_stats_capture`.
ABC_STATS_DUMP_FILENAME = "stats.txt"

#: The staged ABC script file (the engine's own constrained script with the
#: ``stime`` tail widened to also dump statistics), written into the run
#: directory and passed to the ``abc`` pass via ``-script``.
ABC_STIME_CAPTURE_SCRIPT = "abc_stime_capture.script"

#: The ``timing.source`` value marking a delay recovered from ABC's
#: statistics dump rather than its captured log (issue #7010): integer
#: picoseconds, single region, no wire-load echo.
ABC_STIME_STATS_SOURCE = "abc_stime_stats_file"

#: ``timing.precision_ps`` on an :data:`ABC_STIME_STATS_SOURCE` report: ABC
#: casts the delay to an ``int`` before writing the record, so the figure is
#: truncated, never rounded -- a value equal to the requested target cannot
#: establish the target was met.
ABC_STIME_STATS_PRECISION_PS = 1


class AbcRegion(NamedTuple):
    """One ``stime`` report: owning module (or ``None``), delay, wire load."""

    module: str | None
    delay_ps: float
    wire_load: str


def parse_abc_regions(log_text: str) -> list[AbcRegion]:
    """Every ``stime`` report in ``log_text``, in log order."""
    modules = [(m.start(), m.group(1)) for m in _ABC_MODULE_RE.finditer(log_text)]
    regions: list[AbcRegion] = []
    for match in _ABC_STIME_RE.finditer(log_text):
        preceding = [name for pos, name in modules if pos < match.start()]
        regions.append(
            AbcRegion(
                preceding[-1] if preceding else None,
                float(match.group("delay_ps")),
                match.group("wire_load"),
            )
        )
    return regions


class AbcStatsRecord(NamedTuple):
    """One absolute record from an ABC statistics dump: network name and the
    delay ABC measured for it, in integer picoseconds."""

    network: str
    delay_ps: int


def _abc_stats_token_is_int(token: str) -> bool:
    return token.isdigit()


def _abc_stats_token_is_number(token: str) -> bool:
    return bool(re.fullmatch(r"[\d.]+", token))


def parse_abc_stats_dump(text: str) -> list[AbcStatsRecord] | None:
    """Every absolute record in an ABC ``stime -d`` statistics dump, or
    ``None`` when the dump is ambiguous (issue #7010).

    The format is fixed by ABC's own ``Abc_SclDumpStats``: the first dump of
    a network *name* in one ABC process writes ``<name> <PIs> <POs> <nodes>
    <area> <delay-int>``; every later dump of the same name in that process
    appends a *percentage* record relative to the first (``<nodes%> <area%>
    <delay%> <seconds>``, appended directly after the absolute record's
    trailing space). Yosys's ``abc`` pass writes every region's BLIF with
    the same ``.model netlist`` name, so under the single-process engines
    this recovery targets, only the first region is ever absolute.

    A percentage record cannot establish a delay (it is a ratio to the first
    region's own integers), so its presence makes the whole dump ambiguous:
    ``None``, never a misread number. So does any record that does not parse
    as the documented shape. Empty text parses to no records.
    """
    tokens = text.split()
    records: list[AbcStatsRecord] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if not _abc_stats_token_is_number(token):
            name = token
            numbers = tokens[index + 1 : index + 6]
            if len(numbers) != 5 or not all(
                _abc_stats_token_is_int(number) for number in numbers
            ):
                return None
            records.append(AbcStatsRecord(name, int(numbers[4])))
            index += 6
        else:
            # A numeric token at record position can only be a percentage
            # (follow-up) record -- ABC never starts an absolute record with
            # a number.
            return None
    return records


def parse_constrained_abc_script(help_stdout: str) -> str | None:
    """The default ``-liberty … -constr`` ABC script exactly as the engine's
    own ``help abc`` documents it (placeholders like ``{D}`` intact), or
    ``None`` when the help output does not carry the section in the shape
    this parser knows (issue #7010).

    The help text wraps the script across indented continuation lines; the
    first ``for -liberty/-genlib with -constr:`` section is the default
    script (a ``-fast`` engine variants section follows it on builds that
    have one). Returning the engine's own text -- never a copy baked into
    klt -- is what lets the staged capture script preserve the selected
    mapping commands across engine versions.
    """
    lines = help_stdout.splitlines()
    for index, line in enumerate(lines):
        if line.strip() != _ABC_CTR_HEADER:
            continue
        body: list[str] = []
        for follow in lines[index + 1 :]:
            if not follow.strip() or not follow.startswith((" ", "\t")):
                break
            body.append(follow.strip())
        return " ".join(" ".join(body).split()) or None
    return None


def substitute_abc_delay_target(script: str, delay_ps: int | None) -> str:
    """Apply the ``{D}`` substitutions ``help abc`` itself documents for the
    default scripts (issue #7010): ``-D <picoseconds>`` replaces every
    ``{D}``, and when a target is set, every ``dretime`` first becomes
    ``dretime; retime -o {D}``. ``None`` replaces ``{D}`` with the empty
    string and leaves ``dretime`` alone -- exactly what Yosys does for its
    own built-in script.
    """
    if delay_ps is None:
        return script.replace("{D}", "")
    script = script.replace("dretime", "dretime; retime -o {D}")
    return script.replace("{D}", f"-D {delay_ps}")


def widen_stime_to_dump(script: str) -> str | None:
    """The script with its ``stime -p`` report widened to ``stime -pd`` so
    ABC also dumps its statistics file, or ``None`` when the script does not
    end in exactly one ``stime -p`` report (issue #7010). ``-p`` itself is
    kept: an engine whose stdout capture works keeps its full-precision log
    line, and the dump is additive.
    """
    if script.count("stime -p") != 1 or not script.endswith("stime -p"):
        return None
    return script[: -len("stime -p")] + "stime -pd"


def _probe_help_abc() -> str:
    """``yosys -p 'help abc'`` stdout, or ``""`` on any failure (never
    raises) -- the same probe :func:`synthesize._abc_supports_option`
    uses, so the hermetic fakes answer this shape too."""
    # Imported lazily: `synthesize_timeout` itself imports this module's
    # `_ABC_MODULE_RE`, so a module-level import would be circular.
    from .synthesize_timeout import DEFAULT_YOSYS_TIMEOUT_S

    try:
        completed = run_in_group(
            ["yosys", "-p", "help abc"],
            capture_output=True,
            text=True,
            timeout=DEFAULT_YOSYS_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if completed.returncode != 0:
        return ""
    return completed.stdout or ""


@dataclass(frozen=True)
class AbcStatsCapture:
    """One run's statistics-dump recovery plan (issue #7010)."""

    #: The staged ``-script`` file (engine-derived commands, ``stime -pd``
    #: tail), or ``None`` when the mapping commands already carry the dump
    #: flag inline (the ``abc_fast`` inline form).
    staged_script_path: str | None

    #: Where ABC's dump is expected to land: the run directory Yosys is
    #: executed from, so the file belongs to this invocation alone.
    stats_path: str


def plan_abc_stats_capture(
    output_dir: str,
    *,
    constrained: bool,
    user_script: str | None,
    abc_fast: str | None,
    delay_target_ps: int | None,
) -> AbcStatsCapture | None:
    """Stage the statistics-dump recovery for one synthesis run, or return
    ``None`` when it does not apply (issue #7010).

    Applies to the constrained default mapping path -- ``-constr`` resolved,
    no user ``abc_script``, not the native ``-fast`` form (which passes no
    ``-constr`` at all). For the default form the engine's own constrained
    script is parsed from ``help abc``, substituted and widened, then staged
    into the run directory for ``abc -script``; the ``abc_fast`` inline form
    already builds its commands in klt and only needs its ``stime`` tail
    widened, so no file is staged. Every form gets :attr:`AbcStatsCapture.
    stats_path` under ``output_dir``, with any stale dump removed first:
    ABC appends, so a leftover file from a replayed run must never be read
    as this run's measurement.

    ``None`` (this run keeps Yosys's built-in script and today's log-only
    parsing) when anything needed to preserve the selected mapping commands
    verbatim is unavailable: the engine's help does not document the
    constrained script in the parsed shape, the staged path contains
    whitespace (ABC's ``source`` cannot quote it), or the run directory
    cannot be prepared.
    """
    if not constrained or user_script is not None or abc_fast == "native":
        return None
    staged: str | None = None
    if abc_fast is None:
        script = parse_constrained_abc_script(_probe_help_abc())
        if script is not None:
            script = widen_stime_to_dump(
                substitute_abc_delay_target(script, delay_target_ps)
            )
        if script is None:
            return None
        staged = os.path.join(output_dir, ABC_STIME_CAPTURE_SCRIPT)
        if any(character.isspace() for character in staged):
            return None
        try:
            with open(staged, "w", encoding="utf-8") as handle:
                handle.write(script + "\n")
        except OSError:
            return None
    stats_path = os.path.join(output_dir, ABC_STATS_DUMP_FILENAME)
    try:
        os.unlink(stats_path)
    except FileNotFoundError:
        pass
    except OSError:
        return None
    return AbcStatsCapture(staged_script_path=staged, stats_path=stats_path)


def read_abc_stats_timing(
    abc_log_path: str | None, stats_path: str, delay_target_ps: int | None
) -> dict[str, Any] | None:
    """The response's ``timing`` object recovered from ABC's statistics dump
    (issue #7010), or ``None`` when the dump cannot attribute a delay to
    this invocation.

    Only the unambiguous case is reported: exactly one absolute dump record
    and exactly one ``Extracting gate netlist`` banner in the captured ABC
    log -- one measured region, named. Anything else (a missing or unreadable
    dump, a percentage follow-up record, more regions than records) stays
    ``None`` so the caller's ``timing_unavailable`` warning fires rather
    than a misattributed number.

    The shape parallels the ``abc_stime`` one and names its own limits:
    :data:`ABC_STIME_STATS_SOURCE` as ``source``, ``wire_load`` ``null``
    because the dump carries no wire-load echo at all (unknown, not
    "none"), and :data:`ABC_STIME_STATS_PRECISION_PS` because ABC writes
    the delay as an integer.
    """
    try:
        with open(stats_path, encoding="utf-8", errors="replace") as handle:
            dump = handle.read()
    except OSError:
        return None
    records = parse_abc_stats_dump(dump)
    if records is None or len(records) != 1:
        return None
    log_text = ""
    if abc_log_path is not None:
        try:
            with open(abc_log_path, encoding="utf-8", errors="replace") as handle:
                log_text = handle.read()
        except OSError:
            return None
    modules = [m.group(1) for m in _ABC_MODULE_RE.finditer(log_text)]
    if len(modules) != 1:
        return None
    delay_ps = records[0].delay_ps
    return {
        "source": ABC_STIME_STATS_SOURCE,
        "wire_load": None,
        "critical_path_ps": delay_ps,
        "precision_ps": ABC_STIME_STATS_PRECISION_PS,
        "delay_target_ps": delay_target_ps,
        "regions": [{"module": modules[0], "critical_path_ps": delay_ps}],
    }
