"""Tests for the off-host `.include`/`.inc` staging closure (issue #2485,
`src/klayout_tools/sim_staging.py`).

The bug this module fixes: both off-host backends uploaded only the
request's own netlist, so a testbench that `.include`s a separate DUT file
(`klt pex`'s entire testbench contract) resolved that include on the
*executing* host, where the submitting host's path does not exist -- and
failed silently, as `unavailable_measurement` rows indistinguishable from a
real regression.

Nothing here touches a network, an `ssh`/`scp`/`aws` binary, or ngspice:
these are pure path-resolution/rewrite tests over files in `tmp_path`. The
two backend job builders that consume this module are covered in
`tests/test_remote_transport.py` and `tests/test_sim_batch.py`.
"""

from __future__ import annotations

import os

import pytest

from klayout_tools import sim_staging as st


def _write(tmp_path, name: str, text: str):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _stage(netlist_path, **overrides):
    fields = dict(netlist_staged_name="netlist.cir", reserved_names=("request.json",))
    fields.update(overrides)
    return st.stage_netlist(str(netlist_path), **fields)


# --------------------------------------------------------------------------- #
# The happy path: a testbench that `.include`s its DUT
# --------------------------------------------------------------------------- #


def test_netlist_without_includes_is_uploaded_verbatim(tmp_path):
    """The pre-#2485 shape is preserved exactly for the common case: no
    includes means no rewrite, so the file is still pushed byte-for-byte
    from its own path rather than re-serialized through `content`."""
    netlist = _write(tmp_path, "body.spice", "R1 a b 1k\nC1 b 0 1n\n")

    staged = _stage(netlist)

    assert staged.includes == ()
    assert staged.netlist.staged_name == "netlist.cir"
    assert staged.netlist.local_path == str(netlist)
    assert staged.netlist.content is None
    assert staged.files == (staged.netlist,)


def test_included_dut_is_staged_and_the_directive_rewritten(tmp_path):
    """The issue's own reproduction shape: `examples/design-pipeline/`'s
    `10-pex.testbench.spice` carrying `.include "07-reference.spice"`."""
    _write(tmp_path, "07-reference.spice", ".subckt ota_5t_layout_0 a b c d\n.ends\n")
    netlist = _write(
        tmp_path,
        "10-pex.testbench.spice",
        '* testbench\n.include "07-reference.spice"\nXota a b c d ota_5t_layout_0\n',
    )

    staged = _stage(netlist)

    assert [item.staged_name for item in staged.includes] == ["07-reference.spice"]
    assert staged.includes[0].local_path == str(tmp_path / "07-reference.spice")
    # The netlist is now a rewritten payload pointing at the staged name --
    # a job-relative filename, never the submitting host's path.
    assert staged.netlist.content is not None
    assert '.include "07-reference.spice"' in staged.netlist.content
    assert str(tmp_path) not in staged.netlist.content


def test_absolute_include_is_rewritten_to_a_job_relative_name(tmp_path):
    """An absolute submit-host path is the shape `klt pex` writes when it
    re-points a testbench's DUT line at its own extracted netlist -- it
    cannot survive the trip off-host unrewritten."""
    dut = _write(tmp_path / "elsewhere", "extracted.spice", ".subckt dut a b\n.ends\n")
    netlist = _write(tmp_path, "tb.spice", f".include {dut}\nXd a b dut\n")

    staged = _stage(netlist)

    assert [item.staged_name for item in staged.includes] == ["extracted.spice"]
    assert staged.netlist.content is not None
    assert str(dut) not in staged.netlist.content
    assert '.include "extracted.spice"' in staged.netlist.content


def test_include_closure_recurses(tmp_path):
    """The issue's wording is "recursively" -- an included file's own
    includes must be staged too, resolved relative to *that* file's
    directory (ngspice's own rule), not the top netlist's."""
    _write(tmp_path / "sub", "leaf.spice", ".subckt leaf a b\n.ends\n")
    _write(
        tmp_path / "sub",
        "mid.spice",
        '.include "leaf.spice"\n.subckt mid a b\nXl a b leaf\n.ends\n',
    )
    netlist = _write(tmp_path, "tb.spice", '.include "sub/mid.spice"\nXm a b mid\n')

    staged = _stage(netlist)

    assert sorted(item.staged_name for item in staged.includes) == [
        "leaf.spice",
        "mid.spice",
    ]
    mid = next(i for i in staged.includes if i.staged_name == "mid.spice")
    # mid.spice's own directive was rewritten too (it resolved against
    # sub/, and lands flat beside the netlist on the executing host).
    assert mid.content is not None
    assert '.include "leaf.spice"' in mid.content
    leaf = next(i for i in staged.includes if i.staged_name == "leaf.spice")
    assert leaf.local_path == str(tmp_path / "sub" / "leaf.spice")
    assert leaf.content is None


