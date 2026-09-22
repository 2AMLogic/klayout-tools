# Tagged remote compute for heavy workloads

Agent-driven verification pipelines bottleneck on dev laptops: a full local
evidence suite reaches ~15 minutes on an M-series machine (719 tests: oracle
renders, Yosys mapped runs, Verilator/iverilog conformance), and an agent wave
re-runs it 2–3× (builder, judge, merge gate). Single full-clip simulations run
5–90 minutes and block an entire agent while they run. Golden-vector
regenerations exceed tool timeouts; thermal throttling kills long runs
mid-flight. (Issue #2277; the same numbers were reported independently from
2AMLogic/gf180-dx7.)

The adopted pattern is **operator-provisioned, tagged remote compute**:
agents SSH out for heavy runs (sim sweeps, renders, PDK-adjacent work) while
git and forge operations (commits, PRs, issue updates) stay local and fast.
*Split the work, not the repo.*

## What this repo uses

| Item | Value |
|---|---|
| Provider | AWS `us-east-1` only (every other region is denied by policy) |
| Instance | `m5.2xlarge` (8 vCPU, 32 GB) — sized in `.env` as a deliberate, reviewable cost gate |
| Tag | `repo-remote=klayout-tools` on every resource the tool creates; the IAM policy (`RepoRemoteCollaborator`) permits lifecycle actions **only** on tagged resources |
| Idle guard | instance auto-stops after 120 min with no SSH session (a stopped instance costs only its disk) |
| Credentials | never leave the operator host; the provisioning key is used by the local CLI only and is never copied to the VM |

The upstream account (`221082181346`) shares its on-demand vCPU quota with
2AM's own fleet, so anything above 8 vCPU or any GPU family needs a
conversation before launch. The IAM identity cannot see inside or act on the
fleet's `Fleet=loom` hosts, and the tool refuses to reuse any of them — if it
ever does, that is a bug to report, not a permission.

## Provisioning (operator, once)

The mechanics live in [Repo Skills](https://github.com/rjwalters/repo)
(`.claude/skills/repo/scripts/repo-remote.sh` here); see the `2am#999`
handoff document for the authoritative account/policy details, which are
delivered by secure channel and never committed.

Two config layers:

1. `~/.config/repo/remote.env` (`chmod 600`, shared across repos) — provider,
   credentials, region, and the local SSH key whose `.pub` half is imported
   as the launch key pair.
2. `<repo>/.env` (gitignored; this is why `.env` is in `.gitignore`) — the
   per-repo machine spec and, after the first launch, the pinned instance id
   so later runs reuse the same box:

   ```bash
   REPO_REMOTE_INSTANCE_TYPE=m5.2xlarge   # required; no default, by design
   REPO_REMOTE_DISK_GB=100
   REPO_REMOTE_IDLE_SHUTDOWN_MIN=120
   REPO_REMOTE_INSTANCE_ID=i-...          # set after first launch
   ```

## Headless usage

```bash
S=.claude/skills/repo/scripts/repo-remote.sh
$S up               # dry run: plan + est. $/h, creates nothing
$S up --yes --json  # provision; JSON has instance id, IP, ssh alias, cost
$S status --json
$S down --yes       # stop (disk kept, no compute charge)
$S down --yes --delete   # terminate (disk gone)
```

The launch writes an SSH alias (`repo-remote-klayout-tools`), so agents treat
the box as a plain SSH host from then on:

```bash
ssh -A repo-remote-klayout-tools
```

Agent-forwarding (`-A`) is what carries GitHub credentials for the private
clone; heavy runs that need no forge access do not need it.

## House rules

1. **Stop the box when done.** The idle guard covers forgotten sessions;
   deliberate `down` after a burst is still polite.
2. **Terminate what you no longer need.** Stopped-but-forgotten volumes are
   the usual silent cost; `repo-remote status` lists everything under the
   tag.
3. **Only your tag.** Never `--force` past the fleet-host refusal.
4. **No personal data in the repo**, and credentials never enter git, issue
   text, or logs. If the key is exposed, it can be rotated in about a minute.

## What belongs where

- **This guide** — provisioning and operating the remote box (infrastructure).
- **[Remote evidence runs](remote-evidence-runs.md)** — driving long `klt`
  evidence verbs (e.g. `klt equiv`) on a remote/fleet host via the in-repo
  `remote_fleet`/`remote_launcher` machinery, resuming interrupted runs, and
  keeping the evidence envelope truthful about where a run executed.
