"""Opt-in ``klayout`` DRC engine (issue #565): a subprocess wrapper.

Split out of :mod:`klayout_tools.drc` (issue #3704) along the seam the
module docstring there already documents: the default engine is pure
``klayout.db``, while this engine shells out to the standalone ``klayout``
binary (``klayout -b``) to run a PDK-native DRC-DSL script. The public names
:func:`run_drc_klayout_engine`, :func:`load_rule_category_manifest` and
``KLAYOUT_ENGINE_DEFAULT_TIMEOUT_S`` remain importable from
:mod:`klayout_tools.drc`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from typing import Any

from ._layout import load_layout, select_top_cells
from ._provenance import build_provenance, sha256_file
from .coverage import (
    build_check_coverage,
    coverage_rollup,
    rollup_status,
    work_id,
)
from .drc import (
    CODE_KLAYOUT_UNAVAILABLE,
    CODE_KLAYOUT_VERSION_TOO_OLD,
    CODE_MULTIPLE_TOP_CELLS,
    CODE_NEGATIVE_CONTROL_FAILED,
    SCHEMA_VERSION,
    DrcError,
    _report_path,
    _report_repo_root,
)
from .pdk import PdkNotFoundError, find_pdk

# --------------------------------------------------------------------------- #
# "klayout" engine (issue #565): opt-in subprocess wrapper around the
# standalone `klayout` application binary, running a PDK-native DRC-DSL
# script (`.lydrc`/`.drc`) instead of this module's own curated `DrcRule`
# tables. Mirrors `lvs.py`'s `"netgen"` engine (issue #343) closely --
# binary-resolution-without-`shutil.which`-precheck, timeout handling, and
# "never trust the exit code, parse the tool's own report" discipline are all
# copied from `_run_netgen_lvs`/`_parse_netgen_report`/
# `_cleanup_netgen_work_dir` (see those functions' docstrings in `lvs.py`).
# See `docs/cli/drc.md`, "Engine" -> "klayout" (opt-in, issue #565) for the
# caller-facing writeup.
# --------------------------------------------------------------------------- #

#: Default `klayout -b -r ...` wall-clock budget -- the same idiom as
#: `lvs.py`'s `_NETGEN_DEFAULT_TIMEOUT_S`, but scoped to a single DRC deck
#: run rather than a netlist compare. Overridable via `klt drc`'s
#: `--timeout-s` flag.
KLAYOUT_ENGINE_DEFAULT_TIMEOUT_S = 300.0

#: Matches a coordinate pair inside a KLayout report-database (`.lyrdb`) item
#: value string, e.g. the `(0.2,1;0.2,0)` in
#: `"edge-pair: (0,0;0,1)|(0.2,1;0.2,0)"` or the `(0,0;0,1;0.2,1;0.2,0)` in
#: `"polygon: (0,0;0,1;0.2,1;0.2,0)"` (both forms verified empirically against
#: a real `klayout -b -r ... -rd report=...lyrdb` run for this issue --
#: KLayout's RDB writer always reports coordinates in the layout's *user*
#: units, i.e. micrometres, regardless of the check kind). Deliberately
#: generic across every value kind (`edge-pair:`, `polygon:`, `box:`,
#: `edge:`, ...) rather than one regex per kind, since an arbitrary
#: PDK-native deck can emit `.output()` calls this module has no static list
#: of.
_RDB_COORD_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)")


def _strip_rdb_quotes(text: str) -> str:
    """Strip a matching pair of leading/trailing single quotes KLayout's RDB
    writer sometimes wraps a category name in (empirically, when the name
    contains a period -- `'.'` is the RDB category-path hierarchy separator,
    so a literal rule id like ``"poly.width.1"`` is quoted to disambiguate it
    from a three-level category path; a name with no period, e.g. ``"L1"``,
    is written bare). Both forms were verified against real
    ``klayout -b -r ...`` output for this issue -- this normalises them back
    to the same rule id either way."""
    text = text.strip()
    if len(text) >= 2 and text[0] == "'" and text[-1] == "'":
        return text[1:-1]
    return text


def _rdb_value_points_um(value_text: str) -> list[tuple[float, float]]:
    """Every coordinate pair in one RDB item ``<value>`` string, in the
    layout's user units (micrometres) -- see :data:`_RDB_COORD_RE`."""
    return [(float(x), float(y)) for x, y in _RDB_COORD_RE.findall(value_text)]


def _parse_klayout_rdb_report(
    report_path: str, dbu: float
) -> tuple[list[dict[str, Any]], dict[str, int], list[str]]:
    """Classify a KLayout report-database (``.lyrdb``, RDB XML) file into
    ``(violations, rule_counts, rule_categories)`` -- the first two matching
    ``klt drc``'s existing ``violations[]``/``rule_counts`` shape (see
    :func:`run_drc`'s docstring), the third (issue #1996) the sorted,
    deduplicated list of ``<category>`` names the report *declares*.

    ``rule_categories`` is the closest thing an externally-run deck offers to
    "which rules did you actually run": KLayout's DRC DSL creates a category
    the moment a rule calls ``output(...)``, whether or not that rule found
    anything, so an ordinary clean run still declares one category per rule
    and an empty ``<items/>`` list. A report declaring **no** categories at
    all therefore means the deck never reached a single ``output(...)`` --
    the all-rules-gated-behind-an-unset-``--deck-var`` case
    :func:`run_drc_klayout_engine` reports as
    ``coverage.nothing_checked``. (A deck that instead creates its categories
    lazily, only when a violation exists, would report the same emptiness on
    a genuinely clean run; that is an honest "this report cannot distinguish
    the two", not a misclassification this module can silently resolve.)

    Unlike the curated engine (:func:`run_drc`), an arbitrary PDK-native
    ``.lydrc``/``.drc`` script is not built from this module's own
    :class:`~klayout_tools.decks.DrcRule` table -- there is no declarative
    ``check``/``layer`` identity to report per violation, only whatever the
    deck's own ``report(...)``/``.output(...)`` calls wrote to the RDB. This
    parser is therefore necessarily generic: each RDB ``<category>`` becomes
    one ``violations[].rule`` id (with its ``<description>`` carried
    through), ``violations[].check`` is always the literal string
    ``"external"`` (this module cannot recover a `width`/`space`/... kind
    from RDB XML alone), and ``violations[].layer`` echoes the same rule id
    (an external deck's own category naming is the only per-violation layer
    identity available). ``source_cell``/``source_path`` are always ``None``
    -- per-instance attribution (see :func:`_attribute_to_instance`) needs
    this module's own instance-tree walk against the *input* layout, which an
    RDB report does not carry.

    ``bbox``/``polygon`` are derived from every coordinate pair embedded in
    an item's ``<values><value>`` text (see :data:`_RDB_COORD_RE`), converted
    from the RDB's reported micrometre user units back to the input layout's
    own database units via ``dbu`` (``round(value_um / dbu)``) -- the same
    integer-dbu convention :func:`run_drc`'s own ``violations[].bbox`` uses.
    ``polygon`` is populated only for a value whose text starts with
    ``"polygon:"`` (a plain closed-polygon vertex list echoes back
    faithfully); every other value kind (``edge-pair:``, ``box:``, ``edge:``,
    ...) still contributes its points to the ``bbox`` bound but leaves
    ``polygon`` ``None``, matching :func:`run_drc`'s own "``None`` if the
    check produced a degenerate edge pair" convention for the analogous case.
    An item with no recognisable coordinate payload at all gets a degenerate
    zero ``bbox`` rather than being silently dropped -- mirroring
    ``_parse_netgen_report``'s "never silently drop a declared defect"
    discipline (`lvs.py`).

    Raises :class:`DrcError` if ``report_path`` is not well-formed XML at
    all -- a malformed/truncated report must never silently parse as "no
    violations found."
    """
    try:
        tree = ET.parse(report_path)  # noqa: S314 - KLayout-written .lyrdb in the verb-owned output dir; no DTD/external entities consumed
    except ET.ParseError as exc:
        raise DrcError(
            f"could not parse klayout DRC report '{report_path}': {exc}"
        ) from exc

    root = tree.getroot()

    descriptions: dict[str, str] = {}
    categories_el = root.find("categories")
    if categories_el is not None:
        for category_el in categories_el.findall("category"):
            name_el = category_el.find("name")
            if name_el is None or not name_el.text:
                continue
            rule_id = _strip_rdb_quotes(name_el.text)
            desc_el = category_el.find("description")
            descriptions[rule_id] = (
                desc_el.text if desc_el is not None and desc_el.text else ""
            )

    rule_categories = sorted(descriptions)

    violations: list[dict[str, Any]] = []
    rule_counts: dict[str, int] = {}

    items_el = root.find("items")
    if items_el is None:
        return violations, rule_counts, rule_categories

    for item_el in items_el.findall("item"):
        category_el = item_el.find("category")
        cell_el = item_el.find("cell")
        if (
            category_el is None
            or not category_el.text
            or cell_el is None
            or not cell_el.text
        ):
            continue
        rule_id = _strip_rdb_quotes(category_el.text)
        cell_name = cell_el.text

        points_um: list[tuple[float, float]] = []
        polygon_points_um: list[tuple[float, float]] | None = None
        values_el = item_el.find("values")
        if values_el is not None:
            for value_el in values_el.findall("value"):
                if not value_el.text:
                    continue
                value_text = value_el.text
                value_points = _rdb_value_points_um(value_text)
                points_um.extend(value_points)
                if value_text.lstrip().startswith("polygon:") and value_points:
                    polygon_points_um = value_points

        if points_um:
            xs = [round(x / dbu) for x, _ in points_um]
            ys = [round(y / dbu) for _, y in points_um]
            bbox = {
                "left": min(xs),
                "bottom": min(ys),
                "right": max(xs),
                "top": max(ys),
            }
            polygon = (
                [[round(x / dbu), round(y / dbu)] for x, y in polygon_points_um]
                if polygon_points_um
                else None
            )
        else:
            bbox = {"left": 0, "bottom": 0, "right": 0, "top": 0}
            polygon = None

        violations.append(
            {
                "rule": rule_id,
                "description": descriptions.get(rule_id, ""),
                "check": "external",
                "layer": rule_id,
                "cell": cell_name,
                "source_cell": None,
                "source_path": None,
                "bbox": bbox,
                "polygon": polygon,
            }
        )
        rule_counts[rule_id] = rule_counts.get(rule_id, 0) + 1

    # A rule id that produced items but was never declared under
    # `<categories>` (a deck writing RDB XML by hand, say) still counts as a
    # rule this run reported on -- union rather than trust `<categories>`
    # alone, so `rule_categories` can never be empty for a report that
    # plainly found violations.
    return (
        violations,
        rule_counts,
        sorted(set(rule_categories) | set(rule_counts)),
    )


