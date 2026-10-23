"""Tests for the shared output-directory lock (issue #2770).

The contention and crash-recovery tests use **real second processes** and
synchronise on explicit pipe events (the holder prints ``READY`` once it owns
the lock and exits when its stdin closes) -- no sleeps. Engines are mocked.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

from helpers.subprocess_fakes import fake_completed
from klayout_tools import _paths as paths_module
from klayout_tools import output_lock, pex, place_and_route, sim
from klayout_tools.cli import main

pytestmark = [
    pytest.mark.skipif(
        output_lock.fcntl is None, reason="output-directory locking needs fcntl.flock"
    ),
    pytest.mark.usefixtures("real_build_identity_git"),
]

_HOLDER = textwrap.dedent(
    """
    import sys
    from klayout_tools import output_lock

    with output_lock.hold_output_dir(sys.argv[1]):
        print("READY", flush=True)
        sys.stdin.read()  # released when the parent closes the pipe / dies
    print("RELEASED", flush=True)
    """
)


@pytest.fixture(autouse=True)
def _lock_registry(tmp_path_factory, monkeypatch):
    registry = tmp_path_factory.mktemp("klt-lock-registry")
    monkeypatch.setenv(output_lock.LOCK_DIR_ENV, str(registry))
    return registry


class Holder:
    """A real second process that owns ``path`` until released or killed."""

    def __init__(self, path: Path) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-c", _HOLDER, str(path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        assert self.proc.stdout is not None
        assert self.proc.stdout.readline().strip() == "READY"

    def release(self) -> None:
        assert self.proc.stdin is not None and self.proc.stdout is not None
        self.proc.stdin.close()
        assert self.proc.stdout.readline().strip() == "RELEASED"
        self.proc.wait()
        self.proc.stdout.close()

    def kill(self) -> None:
        self.proc.send_signal(signal.SIGKILL)
        self.proc.wait()
        assert self.proc.stdin is not None and self.proc.stdout is not None
        self.proc.stdin.close()
        self.proc.stdout.close()


@pytest.fixture
def holder_factory():
    holders: list[Holder] = []

    def make(path: Path) -> Holder:
        holder = Holder(path)
        holders.append(holder)
        return holder

    yield make
    for holder in holders:
        if holder.proc.poll() is None:
            holder.kill()


# --------------------------------------------------------------------------- #
# Helper: contention, diagnostics, recovery
# --------------------------------------------------------------------------- #


def test_second_process_is_refused_with_owner_diagnostics(tmp_path, holder_factory):
    root = tmp_path / "out"
    holder = holder_factory(root)

    with pytest.raises(output_lock.OutputDirLockedError) as info:
        with output_lock.hold_output_dir(root):
            pytest.fail("must not be acquired")

    err = info.value
    assert err.code == "outdir_locked"
    assert err.owner_pid == holder.proc.pid
    assert err.owner_acquired_at
    assert err.directory == os.path.realpath(root)
    assert str(holder.proc.pid) in str(err)
    # Refusal (and ownership) created nothing under the protected root.
    assert not root.exists()


def test_release_then_retry_succeeds_repeatedly(tmp_path, holder_factory):
    root = tmp_path / "out"
    for _ in range(3):  # repeated cycles catch split-inode races
        holder = holder_factory(root)
        with pytest.raises(output_lock.OutputDirLockedError):
            with output_lock.hold_output_dir(root):
                pass
        holder.release()
        with output_lock.hold_output_dir(root):
            pass


def test_killed_owner_leaves_stale_metadata_but_no_lock(tmp_path, holder_factory):
    root = tmp_path / "out"
    holder = holder_factory(root)
    holder.kill()

    lock_file = output_lock._lock_file_for(
        output_lock._registry_dir(), output_lock.canonical_dir(root)
    )
    assert json.loads(Path(lock_file).read_text())["pid"] == holder.proc.pid

    with output_lock.hold_output_dir(root):
        assert json.loads(Path(lock_file).read_text())["pid"] == os.getpid()


def test_exception_inside_block_releases(tmp_path):
    root = tmp_path / "out"
    with pytest.raises(RuntimeError):
        with output_lock.hold_output_dir(root):
            raise RuntimeError("boom")
    with output_lock.hold_output_dir(root):
        pass


@pytest.mark.parametrize(
    "garbage", [b"", b"not json {{", b'{"pid": "x"}', b"\xff\xfe", b"[1,2]"]
)
def test_malformed_metadata_reports_unknown_owner_and_still_blocks(
    tmp_path, holder_factory, garbage
):
    root = tmp_path / "out"
    holder = holder_factory(root)
    lock_file = output_lock._lock_file_for(
        output_lock._registry_dir(), output_lock.canonical_dir(root)
    )
    with open(lock_file, "r+b") as handle:  # same inode, diagnostics only
        handle.truncate(0)
        handle.write(garbage)

    with pytest.raises(output_lock.OutputDirLockedError) as info:
        with output_lock.hold_output_dir(root):
            pass
    assert info.value.owner_pid is None
    assert "owner unknown" in str(info.value)
    holder.release()


def test_symlink_and_relative_aliases_contend(tmp_path, holder_factory, monkeypatch):
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    holder_factory(real)

    with pytest.raises(output_lock.OutputDirLockedError):
        with output_lock.hold_output_dir(alias):
            pass
    monkeypatch.chdir(tmp_path)
    with pytest.raises(output_lock.OutputDirLockedError):
        with output_lock.hold_output_dir("real/../real"):
            pass


def test_unrelated_roots_are_concurrent(tmp_path, holder_factory):
    holder_factory(tmp_path / "a")
    with output_lock.hold_output_dir(tmp_path / "b"):
        pass


def test_ancestor_and_descendant_exclude_each_other(tmp_path, holder_factory):
    parent = tmp_path / "pex"
    child = parent / "tb" / "schematic"

    holder = holder_factory(parent)
    with pytest.raises(output_lock.OutputDirLockedError) as info:
        with output_lock.hold_output_dir(child):
            pass
    assert info.value.relation == "ancestor"
    assert info.value.owner_pid == holder.proc.pid
    holder.release()

    holder = holder_factory(child)
    with pytest.raises(output_lock.OutputDirLockedError) as info:
        with output_lock.hold_output_dir(parent):
            pass
    # A descendant owner holds only a shared claim: owner is reported unknown
    # rather than attributing a stale record to the wrong process.
    assert info.value.relation == "descendant"
    assert info.value.owner_pid is None
    holder.release()


def test_distinct_descendants_of_a_common_ancestor_are_concurrent(
    tmp_path, holder_factory
):
    holder_factory(tmp_path / "pex" / "a")
    with output_lock.hold_output_dir(tmp_path / "pex" / "b"):
        pass


def test_same_thread_reentry_is_free_for_self_and_descendants(tmp_path):
    root = tmp_path / "out"
    with output_lock.hold_output_dir(root):
        with output_lock.hold_output_dir(root):
            with output_lock.hold_output_dir(root / "x" / "y"):
                pass
        # still owned after the nested exits
        with pytest.raises(output_lock.OutputDirLockedError):
            _in_other_thread(root)
    _in_other_thread(root)


def _in_other_thread(path: Path) -> None:
    """Acquire ``path`` from a fresh thread; re-raise what it raised."""
    caught: list[BaseException] = []

    def target() -> None:
        try:
            with output_lock.hold_output_dir(path):
                pass
        except BaseException as exc:  # noqa: BLE001
            caught.append(exc)

    thread = threading.Thread(target=target)
    thread.start()
    thread.join()
    if caught:
        raise caught[0]


def test_owning_descendant_then_asking_for_ancestor_is_refused(tmp_path):
    with output_lock.hold_output_dir(tmp_path / "pex" / "tb"):
        with pytest.raises(output_lock.OutputDirLockedError):
            with output_lock.hold_output_dir(tmp_path / "pex"):
                pass


def test_unwritable_registry_is_unavailable_not_locked(tmp_path, monkeypatch):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setenv(output_lock.LOCK_DIR_ENV, str(blocker / "registry"))
    with pytest.raises(output_lock.OutputLockUnavailableError) as info:
        with output_lock.hold_output_dir(tmp_path / "out"):
            pass
    assert info.value.code == "outdir_lock_unavailable"
    assert not isinstance(info.value, output_lock.OutputDirLockedError)


def test_missing_flock_support_fails_explicitly(tmp_path, monkeypatch):
    monkeypatch.setattr(output_lock, "fcntl", None)
    with pytest.raises(output_lock.OutputLockUnavailableError, match="flock"):
        with output_lock.hold_output_dir(tmp_path / "out"):
            pass


# --------------------------------------------------------------------------- #
# Entry points (engines mocked)
# --------------------------------------------------------------------------- #


def _sim_request(tmp_path: Path, *, keep_artifacts: bool) -> Path:
    (tmp_path / "body.spice").write_text(
        ".param vdd=1.0\nVdd vdd 0 DC {vdd}\nR1 vdd out 1k\nC1 out 0 1n\n"
    )
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "netlist": "body.spice",
                "analysis": {"kind": "tran", "args": "1n 1u"},
                "options": {"keep_artifacts": keep_artifacts},
                "measurements": [
                    {"name": "vout", "spice": ".meas tran vout FIND v(out) AT=1u"}
                ],
            }
        )
    )
    return request


@pytest.fixture
def stub_ngspice(monkeypatch):
    calls: list[list[str]] = []
    real_which = paths_module.shutil.which
    monkeypatch.setattr(
        paths_module.shutil,
        "which",
        lambda cmd, *a, **k: cmd if cmd == "ngspice" else real_which(cmd, *a, **k),
    )

    def fake_run(cmd, capture_output, text, timeout, cwd=None):
        calls.append(list(cmd))
        with open(cmd[cmd.index("-o") + 1], "w", encoding="utf-8") as handle:
            handle.write("vout                =  1.00000e+00\n")
        return fake_completed("** ngspice-99\n")

    monkeypatch.setattr(sim.subprocess, "run", fake_run)
    return calls


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def test_run_sim_persistent_run_is_refused_without_touching_artifacts(
    tmp_path, holder_factory, stub_ngspice
):
    request = _sim_request(tmp_path, keep_artifacts=True)
    artifacts = tmp_path / ".klt" / "sim"
    artifacts.mkdir(parents=True)
    (artifacts / "sentinel.txt").write_text("owner data")
    before = _snapshot(artifacts)
    holder = holder_factory(artifacts)

    with pytest.raises(output_lock.OutputDirLockedError):
        sim.run_sim(str(request))

    assert stub_ngspice == []
    assert _snapshot(artifacts) == before

    holder.release()
    report = sim.run_sim(str(request))
    assert report["status"] == "pass"
    assert stub_ngspice


def test_run_sim_temporary_only_run_takes_no_lock(
    tmp_path, holder_factory, stub_ngspice
):
    request = _sim_request(tmp_path, keep_artifacts=False)
    holder_factory(tmp_path / ".klt" / "sim")
    assert sim.run_sim(str(request))["status"] == "pass"


def test_run_sim_resume_and_plot_dir_are_persistent(
    tmp_path, holder_factory, stub_ngspice
):
    request = _sim_request(tmp_path, keep_artifacts=False)
    holder = holder_factory(tmp_path / ".klt" / "sim")
    with pytest.raises(output_lock.OutputDirLockedError):
        sim.run_sim(str(request), resume=True)
    holder.release()

    plots = tmp_path / "plots"
    holder = holder_factory(plots)
    with pytest.raises(output_lock.OutputDirLockedError):
        sim.run_sim(str(request), plot_dir=str(plots))
    assert not plots.exists()


def test_run_sim_explicit_artifacts_dir_contends_via_alias(
    tmp_path, holder_factory, stub_ngspice
):
    request = _sim_request(tmp_path, keep_artifacts=True)
    real = tmp_path / "real"
    alias = tmp_path / "alias"
    real.mkdir()
    alias.symlink_to(real, target_is_directory=True)
    holder_factory(real)
    with pytest.raises(output_lock.OutputDirLockedError):
        sim.run_sim(str(request), artifacts_dir=str(alias))


def test_run_sim_artifacts_dir_inside_plot_dir_does_not_self_contend(
    tmp_path, stub_ngspice
):
    request = _sim_request(tmp_path, keep_artifacts=True)
    plots = tmp_path / "plots"
    report = sim.run_sim(
        str(request), artifacts_dir=str(plots / "artifacts"), plot_dir=str(plots)
    )
    assert report["status"] == "pass"
    _in_other_thread(plots)  # released afterwards


def test_nested_sim_inherits_pex_style_ancestor_ownership(tmp_path, stub_ngspice):
    request = _sim_request(tmp_path, keep_artifacts=True)
    work = tmp_path / "pexroot"
    with output_lock.hold_output_dir(work):
        report = sim.run_sim(str(request), artifacts_dir=str(work / "tb" / "schematic"))
    assert report["status"] == "pass"


def _deck_request_path(tmp_path: Path) -> Path:
    from test_sim_decks import deck_request, write_request

    return write_request(tmp_path, deck_request(tmp_path))


def test_run_sim_deck_list_default_outdir_is_owned(tmp_path, holder_factory):
    request = _deck_request_path(tmp_path)
    outdir = tmp_path / ".klt" / "sim"
    outdir.mkdir(parents=True)
    (outdir / "sentinel.txt").write_text("owner data")
    before = _snapshot(outdir)
    holder = holder_factory(outdir)

    with pytest.raises(output_lock.OutputDirLockedError):
        sim.run_sim(str(request))
    assert _snapshot(outdir) == before

    holder.release()
    assert sim.run_sim(str(request))["status"] == "pass"


def test_run_sim_deck_list_explicit_outdir_contends_via_alias(tmp_path, holder_factory):
    request = _deck_request_path(tmp_path)
    real = tmp_path / "real"
    alias = tmp_path / "alias"
    real.mkdir()
    alias.symlink_to(real, target_is_directory=True)
    holder_factory(real)

    with pytest.raises(output_lock.OutputDirLockedError):
        sim.run_sim(str(request), artifacts_dir=str(alias))
    assert _snapshot(real) == {}


def test_run_sim_deck_list_nested_under_owned_ancestor_does_not_self_contend(
    tmp_path,
):
    request = _deck_request_path(tmp_path)
    work = tmp_path / "pexroot"
    with output_lock.hold_output_dir(work):
        report = sim.run_sim(str(request), artifacts_dir=str(work / "tb"))
    assert report["status"] == "pass"


def test_run_sim_deck_list_flag_errors_take_no_lock_and_create_nothing(
    tmp_path, holder_factory
):
    request = _deck_request_path(tmp_path)
    holder_factory(tmp_path / ".klt" / "sim")
    with pytest.raises(sim.SimError, match="--dry-run"):
        sim.run_sim(str(request), dry_run=True)
    assert not (tmp_path / ".klt" / "sim" / "tt_27c").exists()


def test_run_pex_default_and_explicit_roots_are_owned(tmp_path, holder_factory):
    layout = tmp_path / "cell.gds"  # never read: ownership precedes all work
    holder = holder_factory(tmp_path / ".klt" / "pex")
    with pytest.raises(output_lock.OutputDirLockedError):
        pex.run_pex(str(layout), [], "deck")
    holder.release()

    work = tmp_path / "work"
    holder = holder_factory(work / "tb" / "extracted")  # protected descendant
    with pytest.raises(output_lock.OutputDirLockedError):
        pex.run_pex(str(layout), [], "deck", artifacts_dir=str(work))
    assert not (tmp_path / ".klt").exists()


def test_run_pex_without_contention_gets_past_the_lock(tmp_path):
    with pytest.raises(pex.PexError):  # bad invocation, not a lock error
        pex.run_pex(str(tmp_path / "cell.gds"), [], "deck")


def test_run_place_and_route_owns_its_stage_root(tmp_path, holder_factory):
    request = tmp_path / "req.json"
    request.write_text("{}")
    holder = holder_factory(tmp_path / ".klt" / "place-and-route")
    with pytest.raises(output_lock.OutputDirLockedError):
        place_and_route.run_place_and_route(str(request))
    holder.release()
    with pytest.raises(place_and_route.PlaceAndRouteError):  # past the lock
        place_and_route.run_place_and_route(str(request))


# --------------------------------------------------------------------------- #
# CLI: typed error through the shared envelope
# --------------------------------------------------------------------------- #


def _assert_locked_envelope(capsys, command: str, fmt: str, pid: int) -> None:
    captured = capsys.readouterr()
    assert captured.out == ""
    if fmt == "json":
        payload = json.loads(captured.err)
        assert payload["schema_version"] == 1
        error = payload["error"]
        assert error["command"] == command
        assert error["code"] == "outdir_locked"
        assert str(pid) in error["message"]
    else:
        assert captured.err.startswith(f"klt {command}: ")
        assert "owned by another run" in captured.err


@pytest.mark.parametrize("fmt", ["json", "text"])
def test_cli_sim_contention_is_exit_1_empty_stdout(
    tmp_path, holder_factory, stub_ngspice, capsys, fmt
):
    request = _sim_request(tmp_path, keep_artifacts=True)
    holder = holder_factory(tmp_path / ".klt" / "sim")
    assert main(["sim", str(request), "--format", fmt]) == 1
    _assert_locked_envelope(capsys, "sim", fmt, holder.proc.pid)


@pytest.mark.parametrize("fmt", ["json", "text"])
def test_cli_pex_contention_is_exit_1_empty_stdout(
    tmp_path, holder_factory, capsys, fmt
):
    holder = holder_factory(tmp_path / "work")
    code = main(
        [
            "pex",
            str(tmp_path / "cell.gds"),
            "--deck",
            "sky130",
            "--outdir",
            str(tmp_path / "work"),
            "--format",
            fmt,
        ]
    )
    assert code == 1
    _assert_locked_envelope(capsys, "pex", fmt, holder.proc.pid)


@pytest.mark.parametrize("fmt", ["json", "text"])
def test_cli_place_and_route_contention_is_exit_1_empty_stdout(
    tmp_path, holder_factory, capsys, fmt
):
    request = tmp_path / "req.json"
    request.write_text("{}")
    holder = holder_factory(tmp_path / ".klt" / "place-and-route")
    assert main(["place-and-route", str(request), "--format", fmt]) == 1
    _assert_locked_envelope(capsys, "place-and-route", fmt, holder.proc.pid)


# --------------------------------------------------------------------------- #
# Inherited ownership for a child of ``klt pex --measure-command`` (#3291)
# --------------------------------------------------------------------------- #

_CHILD = textwrap.dedent(
    """
    import sys
    from klayout_tools import output_lock

    try:
        with output_lock.hold_output_dir(sys.argv[1]):
            print("OK")
    except output_lock.OutputDirLockedError as exc:
        print("LOCKED " + exc.relation)
    """
)


def _child_result(target: Path, inherit: Path | None) -> str:
    env = dict(os.environ)
    env.pop(output_lock.INHERIT_ENV, None)
    if inherit is not None:
        env[output_lock.INHERIT_ENV] = str(inherit)
    done = subprocess.run(
        [sys.executable, "-c", _CHILD, str(target)],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    return done.stdout.strip()


def test_child_inherits_ownership_at_and_under_marked_root(tmp_path, holder_factory):
    root = tmp_path / "out"
    marked = root / "measure" / "schematic"
    marked.mkdir(parents=True)
    holder_factory(root)

    assert _child_result(marked / ".klt" / "sim", marked) == "OK"
    assert _child_result(marked, marked) == "OK"


def test_child_without_marker_is_still_refused(tmp_path, holder_factory):
    root = tmp_path / "out"
    marked = root / "measure" / "schematic"
    marked.mkdir(parents=True)
    holder_factory(root)

    assert _child_result(marked / ".klt" / "sim", None) == "LOCKED ancestor"


def test_inheritance_never_covers_ancestor_sibling_or_unrelated(
    tmp_path, holder_factory
):
    root = tmp_path / "out"
    marked = root / "measure" / "schematic"
    marked.mkdir(parents=True)
    holder_factory(root)

    assert _child_result(root, marked) == "LOCKED same"
    assert _child_result(root / "measure" / "extracted", marked) == "LOCKED ancestor"
    # An unrelated marker path covers nothing under the held root.
    other = tmp_path / "elsewhere"
    other.mkdir()
    assert _child_result(marked / "x", other) == "LOCKED ancestor"


def test_inheritance_resolves_symlink_alias_of_marked_root(tmp_path, holder_factory):
    root = tmp_path / "out"
    marked = root / "measure" / "schematic"
    marked.mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(marked, target_is_directory=True)
    holder_factory(root)

    assert _child_result(alias / "sim", marked) == "OK"
    assert _child_result(marked / "sim", alias) == "OK"


def test_relative_marker_is_ignored(tmp_path, holder_factory):
    root = tmp_path / "out"
    root.mkdir()
    holder_factory(root)
    env = dict(os.environ, **{output_lock.INHERIT_ENV: "out"})
    done = subprocess.run(
        [sys.executable, "-c", _CHILD, str(root)],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        check=True,
    )
    assert done.stdout.strip() == "LOCKED same"
