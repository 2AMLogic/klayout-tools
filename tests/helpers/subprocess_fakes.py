"""Shared `subprocess.CompletedProcess` test double (issue #831).

`fake_completed()` stands in for the stdlib's `subprocess.CompletedProcess`
wherever a test stubs out `subprocess.run`/`subprocess.Popen` and only needs
`returncode`/`stdout`/`stderr` back. It replaces a dozen near-identical local
`class _FakeCompleted:` / `_FakeCompletedProcess` / `_FakeNetgenCompleted`
definitions that had drifted across the test suite -- most byte-identical,
a few dropping a default or a field.

Parameter order is `(stdout, returncode, stderr)` -- `stdout` first, *not*
`returncode` first -- to match the several call sites inherited from the old
`_FakeCompleted`/`_FakeNetgenCompleted` classes that pass a single positional
string argument (e.g. `_FakeCompleted("** ngspice-99\\n")`). Putting
`returncode` first would silently rebind that positional string to
`returncode` instead of `stdout`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from klayout_tools import _openroad_engine, sim, synthesize


def fake_completed(
    stdout: str = "",
    returncode: int = 0,
    stderr: str = "",
) -> subprocess.CompletedProcess:
    """Build a stand-in `subprocess.CompletedProcess` for mocked subprocess calls."""
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


def read_job_contract(argv, specs):
    """Serve a stored job.json, or an uncapped contract for a pre-existing job."""
    uri, destination = argv[-2:]
    assert uri.endswith("/job.json") and destination == "-"
    spec = specs.get(uri.split("/")[-2])
    return fake_completed(json.dumps(spec.to_job_json() if spec else {"cmd": "true"}))


class FakePopen:
    """Completed OpenROAD invocation with the engine's Popen interface."""

    pid = (1 << 31) - 1  # representable as pid_t, above the kernel PID limit

    def __init__(self, completed: subprocess.CompletedProcess) -> None:
        self._completed = completed
        self.returncode = None

    def poll(self):
        # Report the process as already exited: the engine's launch-time
        # container-id poll gives up immediately rather than waiting out
        # its full bound in every stubbed test (a real child would be
        # running here and report None).
        return self._completed.returncode

    def communicate(self, timeout=None):
        self.returncode = self._completed.returncode
        return self._completed.stdout, self._completed.stderr

    def wait(self, timeout=None):
        return self.returncode


def patch_openroad_run(monkeypatch, fake_run: Callable) -> None:
    """Share one stub between blocking probes and the isolated OpenROAD launch."""

    def popen(argv, **kwargs):
        return FakePopen(subprocess.run(argv, **kwargs))

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(_openroad_engine, "Popen", popen)


_STUB_VERSION_BANNER = "ngspice-99 : stub banner\n"


def _stub_subprocess_run(
    monkeypatch,
    *,
    log_text: str = "",
    stdout: str = "** ngspice-99\n",
    side_effect=None,
    returncode: int = 0,
):
    """Patch `subprocess.run` so `run_sim` runs without the ngspice binary."""
    real_run = subprocess.run

    def fake_run(cmd, capture_output, text, timeout, cwd=None, env=None, **kw):
        if "--version" in cmd:  # the once-per-sweep engine_build probe (#3095)
            return fake_completed(_STUB_VERSION_BANNER)
        if "-o" not in cmd:
            # `subprocess.run` is patched on the shared module object, so calls
            # that are not the ngspice run this stub stands in for land here
            # too -- e.g. `pdk._git_run`'s `git -C <variant> rev-parse` probe
            # of a git-backed PDK variant. ngspice always gets `-o <log>`
            # (issue #5512).
            kw.update(capture_output=capture_output, text=text, timeout=timeout)
            return real_run(cmd, cwd=cwd, env=env, **kw)
        log_path = cmd[cmd.index("-o") + 1]
        if side_effect is not None:
            raise side_effect
        with open(log_path, "w", encoding="utf-8") as handle:
            handle.write(log_text)
        return fake_completed(stdout, returncode=returncode)

    monkeypatch.setattr(sim.subprocess, "run", fake_run)


_COVERAGE_SUMMARY = """\
Coverage Summary:
  line      : 100.0% (  6/  6)
  toggle    : 60.0% (102/170)
  branch    : 100.0% (  4/  4)
  expr      : 100.0% (  5/  5)
  fsm_state : 0.0% (  0/  0)
  fsm_arc   : 0.0% (  0/  0)
"""


def _stub_verilator_coverage(monkeypatch, *, summary=_COVERAGE_SUMMARY, returncode=0):
    """Patch `subprocess.run` so a `klt functional-verification` run reads a
    canned `verilator_coverage` summary without the binary.

    Moved verbatim from `test_functional_verification.py` (size-ratchet
    baseline) with one addition (issue #5550): anything that is not
    `verilator_coverage` goes to the real `subprocess.run` untouched, as
    `_stub_subprocess_run` does -- the response's `provenance` block runs a
    build-identity `git` probe on a dev checkout through the same patched
    module attribute.
    """
    real_run = subprocess.run

    def fake_run(cmd, **kwargs):
        if cmd[0] != "verilator_coverage":
            return real_run(cmd, **kwargs)
        if cmd[1] == "--write-info":
            with open(cmd[2], "w", encoding="utf-8") as handle:
                handle.write("TN:verilator_coverage\nSF:gcd.v\nDA:15,538\n")
            return fake_completed(returncode=returncode)
        return fake_completed(returncode=returncode, stdout=summary)

    monkeypatch.setattr(subprocess, "run", fake_run)


