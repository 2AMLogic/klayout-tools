---
name: loom-builder-worktree
description: "This document covers git worktree management for the Builder role. For the core builder workflow, see `builder.md`."
---
<!-- loom-managed-skill -->
<!-- GENERATED FILE — DO NOT EDIT DIRECTLY.
     Produced by `loom-daemon generate-agent-skills` from
     defaults/.claude/commands/loom/builder-worktree.md (the same source Claude Code
     reads as /loom:builder-worktree via .claude/commands/loom/). This is the
     cross-vendor skill-discovery surface (.agents/skills/<name>/SKILL.md)
     read natively by Codex, Kimi Code, Mistral Vibe, and Grok — see
     runtime-adapters.md §5. To change this file, edit the source above
     and re-run the generator; CI (`loom-daemon generate-agent-skills
     --check`) fails if this file is stale. -->

# Builder: Worktree Workflows

This document covers git worktree management for the Builder role. For the core builder workflow, see `builder.md`.

## On-Demand Git Worktrees

When working on issues, you should **create worktrees on-demand** to isolate your work. This prevents conflicts and allows multiple agents to work simultaneously.

### IMPORTANT: Use the Worktree Helper Script

**Always use `./.loom/scripts/worktree.sh <issue-number>` to create worktrees.** This helper script ensures:
- Correct path (`.loom/worktrees/issue-{number}`)
- Prevents nested worktrees
- Consistent branch naming
- Sandbox compatibility

```bash
# CORRECT - Use the helper script
./.loom/scripts/worktree.sh 84

# WRONG - Don't use git worktree directly
git worktree add .loom/worktrees/issue-84 -b feature/issue-84 main
```

### Why This Matters

1. **Prevents Nested Worktrees**: Helper detects if you're already in a worktree and prevents double-nesting
2. **Sandbox-Compatible**: Worktrees inside `.loom/worktrees/` stay within workspace
3. **Gitignored**: `.loom/worktrees/` is already gitignored
4. **Consistent Naming**: `issue-{number}` naming matches GitHub issues
5. **Safety Checks**: Validates issue numbers, checks for existing directories

### Worktree Workflow Example

```bash
# 1. Claim an issue
gh issue edit 84 --remove-label "loom:issue" --add-label "loom:building"

# 2. Create worktree using helper
./.loom/scripts/worktree.sh 84
# -> Creates: .loom/worktrees/issue-84
# -> Branch: feature/issue-84

# 3. Capture the worktree ABSOLUTE path ONCE (see warning below)
WORKTREE_ABS="$(cd .loom/worktrees/issue-84 && pwd)"
# -> e.g. /Users/you/repo/.loom/worktrees/issue-84

# 3a. Assert it's really a managed worktree BEFORE any edit — the same two
#     checks guard-worktree-paths.sh applies to every Edit/Write/Bash write
#     it confines (#4178):
[[ -f "$WORKTREE_ABS/.loom-managed" ]] || echo "FATAL: no .loom-managed sentinel"
[[ "$(git -C "$WORKTREE_ABS" rev-parse --show-toplevel)" == "$WORKTREE_ABS" ]] || echo "FATAL: toplevel mismatch"

# 4. Do your work using ABSOLUTE paths (implement, test, commit)
#    - Write/Edit: pass "$WORKTREE_ABS/<file>"
#    - Bash:       git -C "$WORKTREE_ABS" ...  OR  cd "$WORKTREE_ABS" && <cmd>
# ... work work work ...

# 5. Push and create PR from the worktree.
#    ALWAYS ./.loom/scripts/create-pr.sh, never a bare `gh pr create` (#6074):
#    it adopts an already-open PR for this branch instead of failing, and it
#    survives the GitHub App permission window that made `git push` succeed
#    while `gh pr create` returned 403 — see builder-pr.md § "Creating the PR".
git -C "$WORKTREE_ABS" push -u origin feature/issue-84
./.loom/scripts/create-pr.sh --title "fix: ..." --body "..." --label "loom:review-requested"

# 6. Worktree cleanup is automatic - DO NOT manually delete worktrees
# Worktrees are cleaned up automatically when PRs merge or by loom-clean
```

