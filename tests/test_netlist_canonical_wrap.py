"""``klt netlist --canonical-wrap`` (issue #5715).

The flag applies the ``+``-continuation fold / whitespace collapse that
``--check``'s comparison (:func:`klayout_tools.netlist._normalize_layout`,
issue #3324) already implements to the *written* netlist, so two hosts whose
xschem builds wrap long cards differently write byte-identical files. These
tests pin both sides of that contract:

- the shared helper :func:`klayout_tools.netlist_paths.fold_continuations`
  (its folding rules and its byte-preservation guarantees), and
- the flag's end-to-end behaviour on both write paths (local stub-xschem and
  ``--backend batch`` with a fake runner), including the JSON field and the
  default-off byte-for-byte guarantee.

Everything runs against a stub `xschem` and a fake batch runner, exactly as
in ``tests/test_netlist.py`` / ``tests/test_netlist_batch.py`` (which this
file imports its fixtures from); no test here touches a real xschem, S3 or
AWS.
"""

from __future__ import annotations

import json

import pytest

import test_netlist_batch as tnb
from klayout_tools.cli import main
from klayout_tools.netlist import _normalize_layout, run_netlist
from klayout_tools.netlist_batch import run_netlist_batch
from klayout_tools.netlist_paths import fold_continuations
from test_netlist import (
    _NETLIST_BODY,
    _STUB_NONZERO_EXIT,
    _write_schematic,
    _write_stub,
    _written,
)
from test_netlist_batch import RUNNER_TREE, _runner_outputs, _status

# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


#: A body that only differs from ``_NETLIST_BODY`` in line layout: the same
#: tokens, re-wrapped the way a different host's xschem would.
_WRAPPED_BODY = (
    "* stub netlist\n"
    ".subckt block a b\n"
    "XM1 a b a a sky130_fd_pr__nfet_01v8\n"
    "+   L=0.15\tW=1\n"
    "C0  a   gnd   10f\n"
    "**.subckt block b a\n"
    "** sym_path: /elsewhere/block.sym\n"
    ".ends\n"
    ".end\n"
)


@pytest.fixture
def argv_log(tmp_path, monkeypatch):
    """Where the stub records the argv it was invoked with, and the wrapped
    body it writes."""
    log = tmp_path / "argv.log"
    monkeypatch.setenv("ARGV_LOG", str(log))
    monkeypatch.setenv("NETLIST_BODY", _WRAPPED_BODY)
    return log


@pytest.fixture
def batch_repo(tmp_path):
    """test_netlist_batch's tracked-schematic repo, re-bound: pytest does not
    carry fixtures across modules by import, so call the wrapped functions."""
    return tnb.repo._get_wrapped_function()(tmp_path)


@pytest.fixture
def batch_fake(tmp_path, monkeypatch):
    """test_netlist_batch's fake transport runner (config from the env)."""
    return tnb.fake._get_wrapped_function()(tmp_path, monkeypatch)


@pytest.fixture
def no_local_xschem(monkeypatch):
    """Fails loudly if anything tries to run xschem on this host."""
    return tnb.no_local_xschem._get_wrapped_function()(monkeypatch)


def _setup(tmp_path):
    return (
        _write_schematic(tmp_path),
        _write_stub(tmp_path, _STUB_NONZERO_EXIT),
        tmp_path / "block.spice",
    )


# --------------------------------------------------------------------------- #
# The folding helper
# --------------------------------------------------------------------------- #


def test_continuation_folds_onto_its_card():
    data = b"M1 a b a a nfet L=0.15\n+ W=1 nf=1\n"
    assert fold_continuations(data) == b"M1 a b a a nfet L=0.15 W=1 nf=1\n"


def test_continuation_never_folds_onto_a_comment():
    data = b"* header\n+ stray\nM1 a b\n"
    assert fold_continuations(data) == b"* header\n+ stray\nM1 a b\n"


def test_whitespace_runs_collapse_on_non_comment_lines():
    data = b"C0  a   gnd\t10f  \n"
    assert fold_continuations(data) == b"C0 a gnd 10f\n"


def test_comment_bytes_survive_verbatim():
    data = (
        b"**.subckt block b a\n"  # port order must NOT be sorted by the writer
        b"** sch_path: /elsewhere/block.sch  \n"
        b"M1 a b\n+ c d\n"
    )
    assert fold_continuations(data) == (
        b"**.subckt block b a\n** sch_path: /elsewhere/block.sch  \nM1 a b c d\n"
    )


def test_orphan_continuation_is_not_dropped():
    data = b"+ leading\n* comment\n+ after comment\n\n+ after blank\n"
    assert fold_continuations(data) == data


def test_quoted_expression_with_spaces_is_untouched():
    data = b"M1 a b ad='int((nf+1)/2) * w/nf * 0.29'\n"
    assert fold_continuations(data) == data


def test_crlf_input_folds_to_lf_cards():
    data = b"M1 a b L=0.15\r\n+ W=1\r\n"
    assert fold_continuations(data) == b"M1 a b L=0.15 W=1\n"


