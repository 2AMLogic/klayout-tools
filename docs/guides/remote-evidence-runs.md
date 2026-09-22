# Remote evidence runs and resumable long-run verbs (issue #2280)

Downstream repos increasingly run evidence generation from **autonomous
agents**, and the agent's process is not a reliable place to keep state:
sessions get reaped, rate limits interrupt, laptops close. Issue #2280
records two gf180-surge incidents in one week — an SXT-023 attempt that
lost hours when its session died with 3 unpushed commits, and an aborted
run that re-did discovery work from scratch. `klt equiv` runs can be long;
this page is the operations contract for (a) running one **remotely**,
(b) **resuming** an interrupted one, and (c) the **checkpoint-push**
convention that bounds what a death can lose.

klt already owns the hard parts; nothing here adds orchestration
infrastructure:

| Piece | Module | What it gives you |
|---|---|---|
| Provision a right-sized host, with cost guardrails and guaranteed teardown | [`remote_launcher.py`](../../src/klayout_tools/remote_launcher.py) (issues #264, #375/#377) | `RemoteLauncher` / `FleetLauncher`: spot/on-demand EC2, `max_hourly_cost_usd` gate, vCPU quota pre-check, teardown on every exit path |
| Push inputs → run one command → pull artifacts | [`remote_transport.py`](../../src/klayout_tools/remote_transport.py) (issue #278) | `JobDescription` + `push_job`/`run_remote_job`/`pull_artifacts` — deliberately job-type-neutral, so any verb can ride it |
| Resume an interrupted run | `klt equiv --resume` (this issue); `klt sim --resume` (issue #473) | Stage-scoped re-entry from committed artifacts; partial artifacts are structurally never verdict-bearing |
| Verify a retrieved envelope locally | `klt equiv <request> --check <report>` (issues #2224 + #2280) | Cheap re-hash (no engine), full `--rerun` re-run-and-diff |

## The remote path

### `klt sim` already has a built-in remote backend

For long SPICE sweeps, do not hand-roll anything:
`request.backend: "remote"` (or `--backend remote`) provisions one
right-sized instance via `RemoteLauncher`, runs the *same* worker-pool
code on it, and pulls the report and artifacts back — see
[`cli/sim.md`](../cli/sim.md)'s "Remote backend" and
[`design/remote-sim-backend-spike.md`](../design/remote-sim-backend-spike.md)
for the full contract. With `remote.hosts > 1` it shards across a fleet
(`remote_fleet.run_fleet`, with the fleet-level cost gate and the one
automatic shard retry). `options.resume` is local-backend-only today;
the checkpoint lives under `--outdir`, so a sweep that dies locally
resumes without re-running completed corners.

### Driving any other verb remotely: push → run → pull

`equiv` has no built-in remote backend — and needs none. The transport is
already generic: a job is *data* (a `JobDescription`), so an agent (or a
20-line driver) can run `klt equiv` on a fleet host with the same
guarantees `klt sim` gets:

```python
from klayout_tools.remote_transport import (
    JobDescription,
    JobInput,
    push_job,
    run_remote_job,
    pull_artifacts,
)

job = JobDescription(
    label="equiv-adder4",
    inputs=(
        # request.json's relative source paths resolve against the job dir
        JobInput(
            remote_name="request.json", label="equiv request", local_path="request.json"
        ),
        JobInput(remote_name="gold.v", label="gold RTL", local_path="gold.v"),
        JobInput(remote_name="gate.v", label="gate netlist", local_path="gate.v"),
    ),
    # `klt` is baked into the remote-sim AMI; exit codes 0/3/4 all mean
    # "ran to completion and emitted its JSON envelope" (see
    # docs/json-contract.md's exit-code additivity rule) -- the shell's
    # exit code is klt's, so run_remote_job can tell them apart.
    command="klt equiv request.json --format json > envelope.json",
    success_exit_codes=(0, 3, 4),
    parse_json_stdout=False,  # the envelope is captured to a file
    artifacts_relative_dir=".klt/equiv",  # scripts, netlist, logs, stage records
)
push_job(host=HOST, remote_job_dir="/home/ubuntu/equiv-adder4", job=job)
run_remote_job(
    host=HOST, remote_job_dir="/home/ubuntu/equiv-adder4", job=job, timeout_s=3600
)
pull_artifacts(
    host=HOST,
    remote_job_dir="/home/ubuntu/equiv-adder4",
    local_artifacts_dir=".klt/equiv",
    job=job,
)
```

The envelope itself is what the remote command printed — capture it
verbatim (`--format json > envelope.json` inside the job, then `scp` it
back with the artifacts, or let `parse_json_stdout=True` return it as the
job's result). Provisioning the box itself, when you need one, is
`RemoteLauncher(...)` + `provision()` + `get_public_ip()` — the cost gate
and teardown guarantees apply exactly as for sim (see
[`design/remote-job-description.md`](../design/remote-job-description.md)
for the full job contract and
[`design/remote-sim-backend-spike.md`](../design/remote-sim-backend-spike.md)
for the provisioning decisions). For any plain SSH-reachable host (a CI
box, a lab machine), skip provisioning and use the push/run/pull calls
directly — they are only `ssh`/`scp` argv builders around the same
conventions.

### Provenance continuity: the envelope describes the run truthfully, wherever it ran

This is the rule that makes the retrieved envelope evidence rather than a
souvenir:

- **`provenance` is built where the verb runs.** A remote `klt equiv` run
  records the *remote* host's truth: its `klt_version` install identity,
  its resolved `engine_version` (`yosys -V`), and the content hashes of
  the sources *it* read. Nothing about retrieval rewrites any of it —
  `pull_artifacts` copies bytes.
- **Input identity is content, not location.** `provenance.input.
  content_hash` covers the request's gold/gate source *bytes* (combined,
  order-independent — see `_provenance._combined_content_hash`), never
  their paths. So the same request at `/home/ubuntu/job/` on the remote
  host and `~/design/` locally hashes identically, and the envelope pins
  the design, not the machine.
- **Verify, don't trust.** After retrieval, `klt equiv request.json
  --check envelope.json` re-hashes the *local* sources and compares them
  against the envelope's recorded hash — the cheap half of the shared
  `--check` contract (issue #2224), which is exactly the cross-host
  verification step (see the round-trip below). Full mode (`--rerun`)
  re-runs the proof locally and diffs verdict-bearing fields; note it
  *legitimately* drifts on the echoed absolute `gold`/`gate` source paths
  across hosts — cheap mode is the cross-host check, full mode answers
  "does this reproduce *here*".

## Resumable runs: `klt equiv --resume`

Long proofs die. `--resume` makes re-entry idempotent instead of
all-or-nothing, using the stage structure the sequential engine already
has (stage 1: `equiv_make`/`equiv_induct` induction with its bounded
cut-point refinement loop; stage 2: the bounded counterexample search
that runs only when stage 1 leaves cells unproven).

### The contract

- **Stage 1 commits a record when it completes.**
  `.klt/equiv/stage1.commit.json` is written atomically (temp file +
  `os.replace`) only after stage 1 reaches a classified outcome, carrying
  the request **fingerprint** (SHA-256 over the sources' content hashes
  plus engine/depth/backend/timeout — path-independent, so a moved
  checkout or a pulled-back artifact set is still resumable), the
  classification (`"all_proven"` or `"unproven_cells"`), the cut-point
  blacklist, and the refinement count. A stage that died or timed out
  before classifying leaves (or leaves untouched) a record explicitly
  marked **`"partial": true`**.
- **`--resume` re-enters from the last committed stage artifact.** A
  fingerprint-matched, fully corroborated record skips stage 1 entirely:
  `all_proven` produces the final `"equivalent"` envelope without
  re-running Yosys at all; `unproven_cells` re-enters directly at stage
  2. Anything else re-runs stage 1. The resumed envelope carries an
  additive `resume` block (present only when `--resume` was given):
  `{"resumed_stage": 0 | 1, "record_path": ...}`.
- **Partial artifacts can never satisfy a verdict check.** The loader
  rejects — and the run re-runs the stage, emitting a
  `resume_stage_record_discarded` warning diagnostic naming the reason —
  for: a `partial: true` record (or one with no `partial` marker at
  all), a fingerprint mismatch, any missing/mistyped field, a missing
  committed log artifact, or a log whose own bytes do not corroborate
  the recorded classification (an `all_proven` record whose log lacks
  Yosys's `Equivalence successfully proven!` line cannot exist honestly,
  so it cannot be resumed from). There is no envelope-level "partial":
  **a `klt equiv` JSON envelope exists only for a run that reached a
  verdict**; a killed run emits nothing, and what it left behind says
  `partial` on itself. This is #2280's negative-control criterion, made
  structural rather than procedural.
- **An envelope is never verdict-bearing unless the record is.** The only
  path from a stage record to an envelope `status` runs the full
  validation above — enforced in `equiv.py` and pinned by
  `tests/test_equiv_resume.py`.
- **Byte-identity and its declared exceptions.** A resumed run's envelope
  equals an uninterrupted run's except in: `elapsed_s` (wall-clock),
  the `resume` block itself, and — only when the resume happens on a
  different host — the host-scoped identity fields
  (`provenance.klt_version`, `engine_version`). Nothing else may drift;
  `test_killed_run_resumes_from_committed_stage_artifacts` (and its
  `all_proven` and refinement variants) pins this against an
  uninterrupted control run.
- **The combinational engine** is a single Yosys subprocess — no earlier
  stage artifact exists, so `--resume` is accepted, re-runs its one
  stage, and reports `resumed_stage: 0`. Kept uniform so an agent fleet
  issues one retry command regardless of engine. (`klt sim --resume`
  works the same opt-in way for sweeps; its checkpoint is per-corner
  under `--outdir` — see [`cli/sim.md`](../cli/sim.md).)

### The checkpoint-push convention for agent fleets

The resume contract bounds what a death costs *inside* one run; the
checkpoint-push convention bounds what it costs *across* sessions — it is
the gf180-surge fix for the lost-work class:

> **After every green stage, push.** A stage is green when its process
> exited with a "report was emitted" code (for `klt`: anything but 1/2 —
> see `docs/json-contract.md`'s exit-code additivity rule; for a stage
> boundary inside `klt equiv --resume`: the stage record exists and is
> not `partial`). Commit and push, in one atomic step: the envelope
> (or, mid-run, the stage record plus its log), and a one-line note of
> the command that produced them. Then — and only then — tear the
> session/instance down.

Concretely, for a long evidence run on a remote host:

```bash
klt equiv request.json --resume --format json > envelope.json
code=$?
git add envelope.json .klt/equiv/stage1.commit.json
git commit -m "equiv: adder4 pre/post-route ($code)" && git push
# only now is it safe to lose this session
```

A run killed between pushes loses at most its current in-flight stage;
`--resume` (or a fresh session re-issuing the same command) continues
from the last committed artifact instead of restarting discovery. The
combination is the whole point: **checkpoint-push makes the committed
stage artifacts durable; `--resume` makes them resumable.**

## The round-trip (acceptance criterion, end to end)

Run one fixture remotely, retrieve it, verify it locally:

```bash
# 1. Local: a tiny request (any request works; sources are pushed by name)
cat > request.json <<'JSON'
{"gold": {"sources": ["gold.v"], "top": "top"},
 "gate": {"sources": ["gate.v"], "top": "top"}}
JSON

# 2. Push, run on the remote host, pull artifacts + envelope back
#    (push_job/run_remote_job/pull_artifacts as shown above, or plain
#    ssh/scp following the same layout: job dir with the request and
#    sources, `.klt/equiv/` for artifacts)
ssh $HOST "mkdir -p ~/rt && cd ~/rt" &&
scp request.json gold.v gate.v $HOST:rt/ &&
ssh $HOST "cd ~/rt && klt equiv request.json --format json > envelope.json" &&
scp $HOST:rt/envelope.json envelope.json &&
scp -r $HOST:rt/.klt/equiv .klt/equiv

# 3. Local: cheap verification — no engine, content-hash comparison
klt equiv request.json --check envelope.json
# -> status: "match" (exit 0)
```

Step 3 is the provenance-continuity check doing its job: the envelope
records the remote host's identity and the *content* hash of the sources
it read; `--check` re-derives that hash from the local copies and
confirms the committed evidence still reproduces from them. Commit both
(`envelope.json` and this check's output in your evidence note) and
checkpoint-push.

## Out of scope (unchanged by #2280)

- No new orchestration infrastructure: provisioning stays
  `RemoteLauncher`/`FleetLauncher`, transport stays
  `remote_transport`, and neither module changed.
- No job queues or schedulers, and no change to verdict semantics: a
  partial artifact cannot become a verdict, a resumed verdict is the
  same verdict the uninterrupted run would have produced, and
  `inconclusive` still means exactly what it always meant.
