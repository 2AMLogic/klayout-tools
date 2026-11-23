"""ABC statistics-dump timing recovery for `klt synthesize` (issue #7010).

The YoWASP/WASI Yosys wheels run ABC in-process and lose its redirected
stdout entirely, so the `stime -p` summary never reaches the captured ABC
log and `timing` was unconditionally `null`. ABC's own `stime -d` flag
still dumps one record per mapped network to a `stats.txt` beside the ABC
process's working directory, so the recovery stages the engine's *own*
constrained script (parsed from `help abc`) with the `stime` tail widened
to `-pd` and executes Yosys from the run directory.

The fixtures below are real captured bytes: the help section verbatim from
the `yowasp-yosys` 0.68.0.0.post1208 wheel (`Yosys 0.68, git sha1
38e001a6f`), and the statistics dumps its ABC wrote for the issue's own
8-bit counter against sky130_fd_sc_hd `tt_025C_1v80` (single region) and a
two-module variant (one absolute record plus the percentage follow-up
record ABC appends for a repeated network name).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from helpers.subprocess_fakes import fake_completed
from klayout_tools import synthesize
from klayout_tools import synthesize_abc_timing as recovery
from test_synthesize import (
    _GCD_MODULE_STATS,
    _setup_success_env,
    _stub_yosys_success,
)

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

#: `yosys -p 'help abc'` output, Yosys 0.68 wheel, trimmed to the section
#: block this recovery parses (banner and unrelated options removed; the
#: constrained script and its neighbours are byte-verbatim, including the
#: 10-space body indent the parser unfolds).
_HELP_ABC = """
yosys> help abc

    -script <file>
        use the specified ABC script file instead of the default script.

        if no -script parameter is given, the following scripts are used:

        for -liberty/-genlib without -constr:
          strash; &get -n; &fraig -x; &put; scorr; dc2; dretime; strash;
               &get -n; &dch -f; &nf {D}; &put

        for -liberty/-genlib with -constr:
          strash; &get -n; &fraig -x; &put; scorr; dc2; dretime; strash;
               &get -n; &dch -f; &nf {D}; &put; buffer; upsize {D};
               dnsize {D}; stime -p

        for -lut/-luts (only one LUT size):
          strash; &get -n; &fraig -x; &put; scorr; dc2; dretime; strash;
               dch -f; if; mfs2; lutpack -S 1