#: KLayout's own error prefix for a deck-script failure, as emitted by
#: ``klayout -b -r`` (verified against a real 0.28.16 run for issue #1941:
#: ``ERROR: In <deck>: undefined local variable or method '...'`` followed by
#: ``ERROR: NameError: ... in Executable::execute``). Anchored at column 0:
#: the backtrace lines KLayout prints after each ``ERROR:`` line are indented,
#: and a deck's own rule names/progress output (``Executing rule
#: ERROR_CHECK.1``) never starts a line with the bare prefix -- so the anchor
#: is what keeps this from misreading ordinary deck output as a failure.
_KLAYOUT_ERROR_LINE_RE = re.compile(r"^ERROR\b.*$", re.MULTILINE)


_SIGNAL_NUMBER_RE = re.compile(r"Signal number:\s*(\d+)")


def _klayout_crash_signal(returncode: int, output: str) -> str | None:
    """Name the signal that killed klayout, or None when it did not crash.

    POSIX reports death by signal as a negative return code; klayout's own
    crash handler instead prints ``Signal number: N`` and exits normally.
    """
    number = -returncode if returncode < 0 else None
    if number is None:
        match = _SIGNAL_NUMBER_RE.search(output)
        number = int(match.group(1)) if match else None
    if number is None:
        return None
    try:
        return signal.Signals(number).name
    except ValueError:
        return f"signal {number}"


def _klayout_deck_error_lines(*streams: str | None) -> list[str]:
    """Every ``ERROR``-prefixed line in the given ``klayout`` output streams
    (issue #1941), in order, stdout before stderr.

    ``klayout -b -r`` writes its deck-script diagnostics to one or the other
    depending on build/platform, so both are scanned. An empty list means
    KLayout itself reported no error -- not that the deck necessarily ran to
    completion (see :func:`run_drc_klayout_engine`, which pairs this with the
    exit status).
    """
    lines: list[str] = []
    for stream in streams:
        if not stream:
            continue
        lines.extend(
            match.group(0).rstrip() for match in _KLAYOUT_ERROR_LINE_RE.finditer(stream)
        )
    return lines


#: Cap on how many lines of klayout's own output a deck-abort error message
#: carries (issue #2333). The whole captured stdout/stderr is surfaced below
#: this size -- the point of the change is that a *non*-``ERROR``-prefixed
#: line (``sh: pmap: command not found``) is usually the causal one -- but a
#: deck that prints a per-rule progress line for thousands of rules should
#: not turn a single error into a multi-megabyte message. When the cap trips,
#: the **tail** is what survives (the abort is at the end of the run) and any
#: ``ERROR`` line from the omitted head is repeated so the detection signal is
#: never lost to truncation.
_KLAYOUT_DECK_ERROR_DETAIL_MAX_LINES = 500


def _klayout_deck_error_detail(
    stdout: str | None, stderr: str | None, error_lines: list[str]
) -> str:
    """The klayout output to quote in a deck-abort :class:`DrcError` (issue
    #2333), stdout before stderr.

    Deliberately **not** ``error_lines`` alone. ``_klayout_deck_error_lines``
    keeps only lines starting with ``ERROR`` -- correct for *detecting* a
    failed deck (a rule named ``ERROR_CHECK.1`` must not be misread as one),
    but wrong for *explaining* it: the line that says why is routinely not
    ``ERROR``-prefixed. The motivating case is a PDK driver that shells out
    to a host utility the machine does not have, where klayout's captured
    output is::

        sh: pmap: command not found
        ERROR: In main.drc: undefined method 'strip' for nil
        ERROR: NoMethodError: undefined method 'strip' for nil in ...

    Quoting only the ``ERROR`` lines drops the one line that identifies the
    real cause and leaves a nil-dereference that reads like a broken rule
    deck. So the full captured output is surfaced (subject to
    :data:`_KLAYOUT_DECK_ERROR_DETAIL_MAX_LINES`), and ``error_lines`` is
    used only as the fallback for the impossible-in-practice case of an empty
    capture.
    """
    chunks = [
        stream.strip() for stream in (stdout, stderr) if stream and stream.strip()
    ]
    combined = "\n".join(chunks)
    if not combined:
        return "\n".join(error_lines)

    lines = combined.splitlines()
    if len(lines) <= _KLAYOUT_DECK_ERROR_DETAIL_MAX_LINES:
        return combined

    kept = lines[-_KLAYOUT_DECK_ERROR_DETAIL_MAX_LINES:]
    kept_set = {line.rstrip() for line in kept}
    dropped_errors = [line for line in error_lines if line.rstrip() not in kept_set]
    head = [
        f"... ({len(lines) - len(kept)} earlier lines omitted; "
        "klt kept the tail, where the abort is)"
    ]
    if dropped_errors:
        head.extend(dropped_errors)
        head.append("... (end of ERROR lines recovered from the omitted section)")
    return "\n".join(head + kept)


def _check_klayout_run(
    completed: subprocess.CompletedProcess[str],
    report_path: str,
    *,
    allow_deck_errors: bool,
) -> tuple[list[str], bool]:
    """Judge a finished ``klayout -b -r`` run; raise :class:`DrcError` when its
    report cannot be trusted, else return ``(error_lines, deck_errored)``.

    ``deck_errored`` is true only when the run failed but ``allow_deck_errors``
    let the partial report through.
    """
    if not os.path.isfile(report_path):
        # klayout -b -r can exit 0 even when the deck script errored out
        # before reaching report(...) -- never trust the exit code alone
        # (see :func:`run_drc_klayout_engine`'s docstring). No report file at
        # all means no trustworthy verdict is possible; surface klayout's own
        # stdout/stderr rather than a bare "no report" message. Not
        # tolerable via allow_deck_errors: there is no partial report to
        # accept here, only nothing at all.
        output = (completed.stdout or completed.stderr or "").strip()
        crash = _klayout_crash_signal(completed.returncode, output)
        if crash is not None:
            # A crash (#3811) is a klayout/deck bug, not a deck script
            # error: name it so it is not mistaken for a mistyped deck.
            raise DrcError(
                f"klayout crashed ({crash}) before producing a report "
                f"file. klayout's own output:\n{output}"
            )
        raise DrcError(
            "klayout did not produce a report file -- the deck script "
            "likely failed before completing. klayout's own output:\n" + output
        )

    # ...and the report file's presence is not a completion signal either
    # (issue #1941): a deck that called report(...) early and then aborted
    # leaves a partial report behind. Either of klayout's own failure
    # signals -- a non-zero exit status, or an ERROR-prefixed line in its
    # output -- means the rules after the abort never ran, so the partial
    # report's zero violations must not be reported as "clean".
    error_lines = _klayout_deck_error_lines(completed.stdout, completed.stderr)
    deck_errored = completed.returncode != 0 or bool(error_lines)
    if deck_errored and not allow_deck_errors:
        # Issue #2333: the *whole* captured output, not `error_lines`
        # alone -- the causal line is routinely not ERROR-prefixed.
        detail = _klayout_deck_error_detail(
            completed.stdout, completed.stderr, error_lines
        )
        raise DrcError(
            "klayout reported an error while running the deck script "
            f"(exit status {completed.returncode}) -- the deck likely "
            "aborted part-way through, so the report file it did write "
            "covers only the rules that ran before the abort, and its "
            "verdict cannot be trusted. Pass --allow-deck-errors to "
            "accept a partial report anyway. klayout's own output:\n" + detail
        )
    return error_lines, deck_errored


