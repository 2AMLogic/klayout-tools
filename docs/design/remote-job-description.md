# Generic job description for the remote execution backend (Epic #253 Phase 3, issue #278)

**Status:** implemented. This note documents what
[`src/klayout_tools/remote_transport.py`](../../src/klayout_tools/remote_transport.py)'s
push/run/collect surface now accepts as data, so a future `extract`/`lvs`/DRC
remote backend has a contract to implement against without re-reading the
diff. It does **not** implement that adoption itself — per issue #278's own
scope note, quoting the epic body: "Adoption by `extract`/`lvs`/DRC is
explicitly out of scope."

## Why this exists

Epic #253's overview states the backend is "designed so the job type is a
parameter — `extract`/`lvs`/DRC can reuse it later without redesign," and its
Success Criteria include "Job type is a parameter of the backend." Phase 2
(#264 `remote_launcher.py`, #265 `remote_transport.py` + `sim.py` wiring)
built directly against `klt sim`'s corner-matrix job shape to prove the
transport works end-to-end first — reasonable sequencing, but it left the
push/run/collect surface hard-coding `klt sim`'s own filenames, remote
command, exit codes, and artifacts directory. This issue generalizes that
surface into data before a second job type shows up to prove (or disprove)
the abstraction against a real second caller.

## Audit: what was `klt sim`-specific before this issue

| Assumption | Where | Resolution |
| ---------- | ----- | ---------- |
| `push_job` always uploaded exactly one netlist file plus one generated JSON request document, at hard-coded filenames (`REMOTE_NETLIST_FILENAME`/`REMOTE_REQUEST_FILENAME`). | `remote_transport.push_job` | Generalized to `job.inputs: tuple[JobInput, ...]` — any number of files, each either an existing local path or inline text content, at a caller-chosen `remote_name`. `klt sim`'s two inputs are now `sim._build_remote_job_description`'s own data, not the module's. |
| The remote command was a literal string: `"klt sim {REMOTE_REQUEST_FILENAME} --backend local-parallel --format json"`. | `remote_transport.run_remote_sim` | Generalized to `job.command: str`, run verbatim (after a `cd` into the job directory) by the renamed `run_remote_job`. |
| Only exit codes `0`/`3`/`4` (`klt sim`'s own pass/measurement-failure/corner-error codes) were treated as "produced a parseable result"; anything else was a transport failure. | same | Generalized to `job.success_exit_codes: tuple[int, ...]`, defaulting to `(0,)` for a typical "0 means success" command. |
| stdout was always parsed as a JSON object and returned as `dict`. | same | Generalized to `job.parse_json_stdout: bool` (default `True`, matching every `klt` verb's own JSON-contract convention) — `False` returns the raw stdout string for a job type that doesn't speak JSON on stdout. |
| The collected artifact directory was a hard-coded `<remote_job_dir>/.klt/sim` (`artifacts_root`'s old no-argument form). | `remote_transport.pull_artifacts` / `artifacts_root` | Generalized to `job.artifacts_relative_dir: str \| None`, job-relative and caller-chosen; `None` means "nothing to collect" (`pull_artifacts` becomes a no-op, no `scp` call made). `artifacts_root` now takes this as an explicit second argument, defaulting to `.klt/sim` for source-compatibility with the one caller that still wants that default. |
| Error messages named `klt sim` literally (`"remote 'klt sim' failed"`, `"... timed out"`, `"... did not return valid JSON"`). | same | Generalized via `job.label: str`, interpolated into the same three messages — `sim.py` sets `label="klt sim"` so `klt sim`'s own error text (and every existing test assertion pinned to it) is byte-identical; a future job type supplies its own label. |
| `RemoteLauncher`'s sizing (`corner_count`, `threads_per_corner`) and AMI resolution (`pdk`, `region` → manifest lookup) are named after `klt sim`'s own vocabulary. | `remote_launcher.py` | **Left as-is, documented rather than renamed.** The *semantics* already generalize without a rename: "N parallel work units, T threads each" sizes an instance for any batch fan-out (an LVS run across N cells, a DRC run across N sub-blocks, ...), and AMI resolution is already keyed on `(pdk, region)` — data any PDK-consuming job type shares, not something `klt sim` specific. Several `select_instance_type`/`RemoteLauncher` test assertions pin the literal parameter-name strings in `RemoteLaunchError` messages (e.g. `match="corner_count"`); renaming the parameter without changing those user-facing error strings would be inconsistent, and changing them isn't a behavior improvement Phase 3 needs. A future adopter passes its own unit count under the existing parameter name (e.g. `RemoteLauncher(corner_count=len(lvs_targets), ...)`) — awkward naming, not a redesign blocker. |
| The AMI manifest schema (`data/remote-sim-ami-manifest.json`) has no `job_type`/toolchain discriminator — every entry implicitly means "ngspice + this PDK baked in." | `remote_launcher.resolve_ami` | **Deferred, not solved here.** `resolve_ami`'s `manifest_path` parameter is already overridable, so a future `extract`/`lvs`/DRC backend can point at its own manifest file (a different AMI-build pipeline, e.g. `scripts/aws/build-remote-extract-ami.sh`) without any code change here. Adding a `job_type` column to *one* shared manifest, if that turns out to be preferable, is schema evolution for that future issue to decide — out of this issue's scope (no new AWS-facing behavior). |

## The `JobDescription` contract

```python
from klayout_tools.remote_transport import JobDescription, JobInput

job = JobDescription(
    label="klt sim",  # used in error/log messages only
    inputs=(
        JobInput(remote_name="netlist.cir", label="netlist", local_path=netlist_path),
        JobInput(
            remote_name="request.json",
            label="request",
            content=json.dumps(remote_request),
        ),
    ),
    command="klt sim request.json --backend local-parallel --format json",
    success_exit_codes=(0, 3, 4),
    parse_json_stdout=True,  # default
    artifacts_relative_dir=".klt/sim",
)
```

- **`push_job(host=..., remote_job_dir=..., job=job, ...)`** creates
  `remote_job_dir` and uploads each `job.inputs` entry to
  `<remote_job_dir>/<JobInput.remote_name>` — `local_path` inputs upload
  directly; `content` inputs are written to a local temp file first (removed
  once the upload completes or fails).
- **`run_remote_job(host=..., remote_job_dir=..., job=job, timeout_s=..., ...)`**
  SSHes in, `cd`s into `remote_job_dir`, and runs `job.command`. An exit code
  in `job.success_exit_codes` is success; anything else raises
  `RemoteTransportError`. On success, returns `json.loads(stdout)` (asserted
  to be a JSON object) when `job.parse_json_stdout` is true, else the raw
  stdout string.
- **`pull_artifacts(host=..., remote_job_dir=..., local_artifacts_dir=..., job=job, ...)`**
  copies `<remote_job_dir>/<job.artifacts_relative_dir>` down to
  `local_artifacts_dir`, or does nothing if `job.artifacts_relative_dir is
  None`.
- **`cleanup_job`** and **`job_dir`** were already job-type-agnostic (a
  `rm -rf` of the job directory, and a path-join of user/job-id) and needed
  no change.

`sim._build_remote_job_description` (`src/klayout_tools/sim.py`) is the one
and only place today's `klt sim` job shape is constructed — it is the
worked example a future job type's own `_build_remote_job_description`-shaped
function follows.

## What a future `extract`/`lvs`/DRC remote backend still has to bring

This issue only generalizes the transport `sim.py` already had working. A
future adopter still needs, per job type:

- Its own `JobDescription` builder (what files to push, what command to run,
  what to collect back) — the pattern `sim._build_remote_job_description`
  demonstrates.
- Its own AMI (baked toolchain + PDK data for that job type) and manifest
  file/entries, resolved via `remote_launcher.resolve_ami`'s existing
  `manifest_path` override (see the audit table above).
- Its own sizing call into `remote_launcher.select_instance_type`/
  `RemoteLauncher(corner_count=..., threads_per_corner=...)` — awkward
  parameter names for a non-sim job type, not a blocker (see above).
- Its own request-field surface in the calling command's own CLI/JSON
  contract (`remote.region`, `remote.ssh_key_path`, etc. are already generic
  launcher/transport concerns; a new command wires them the same way
  `sim.py`'s `_run_remote` does).

None of that is implemented by this issue — see #278's acceptance criteria
("Adoption by `extract`/`lvs`/DRC is explicitly out of scope").