"""

#: The constrained script the wheel's help documents, unfolded to one line.
_CTR_SCRIPT = (
    "strash; &get -n; &fraig -x; &put; scorr; dc2; dretime; strash; "
    "&get -n; &dch -f; &nf {D}; &put; buffer; upsize {D}; dnsize {D}; stime -p"
)

#: The single-region statistics dump the wheel's ABC wrote for the issue's
#: 8-bit counter (leading newline, trailing space: ABC's own formatting).
_SINGLE_REGION_DUMP = "\nnetlist 9 8 16 126 771 "

#: The two-module variant: the second region appends a *percentage* record
#: (nodes% area% delay% seconds) directly after the absolute record's
#: trailing space -- the ambiguity that must never be read as a delay.
_MULTI_REGION_DUMP = "\nnetlist 10 9 26 203 756  3.8 1.5 5.7 0.04"

_SINGLE_REGION_BANNER = (
    "4.22.1. Extracting gate netlist of module `\\top' "
    "to `<abc-temp-dir>/input.blif'..\n"
)


def test_parse_constrained_abc_script_reads_the_engines_own_help():
    """The staged script is the engine's documented default, not a copy
    baked into klt -- parsed from real 0.68-wheel help bytes."""
    assert recovery.parse_constrained_abc_script(_HELP_ABC) == _CTR_SCRIPT


def test_parse_constrained_abc_script_returns_none_without_the_section():
    """An engine whose help does not document the constrained script (the
    pre-stime era, or a reworded future help) disables the capture rather
    than guessing commands."""
    assert recovery.parse_constrained_abc_script("    -dont_use <cell>\n") is None
    assert recovery.parse_constrained_abc_script("") is None


def test_substitute_abc_delay_target_matches_the_documented_rules():
    """`help abc` documents both substitutions: `{D}` -> `-D <ps>` (or the
    empty string), and `dretime` -> `dretime; retime -o {D}` only when a
    target is set."""
    assert (
        recovery.substitute_abc_delay_target(_CTR_SCRIPT, None)
        == "strash; &get -n; &fraig -x; &put; scorr; dc2; dretime; strash; "
        "&get -n; &dch -f; &nf ; &put; buffer; upsize ; dnsize ; stime -p"
    )
    assert recovery.substitute_abc_delay_target(_CTR_SCRIPT, 1000) == (
        "strash; &get -n; &fraig -x; &put; scorr; dc2; dretime; "
        "retime -o -D 1000; strash; &get -n; &dch -f; &nf -D 1000; &put; "
        "buffer; upsize -D 1000; dnsize -D 1000; stime -p"
    )


def test_widen_stime_to_dump_requires_the_documented_tail():
    widened = recovery.widen_stime_to_dump(_CTR_SCRIPT)
    assert widened is not None and widened.endswith("stime -pd")
    assert widened.count("stime -") == 1
    assert recovery.widen_stime_to_dump("strash; map") is None
    assert recovery.widen_stime_to_dump("stime -p; stime -p") is None


def test_parse_abc_stats_dump_accepts_only_absolute_records():
    """Real wheel bytes: one absolute record parses with its integer
    delay; the two-region dump's percentage follow-up makes the whole dump
    ambiguous (`None`, never a ratio misread as picoseconds)."""
    assert recovery.parse_abc_stats_dump(_SINGLE_REGION_DUMP) == [
        recovery.AbcStatsRecord("netlist", 771)
    ]
    assert recovery.parse_abc_stats_dump(_MULTI_REGION_DUMP) is None
    assert recovery.parse_abc_stats_dump("") == []
    assert recovery.parse_abc_stats_dump("netlist 9 8 16 126") is None
    assert recovery.parse_abc_stats_dump("garbage") is None


def test_read_abc_stats_timing_shapes_the_single_region_case(tmp_path):
    abc_log = tmp_path / "top_abc.log"
    abc_log.write_text(_SINGLE_REGION_BANNER + "no stime line at all\n")
    dump = tmp_path / "stats.txt"
    dump.write_text(_SINGLE_REGION_DUMP)

    timing = recovery.read_abc_stats_timing(str(abc_log), str(dump), None)

    assert timing == {
        "source": "abc_stime_stats_file",
        "wire_load": None,
        "critical_path_ps": 771,
        "precision_ps": 1,
        "delay_target_ps": None,
        "regions": [{"module": "top", "critical_path_ps": 771}],
    }


def test_read_abc_stats_timing_rejects_every_ambiguous_case(tmp_path):
    abc_log = tmp_path / "top_abc.log"
    abc_log.write_text(_SINGLE_REGION_BANNER)
    dump = tmp_path / "stats.txt"

    dump.write_text(_MULTI_REGION_DUMP)
    assert recovery.read_abc_stats_timing(str(abc_log), str(dump), None) is None

    abc_log.write_text(_SINGLE_REGION_BANNER + _SINGLE_REGION_BANNER)
    dump.write_text(_SINGLE_REGION_DUMP)
    assert recovery.read_abc_stats_timing(str(abc_log), str(dump), None) is None

    assert (
        recovery.read_abc_stats_timing(str(abc_log), str(tmp_path / "gone"), None)
        is None
    )


def _patch_help(monkeypatch, text):
    monkeypatch.setattr(recovery, "_probe_help_abc", lambda: text)


def test_plan_stages_the_engine_script_and_clears_a_stale_dump(tmp_path, monkeypatch):
    _patch_help(monkeypatch, _HELP_ABC)
    stale = tmp_path / recovery.ABC_STATS_DUMP_FILENAME
    stale.write_text("netlist 1 1 1 1 1 ")

    capture = recovery.plan_abc_stats_capture(
        str(tmp_path),
        constrained=True,
        user_script=None,
        abc_fast=None,
        delay_target_ps=None,
    )

    assert capture is not None
    assert not stale.exists()
    assert capture.stats_path == str(tmp_path / recovery.ABC_STATS_DUMP_FILENAME)
    staged = Path(capture.staged_script_path)
    assert staged == tmp_path / recovery.ABC_STIME_CAPTURE_SCRIPT
    assert staged.read_text(encoding="utf-8") == (
        "strash; &get -n; &fraig -x; &put; scorr; dc2; dretime; strash; "
        "&get -n; &dch -f; &nf ; &put; buffer; upsize ; dnsize ; stime -pd\n"
    )


def test_plan_declines_every_case_it_cannot_preserve(tmp_path, monkeypatch):
    _patch_help(monkeypatch, _HELP_ABC)
    kwargs = {
        "constrained": True,
        "user_script": None,
        "abc_fast": None,
        "delay_target_ps": None,
    }
    assert recovery.plan_abc_stats_capture(str(tmp_path), **kwargs) is not None
    assert (
        recovery.plan_abc_stats_capture(
            str(tmp_path), **{**kwargs, "user_script": "map.abc"}
        )
        is None
    )
    assert (
        recovery.plan_abc_stats_capture(
            str(tmp_path), **{**kwargs, "abc_fast": "native"}
        )
        is None
    )
    assert (
        recovery.plan_abc_stats_capture(
            str(tmp_path), **{**kwargs, "constrained": False}
        )
        is None
    )
    spaced = tmp_path / "has space"
    spaced.mkdir()
    assert recovery.plan_abc_stats_capture(str(spaced), **kwargs) is None
    _patch_help(monkeypatch, "no constrained section here")
    assert recovery.plan_abc_stats_capture(str(tmp_path), **kwargs) is None


def test_write_script_emits_absolute_run_lines_for_the_capture(tmp_path, monkeypatch):
    """In-repo form: the commit script keeps issue #1844's repo-relative
    paths (and names the staged `-script`), while the runnable sibling the
    caller executes from the run directory carries absolute paths."""
    _patch_help(monkeypatch, _HELP_ABC)
    capture = recovery.plan_abc_stats_capture(
        str(tmp_path),
        constrained=True,
        user_script=None,
        abc_fast=None,
        delay_target_ps=None,
    )
    liberty = tmp_path / "cells.lib"
    liberty.write_text("liberty stub\n")
    source = tmp_path / "gcd.v"
    source.write_text("module gcd(); endmodule\n")

    returned = synthesize._write_script(
        script_path=str(tmp_path / "synth_gcd.ys"),
        sources=[str(source)],
        hdl_toplevel="gcd",
        liberty_path=str(liberty),
        stats_path=str(tmp_path / "gcd_stats.json"),
        netlist_path=str(tmp_path / "gcd_synth.v"),
        constr_path=str(tmp_path / "gcd_abc.constr"),
        abc_log_path=str(tmp_path / "gcd_abc.log"),
        repo_root=str(tmp_path),
        stats_capture=capture,
    )

    run_path = synthesize.run_script_path(str(tmp_path / "synth_gcd.ys"))
    assert returned == run_path
    commit = Path(tmp_path / "synth_gcd.ys").read_text(encoding="utf-8")
    run = Path(run_path).read_text(encoding="utf-8")
    assert f"-script {capture.staged_script_path}" in run
    assert "read_verilog gcd.v" in commit
    assert f"read_verilog {source}" in run
    assert "stats.txt" not in commit and "stats.txt" not in run


def _capture_stub_yosys(monkeypatch, *, abc_log_text, stats_dump, top="gcd"):
    """A `_stub_yosys_success` variant for engines that lose ABC's stdout:
    the captured ABC log carries no `stime` line, and the statistics dump
    lands in the directory the Yosys child was spawned with (`cwd=`)."""

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["yosys", "-p", "help abc"]:
            return fake_completed(stdout=_HELP_ABC)
        if cmd[:2] == ["yosys", "-V"]:
            return fake_completed(stdout="Yosys 0.68 (git sha1 38e001a6f)\n")

        def write(path, text):
            Path(path).write_text(text, encoding="utf-8")

        cwd = kwargs.get("cwd")
        write(
            f"{cwd}/gcd_stats.json",
            json.dumps({"modules": {f"\\{top}": _GCD_MODULE_STATS}}),
        )
        write(f"{cwd}/gcd_synth.v", "// fake mapped netlist\n")
        write(f"{cwd}/gcd_abc.log", abc_log_text)
        write(f"{cwd}/{recovery.ABC_STATS_DUMP_FILENAME}", stats_dump)
        return fake_completed(returncode=0, stdout="run\n")

    monkeypatch.setattr(synthesize.subprocess, "run", fake_run)


def _prepared_repo(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir(exist_ok=True)
    return _setup_success_env(tmp_path, monkeypatch)


def _run_dir_of(report, repo_root):
    path = Path(report["script_path"]["path"] or "")
    if not path.is_absolute():
        path = repo_root / path
    return path.parent


def test_run_synthesize_recovers_timing_from_the_statistics_dump(tmp_path, monkeypatch):
    """The issue's own shape, end to end: WASI-wheel engine, an ABC log
    with the region banner but no `stime` summary, one absolute dump
    record -- `timing` recovers with its reduced precision named, and no
    `timing_unavailable` warning fires."""
    request = _prepared_repo(tmp_path, monkeypatch)
    _capture_stub_yosys(
        monkeypatch,
        abc_log_text=_SINGLE_REGION_BANNER.replace("\\top", "\\gcd"),
        stats_dump=_SINGLE_REGION_DUMP,
    )

    report = synthesize.run_synthesize(request)

    assert report["timing"] == {
        "source": "abc_stime_stats_file",
        "wire_load": None,
        "critical_path_ps": 771,
        "precision_ps": 1,
        "delay_target_ps": None,
        "regions": [{"module": "gcd", "critical_path_ps": 771}],
    }
    assert report["warnings"] == {"total": 0, "by_category": {}, "representatives": []}
    run_dir = _run_dir_of(report, tmp_path)
    assert (run_dir / recovery.ABC_STATS_DUMP_FILENAME).is_file()
    assert (run_dir / recovery.ABC_STIME_CAPTURE_SCRIPT).is_file()


def test_run_synthesize_keeps_the_log_authoritative_over_the_dump(
    tmp_path, monkeypatch
):
    """A native-like engine whose captured log *does* carry the full
    precision `stime` line wins over the dump even though both exist --
    including per-region attribution and no `precision_ps` field."""
    from test_synthesize import _ABC_STIME_LINE

    request = _setup_success_env(tmp_path, monkeypatch)
    _capture_stub_yosys(
        monkeypatch,
        abc_log_text=(
            "4.22.1. Extracting gate netlist of module `\\gcd' "
            "to `<abc-temp-dir>/input.blif'..\n" + _ABC_STIME_LINE + "\n"
        ),
        stats_dump=_SINGLE_REGION_DUMP,
    )

    report = synthesize.run_synthesize(request)

    assert report["timing"]["source"] == "abc_stime"
    assert report["timing"]["critical_path_ps"] == 2485.93
    assert "precision_ps" not in report["timing"]


def test_run_synthesize_warns_when_the_dump_cannot_attribute_a_delay(
    tmp_path, monkeypatch
):
    """Multi-region percentage records stay honestly `null`, with the
    capture-aware `timing_unavailable` wording naming both sources."""
    request = _setup_success_env(tmp_path, monkeypatch)
    _capture_stub_yosys(
        monkeypatch,
        abc_log_text=_SINGLE_REGION_BANNER.replace("\\top", "\\gcd") * 2,
        stats_dump=_MULTI_REGION_DUMP,
    )

    report = synthesize.run_synthesize(request)

    assert report["timing"] is None
    representative = report["warnings"]["representatives"][0]
    assert representative["category"] == "timing_unavailable"
    assert "statistics dump cannot attribute a delay" in representative["text"]


def test_stubbed_engines_without_the_help_section_are_unchanged(tmp_path, monkeypatch):
    """The shared hermetic fixture's help fragment carries no constrained
    section, so every pre-#7010 hermetic run keeps the built-in script and
    the log-only timing parse -- no capture is staged at all."""
    request = _prepared_repo(tmp_path, monkeypatch)
    _stub_yosys_success(monkeypatch)

    report = synthesize.run_synthesize(request)

    assert report["timing"]["source"] == "abc_stime"
    run_dir = _run_dir_of(report, tmp_path)
    assert not (run_dir / recovery.ABC_STATS_DUMP_FILENAME).exists()
    assert not (run_dir / recovery.ABC_STIME_CAPTURE_SCRIPT).exists()