#: Ruby's shell-out forms, as they appear in a KLayout DRC-DSL driver script
#: (issue #2333). Each alternative captures the *command string* the host
#: shell would run, which :func:`_shell_command_words` then reduces to the
#: executables it names. Backticks and ``%x`` are the literal forms; the rest
#: are the method calls whose first argument is a command line.
_RUBY_SHELL_OUT_RES = (
    # `pmap #{Process.pid} | tail -1`
    re.compile(r"`(?P<cmd>[^`\n]*)`"),
    # %x{...} / %x(...) / %x[...]
    re.compile(r"%x[{(\[](?P<cmd>[^)}\]\n]*)[)}\]]"),
    # system("..."), exec('...'), spawn("..."), IO.popen("..."),
    # Open3.capture3("..."), Open3.popen3('...'), ...
    re.compile(
        r"\b(?:system|exec|spawn|IO\.popen|Open3\.\w+)\s*\(?\s*"
        r"""(?:"(?P<cmd_dq>[^"\n]*)"|'(?P<cmd_sq>[^'\n]*)')"""
    ),
)

#: Ruby's literal-path include forms, so a driver split across files is
#: scanned as a whole (issue #2333). Only string *literals* are followed --
#: a computed path is unknowable statically and is simply not scanned.
_RUBY_INCLUDE_RE = re.compile(
    r"""\b(?:require_relative|require|load)\s*\(?\s*"""
    r"""(?:"(?P<path_dq>[^"\n]*)"|'(?P<path_sq>[^'\n]*)')"""
)

#: Words that name no external executable: shell builtins/keywords, plus the
#: redirection/grouping artifacts a naive split can leave behind. Anything
#: here is skipped rather than looked up on PATH.
_SHELL_BUILTIN_WORDS = frozenset(
    {
        ".",
        ":",
        "[",
        "alias",
        "bg",
        "break",
        "case",
        "cd",
        "command",
        "continue",
        "do",
        "done",
        "echo",
        "elif",
        "else",
        "esac",
        "eval",
        "exec",
        "exit",
        "export",
        "false",
        "fg",
        "fi",
        "for",
        "function",
        "if",
        "jobs",
        "local",
        "printf",
        "pwd",
        "read",
        "return",
        "set",
        "shift",
        "source",
        "test",
        "then",
        "time",
        "trap",
        "true",
        "type",
        "ulimit",
        "umask",
        "unalias",
        "unset",
        "until",
        "wait",
        "while",
    }
)

#: A word that could plausibly name an executable: a bare command name or a
#: path to one. Anything else (a redirection fragment, a glob, a quote-mangled
#: token) is skipped -- the preflight only reports what it is sure about.
_COMMAND_WORD_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.+-]*$|^[./~][\w./+-]*$")

#: Upper bound on how many files one deck's include graph is scanned for
#: (issue #2333) -- a static scan must never become the expensive part of a
#: DRC run, and a driver that pulls in more than this is past the point where
#: a heuristic preflight is useful.
_DECK_SCAN_MAX_FILES = 64


def _shell_command_words(command: str) -> list[str]:
    """The executables a shell would run for ``command`` -- the first word of
    each pipeline/list segment (issue #2333).

    Conservative by design: a segment whose command word is Ruby-interpolated
    (``#{...}``), a shell builtin, or anything that does not look like a
    command name at all yields nothing rather than a guess, because every word
    returned here becomes a hard failure when it is missing from PATH.
    """
    words: list[str] = []
    for segment in re.split(r"\|\||&&|[|;\n]", command):
        tokens = segment.strip().lstrip("(").strip().split()
        # Leading `FOO=bar` environment assignments precede the command.
        while tokens and re.fullmatch(r"[A-Za-z_]\w*=.*", tokens[0]):
            tokens.pop(0)
        if not tokens:
            continue
        word = tokens[0]
        # Only a *matched* surrounding quote pair is stripped: a stray
        # apostrophe (Ruby renders names as `foo', so a line quoting two of
        # them pairs the backticks by accident) must keep it and be rejected
        # by `_COMMAND_WORD_RE` below rather than become a bogus lookup.
        if len(word) >= 2 and word[0] == word[-1] and word[0] in "\"'":
            word = word[1:-1]
        if "#{" in word or "$" in word:
            continue
        if word in _SHELL_BUILTIN_WORDS or not _COMMAND_WORD_RE.match(word):
            continue
        words.append(word)
    return words