def test_trailing_lone_continuation_vanishes():
    data = b"M1 a b\n+\n"
    assert fold_continuations(data) == b"M1 a b\n"


def test_no_continuations_is_byte_identical():
    assert fold_continuations(_NETLIST_BODY.encode()) == _NETLIST_BODY.encode()


def test_fold_is_idempotent():
    folded = fold_continuations(_WRAPPED_BODY.encode())
    assert fold_continuations(folded) == folded


def test_compare_side_still_folds_across_comments_with_ignore_comments():
    """The compare-only behaviour the writer must NOT gain: with
    ``ignore_comments`` a dropped comment does not reset the join, so a
    ``+`` folds onto the card before the comment (issue #4525)."""
    data = b"M1 a b\n* header\n+ c\n"
    assert _normalize_layout(data, True) == b"M1 a b c"
    assert _normalize_layout(data) == b"M1 a b\n* header\n+ c\n"


# --------------------------------------------------------------------------- #
# The flag on the local write path
# --------------------------------------------------------------------------- #


def test_flag_folds_the_written_netlist(tmp_path, argv_log):
    schematic, binary, output = _setup(tmp_path)
    report = run_netlist(
        schematic, str(output), xschem_binary=binary, canonical_wrap=True
    )
    text = output.read_text()
    assert report["canonical_wrap"] is True
    assert not any(line.lstrip().startswith("+") for line in text.splitlines()), text
    # The folded device card and the collapsed whitespace of C0's card:
    assert "XM1 a b a a sky130_fd_pr__nfet_01v8 L=0.15 W=1\n" in text
    assert "C0 a gnd 10f\n" in text
    # Comments keep their bytes -- including the unsorted **.subckt ports:
    assert "**.subckt block b a\n" in text
    assert "** sym_path: /elsewhere/block.sym\n" in text
    assert "** klt-netlist-inputs: " in text  # provenance header untouched
    assert report["netlist"]["bytes"] == len(output.read_bytes())


def test_default_off_writes_todays_bytes(tmp_path, argv_log):
    schematic, binary, output = _setup(tmp_path)
    report = run_netlist(schematic, str(output), xschem_binary=binary)
    assert report["canonical_wrap"] is False
    # The stub's `printf '%s\n'` adds one trailing newline after the body.
    assert output.read_text() == _written(_WRAPPED_BODY) + "\n"


def test_canonical_bytes_are_a_fixed_point_of_check(tmp_path, argv_log):
    """A netlist written with --canonical-wrap checks `match` -- with and
    without the flag on the checking run."""
    schematic, binary, output = _setup(tmp_path)
    run_netlist(schematic, str(output), xschem_binary=binary, canonical_wrap=True)
    for wrap in (False, True):
        report = run_netlist(
            schematic,
            str(output),
            check=True,
            xschem_binary=binary,
            canonical_wrap=wrap,
        )
        assert report["status"] == "match"


def test_canonicalizing_twice_is_byte_identical(tmp_path, argv_log):
    schematic, binary, first = _setup(tmp_path)
    second = tmp_path / "again.spice"
    run_netlist(schematic, str(first), xschem_binary=binary, canonical_wrap=True)
    run_netlist(schematic, str(second), xschem_binary=binary, canonical_wrap=True)
    assert first.read_bytes() == second.read_bytes()


def test_netlist_without_continuations_is_byte_identical(tmp_path, monkeypatch):
    """A fold-free netlist gains nothing and loses nothing: canonical output
    is byte-identical to the un-flagged run."""
    monkeypatch.setenv("NETLIST_BODY", _NETLIST_BODY)
    schematic, binary, plain = _setup(tmp_path)
    canonical = tmp_path / "canonical.spice"
    run_netlist(schematic, str(plain), xschem_binary=binary)
    run_netlist(schematic, str(canonical), xschem_binary=binary, canonical_wrap=True)
    assert plain.read_bytes() == canonical.read_bytes()


def test_two_xschem_builds_write_identical_bytes_under_all_three_flags(
    tmp_path, monkeypatch
):
    """The issue's end-to-end acceptance, offline: the only inputs a written
    netlist takes from its host are the wrap column, the checkout paths and
    the xschem version. Two stub bodies standing in for two hosts' builds --
    same tokens, different wrapping and checkout paths -- write byte-identical
    files under ``--canonical-wrap --relative-paths --no-provenance-header``."""
    host_a = (
        "* stub netlist\n"
        ".subckt block a b\n"
        "XM1 a b a a sky130_fd_pr__nfet_01v8\n"
        "+ L=0.15\n"
        "+ W=1\n"
        "** sch_path: /home/hostA/repo/design/block.sch\n"
        ".ends\n"
        ".end"
    )
    host_b = (
        "* stub netlist\n"
        ".subckt block a b\n"
        "XM1 a b a a\n"
        "+ sky130_fd_pr__nfet_01v8 L=0.15 W=1\n"
        "** sch_path: /srv/hostB/checkout/design/block.sch\n"
        ".ends\n"
        ".end"
    )
    outputs = []
    for body in (host_a, host_b):
        work = tmp_path / f"checkout-{len(outputs)}"
        work.mkdir()
        monkeypatch.setenv("NETLIST_BODY", body)
        schematic, binary, output = _setup(work)
        run_netlist(
            schematic,
            str(output),
            xschem_binary=binary,
            canonical_wrap=True,
            relative_paths=True,
            provenance_header=False,
        )
        outputs.append(output.read_bytes())
    assert outputs[0] == outputs[1]
    assert outputs[0] == (
        b"* stub netlist\n"
        b".subckt block a b\n"
        b"XM1 a b a a sky130_fd_pr__nfet_01v8 L=0.15 W=1\n"
        b"** sch_path: block.sch\n"
        b".ends\n"
        b".end\n"
    )


