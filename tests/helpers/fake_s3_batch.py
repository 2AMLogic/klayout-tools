"""Shared fake S3 batch-transport command runner for the batch-verb tests.

Consolidates the four drifted ``_FakeRunner`` doubles previously duplicated in
``test_extract_batch.py``, ``test_netlist_batch.py``, ``test_sim_commands.py``
and ``test_signoff_generator_batch.py`` (issue #5621): a callable standing in
for both ``aws s3`` and the batch launch script that records every argv, keeps
a copy of every uploaded file under ``keep_dir``, answers ``.../status.json``
reads, and materializes ``outputs`` (text or bytes) into the collect
destination.

Two status modes. Default reads a FIFO ``statuses`` queue of prepared JSON
documents (an empty queue answers ``NoSuchKey``). Construct with ``job_id``
(and optionally ``tool``) to always answer with the fixed 8-key document built
from the ``state``/``exit_code`` fields, so a test can flip ``runner.state``
between calls; ``status`` is an alias for ``state``
(signoff_generator's historic attribute name).

Injection hook: map an argv substring to a ``fake_completed`` return value --
or an ``Exception`` instance to raise -- in ``failures``.

Not every batch-shaped fake belongs here: ``test_remote_transport.py``'s
``_FakeRunner`` (a subprocess/ssh argv fake with no S3 awareness),
``test_sim_batch.py``'s (substring-keyed response queue plus ``sleep``
recording) and ``test_place_and_route_batch.py``'s (per-job/per-corner
runner) are genuinely different and intentionally left alone.
"""

from __future__ import annotations

import json
from pathlib import Path

from helpers.subprocess_fakes import fake_completed, keep_uploaded


class FakeS3BatchRunner:
    """Records every argv, keeps a copy of every uploaded file, answers
    `status.json` reads (FIFO queue, or a fixed document when constructed
    with a ``job_id``), and materializes ``outputs`` into the collect
    destination -- standing in for `aws` and the launch script."""

    def __init__(
        self,
        keep_dir: Path,
        *,
        job_id: str | None = None,
        tool: str = "batch",
        state: str = "done",
        exit_code: str = "0",
    ) -> None:
        self.calls: list[list[str]] = []
        self.keep_dir = keep_dir
        self.uploads: dict[str, Path] = {}
        self.statuses: list[str] = []
        self.outputs: dict[str, str | bytes] = {}
        self.failures: dict[str, object] = {}
        self.job_id = job_id
        self.tool = tool
        self.state = state
        self.exit_code = exit_code

    @property
    def status(self) -> str:
        """Alias for `state` (signoff_generator's historic attribute name)."""
        return self.state

    @status.setter
    def status(self, value: str) -> None:
        self.state = value

    def __call__(self, argv: list[str], timeout_s: float) -> object:
        self.calls.append(list(argv))
        joined = " ".join(argv)
        for key, value in self.failures.items():
            if key in joined:
                if isinstance(value, Exception):
                    raise value
                return value
        if "cp" in argv:
            source, destination = argv[argv.index("cp") + 1 : argv.index("cp") + 3]
            if destination.startswith("s3://"):
                self.uploads.update(
                    keep_uploaded(argv, source, destination, self.keep_dir)
                )
                return fake_completed()
            if source.endswith("/status.json"):
                if self.job_id is None:
                    if not self.statuses:
                        return fake_completed(returncode=1, stderr="NoSuchKey")
                    state = (
                        self.statuses.pop(0)
                        if len(self.statuses) > 1
                        else self.statuses[0]
                    )
                    return fake_completed(stdout=state)
                return fake_completed(stdout=self._fixed_status_document())
            if source.endswith("/outputs/"):
                for name, content in self.outputs.items():
                    path = Path(destination) / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    if isinstance(content, bytes):
                        path.write_bytes(content)
                    else:
                        path.write_text(content, encoding="utf-8")
                return fake_completed()
        return fake_completed()

    def _fixed_status_document(self) -> str:
        return json.dumps(
            {
                "job_id": self.job_id,
                "state": self.state,
                "exit_code": self.exit_code,
                "instance_id": "i-0fake",
                "region": "us-east-1",
                "lifecycle": "spot",
                "spot": True,
                "tool": self.tool,
            }
        )

    @property
    def job_json(self) -> dict:
        return json.loads(self.uploads["job.json"].read_text(encoding="utf-8"))

    def aws_calls(self) -> list[list[str]]:
        return [argv for argv in self.calls if argv and argv[0] == "aws"]

    def submissions(self) -> list[list[str]]:
        return [argv for argv in self.calls if "job.json" in " ".join(argv)]