def _upload_objects(
    argv: list[str], source: str, destination: str
) -> list[tuple[Path, str]]:
    """Every ``(local file, destination key)`` pair one ``cp`` upload
    ships.

    Since klayout-tools#5539 a job's whole ``inputs/`` tree goes up in a
    **single** recursive copy of a local staging directory, so the fake S3
    transports that were written for one object per call unpack that shape
    here rather than each growing its own copy of the walk.
    """
    if "--recursive" in argv and destination.endswith("/inputs"):
        root = Path(source)
        return [
            (path, f"{destination}/{path.relative_to(root).as_posix()}")
            for path in sorted(root.rglob("*"))
            if path.is_file()
        ]
    return [(Path(source), destination)]


def keep_uploaded(
    argv: list[str], source: str, destination: str, keep_dir: Path
) -> dict[str, Path]:
    """Copy every object one ``cp`` upload ships into ``keep_dir``, keyed
    by basename, and return what was kept -- a fake S3 transport merges the
    result into its own ``uploads`` record (issue #5539)."""
    kept: dict[str, Path] = {}
    for path, key in _upload_objects(argv, source, destination):
        name = key.rsplit("/", 1)[-1]
        target = Path(keep_dir) / name
        shutil.copyfile(path, target)
        kept[name] = target
    return kept


def replay_upload(
    argv: list[str],
    source: str,
    destination: str,
    handle: Callable[[Path, str], None],
) -> None:
    """Call ``handle(local file, destination key)`` for every object one
    ``cp`` upload ships -- :func:`keep_uploaded`'s sibling for a fake that
    records the full key rather than a basename (issue #5539)."""
    for path, key in _upload_objects(argv, source, destination):
        handle(path, key)


#: Joined-argv substring that identifies the **one** ``aws s3 cp
#: --recursive`` call that uploads a job's whole ``inputs/`` tree (issue
#: #5539). Usable as a `_FakeRunner.queue` key / `argvs_matching` needle
#: where the pre-#5539 tests matched a single object's key.
INPUTS_UPLOAD_ARGV = "/inputs --recursive"


def inputs_upload_of(argv: list[str]) -> tuple[str, str] | None:
    """``(local staged inputs dir, ``s3://.../inputs`` prefix)`` when
    ``argv`` is a job's recursive ``inputs/`` upload, else ``None``.

    Since #5539 ``submit_job`` ships every input in one call, so a test
    that used to recognise an upload by its own object key (``"inputs/
    request.json" in joined``) recognises this call instead and looks the
    file up inside the staged tree.
    """
    if "cp" not in argv or "--recursive" not in argv:
        return None
    source, destination = argv[argv.index("cp") + 1 : argv.index("cp") + 3]
    if not destination.endswith("/inputs"):
        return None
    return source, destination


def uploaded_input_bytes(argv: list[str]) -> dict[str, bytes] | None:
    """Every staged input's bytes, keyed by its ``inputs/``-relative name,
    for a recursive ``inputs/`` upload ``argv`` -- readable only while that
    call is in flight (the staging directory is a `TemporaryDirectory`)."""
    pair = inputs_upload_of(argv)
    if pair is None:
        return None
    root = pair[0]
    staged: dict[str, bytes] = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            path = os.path.join(dirpath, name)
            staged[os.path.relpath(path, root)] = Path(path).read_bytes()
    return staged


def uploaded_request_of(argv: list[str]) -> tuple[dict, str] | None:
    """``(the uploaded worker request document, that job's id)`` when
    ``argv`` is a job's recursive ``inputs/`` upload carrying a
    ``request.json``, else ``None``."""
    pair = inputs_upload_of(argv)
    if pair is None:
        return None
    source, destination = pair
    document = Path(source, "request.json")
    if not document.is_file():
        return None
    job_id = destination.partition("/jobs/")[2].split("/")[0]
    return json.loads(document.read_text()), job_id


def _patch_yosys_run(monkeypatch, fake) -> None:
    """Install a ``subprocess.run``-shaped fake across the Yosys spawns.

    Every Yosys invocation -- the ``yosys -s`` script run and the ``yosys
    -V`` / ``help abc`` capability probes -- spawns through
    ``_engine_proc.run_in_group``, which delegates to ``subprocess.run``
    (start_new_session + group kill, issue #5871). Patching ``run`` on the
    shared stdlib module intercepts all of them; a mocked ``run`` spawns
    nothing, so ``run_in_group`` records no pid and kills nothing.

    Tests exercising the REAL spawn path (the process-group regression
    tests in ``test_synthesize_timeout.py``) patch nothing and put a stub
    ``yosys`` binary on ``PATH`` instead.
    """
    monkeypatch.setattr(synthesize.subprocess, "run", fake)