def test_canonical_netlist_reads_back_with_the_same_counts(tmp_path, argv_log):
    """A canonically-written netlist is consumable downstream: through the
    ``NetlistSpiceReader`` path ``klt lvs`` uses on its reference side it
    parses without error and carries the same circuit/net/device counts as
    the un-flagged run."""
    import klayout.db as kdb

    schematic, binary, plain = _setup(tmp_path)
    canonical = tmp_path / "canonical.spice"
    run_netlist(schematic, str(plain), xschem_binary=binary)
    run_netlist(schematic, str(canonical), xschem_binary=binary, canonical_wrap=True)

    def counts(path):
        netlist = kdb.Netlist()
        netlist.read(str(path), kdb.NetlistSpiceReader())
        return [
            (
                circuit.name,
                sum(1 for _ in circuit.each_net()),
                sum(1 for _ in circuit.each_device()),
            )
            for circuit in netlist.each_circuit()
        ]

    assert counts(canonical) == counts(plain)


def test_check_mode_payload_has_no_canonical_wrap(tmp_path, argv_log):
    """The field mirrors ``provenance_header``: generate mode only."""
    schematic, binary, output = _setup(tmp_path)
    output.write_text(_written(_WRAPPED_BODY) + "\n", encoding="utf-8")
    report = run_netlist(
        schematic,
        str(output),
        check=True,
        xschem_binary=binary,
        canonical_wrap=True,
    )
    assert report["status"] == "match"
    assert "canonical_wrap" not in report


def test_cli_flag_threads_through_to_json(tmp_path, argv_log, capsys):
    schematic, binary, output = _setup(tmp_path)
    code = main(
        [
            "netlist",
            schematic,
            "-o",
            str(output),
            "--xschem-binary",
            binary,
            "--canonical-wrap",
            "--format",
            "json",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["canonical_wrap"] is True
    assert "XM1 a b a a sky130_fd_pr__nfet_01v8 L=0.15 W=1\n" in output.read_text()


def test_cli_without_flag_reports_false(tmp_path, argv_log, capsys):
    schematic, binary, output = _setup(tmp_path)
    code = main(
        [
            "netlist",
            schematic,
            "-o",
            str(output),
            "--xschem-binary",
            binary,
            "--format",
            "json",
        ]
    )
    assert code == 0
    assert json.loads(capsys.readouterr().out)["canonical_wrap"] is False


# --------------------------------------------------------------------------- #
# The flag on the batch write path (client-side post-processing)
# --------------------------------------------------------------------------- #

_BATCH_BODY = (
    f"** sch_path: {RUNNER_TREE}/design/block.sch\n"
    "**.subckt block a b\n"
    "XM1 a b a a sky130_fd_pr__nfet_01v8\n"
    "+ L=0.15 W=1\n"
    "**.ends\n"
    ".end\n"
)

_BATCH_FOLDED_CARD = "XM1 a b a a sky130_fd_pr__nfet_01v8 L=0.15 W=1\n"


def test_batch_applies_the_fold(tmp_path, batch_repo, batch_fake, no_local_xschem):
    fake = batch_fake
    repo = batch_repo
    fake.statuses = [_status("done")]
    fake.outputs = _runner_outputs(_BATCH_BODY)
    output = tmp_path / "block.spice"
    report = run_netlist_batch(
        str(repo / "design" / "block.sch"),
        str(output),
        timeout_s=60,
        pdk="sky130A",
        canonical_wrap=True,
        runner=fake,
        sleep=lambda _s: None,
    )
    assert report["canonical_wrap"] is True
    text = output.read_text()
    assert _BATCH_FOLDED_CARD in text
    assert "+ L=0.15 W=1" not in text


def test_batch_default_off_leaves_the_runners_bytes(tmp_path, batch_repo, batch_fake):
    fake = batch_fake
    repo = batch_repo
    fake.statuses = [_status("done")]
    fake.outputs = _runner_outputs(_BATCH_BODY)
    output = tmp_path / "block.spice"
    report = run_netlist_batch(
        str(repo / "design" / "block.sch"),
        str(output),
        timeout_s=60,
        pdk="sky130A",
        runner=fake,
        sleep=lambda _s: None,
    )
    assert report["canonical_wrap"] is False
    assert "+ L=0.15 W=1\n" in output.read_text()
    assert _BATCH_FOLDED_CARD not in output.read_text()