def test_include_cycle_terminates_and_stages_each_file_once(tmp_path):
    _write(tmp_path, "a.spice", '.include "b.spice"\n')
    _write(tmp_path, "b.spice", '.include "a.spice"\n')
    netlist = _write(tmp_path, "tb.spice", '.include "a.spice"\n')

    staged = _stage(netlist)

    assert sorted(item.staged_name for item in staged.includes) == [
        "a.spice",
        "b.spice",
    ]


def test_same_file_included_twice_is_staged_once(tmp_path):
    _write(tmp_path, "dut.spice", ".subckt dut a b\n.ends\n")
    netlist = _write(
        tmp_path, "tb.spice", '.include "dut.spice"\n.include "./dut.spice"\n'
    )

    staged = _stage(netlist)

    assert [item.staged_name for item in staged.includes] == ["dut.spice"]
    assert staged.netlist.content is not None
    assert staged.netlist.content.count('.include "dut.spice"') == 2


def test_basename_collision_gets_a_distinct_staged_name(tmp_path):
    """Two different files with the same basename must not overwrite each
    other in the flat job directory."""
    _write(tmp_path / "one", "dut.spice", ".subckt dut_one a b\n.ends\n")
    _write(tmp_path / "two", "dut.spice", ".subckt dut_two a b\n.ends\n")
    netlist = _write(
        tmp_path, "tb.spice", '.include "one/dut.spice"\n.include "two/dut.spice"\n'
    )

    staged = _stage(netlist)

    names = [item.staged_name for item in staged.includes]
    assert len(names) == len(set(names)) == 2
    assert "dut.spice" in names
    assert staged.netlist.content is not None
    for name in names:
        assert f'.include "{name}"' in staged.netlist.content


def test_staged_names_never_collide_with_reserved_job_filenames(tmp_path):
    """A DUT file literally named `request.json`/`netlist.cir` must not
    clobber the job's own two reserved inputs."""
    _write(tmp_path, "request.json", "* not really json\n")
    _write(tmp_path, "netlist.cir", "* a second netlist\n")
    netlist = _write(
        tmp_path, "tb.spice", '.include "request.json"\n.include "netlist.cir"\n'
    )

    staged = _stage(netlist)

    names = {item.staged_name for item in staged.includes}
    assert "request.json" not in names
    assert "netlist.cir" not in names
    assert len(names) == 2


def test_staged_names_are_always_safe_job_relative_filenames(tmp_path):
    """Path traversal defense: the staged name is generated here, never
    taken from the netlist's own target text, so nothing a netlist declares
    can write outside the remote job directory."""
    _write(tmp_path / "weird dir", "a b;c.spice", ".subckt s a b\n.ends\n")
    netlist = _write(tmp_path, "tb.spice", '.include "weird dir/a b;c.spice"\n')

    staged = _stage(netlist)

    name = staged.includes[0].staged_name
    assert not os.path.isabs(name)
    assert "/" not in name and ".." not in name
    assert name == "a_b_c.spice"


def test_directive_spelling_and_quoting_are_all_matched(tmp_path):
    _write(tmp_path, "one.spice", "* one\n")
    _write(tmp_path, "two.spice", "* two\n")
    _write(tmp_path, "three.spice", "* three\n")
    netlist = _write(
        tmp_path,
        "tb.spice",
        "  .INC one.spice\n.include 'two.spice'\n.Include \"three.spice\"\n",
    )

    staged = _stage(netlist)

    assert sorted(item.staged_name for item in staged.includes) == [
        "one.spice",
        "three.spice",
        "two.spice",
    ]


def test_non_directive_lines_are_left_byte_identical(tmp_path):
    """Only `.include`/`.inc` lines are touched -- a comment mentioning the
    word, a `.lib` card, and CRLF line endings all survive untouched."""
    _write(tmp_path, "dut.spice", "* dut\n")
    netlist = tmp_path / "tb.spice"
    netlist.write_bytes(
        b"* this deck will .include the dut\r\n"
        b".lib /pdk/sky130.lib.spice tt\r\n"
        b'.include "dut.spice"\r\n'
        b"R1 a b 1k\r\n"
    )

    staged = _stage(netlist)

    content = staged.netlist.content
    assert content is not None
    assert "* this deck will .include the dut\r\n" in content
    assert ".lib /pdk/sky130.lib.spice tt\r\n" in content
    assert '.include "dut.spice"\r\n' in content
    assert content.endswith("R1 a b 1k\r\n")


# --------------------------------------------------------------------------- #
# Refuse what cannot be staged (the issue's candidate fix 2)
# --------------------------------------------------------------------------- #


def test_unresolvable_include_raises_a_named_error(tmp_path):
    netlist = _write(tmp_path, "tb.spice", '* tb\n.include "missing-dut.spice"\n')

    with pytest.raises(st.IncludeStagingError) as excinfo:
        _stage(netlist)

    message = str(excinfo.value)
    # Actionable: names the offending file, its line, the target, and where
    # the target was looked for.
    assert "missing-dut.spice" in message
    assert "line 2" in message
    assert str(tmp_path) in message