def _deck_script_files(deck_file: str) -> list[str]:
    """``deck_file`` plus every literal-path Ruby include reachable from it
    (issue #2333), depth-first, de-duplicated, bounded by
    :data:`_DECK_SCAN_MAX_FILES`.

    A PDK's driver is often one file, but the shapes that carry host
    assumptions (a shared logging/util prologue, a per-topic rule table) split
    it across a handful. Unreadable or computed-path includes are skipped, not
    an error: this is a diagnostic aid, and a file it cannot read is simply
    one it cannot make claims about.
    """
    seen: set[str] = set()
    ordered: list[str] = []
    pending = [os.path.abspath(deck_file)]
    while pending and len(ordered) < _DECK_SCAN_MAX_FILES:
        current = pending.pop(0)
        if current in seen or not os.path.isfile(current):
            continue
        seen.add(current)
        ordered.append(current)
        try:
            with open(current, encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        except OSError:
            continue
        base = os.path.dirname(current)
        for match in _RUBY_INCLUDE_RE.finditer(text):
            raw = match.group("path_dq") or match.group("path_sq") or ""
            if not raw or "#{" in raw:
                continue
            candidate = raw if os.path.isabs(raw) else os.path.join(base, raw)
            for resolved in (candidate, candidate + ".rb"):
                if os.path.isfile(resolved):
                    pending.append(os.path.abspath(resolved))
                    break
    return ordered


#: Ruby ``$name`` global reads. A leading letter/underscore excludes the
#: special ``$0``/``$1``/``$!`` forms.
_DECK_GLOBAL_RE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")

#: Globals that are never a caller-tunable deck variable: ``klt`` supplies
#: ``input``/``report`` itself, ``top_cell`` is the optional cell name the
#: PDK decks read under ``if $input``, and the rest are Ruby's own.
_DECK_GLOBAL_IGNORED = frozenset(
    {
        "input", "report", "top_cell",
        "stdout", "stderr", "stdin", "PROGRAM_NAME", "LOAD_PATH",
        "LOADED_FEATURES", "DEBUG", "VERBOSE", "FILENAME",
    }
)  # fmt: skip

#: ``if not defined? $x`` / ``unless defined? $x`` opening a default block.
_DECK_DEFAULT_BLOCK_RE = re.compile(
    r"^\s*(?:if\s+(?:not|!)\s*defined\?\s*\(?\s*\$(\w+)\s*\)?|"
    r"unless\s+defined\?\s*\(?\s*\$(\w+)\s*\)?)\s*$"
)
#: ``$x = <value> unless defined? $x`` / ``... if not defined? $x``.
_DECK_DEFAULT_INLINE_RE = re.compile(
    r"^\s*\$(\w+)\s*=\s*(?P<value>.+?)\s+"
    r"(?:unless\s+defined\?|if\s+(?:not|!)\s*defined\?)\s*\(?\s*\$(\w+)\s*\)?\s*$"
)
_DECK_ASSIGN_RE = re.compile(r"^\s*\$(\w+)\s*(?:\|\|)?=\s*(?P<value>.+?)\s*$")
_DECK_OR_ASSIGN_RE = re.compile(r"^\s*\$(\w+)\s*\|\|=\s*(?P<value>.+?)\s*$")


def _ruby_literal(value: str) -> str:
    """``"false"``/``'3'`` -> ``false``/``3``; anything else is kept verbatim."""
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _record_deck_default(
    line: str, guarded: str | None, defaults: dict[str, str | None]
) -> str | None:
    """Fold one deck line into ``defaults`` (see :func:`scan_deck_globals`);
    returns the ``defined?``-guarded global whose block is still open."""
    block = _DECK_DEFAULT_BLOCK_RE.match(line)
    if block:
        guarded = block.group(1) or block.group(2)
        defaults.setdefault(guarded, None)
        return guarded
    inline = _DECK_DEFAULT_INLINE_RE.match(line) or _DECK_OR_ASSIGN_RE.match(line)
    if inline:
        defaults.setdefault(inline.group(1), _ruby_literal(inline.group("value")))
        return guarded
    assign = _DECK_ASSIGN_RE.match(line)
    if guarded and assign and assign.group(1) == guarded:
        if defaults.get(guarded) is None:
            defaults[guarded] = _ruby_literal(assign.group("value"))
        return None
    return guarded


def scan_deck_globals(deck_file: str) -> list[dict[str, Any]]:
    """Best-effort static scan of the ``-rd`` globals a KLayout DRC deck
    consumes (issue #3816), following literal includes as
    :func:`_deck_script_files` does.

    Returns a name-sorted list of ``{"name", "required_by_caller",
    "default"}``. A global whose first assignment is guarded by ``defined?``
    (block, trailing modifier, or ``||=``) is ``required_by_caller: False``
    with that assignment's literal as ``default``; every other ``$name``
    mentioned outside a comment is ``required_by_caller: True`` with
    ``default: None``. See :data:`_DECK_GLOBAL_IGNORED` for what is skipped.

    A heuristic, not a Ruby parser: a missing entry is not proof the deck
    ignores a variable, and an unreadable file contributes nothing.
    """
    defaults: dict[str, str | None] = {}
    seen: set[str] = set()
    for source in _deck_script_files(deck_file):
        try:
            with open(source, encoding="utf-8", errors="replace") as handle:
                lines = handle.read().splitlines()
        except OSError:
            continue
        guarded: str | None = None
        for line in lines:
            if line.lstrip().startswith("#"):
                continue
            guarded = _record_deck_default(line, guarded, defaults)
            seen.update(_DECK_GLOBAL_RE.findall(line))
    names = {n for n in seen | set(defaults) if n not in _DECK_GLOBAL_IGNORED}
    # A name only ever assigned (never read, never default-guarded) is deck
    # internal state, not an input; but we cannot tell reads from writes
    # line-wise beyond the default forms, so every mentioned name is listed.
    return [
        {
            "name": name,
            "required_by_caller": name not in defaults,
            "default": defaults.get(name),
        }
        for name in sorted(names)
    ]


def _missing_deck_globals_warning(
    deck_file: str, deck_vars: dict[str, str] | None
) -> list[dict[str, Any]]:
    """The ``warnings[]`` entry (issue #3816) for a deck that reads
    ``required_by_caller`` globals while the run supplied none of them.

    Empty list (no warning) when the deck needs none, or when any
    ``--deck-var`` covers a required one. Never affects status/exit code.
    """
    supplied = set(deck_vars or {})
    required = [
        g["name"] for g in scan_deck_globals(deck_file) if g["required_by_caller"]
    ]
    if not required or supplied & set(required):
        return []
    names = ", ".join(required)
    return [
        {
            "category": "deck_globals_not_supplied",
            "count": 1,
            "text": (
                f"deck {os.path.basename(deck_file)} reads -rd global(s) "
                f"[{names}] and no --deck-var supplied any of them; rule "
                "groups gated on them may silently not run (coverage can be "
                "reported as unknown, as in issue #1302). Run `klt pdk decks` "
                "to see each deck's globals and defaults. Static heuristic "
                "scan: a listed global may be harmless."
            ),
        }
    ]


def _scan_deck_host_commands(deck_file: str) -> dict[str, str]:
    """Every host executable the deck script (and its literal includes) shells
    out to, mapped to the ``<file>:<line>`` where it was first seen (issue
    #2333).

    A static, deliberately shallow scan -- see
    :func:`_missing_deck_host_commands` for what that means for the caller.
    """
    found: dict[str, str] = {}
    for source in _deck_script_files(deck_file):
        try:
            with open(source, encoding="utf-8", errors="replace") as handle:
                lines = handle.read().splitlines()
        except OSError:
            continue
        for number, line in enumerate(lines, start=1):
            if line.lstrip().startswith("#"):
                continue
            for pattern in _RUBY_SHELL_OUT_RES:
                for match in pattern.finditer(line):
                    groups = match.groupdict()
                    command = (
                        groups.get("cmd")
                        or groups.get("cmd_dq")
                        or groups.get("cmd_sq")
                        or ""
                    )
                    for word in _shell_command_words(command):
                        found.setdefault(word, f"{source}:{number}")
    return found


def _missing_deck_host_commands(deck_file: str) -> dict[str, str]:
    """The subset of :func:`_scan_deck_host_commands` that is not on ``PATH``,
    mapped to its first ``<file>:<line>`` citation (issue #2333).

    Both directions of error are accepted on purpose, and both are safe:

    - **False negative** (a shell-out through a computed command string, an
      include this cannot resolve): the run proceeds exactly as it did before
      this check existed, and the deck's own failure is still caught by the
      deck-abort check -- now quoting the ``sh: ...: command not found`` line
      that explains it (:func:`_klayout_deck_error_detail`).
    - **False positive** (a shell-out on a branch this run would never take):
      the caller is told which utility and which line, and
      ``--allow-missing-host-tools`` runs the deck anyway.

    What is *not* acceptable is guessing: a word that might not be a command
    name is never reported, since every word here fails the run by default.
    """
    import shutil

    return {
        command: where
        for command, where in _scan_deck_host_commands(deck_file).items()
        if shutil.which(command) is None
    }


_KLAYOUT_EXECUTABLE = "klayout"

KLAYOUT_VERSION_PROBE_TIMEOUT_S = 15.0
"""Wall-clock budget for the ``klayout -v`` version probe (issue #2689)."""

_REQUESTED_VERSION_RE = re.compile(r"^\d+(?:\.\d+)*$")
_REPORTED_VERSION_RE = re.compile(r"\d+(?:\.\d+)+")


def _parse_requested_klayout_version(value: str) -> tuple[int, ...]:
    """Validate a caller-declared ``min_klayout_version`` (issue #2689): dotted
    decimal components only (``0.30.12``). Raises :class:`DrcError` otherwise."""
    text = value.strip() if isinstance(value, str) else value
    if not isinstance(text, str) or not _REQUESTED_VERSION_RE.match(text):
        raise DrcError(
            f"invalid minimum KLayout version {value!r}: expected dotted "
            "decimal components such as '0.30.12'"
        )
    return tuple(int(part) for part in text.split("."))


def _klayout_version_at_least(
    found: tuple[int, ...], required: tuple[int, ...]
) -> bool:
    """Numeric component-wise comparison (``0.30.12 > 0.30.9``); missing
    trailing components count as zero."""
    width = max(len(found), len(required))
    pad = lambda v: v + (0,) * (width - len(v))  # noqa: E731
    return pad(found) >= pad(required)


def _preflight_klayout_version(executable: str, min_version: str | None) -> None:
    """Refuse to run a deck on a standalone ``klayout`` application older than
    the caller-declared minimum (issue #2689).

    A no-op when ``min_version`` is ``None`` (existing behaviour: no
    compatibility is claimed or checked). Otherwise probes the *same*
    executable the deck will run with (``<executable> -v``, bounded by
    :data:`KLAYOUT_VERSION_PROBE_TIMEOUT_S`) and raises :class:`DrcError` for
    an invalid minimum, a missing/failed/timed-out/unparseable probe, or a
    detected version below the minimum. Never bypassed by
    ``allow_deck_errors`` or ``allow_missing_host_tools``. Passing proves only
    the version, not that an arbitrary deck is compatible.
    """
    if min_version is None:
        return
    required = _parse_requested_klayout_version(min_version)
    required_text = ".".join(str(c) for c in required)
    try:
        probe = subprocess.run(
            [executable, "-v"],
            capture_output=True,
            text=True,
            timeout=KLAYOUT_VERSION_PROBE_TIMEOUT_S,
        )
    except FileNotFoundError as exc:
        raise DrcError(
            f"cannot verify the minimum KLayout version ({required_text}): "
            f"{executable!r} binary not found on PATH. Install KLayout "
            f">= {required_text} (https://www.klayout.de/build.html) or omit "
            f"the minimum version. ({exc})",
            code=CODE_KLAYOUT_UNAVAILABLE,
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise DrcError(
            f"cannot verify the minimum KLayout version ({required_text}): "
            f"'{executable} -v' did not complete within "
            f"{KLAYOUT_VERSION_PROBE_TIMEOUT_S}s. Check the installation."
        ) from exc
    except OSError as exc:
        raise DrcError(
            f"cannot verify the minimum KLayout version ({required_text}): "
            f"could not launch '{executable} -v' ({exc}). Check that the "
            f"binary is executable and install KLayout >= {required_text}.",
            code=CODE_KLAYOUT_UNAVAILABLE,
        ) from exc
    output = ((probe.stdout or "") + "\n" + (probe.stderr or "")).strip()
    if probe.returncode != 0:
        raise DrcError(
            f"cannot verify the minimum KLayout version ({required_text}): "
            f"'{executable} -v' exited with status {probe.returncode}. "
            f"Reinstall or update KLayout. Output:\n{output}"
        )
    match = _REPORTED_VERSION_RE.search(output)
    if match is None:
        raise DrcError(
            f"cannot verify the minimum KLayout version ({required_text}): "
            f"could not parse a version from '{executable} -v' output. "
            f"Check the installation. Output:\n{output}"
        )
    found = tuple(int(part) for part in match.group(0).split("."))
    if not _klayout_version_at_least(found, required):
        raise DrcError(
            f"the standalone KLayout application on PATH is version "
            f"{match.group(0)}, but this run requires >= {required_text}. "
            f"Install or update the KLayout application to {required_text} "
            "or newer (https://www.klayout.de/build.html); the pip `klayout` "
            "package is a separate install and is not used by --engine "
            "klayout. See docs/cli/drc.md, 'Engine' -> 'klayout'.",
            code=CODE_KLAYOUT_VERSION_TOO_OLD,
        )


def _deck_tree_identity(deck_file: str) -> dict[str, Any] | None:
    """Additive identity for a deck that is a *tree* of files (issue #3076).

    ``provenance.deck.content_hash`` pins only the entry script; a vendor
    deck that pulls in sibling files via literal ``require``/``load`` is not
    pinned by it. Returns ``None`` for a single-file deck (its identity is
    unchanged), else ``{"file_count", "content_hash", "release"}`` where
    ``content_hash`` is a sha256 over the sorted ``(relative path, file
    hash)`` pairs of the entry script plus every literal include reachable
    from it, and ``release`` is ``git describe --tags --always --dirty`` of
    the deck's checkout (``None`` when it is not a git checkout or git is
    unavailable). Computed-path includes are not followed (see
    :func:`_deck_script_files`).
    """
    files = _deck_script_files(deck_file)
    if len(files) < 2:
        return None
    root = os.path.commonpath([os.path.dirname(f) for f in files])
    digest = hashlib.sha256()
    for rel, path in sorted((os.path.relpath(f, root), f) for f in files):
        digest.update(f"{rel}\0{sha256_file(path)}\n".encode())
    release: str | None = None
    try:
        described = subprocess.run(
            [
                "git",
                "-C",
                os.path.dirname(os.path.abspath(deck_file)),
                "describe",
                "--tags",
                "--always",
                "--dirty",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if described.returncode == 0:
            release = described.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        release = None
    return {
        "file_count": len(files),
        "content_hash": f"sha256:{digest.hexdigest()}",
        "release": release,
    }


def _run_negative_control(
    control_path: str,
    deck_file: str,
    categories: list[str] | None,
    **engine_kwargs: Any,
) -> dict[str, Any]:
    """Run the same deck against a known-bad fixture and require findings
    (issue #3076): proof the supplemental deck actually executed, so a
    vacuously clean verdict cannot pass for a real one. Raises
    :class:`DrcError` (code ``negative_control_failed``) when the fixture
    yields no violation, or misses an expected rule category."""
    control = run_drc_klayout_engine(control_path, deck_file, **engine_kwargs)
    found = sorted({v["rule"] for v in control["violations"]})
    missing = sorted(set(categories or []) - set(found))
    if control["violation_count"] < 1 or missing:
        detail = (
            f"missing expected categories {missing}"
            if control["violation_count"] and missing
            else "no violations reported"
        )
        raise DrcError(
            f"negative control {control_path!r} failed on the same deck: "
            f"{detail} -- the deck may not have executed its rules, so a "
            "clean verdict on the real layout cannot be trusted",
            code=CODE_NEGATIVE_CONTROL_FAILED,
        )
    return {
        "file": control_path,
        "status": "passed",
        "violation_count": control["violation_count"],
        "rules_found": found,
        "expected_categories": sorted(categories or []),
    }


def _preflight_deck_host_tools(deck_file: str, allow_missing_host_tools: bool) -> None:
    """Raise :class:`DrcError` if the deck script shells out to a command
    this host does not have (issue #2333), naming the command and where it is
    used. A no-op when ``allow_missing_host_tools`` is set.

    Called *before* the ``klayout`` subprocess is launched: the failure it
    prevents is not a rule-checking failure at all, and the error the deck
    itself would produce names something else entirely.
    """
    if allow_missing_host_tools:
        return
    missing = _missing_deck_host_commands(deck_file)
    if missing:
        raise DrcError(_missing_host_commands_message(missing))


def _missing_host_commands_message(missing: dict[str, str]) -> str:
    """The actionable error text for a deck whose host utilities are absent
    (issue #2333), one ``deck requires '<cmd>' (not found on PATH)`` line per
    utility with the ``<file>:<line>`` that shells out to it.

    Phrased so the *first* thing the caller reads is the missing utility --
    the whole point of the preflight is that the failure the deck itself
    produces names something else entirely.
    """
    bullets = "\n".join(
        f"  - deck requires {command!r} (not found on PATH) -- shelled out to "
        f"at {where}"
        for command, where in sorted(missing.items())
    )
    return (
        "the DRC-DSL deck script shells out to a host utility this machine "
        "does not have, so it would abort before reporting rules (typically "
        "with a downstream error naming something else entirely):\n"
        f"{bullets}\n"
        "Install the missing utility (e.g. `pmap` ships with procps on "
        "Linux and has no macOS equivalent), point --deck-file at a driver "
        "that does not need it, or pass --allow-missing-host-tools to run "
        "the deck anyway. See docs/cli/drc.md, 'Engine' -> 'klayout'."
    )


def _cleanup_klayout_drc_work_dir(work_dir: str) -> None:
    import shutil

    shutil.rmtree(work_dir, ignore_errors=True)


#: ``coverage_assertion.kind`` of the count assertion (issue #2697).
COVERAGE_ASSERTION_COUNT_KIND = "expected_rule_categories"
#: ``coverage_assertion.kind`` of the exact-set assertion (issue #2806).
COVERAGE_ASSERTION_NAMES_KIND = "expected_rule_category_names"


def _evaluate_category_assertion(
    expected: int | None,
    rule_categories: list[str],
    deck_errored: bool,
    expected_names: list[str] | None = None,
) -> dict[str, Any] | None:
    """Evaluate the opt-in ``expected_rule_categories`` assertion (issue
    #2697) -- or its exact-set sibling ``expected_rule_category_names``
    (issue #2806) -- against the deduplicated declared categories. ``None``
    when neither is requested; raises :class:`DrcError` on a mismatch of a
    run that did not tolerate deck errors; a tolerated-error run is never
    satisfied."""
    if expected_names is not None:
        return _evaluate_category_set_assertion(
            expected_names, rule_categories, deck_errored
        )
    if expected is None:
        return None
    observed = len(set(rule_categories))
    if observed != expected and not deck_errored:
        raise DrcError(
            "rule-category coverage assertion failed: expected "
            f"{expected} unique rule categories "
            f"(--expect-rule-categories) but the deck's report "
            f"declared {observed} -- the deck did not declare the "
            "rule set you vouched for (a gated-off rule group, a "
            "missing --deck-var, or a changed deck?). Fix the deck "
            "invocation or update the expected count."
        )
    return {
        "kind": COVERAGE_ASSERTION_COUNT_KIND,
        "expected": expected,
        "observed": observed,
        "satisfied": observed == expected and not deck_errored,
    }


def _evaluate_category_set_assertion(
    expected_names: list[str], rule_categories: list[str], deck_errored: bool
) -> dict[str, Any]:
    """The exact-set variant (issue #2806): the unique declared RDB category
    names must equal ``expected_names`` exactly (case-sensitive; order and
    duplicate declarations are irrelevant). A same-size swap fails just like
    a missing or extra category. Raises :class:`DrcError` listing the sorted
    missing and unexpected names unless the run tolerated deck errors, in
    which case the assertion is recorded unsatisfied instead."""
    expected_set = set(expected_names)
    observed_set = set(rule_categories)
    missing = sorted(expected_set - observed_set)
    unexpected = sorted(observed_set - expected_set)
    if (missing or unexpected) and not deck_errored:
        raise DrcError(
            "rule-category coverage assertion failed: the deck's report "
            "declared a different set of rule categories than the "
            "expected set (--expect-rule-categories-file) vouched for "
            f"(expected {len(expected_set)}, declared {len(observed_set)}). "
            f"missing (expected, not declared): {json.dumps(missing)}; "
            f"unexpected (declared, not expected): {json.dumps(unexpected)}"
            " -- a gated-off rule group, a missing --deck-var, a "
            "data-dependent declaration, or a changed deck? Fix the deck "
            "invocation or re-review the expected category list."
        )
    return {
        "kind": COVERAGE_ASSERTION_NAMES_KIND,
        "expected_names": sorted(expected_set),
        "observed_names": sorted(observed_set),
        "missing_names": missing,
        "unexpected_names": unexpected,
        "satisfied": not missing and not unexpected and not deck_errored,
    }


def _unmodeled_rules_for_deck(deck_file: str) -> list[str]:
    """Official rule ids the deck knowingly does not check (issue #3812).

    The engine runs an arbitrary deck script, so identity is by content: only
    a file byte-identical to the packaged ASAP7 deck gets that deck's gap
    list. An edited copy or any other deck gets ``[]``."""
    from .decks.asap7_rules import DECK_PATH, NOT_IMPLEMENTED

    try:
        if sha256_file(str(DECK_PATH)) != sha256_file(deck_file):
            return []
    except OSError:
        return []
    return sorted({gap.id for gap in NOT_IMPLEMENTED})


def _packaged_asap7_layer_rules(
    deck_file: str,
    layout: Any,
    areas: dict[str, tuple[int, int, str]],
    extra: dict[str, str] | None = None,
) -> list[dict[str, str]]:
    """``{rule, layer, description}`` entries for the ``areas`` table whose
    layer has shapes in ``layout``, for a run of the packaged ASAP7 deck. A
    different deck file yields none -- its content must match the packaged
    deck byte for byte."""
    from .decks import asap7_rules

    if sha256_file(deck_file) != sha256_file(str(asap7_rules.DECK_PATH)):
        return []
    present = {
        (info.layer, info.datatype)
        for index, info in zip(
            layout.layer_indexes(), layout.layer_infos(), strict=True
        )
        if any(not cell.shapes(index).is_empty() for cell in layout.each_cell())
    }
    return [
        {
            "rule": rule,
            "layer": f"{layer}/{datatype}",
            "description": description,
            **(extra or {}),
        }
        for rule, (layer, datatype, description) in sorted(areas.items())
        if (layer, datatype) in present
    ]


def _packaged_asap7_unchecked_rules(
    deck_file: str, layout: Any
) -> list[dict[str, str]]:
    """``coverage.unchecked_rules`` for a run of the packaged ASAP7 deck
    (issue #3807): areas the deck knowingly does not check (PAD), named only
    when the layout has shapes on the layer, like the curated engine's
    ``unchecked_rules``."""
    from .decks import asap7_rules

    return _packaged_asap7_layer_rules(deck_file, layout, asap7_rules.UNCHECKED_AREAS)


def _packaged_asap7_isolation_only_rules(
    deck_file: str, layout: Any
) -> list[dict[str, str]]:
    """``coverage.isolation_only_rules`` for a run of the packaged ASAP7 deck
    (issue #4029): rules whose verdict depends on the top cell being judged in
    free space, so a boundary artifact of a cell built to be abutted is not
    mistaken for a real violation. Informational; the marker describes the
    check, not its outcome."""
    from .decks import asap7_rules

    return _packaged_asap7_layer_rules(
        deck_file,
        layout,
        asap7_rules.ISOLATION_ONLY_RULES,
        {"reason": "isolation_only"},
    )


def _klayout_engine_coverage(
    rule_counts: dict[str, int],
    rule_categories: list[str],
    coverage_assertion: dict[str, Any] | None,
    unchecked_rules: list[dict[str, str]] | None = None,
    unmodeled_rules: list[str] | None = None,
    isolation_only_rules: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """The klayout engine's ``coverage`` block. Categories are declarations,
    not execution instrumentation, unless the caller's satisfied
    ``expected_rule_categories`` assertion vouches for them (issue #2697);
    otherwise only finding-producing rules are checked and the
    ``unmeasured_rule_execution`` sentinel keeps coverage unknown."""
    asserted = bool(coverage_assertion and coverage_assertion["satisfied"])
    checked = (
        sorted(set(rule_counts) | set(rule_categories))
        if asserted
        else sorted(rule_counts)
    )
    unknown = (
        []
        if asserted
        else [
            {
                # `work_id` namespaces this sentinel so it cannot collide
                # with a real (caller-chosen) RDB category name.
                "id": work_id("engine_execution", "klayout"),
                "reason": "unmeasured_rule_execution",
            }
        ]
    )
    return {
        "deck_layers": [],
        "layers_checked": [],
        "rule_categories": rule_categories,
        "rules_checked": checked,
        "layers_in_stream_without_rules": [],
        "rules_skipped": [],
        "voltage_domain_warnings": [],
        "unchecked_rules": unchecked_rules or [],
        "deck_scope": [],
        "unmodeled_rules": unmodeled_rules or [],
        "isolation_only_rules": isolation_only_rules or [],
        **build_check_coverage(checked=checked, unknown=unknown),
    }


def _validate_expected_rule_categories(value: Any) -> None:
    """Reject anything but ``None`` or a positive ``int`` (issue #2697)."""
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise DrcError(
            "expected_rule_categories (--expect-rule-categories) must be a "
            f"positive integer (got {value!r})"
        )


def _validate_expected_rule_category_names(
    value: Any, expected_count: Any = None
) -> list[str] | None:
    """Normalize the exact-set assertion's ``expected_rule_category_names``
    (issue #2806) into a sorted, deduplicated list, or ``None`` when omitted.

    Accepts any list/tuple/set/frozenset of non-empty strings, compared
    exactly (case-sensitive, no stripping, no glob interpretation -- the
    manifest loader :func:`load_rule_category_manifest` has already trimmed
    its lines). A bare string, an empty collection, a non-string or a
    blank name raises :class:`DrcError`, as does combining it with the count
    assertion ``expected_count`` -- the two are mutually exclusive.
    """
    if value is None:
        return None
    if expected_count is not None:
        raise DrcError(
            "expected_rule_categories (--expect-rule-categories) and "
            "expected_rule_category_names (--expect-rule-categories-file) "
            "are mutually exclusive -- pass one rule-category assertion"
        )
    if not isinstance(value, (list, tuple, set, frozenset)):
        raise DrcError(
            "expected_rule_category_names (--expect-rule-categories-file) "
            f"must be a list of rule category names (got {type(value).__name__})"
        )
    names: set[str] = set()
    for name in value:
        if not isinstance(name, str) or not name.strip():
            raise DrcError(
                "expected_rule_category_names (--expect-rule-categories-file) "
                f"must contain only non-empty strings (got {name!r})"
            )
        names.add(name)
    if not names:
        raise DrcError(
            "expected_rule_category_names (--expect-rule-categories-file) "
            "must name at least one rule category (got an empty set)"
        )
    return sorted(names)


def load_rule_category_manifest(path: str) -> list[str]:
    """Read an ``--expect-rule-categories-file`` manifest (issue #2806):
    UTF-8 text, one exact rule category name per line. Surrounding
    whitespace is trimmed, blank lines are ignored and duplicates are
    deduplicated; there is no comment syntax and no glob expansion, so a
    line ``# M1.a`` names the category ``# M1.a``. Returns the sorted,
    deduplicated names.

    Raises :class:`DrcError` for an unreadable file, invalid UTF-8, or a
    manifest that resolves to no names at all -- all before KLayout is
    ever launched.
    """
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        raise DrcError(
            f"could not read rule-category manifest "
            f"(--expect-rule-categories-file) '{path}': {exc.strerror or exc}"
        ) from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DrcError(
            f"rule-category manifest (--expect-rule-categories-file) '{path}' "
            f"is not valid UTF-8: {exc}"
        ) from exc
    if text.startswith("﻿"):
        text = text[1:]
    names = {line.strip() for line in text.splitlines()}
    names.discard("")
    if not names:
        raise DrcError(
            f"rule-category manifest (--expect-rule-categories-file) '{path}' "
            "names no rule categories (empty or blank-only file)"
        )
    return sorted(names)


def _rerun_assertion_kwargs(committed: dict[str, Any]) -> dict[str, Any]:
    """The ``run_drc_klayout_engine`` keyword reproducing a committed
    report's ``coverage_assertion`` on ``--rerun`` (issues #2697/#2806),
    dispatched on ``kind``. A set assertion replays its embedded
    ``expected_names`` -- the original manifest file is not needed. A
    malformed assertion raises :class:`DrcError` rather than leaking an
    array into the count path."""
    assertion = committed.get("coverage_assertion")
    if assertion is None:
        return {}
    if not isinstance(assertion, dict):
        raise DrcError(
            "committed report's 'coverage_assertion' is malformed (expected "
            f"an object, got {type(assertion).__name__})"
        )
    kind = assertion.get("kind", COVERAGE_ASSERTION_COUNT_KIND)
    if kind == COVERAGE_ASSERTION_COUNT_KIND:
        expected = assertion.get("expected")
        if isinstance(expected, bool) or not isinstance(expected, int) or expected < 1:
            raise DrcError(
                "committed report's 'coverage_assertion.expected' is "
                f"malformed (expected a positive integer, got {expected!r})"
            )
        return {"expected_rule_categories": expected}
    if kind == COVERAGE_ASSERTION_NAMES_KIND:
        names = assertion.get("expected_names")
        if not isinstance(names, list):
            raise DrcError(
                "committed report's 'coverage_assertion.expected_names' is "
                f"malformed (expected a list of names, got {names!r})"
            )
        try:
            resolved = _validate_expected_rule_category_names(names)
        except DrcError as exc:
            raise DrcError(
                "committed report's 'coverage_assertion.expected_names' is "
                f"malformed: {exc}"
            ) from exc
        return {"expected_rule_category_names": resolved}
    raise DrcError(
        f"committed report's 'coverage_assertion.kind' {kind!r} is not "
        f"recognized (expected {COVERAGE_ASSERTION_COUNT_KIND!r} or "
        f"{COVERAGE_ASSERTION_NAMES_KIND!r})"
    )


def _resolve_provenance_pdk(
    pdk_variant: str | None, pdk_root: str | None
) -> dict[str, Any] | None:
    """The :func:`klayout_tools.pdk.find_pdk` record for ``provenance.pdk``
    when ``--pdk``/``--pdk-root`` was given, else ``None``. A missing PDK
    raises :class:`DrcError`."""
    if pdk_variant is None and pdk_root is None:
        return None
    try:
        return find_pdk(variant=pdk_variant, root=pdk_root)
    except PdkNotFoundError as exc:
        raise DrcError(str(exc)) from exc


def run_drc_klayout_engine(
    path: str,
    deck_file: str,
    top: str | None = None,
    timeout_s: float = KLAYOUT_ENGINE_DEFAULT_TIMEOUT_S,
    deck_vars: dict[str, str] | None = None,
    pdk_variant: str | None = None,
    pdk_root: str | None = None,
    allow_deck_errors: bool = False,
    allow_missing_host_tools: bool = False,
    expected_rule_categories: int | None = None,
    min_klayout_version: str | None = None,
    expected_rule_category_names: list[str] | None = None,
    negative_control: str | None = None,
    negative_control_categories: list[str] | None = None,
) -> dict[str, Any]:
    """Run a PDK-native KLayout DRC-DSL rule-deck script (``deck_file``,
    typically resolved via :func:`klayout_tools.pdk.drc_deck_file` or an
    explicit ``--deck-file`` override) against the layout at ``path``, via
    the standalone ``klayout`` application binary -- opt-in alongside
    :func:`run_drc`'s curated, pip-only engine (see ``docs/cli/drc.md``,
    "Engine").

    Invokes::

        klayout -b -r <deck_file> -rd input=<path> -rd report=<tmp>.lyrdb

    -- the standard batch-mode DRC-DSL invocation KLayout's own tooling
    documents and open_pdks' sky130 deck itself embeds as a usage comment
    (verified for this issue against a real ``volare``-fetched sky130A
    install's ``libs.tech/klayout/drc/sky130A.lydrc``). The report path is
    always a ``.lyrdb`` (RDB XML) file, regardless of what ``deck_file``
    itself would otherwise default to, so the result is always the
    structured format :func:`_parse_klayout_rdb_report` reads -- never
    KLayout's plain-text list alternative.

    ``deck_vars`` (issue #1302, CLI: repeatable ``--deck-var name=value``)
    appends additional ``-rd name=value`` pairs to the invocation, after the
    ``input``/``report`` pair, in insertion order. Some PDK-native decks are
    assembled from feature-toggle globals a PDK's own run wrapper would
    normally set (FEOL/BEOL enable flags, a metal-stack selector, etc.) and
    gate every rule behind them -- without a way to set those extra globals,
    every rule's enable condition evaluates false and the deck silently
    checks nothing while still producing a well-formed, empty report (a
    false "clean" verdict indistinguishable from a real one). ``deck_vars``
    is empty/``None`` by default, so the existing zero-config contract for a
    self-contained deck (e.g. sky130A's ``sky130A.lydrc``, which needs only
    ``input``/``report``) is unchanged.

    ``negative_control`` (issue #3076, CLI ``--negative-control``) names a
    known-bad fixture layout that the same deck must flag: it is run first
    with identical options, and zero violations (or a missing
    ``negative_control_categories`` entry) raises :class:`DrcError` with
    code ``negative_control_failed`` rather than reporting the real layout's
    verdict. The outcome is recorded under ``negative_control``.

    Mirrors ``lvs.py``'s ``_run_netgen_lvs`` in every structural way that
    module's docstring calls out:

    - **Binary resolution**: no ``shutil.which`` precheck -- ``subprocess.run``
      is attempted directly and a ``FileNotFoundError`` is caught and
      re-raised as an actionable :class:`DrcError` naming the missing binary
      and how to get one, or how to fall back to the curated engine.
    - **Timeout**: ``subprocess.TimeoutExpired`` -> :class:`DrcError`.
    - **Never trusts the exit code *alone*.** ``klayout -b -r`` can exit
      ``0`` even when the deck script itself errored out before reaching
      ``report(...)`` (e.g. a deck expecting a variable this invocation does
      not set), so a missing report file is its own failure -> :class:`DrcError`
      carrying klayout's own stdout/stderr, never a silent ``status:
      "clean"`` (mirroring netgen's "no log file at all" check).
    - **...but the report file's presence is not a completion signal
      either** (issue #1941). A PDK-native deck typically calls
      ``report(...)`` near the top and appends rules as they run, so a deck
      that aborts part-way through -- an unsupported DRC-DSL construct, a
      typo'd method -- still leaves a *partial* report behind covering only
      the rules that ran before the abort. A non-zero exit status **or** an
      ``ERROR``-prefixed line in klayout's own output (either signal, since
      neither alone is reliable) therefore raises :class:`DrcError` even
      when a report file exists, rather than reporting the partial report's
      zero violations as ``status: "clean"``. ``allow_deck_errors=True``
      (CLI: ``--allow-deck-errors``) is the escape hatch for a caller who
      has deliberately scoped around a known-unrunnable rule: the partial
      report is accepted, and the run records what it tolerated in the
      additive ``engine_deck_errors`` field (see below) so the verdict is
      never *silently* clean. It never tolerates a missing report file. The
      raised error quotes klayout's **whole** captured output, not just its
      ``ERROR``-prefixed lines (issue #2333): the line that explains an abort
      is routinely not ``ERROR``-prefixed (``sh: pmap: command not found``),
      and dropping it leaves a downstream traceback that misdiagnoses a host
      problem as a broken rule deck. Only the *message* is widened -- what
      counts as a deck error is still the ``ERROR``-prefix test, so a rule
      named ``ERROR_CHECK.1`` is still not misread as a failure.
    - **Workdir**: ``tempfile.mkdtemp``, cleaned up via
      :func:`_cleanup_klayout_drc_work_dir` (``shutil.rmtree(...,
      ignore_errors=True)``) in a ``finally``.

    **Host preflight (issue #2333).** Running a PDK's own driver script makes
    that vendor's *host* assumptions part of this engine's contract, and
    those assumptions routinely have nothing to do with rule checking: a real
    open-PDK driver installs a Ruby ``Logger`` formatter that shells out to
    procps ``pmap(1)`` on every log line, so on a host without ``pmap``
    (macOS, minimal container images) the deck dies on its *first*
    ``logger.info`` -- before a single rule runs -- with a nil dereference
    that reads like a broken rule deck. Before launching klayout, the deck
    script and its literal-path Ruby includes are scanned for shelled-out
    command names (:func:`_scan_deck_host_commands`) and any that are not on
    ``PATH`` fail the run with a :class:`DrcError` naming them and where they
    are used (``deck requires 'pmap' (not found on PATH)``), instead of
    letting the deck surface an unrelated downstream traceback.
    ``min_klayout_version`` (CLI: ``--min-klayout-version``, issue #2689)
    declares the minimum standalone application version; the same executable
    is probed with ``-v`` before the deck launches and an older, missing or
    unparseable probe raises :class:`DrcError`. Omitted: no probe. Neither
    allow flag bypasses it, and passing proves nothing about deck
    compatibility beyond the version.

    ``allow_missing_host_tools=True`` (CLI: ``--allow-missing-host-tools``)
    skips the check, for a driver whose shell-out is on a branch this run
    never takes. The scan is static and deliberately conservative -- it
    reports only words it is sure name a command -- so it is a diagnostic
    aid, never a guarantee; a shell-out it cannot see still fails the way it
    always did, now with the causal ``sh: ...: command not found`` line
    quoted in the deck-abort error (see below).

    ``top`` (issue #3788) names the one top cell to check. A deck reads the
    whole ``$input`` stream as a single-top layout, so when the stream has
    more than one top cell and ``top`` is given, the deck is run against a
    temporary stream holding only that cell's subtree (the input file is not
    modified). A multi-top stream without ``top`` raises :class:`DrcError`
    (``code="multiple_top_cells"``) before KLayout is launched; a single-top
    stream is passed through unchanged.

    The returned dict matches :func:`run_drc`'s documented top-level shape
    (``schema_version``, ``file``, ``deck``, ``dbu_um``, ``status``,
    ``violation_count``, ``rule_counts``, ``violations``, ``coverage``,
    ``provenance``) plus one additive field, ``"engine": "klayout"`` (the
    curated engine's own output carries no ``engine`` key at all, unchanged,
    since it has always been the sole engine until this issue) -- and, only
    on a run that actually tolerated deck errors via ``allow_deck_errors``,
    a second additive ``engine_deck_errors`` field (issue #1941):
    ``{"exit_status": <int>, "error_lines": [<klayout's own ERROR lines>]}``.
    That key is omitted entirely otherwise, so an ordinary run's payload is
    byte-identical to previous releases'. ``deck`` is
    ``deck_file`` itself (there is no separate short deck *name* the way the
    curated engine's ``sky130``/``gf180mcu`` deck identifiers are -- an
    arbitrary PDK-native script is identified by its own path).

    Unlike the curated engine, most of ``coverage`` cannot be meaningfully
    populated for an externally-run script this module has no declarative
    rule table for -- the layer-level sub-fields
    (``deck_layers``/``layers_checked``/``layers_in_stream_without_rules``/
    ``rules_skipped``/``voltage_domain_warnings``/``deck_scope``) are all
    empty lists (never fabricated), a known, documented limitation (see
    ``docs/cli/drc.md``, "Engine" -> "klayout").

    Two sub-fields *are* populated, on a distinction issue #2108 sharpened.
    ``coverage.rule_categories`` (issue #1996) is the sorted list of rule
    categories the deck's own report declares -- KLayout's DRC DSL opens a
    ``<category>`` the moment a rule calls ``output(...)``, whether or not
    that call found anything, so this is the closest thing an RDB report
    offers to "which rules did you attempt". It is *not* proof of execution,
    though: a deck that gates its whole rule set behind a feature-toggle
    global set via ``-rd`` (``--deck-var``) which this invocation left unset
    produces the same well-formed, zero-category-or-zero-item report as one
    that genuinely ran everything and found nothing -- previously
    indistinguishable from a real ``status: "clean"``. ``coverage.checked``
    (the common contract's identity list, plus this module's own
    ``coverage.rules_checked`` alias) is therefore *not* ``rule_categories``:
    it is only the rule ids that actually produced a finding (a real item is
    the one signal this format cannot fake). Whenever no violation exists --
    regardless of whether ``rule_categories`` is empty or populated --
    ``coverage.known`` is ``False`` and ``coverage.unknown`` carries a single
    ``engine_execution`` sentinel with reason ``unmeasured_rule_execution``,
    and the envelope's own ``status`` is ``"coverage_unknown"`` (exit 4) --
    never ``"clean"``. There is no instrumentation interface in this release
    that lets an externally-run deck claim known-full coverage; see the
    shared convention in :mod:`klayout_tools.coverage` and the
    ``drc --engine klayout`` row of ``docs/coverage-contract.md``. A found
    violation always overrides: ``status`` is ``"violations"`` and that
    rule's id is recorded as checked, regardless of coverage unknownness
    elsewhere in the same run.

    ``expected_rule_categories`` (CLI ``--expect-rule-categories N``, issue
    #2697) is an opt-in, caller-supplied coverage assertion: "this deck, with
    these inputs, declares exactly ``N`` unique RDB rule categories, and I
    vouch that declaring them means they ran". It must be a positive ``int``
    (``0``, negatives, ``bool`` and non-integers raise :class:`DrcError`
    before KLayout is launched). After a successful run the count is compared
    with ``len(coverage.rule_categories)``: a mismatch raises
    :class:`DrcError` naming both counts; an exact match marks every declared
    category as checked, sets ``coverage.known`` and clears the
    ``unmeasured_rule_execution`` sentinel, so zero findings derive
    ``status: "clean"`` (exit 0) through the shared rollup. Findings still
    yield ``"violations"``. The assertion is recorded under the additive
    top-level ``coverage_assertion`` field. A run that tolerated deck errors
    (``allow_deck_errors``) can never satisfy it: no mismatch error is raised
    for such a partial run, but ``coverage_assertion.satisfied`` is ``False``
    and coverage stays unknown. Omitted, behaviour is unchanged.

    ``expected_rule_category_names`` (CLI ``--expect-rule-categories-file
    PATH``, issue #2806) is the exact-set form of the same caller-vouched
    assertion: a non-empty collection of exact (case-sensitive) category
    names, mutually exclusive with ``expected_rule_categories`` (both
    validated before KLayout is launched). The unique declared categories
    must equal that set exactly -- a same-size swap fails just like a
    missing or extra name, raising :class:`DrcError` that lists the sorted
    missing and unexpected names. A match grants the same caller-vouched
    coverage as a count match; the report records it under
    ``coverage_assertion`` with ``kind: "expected_rule_category_names"``
    (``expected_names``/``observed_names``/``missing_names``/
    ``unexpected_names``/``satisfied``). Neither form proves rule execution
    independently: the names must be reviewed for the deck revision,
    switches and inputs in use, not copied from the accepted run.

    ``pdk_variant``/``pdk_root`` (the ``--pdk``/``--pdk-root`` flags, issue
    #1901) are resolved via :func:`klayout_tools.pdk.find_pdk`, when either
    is given, purely so ``provenance.pdk`` can record which PDK revision the
    run is evidence for -- **in addition to**, not instead of, this engine's
    existing use of ``--pdk``/``--pdk-root`` one layer up in
    ``cli/drc_cmd.py`` to resolve ``deck_file`` itself via
    :func:`klayout_tools.pdk.drc_deck_file` when ``--deck-file`` is omitted.
    Omitting both leaves ``provenance.pdk`` ``None``, matching every prior
    release's behaviour.

    Raises :class:`DrcError` for every failure mode above, plus a missing/
    unreadable ``path`` or ``deck_file`` (checked before the subprocess is
    launched, the same fail-fast order :func:`run_drc` uses for ``path``),
    or ``pdk_variant``/``pdk_root`` given but no matching PDK found.
    """
    _validate_expected_rule_categories(expected_rule_categories)
    expected_names = _validate_expected_rule_category_names(
        expected_rule_category_names, expected_rule_categories
    )
    # Resolved only when `--pdk`/`--pdk-root` was given -- purely for
    # `provenance.pdk` (issue #1901); this engine's own rules come from
    # `deck_file`, resolved separately (possibly using the same flags, one
    # layer up in `cli/drc_cmd.py`'s `drc_deck_file` call). Mirrors
    # `run_drc`'s identical guard so an omitted `--pdk` still yields
    # `provenance.pdk: null`.
    pdk_info = _resolve_provenance_pdk(pdk_variant, pdk_root)

    if not os.path.isfile(deck_file):
        raise DrcError(f"deck file not found: {deck_file}")

    # Issue #2333: a vendor driver's host assumptions are not rule checking,
    # but they abort the deck all the same -- and the abort they produce
    # names the wrong thing. Fail before the subprocess, naming the utility.
    _preflight_deck_host_tools(deck_file, allow_missing_host_tools)

    # Issue #2689: opt-in application-version floor, probed on the very
    # executable launched below, before any deck work.
    _preflight_klayout_version(_KLAYOUT_EXECUTABLE, min_klayout_version)

    negative_control_result = None
    if negative_control is not None:
        negative_control_result = _run_negative_control(
            negative_control,
            deck_file,
            negative_control_categories,
            top=top,
            timeout_s=timeout_s,
            deck_vars=deck_vars,
            pdk_variant=pdk_variant,
            pdk_root=pdk_root,
            allow_deck_errors=allow_deck_errors,
            allow_missing_host_tools=allow_missing_host_tools,
        )

    # Validates `path` (missing/directory/unreadable) before the subprocess
    # is launched, the same fail-fast order `run_drc` uses -- and gives this
    # engine the input layout's own `dbu`, needed to convert the RDB report's
    # micrometre coordinates back to database units (see
    # `_parse_klayout_rdb_report`).
    layout = load_layout(path, DrcError)
    dbu = layout.dbu

    # Issue #3788: a deck reads `$input` as a single-top layout, so the
    # stream handed to it must have exactly one top cell. Resolve that before
    # launching KLayout rather than let the deck die with a generic message.
    top_cells = select_top_cells(layout, top, DrcError)
    if top is None and len(top_cells) > 1:
        names = ", ".join(sorted(cell.name for cell in top_cells))
        raise DrcError(
            f"'{path}' has {len(top_cells)} top cells ({names}); the klayout "
            "engine needs a single top cell -- pass --top to select one",
            code=CODE_MULTIPLE_TOP_CELLS,
        )

    work_dir = tempfile.mkdtemp(prefix="klt-drc-klayout-")
    try:
        report_path = os.path.join(work_dir, "report.lyrdb")
        input_path = path
        if top is not None and [c.name for c in layout.top_cells()] != [top]:
            # `top` names a cell other than the stream's sole top cell (one of
            # several tops, or a child of a single top): only its subtree
            # reaches the deck; the caller's file is untouched.
            input_path = os.path.join(work_dir, "input-top.gds")
            top_cells[0].write(input_path)
        cmd = [
            _KLAYOUT_EXECUTABLE,
            "-b",
            "-r",
            deck_file,
            "-rd",
            f"input={input_path}",
            "-rd",
            f"report={report_path}",
        ]
        for name, value in (deck_vars or {}).items():
            cmd.extend(["-rd", f"{name}={value}"])
        try:
            completed = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout_s
            )
        except FileNotFoundError as exc:
            raise DrcError(
                "could not launch klayout: binary not found on PATH. "
                "Install KLayout (https://www.klayout.de/build.html) or "
                "omit --engine klayout to use the curated deck instead. "
                f"({exc})",
                code=CODE_KLAYOUT_UNAVAILABLE,
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise DrcError(
                f"klayout did not complete within {timeout_s}s (raise "
                "--timeout-s to allow more time)"
            ) from exc

        error_lines, deck_errored = _check_klayout_run(
            completed, report_path, allow_deck_errors=allow_deck_errors
        )

        violations, rule_counts, rule_categories = _parse_klayout_rdb_report(
            report_path, dbu
        )
        coverage_assertion = _evaluate_category_assertion(
            expected_rule_categories, rule_categories, deck_errored, expected_names
        )
    finally:
        _cleanup_klayout_drc_work_dir(work_dir)

    violations.sort(
        key=lambda v: (
            v["rule"],
            v["cell"],
            v["bbox"]["left"],
            v["bbox"]["bottom"],
            v["bbox"]["right"],
            v["bbox"]["top"],
        )
    )

    coverage = _klayout_engine_coverage(
        rule_counts,
        rule_categories,
        coverage_assertion,
        _packaged_asap7_unchecked_rules(deck_file, layout),
        _unmodeled_rules_for_deck(deck_file),
        _packaged_asap7_isolation_only_rules(deck_file, layout),
    )
    deck_global_warnings = _missing_deck_globals_warning(deck_file, deck_vars)
    rollup = coverage_rollup({"coverage": coverage}, failed=bool(violations))
    return {
        "schema_version": SCHEMA_VERSION,
        # Issue #2659: same `{path, scope}` normalization the curated engine's
        # own response builder applies -- see `drc._report_path`.
        "file": _report_path(path, repo_root=_report_repo_root(path)),
        "deck": deck_file,
        "engine": "klayout",
        # Additive, and present *only* when --allow-deck-errors actually
        # tolerated a failed deck run (issue #1941) -- an ordinary run's
        # payload is unchanged, and a tolerated one is self-describing
        # rather than silently "clean".
        **(
            {
                "engine_deck_errors": {
                    "exit_status": completed.returncode,
                    "error_lines": error_lines,
                }
            }
            if deck_errored
            else {}
        ),
        "dbu_um": dbu,
        "status": rollup_status(rollup, success="clean", failure="violations"),
        "violation_count": len(violations),
        "rule_counts": dict(sorted(rule_counts.items())),
        "violations": violations,
        "coverage": coverage,
        # Additive (issue #3816): only present when the deck reads globals a
        # caller must supply and the run supplied none. Never alters status.
        **({"warnings": deck_global_warnings} if deck_global_warnings else {}),
        **(
            {"coverage_assertion": coverage_assertion}
            if coverage_assertion is not None
            else {}
        ),
        **(
            {"negative_control": negative_control_result}
            if negative_control_result is not None
            else {}
        ),
        "provenance": _with_deck_tree(
            build_provenance(
                deck_name=os.path.basename(deck_file),
                deck_path=deck_file,
                pdk=pdk_info,
                input_path=path,
                deck_options=deck_vars,
                include_klayout_version_mismatch=True,
            ),
            deck_file,
        ),
    }


def _with_deck_tree(provenance: dict[str, Any], deck_file: str) -> dict[str, Any]:
    """Add ``provenance.deck.tree`` (issue #3076) for a multi-file deck."""
    tree = _deck_tree_identity(deck_file)
    deck = provenance.get("deck")
    if tree is not None and isinstance(deck, dict):
        deck["tree"] = tree
    return provenance
