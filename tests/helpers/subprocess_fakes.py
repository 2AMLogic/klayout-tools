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

from klayout_tools import sim


def fake_completed(
    stdout: str = "",
    returncode: int = 0,
    stderr: str = "",
) -> subprocess.CompletedProcess:
    """Build a stand-in `subprocess.CompletedProcess` for mocked subprocess calls."""
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


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