def test_unresolvable_nested_include_raises_naming_the_included_file(tmp_path):
    _write(tmp_path, "mid.spice", '.include "nowhere.spice"\n')
    netlist = _write(tmp_path, "tb.spice", '.include "mid.spice"\n')

    with pytest.raises(st.IncludeStagingError, match="mid.spice"):
        _stage(netlist)


def test_include_closure_file_count_is_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(st, "MAX_STAGED_INCLUDES", 2)
    for index in range(3):
        _write(tmp_path, f"inc{index}.spice", f"* {index}\n")
    netlist = _write(
        tmp_path,
        "tb.spice",
        "".join(f'.include "inc{index}.spice"\n' for index in range(3)),
    )

    with pytest.raises(st.IncludeStagingError, match="exceeds 2 files"):
        _stage(netlist)


def test_include_closure_byte_budget_is_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(st, "MAX_STAGED_BYTES", 16)
    _write(tmp_path, "big.spice", "* " + "x" * 64 + "\n")
    netlist = _write(tmp_path, "tb.spice", '.include "big.spice"\n')

    with pytest.raises(st.IncludeStagingError, match="exceeds"):
        _stage(netlist)


def test_non_utf8_netlist_with_an_include_is_refused_not_re_encoded(tmp_path):
    _write(tmp_path, "dut.spice", "* dut\n")
    netlist = tmp_path / "tb.spice"
    netlist.write_bytes(b'* caf\xe9 header\n.include "dut.spice"\n')

    with pytest.raises(st.IncludeStagingError, match="UTF-8"):
        _stage(netlist)


def test_non_utf8_netlist_without_includes_still_uploads_verbatim(tmp_path):
    netlist = tmp_path / "tb.spice"
    netlist.write_bytes(b"* caf\xe9 header\nR1 a b 1k\n")

    staged = _stage(netlist)

    assert staged.netlist.local_path == str(netlist)
    assert staged.netlist.content is None


# --------------------------------------------------------------------------- #
# Includes the executing host resolves for itself
# --------------------------------------------------------------------------- #


def test_environment_variable_targets_are_left_verbatim(tmp_path):
    """`$PDK_ROOT/...` describes a path on whichever host expands it -- it
    is the executing host's to resolve, and must not be refused as
    unresolvable just because this host cannot see it."""
    netlist = _write(
        tmp_path, "tb.spice", '.include "$PDK_ROOT/libs.tech/ngspice/foo.spice"\n'
    )

    staged = _stage(netlist)

    assert staged.includes == ()
    assert staged.netlist.local_path == str(netlist)
    assert staged.host_resolved == ("$PDK_ROOT/libs.tech/ngspice/foo.spice",)


def test_pdk_rooted_includes_are_left_verbatim(tmp_path):
    """A model file under the PDK root the request already forwards is
    provisioned on the executing host (`docs/cli/sim.md`'s "Remote
    backend") -- staging it would push a PDK's whole model closure through
    the transport on every submit."""
    pdk_root = tmp_path / "pdks"
    models = _write(pdk_root / "sky130A", "models.spice", "* models\n")
    _write(tmp_path, "dut.spice", "* dut\n")
    netlist = _write(
        tmp_path, "tb.spice", f'.include "{models}"\n.include "dut.spice"\n'
    )

    staged = _stage(netlist, roots=(str(pdk_root),))

    assert [item.staged_name for item in staged.includes] == ["dut.spice"]
    assert staged.host_resolved == (str(models),)
    assert staged.netlist.content is not None
    assert str(models) in staged.netlist.content


def test_host_resolved_roots_reads_the_request_then_the_environment(monkeypatch):
    monkeypatch.setenv("PDK_ROOT", "/env/pdks")
    assert st.host_resolved_roots({"models": {"pdk_root": "/req/pdks"}}) == (
        "/req/pdks",
        "/env/pdks",
    )
    assert st.host_resolved_roots({}) == ("/env/pdks",)
    monkeypatch.delenv("PDK_ROOT")
    assert st.host_resolved_roots({}) == ()


def test_stage_sim_netlist_reraises_as_a_sim_error_naming_the_backend(tmp_path):
    """Both backends surface a staging failure as `klt sim`'s own documented
    "could not run" error (exit 1), never as a submitted job that comes back
    undiagnosable."""
    from klayout_tools.sim import SimError

    netlist = _write(tmp_path, "tb.spice", '.include "missing.spice"\n')

    with pytest.raises(SimError) as excinfo:
        st.stage_sim_netlist(
            {"models": {"pdk": "sky130A"}},
            str(netlist),
            netlist_staged_name="netlist.cir",
            reserved_names=("request.json",),
            backend="batch",
        )
    assert "backend 'batch'" in str(excinfo.value)
    assert "missing.spice" in str(excinfo.value)