### CRITICAL: `cd` Does NOT Persist Across Tool Calls

**The harness resets your working directory between tool calls.** A `cd
.loom/worktrees/issue-N` in one Bash call does **not** carry over to the next
Write, Edit, or Bash call — the next call starts back at the main repo root.
If you rely on a persisted `cd` and then use a repo-relative path, your file
operation lands in the **main worktree** instead of your issue worktree,
silently contaminating main (#3513, recurrence of #2802).

**Do this instead:**

1. Capture the worktree's absolute path **once**, right after creating it:
   ```bash
   WORKTREE_ABS="$(cd .loom/worktrees/issue-N && pwd)"
   ```
2. Use absolute paths for **every** file-mutating operation thereafter:
   - **Write / Edit tools** — pass the full path `"$WORKTREE_ABS/path/to/file"`.
     These tools have no cwd; the path you give is the path written.
   - **Bash** — either re-assert `cd "$WORKTREE_ABS" &&` at the **start of each
     file-mutating invocation**, or use `git -C "$WORKTREE_ABS" ...` and
     absolute paths. Never assume an earlier `cd` is still in effect.
3. Before committing, verify your changes are in the worktree and main is clean:
   ```bash
   git -C "$WORKTREE_ABS" status        # changes should be HERE
   ./.loom/scripts/check-main-clean.sh  # backstop: exits 3 if main is dirty
   ```

**A guard denial is not a signal to retry via a different tool.** Two
independent PreToolUse guards confine writes to your worktree while any
managed worktree exists: `guard-worktree-paths.sh` on the Edit/Write matcher,
and `guard-destructive-generic.sh` on the Bash matcher for the common write
idioms (`>`/`>>` redirection, `tee`, `sed -i`, `cp`/`mv`) (#4178). If one
denies, the fix is always to re-derive `$WORKTREE_ABS` and use it — never to
fall back from Edit/Write to a Bash write (or vice versa) for the same target.
That fallback is exactly how sweep #4063 escaped worktree isolation and edited
live guard hooks in the main checkout.

### Collision Detection

The worktree helper script prevents common errors:

```bash
# If you're already in a worktree
./.loom/scripts/worktree.sh 84
# -> ERROR: You are already in a worktree!
# -> Instructions to return to main before creating new worktree

# If directory already exists
./.loom/scripts/worktree.sh 84
# -> Checks if it's a valid worktree or needs cleanup
```

### Working Without Worktrees

**You start in the main workspace.** Only create a worktree when you claim an issue and need isolation:

- **NO worktree needed**: Browsing code, reading files, checking status
- **CREATE worktree**: When claiming an issue and starting implementation

This on-demand approach prevents worktree clutter and reduces resource usage.

## Shelving work and recovering edits

Linked worktrees share `refs/stash`. Two Builders using raw `git stash` can
pop or drop each other's edits (#4821).

- **Save a patch.** Run `./.loom/scripts/worktree.sh snapshot <issue-number>`.
  It writes staged and unstaged tracked changes to
  `<worktree-root>/.snapshots/issue-<N>-<timestamp>.patch` and leaves your
  edits in place. Add `--include-untracked` to capture new text files too.
  A `Binary files … differ` line contains no bytes for `git apply` to restore.
  Keep a separate, verified copy of each such file before removing the worktree.
- **Compare against a clean baseline.** Run
  `./.loom/scripts/worktree.sh stash-push <issue-number>`. It saves tracked
  edits under `refs/loom/stash-baseline/issue-<N>` and resets that worktree
  to HEAD. Add `--include-untracked` to move new files aside too. Run the
  baseline check in the issue worktree, then run
  `./.loom/scripts/worktree.sh stash-pop <issue-number>` to restore the edits
  and any files moved aside, even if the check failed (#5217).
- **Compare against a baseline in the main checkout.** Use
  `./.loom/scripts/worktree.sh stash-push main` and
  `./.loom/scripts/worktree.sh stash-pop main`. This pair uses
  `refs/loom/stash-baseline/main` and leaves the shared stack alone (#6076).

Inside a managed worktree with a second managed worktree active, the guard
denies raw stash creation (`git stash`, `git stash push`, `git stash save`)
before anything changes. Its message gives the helper commands for your issue.
Raw `git stash pop`, `drop`, and `clear` ask for confirmation when the stack
is at risk, including in main. A headless Builder cannot answer that prompt (#5754).

### Replay quarantined edits

`check-main-clean.sh --quarantine` uses the shared stash to rescue edits from
the main checkout. On exit 4, use the **commit SHA** printed on the
`Rescue ref` line so another stash cannot change which edits you replay.
Inspect the paths. If they all belong to the issue, apply the patch in its
worktree.

```bash
git -C "<MAIN_ROOT>" stash show --include-untracked --name-status "<rescue-sha>"
git -C "<MAIN_ROOT>" stash show --include-untracked --binary -p "<rescue-sha>" |
  git -C "<WORKTREE_ABS>" apply -
```

A raw `git stash pop` in main would put the edits back in the checkout you just cleaned.

## Handling Merge Conflicts in Worktrees

When your feature branch has conflicts with main, you have two options depending on the severity of divergence.

### Option 1: Resolve Conflicts in the Worktree (Recommended)

For minor conflicts or small divergence from main:

```bash
# In the worktree. Merge unless the repo allows rebase; unknown = merge (#10771)
BRANCH=$(git branch --show-current)
BRANCH_UPDATE=$(loom-daemon branch-update strategy) || BRANCH_UPDATE=merge
if [ "$BRANCH_UPDATE" = rebase ]; then  # rebase rewrites: pin the lease first (#9487)
  LEASE=$(loom-daemon push-lease pin-flag --branch "$BRANCH" --local-ref "refs/heads/$BRANCH") || exit 1; fi
git fetch origin main
if [ "$BRANCH_UPDATE" = rebase ]; then git rebase origin/main; else git merge --no-ff -m "Merge origin/main into $BRANCH (Builder: sync with main)" origin/main; fi

# If conflicts occur: edit the files, `git add <resolved-files>`, then
# `git commit --no-edit` (merge) or `git rebase --continue` (rebase)

# Version-bearing-file sync gate (#7168, #7341, #7743). If it
# fires your branch carries a version-bearing edit. Never hand-patch VERSION/CLAUDE.md/etc
# or `version.sh bump` -- `git checkout origin/main -- <files>`, commit, re-gate.
if [ -x ./.loom/scripts/version-check-gate.sh ] && ! ./.loom/scripts/version-check-gate.sh --fix-hint "then push."; then
  echo "Version-bearing files out of sync after update (see BLOCKER:/Fix: above) - revert, never bump"
  exit 1
fi

# Merge: plain push (rejected = fetch + merge again, never force). Rebase: the pinned lease.
if [ "$BRANCH_UPDATE" = rebase ]; then git push "$LEASE" origin "HEAD:$BRANCH"; else git push origin "HEAD:$BRANCH"; fi
```

**When to use this approach:**
- Few files have conflicts
- Conflicts are straightforward to resolve
- Your changes are relatively small

### Option 2: Create a Fresh Worktree (For Significant Divergence)

If conflicts are too complex or main has changed significantly:

```bash
git diff --binary HEAD > ~/my-changes.patch   # 1. save tracked changes (copy untracked files separately)
cd /path/to/main/repo                         # 2. back to the main repository
git worktree remove .loom/worktrees/issue-XX --force   # 3. remove the stale worktree
git branch -D feature/issue-XX                # 4. delete the old branch
git fetch origin main                         # 5. fetch latest main
./.loom/scripts/worktree.sh XX                # 6. fresh worktree from updated main
cd .loom/worktrees/issue-XX                   # 7. re-apply: cherry-pick, patch, or reimplement
```

**When to use this approach:**
- Many files have conflicts
- Main has diverged significantly (many commits ahead)
- Conflicts are in areas you didn't intentionally change
- Easier to reimplement than untangle

### Never Do This

**Don't delete worktrees manually with `git worktree remove`**
- Running `git worktree remove` while your shell is in the worktree corrupts shell state
- Even `pwd` will fail with "No such file or directory" errors
- Use `loom-clean` for safe cleanup (handles edge cases)
- Worktrees auto-cleanup when PRs merge

**Don't switch to the main repository directory to work on features**
- Always work in worktrees for isolation
- Main should stay clean and on the default branch

**Don't create branches directly in main**
- Always use `./.loom/scripts/worktree.sh` to create branches
- Prevents nested worktree issues

**Don't leave a detached process running after your session ends**
- It outlives the sweep, holds files open in a worktree that is auto-removed on
  merge, and loads the host with work no owner can be found for.
- **Never `launchctl submit`**: its jobs are **KeepAlive**, so launchd re-runs a
  one-shot script every time it exits, forever (#8478: 25 orphaned `ngspice`,
  load 58, 12h of suppressed dispatch).
- Long compute → the repo's batch backend, or scoped to fit the session, or
  `loom:blocked` naming the compute gap: `.loom/docs/long-running-compute.md`.

**Never `git push --force`, and never a bare `--force-with-lease`**
- A bare lease trusts `refs/remotes/origin/<branch>`, shared by every worktree: a sibling's fetch makes it silently overwrite their push (#9487). Pin it (`loom-daemon push-lease pin-flag`)
- Merge mode (`loom-daemon branch-update strategy`, #10771): never force at all

### Preventing Merge Conflicts

To minimize conflicts in the first place:

1. **Sync frequently**: before significant work, update from `origin/main` as in Option 1
2. **Keep PRs small**: smaller PRs = fewer conflicts = faster merges
3. **Communicate**: on shared areas, coordinate with other builders
4. **Sync before PR** — a required gate (#7668): `builder-pr.md` § "Pre-Push
   Rebase: Sync with `origin/main`", run immediately before `git push` / opening the PR.

## Claiming Workflow (Parallel Mode)

When working with parallel agents (multiple Builders running simultaneously), use the atomic claiming system to prevent race conditions.

### Why Use Atomic Claims?

**The Problem with Labels Alone:**
- Two Builders see `loom:issue` at the same time
- Both try to claim by adding `loom:building`
- Race condition: both may succeed, causing duplicate work

**The Solution:**
- Use `loom-claim` for atomic file-based locking
- Label change is still needed (for visibility), but loom-claim prevents races
- First Builder to claim wins; others move to next issue

> `loom-claim` is a PATH shim over `loom-daemon claim` (issue #4275 ported the
> implementation from Python to native Rust). The command name, positional
> grammar and exit codes below are unchanged, and the shim needs no pip install
> — it is provisioned next to the `loom-daemon` binary. The `command -v
> loom-claim` degradation guard below still applies unchanged.

### Claiming Workflow

**1. Check for stop signals before claiming new work:**

```bash
# Check if stop signal exists (graceful shutdown)
if ./.loom/scripts/signal.sh check "$AGENT_ID"; then
  echo "Stop signal received, completing current work and exiting"
  exit 0
fi
```

**2. Attempt atomic claim before label change:**

```bash
# Try to claim atomically (prevents race conditions)
if loom-claim claim "$ISSUE_NUMBER" "$AGENT_ID"; then
  # Claim succeeded - now update labels for visibility
  gh issue edit "$ISSUE_NUMBER" --remove-label "loom:issue" --add-label "loom:building"
  echo "Claimed issue #$ISSUE_NUMBER"
else
  # Another agent claimed it first
  echo "Issue #$ISSUE_NUMBER already claimed, trying next issue"
  continue  # In a loop, move to next issue
fi
```

**3. Extend claim for long-running work:**

```bash
# If work takes longer than 30 minutes, extend the claim
loom-claim extend "$ISSUE_NUMBER" "$AGENT_ID" 3600  # Extend by 1 hour
```

**4. Release claim on completion or abandonment:**

```bash
# When PR is created or work is blocked, release the claim
loom-claim release "$ISSUE_NUMBER" "$AGENT_ID"
```

### Full Parallel Mode Example

```bash
#!/bin/bash
AGENT_ID="${AGENT_ID:-builder-$$}"

while true; do
  # Check for stop signal
  if ./.loom/scripts/signal.sh check "$AGENT_ID"; then
    echo "Stop signal received, exiting"
    exit 0
  fi

  # Find available issues
  EXCL="$(./.loom/scripts/skip-labels.sh --jq-not)"
  ISSUES=$(gh issue list --label="loom:issue" --state=open --limit 500 --json number,labels --jq ".[] | select($EXCL) | .number")

  for ISSUE_NUMBER in $ISSUES; do
    # Try atomic claim
    if loom-claim claim "$ISSUE_NUMBER" "$AGENT_ID" 1800; then
      # Claim succeeded
      gh issue edit "$ISSUE_NUMBER" --remove-label "loom:issue" --add-label "loom:building"

      # Create worktree and do work
      ./.loom/scripts/worktree.sh "$ISSUE_NUMBER"
      cd ".loom/worktrees/issue-$ISSUE_NUMBER"

      # ... implement feature ...
      # ... run tests ...
      # ... create PR ...

      # Release claim (PR will handle the rest)
      loom-claim release "$ISSUE_NUMBER" "$AGENT_ID"

      break  # Move to next iteration
    fi
  done

  # No issues available, wait before retrying
  sleep 60
done
```

### Claim Commands Reference

| Command | Purpose |
|---------|---------|
| `loom-claim claim <issue> [agent-id] [ttl]` | Atomically claim an issue (default TTL: 30 min) |
| `loom-claim extend <issue> <agent-id> [seconds]` | Extend claim TTL for long work |
| `loom-claim release <issue> [agent-id]` | Release claim when done |
| `loom-claim check <issue>` | Check if issue is claimed |
| `loom-claim list` | List all active claims |
| `loom-claim cleanup` | Remove expired claims |

### Signal Commands Reference

| Command | Purpose |
|---------|---------|
| `signal.sh stop <agent-id\|all>` | Send stop signal |
| `signal.sh check <agent-id>` | Check for stop signal (exit 0 if signal exists) |
| `signal.sh clear <agent-id\|all>` | Clear stop signal |

### When to Use Parallel Mode

**Use atomic claiming when:**
- Multiple Builder agents run simultaneously (Daemon Mode dispatching parallel sweeps)
- Risk of two agents picking the same issue
- Need graceful shutdown capability

**Skip atomic claiming when:**
- Single Builder in Manual Orchestration Mode
- Human directly assigns issues
- Testing/debugging workflows

### Graceful Degradation

If `loom-claim` or `signal.sh` don't exist (older installations), fall back to label-only claiming:

```bash
# Check if claiming system exists
if command -v loom-claim &>/dev/null; then
  # Use atomic claiming
  loom-claim claim "$ISSUE_NUMBER" "$AGENT_ID" || continue
fi

# Always update labels (works with or without claiming system)
gh issue edit "$ISSUE_NUMBER" --remove-label "loom:issue" --add-label "loom:building"
```
