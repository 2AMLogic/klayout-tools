# Ratification reviewer lane

The daemon-side lane that gives a ratification PR two independent key reviews
(EE and market) and records each as a marker comment. Part of #13186; the
Champion hold that consumes the result is in [`ratification-hold.md`](ratification-hold.md).

Code: `loom-daemon/src/role_runner/ratification_lane.rs` (selection, markers,
tick), `ratification_dispatch.rs` (the two sessions) and
`ratification_attempts.rs` (the attempt bound).

## Config

`ratificationLane` in `.loom/config.json`. Off by default; a missing,
malformed or half-configured block means the lane never runs.

| Key | Meaning |
|-----|---------|
| `enabled` | `true` to run the lane (anything else is off) |
| `ee`, `market` | each `{repo, path, commit}`: the instruction file for that key, pinned to a full 40-hex commit SHA. Both are required |

### Enabling it fleet-wide

Set the block once in the fleet-tier config (a repo's own `.loom/config.json`
overrides it; see [`fleet-config-lifecycle.md`](fleet-config-lifecycle.md)):

```json
{
  "ratificationLane": {
    "enabled": true,
    "ee":     {"repo": "<owner>/<instructions-repo>", "path": "<path-to-ee.md>",     "commit": "<40-hex SHA>"},
    "market": {"repo": "<owner>/<instructions-repo>", "path": "<path-to-market.md>", "commit": "<40-hex SHA>"}
  }
}
```

The `{repo, path, commit}` pins for `ee` and `market` are operator-owned values;
Loom ships no defaults for them, so the lane stays off until the operator sets
them. Selection also honors `champion.ratificationPaths` / `ratificationPhrases`.

## Flow

1. The Champion role tick calls the lane for the root when `enabled`.
2. Selection finds open PRs that Champion's ratification hold would hold: the
   same predicate (`champion_hold::ratification::scan` returns `Hold`), so every
   spec/decision file declaring ratification-by-merge counts, not only status
   flips. A candidate file that cannot be read is not selected (logged). A PR
   whose current head already carries both markers is skipped. The head SHA is captured here.
3. Only the keys still missing a current-head marker, and neither capped nor
   backed off ([Attempt bound](#attempt-bound-15315)), are dispatched (#14797),
   `ratification-ee` and `ratification-market`, in that order when both are
   missing. Each prompt is, in order: the pinned instructions, the PR
   reference, for a market review of a public repo the public-sources-only
   line, and last **Loom's lane output contract** (#15315) — one line saying
   that Loom posts the final message (so no comment and no
   `scripts/ratify-key.sh`), that the review starts at the key's review
   heading, that the template's angle-bracket parts are placeholders, and the
   key's verdict words, generated from the same list the parser accepts. The
   contract names the heading in prose and never contains the heading or a
   verdict line itself. Before parsing, every verbatim copy of the prompt in
   the session output is cut out (the **echo trim**), so a review written
   before or after an echoed prompt still parses and the template inside the
   echo never does; an output that is only the echo has no heading.
4. For each attempted key whose session ends in success **and** whose output
   parses as that key's review (below) the daemon posts one comment. A key
   that failed closed, whose session did not succeed, or whose output carries
   no verdict posts nothing and the reason is logged; the other key is
   unaffected. A withheld key posts no marker, so it is still missing on a
   later tick; whether it is dispatched again at the same head is the
   attempt bound's decision, below.

### Attempt bound (#15315)

Without a bound a key that can never yield a parseable review at a head — or
a host fault that refuses every launch — is re-dispatched every step tick on
every host. Selection does not change; the bound sits between selection and
dispatch, and posts nothing to the PR.

- **Counted attempts.** A session that ran and left no usable review —
  `output_missing`, `verdict_invalid` or `verdict_placeholder`, or an
  `invocation_failed` session that launched and failed with a class the PR
  may have caused (`timeout-ceiling`, an `exit-<n>`, `runtime-fatal`,
  `model-refusal`, `killed-by-signal`, unclassified) — is one counted attempt
  for its `(repo, PR, head, key)`. At most **3** are
  dispatched in any trailing **24 h**. At the cap the key is **capped**: not
  dispatched until the oldest of its last three attempts is 24 h old, or at
  once when the PR gets a new head (attempts are recorded per head).
- **The ledger.** Counted attempts are recorded per host and workspace in
  `.loom/state/ratification-lane/attempts.json` (`{repo, pr, head_sha, key,
  class, at}` per attempt, `at` being when its session ended; written
  atomically as soon as a PR's sessions have recorded an attempt, so a
  daemon that exits mid-pass keeps them; `.loom/state/` is never tracked). Each pass drops attempts older than the window and attempts for
  a PR the forge's open-PR listing no longer shows at that head (closed, or
  pushed). A PR whose files or comments could not be read this pass is still
  listed, so it keeps its attempts, and an empty listing (which may be a
  failed read) forgets nothing. It is an efficiency guard, not a gate, and it fails open: an
  unreadable or corrupt ledger counts as empty (logged once), and a failed
  save keeps the attempts in daemon memory, so this host still stops at the
  cap. A ledger written under other parse rules counts nothing. Each host
  keeps its own ledger, so the fleet-wide bound is three per host per day.
  There is no clear command: the window expires on its own, and deleting
  the file is the manual reset.
- **Not counted: backoffs.** A failure the PR did not cause never counts. It
  delays the next try by **15 minutes** in daemon memory (reset on restart):
  - a pinned-instruction read that failed (for example `deferred: reader
    budget low`): that pin, for every workspace on the host;
  - a launch refused before its CLI ran (`pre_launch_failed`), a launched
    session that failed with an account, pool, credential or host class
    (`account-pool-exhausted`, `account-exhausted:*`, `no-usable-account`,
    `session-limit`, `credential-expired`, `runtime-timeout`,
    `sandbox-unavailable`, `cwd-deleted`, the daemon's own `launch-failed`
    and `wait-failed`, or a launch-refusal class such as
    `preflight-no-cli-start`), or one the role timeout stopped on a saturated
    host: that key, for the repo;
  - a marker post the forge refused (`post_failed`): that PR, head and key
    only.

  A version roll stopping a session is neither counted nor backed off.
- `ratification-lane status` prints each key's counted attempts and, when
  capped, the release time ([below](#reading-the-markers-ratification-lane-status)).

### Missing-key retry (#14797)

A key is **complete** for a head when a trusted comment (the same trust
policy as every marker read, `.loom/docs/comment-trust.md`) carries its
marker for the PR's *current* head — whether the lane or a hand-run lane
posted it, and whatever the verdict it records (`escalate` and
`request-changes` reviews were still performed). A later tick dispatches,
reads instructions for and posts **only the incomplete keys**; a complete key
consumes no role session, no marker post and no withheld count. The head is
still the one captured at selection: a push mid-review leaves the new head
with no markers, so both keys run again there. Two completed keys do not
release the separate human ratification hold
([`ratification-hold.md`](ratification-hold.md)).

### Unreadable comment lists

The marker state lives in the PR's comment history, so the lane must be able
to tell "read, and there are no markers" from "could not read". A comment
listing that fails or does not parse is **unreadable**: the PR is skipped
before any instruction read or session runs and counts in the tick's
`unreadable` (a `degraded` tick), the same accounting as an unreadable
candidate file. An unreadable list never authorizes a rerun — treating it as
"no markers" would duplicate completed keys. A successfully read empty list
selects both keys as missing.

## Tick outcome counts

The existing `ratification_lane.tick` span reports one final outcome per
attempted key. Each entry below is a count at
`loom.ratification.<ee|market>.<outcome>`. All fourteen fields are present,
including zeroes. Review content and diagnostic details stay off the span.

| Outcome | When it is counted |
|---|---|
| `posted` | The marker was posted for a valid verdict, including a negative review. |
| `pre_launch_failed` | An instruction pin failed, the runner reported a known pre-launch refusal, or the launcher refused before the CLI ran (for example exit 78 with `# LOOM_LAUNCH_REJECTED` and no CLI-start marker). |
| `invocation_failed` | An invocation did not succeed, including interruption or failure with an unknown launch stage. |
| `output_missing` | The invocation succeeded without captured output. |
| `verdict_invalid` | The captured output did not parse as a review for that key. |
| `verdict_placeholder` | The review's heading or verdict line is still the instruction template's placeholder (`<…>` or a `a \| b` list): the template was copied, not filled. |
| `post_failed` | The review parsed and the forge did not accept the marker post. |

The two `posted` fields sum to `loom.ratification.posted`. The other
twelve sum to `loom.ratification.withheld`. A trusted marker for the current
head skips that key, and an unreadable candidate counts once in
`loom.ratification.unreadable`. Neither case adds a key outcome.

Two header counts report the [attempt bound](#attempt-bound-15315), and are
not key outcomes either: `loom.ratification.capped` counts missing keys not
dispatched because they reached the cap, and `loom.ratification.deferred`
missing keys not dispatched because of a backoff. A tick whose candidates
were all readable but had every missing key capped or deferred has reason
`held` (`selected` counts those candidates; `ran` does not). An unreadable
PR still makes the tick `degraded`, and any dispatch makes it `ran`.

### Check retained counts after rollout

After ordinary rollout, substitute the exact deployed daemon SHA and a
bounded UTC interval with retained work in the query below. It collapses
replayed spans by trace and span ID before comparing and summing the same
tick rows. Rows missing fields are counted separately from genuine zeroes.
Require nonzero `ticks`, zero `incomplete_ticks`, and zero
`unreconciled_ticks`. Use a window with nonzero posted or withheld counts
to observe actual outcomes. Check the fields remain present through the
collector route if any are missing.

```sql
WITH
  ['ee', 'market'] AS review_keys,
  ['posted', 'pre_launch_failed', 'invocation_failed', 'output_missing',
   'verdict_invalid', 'verdict_placeholder', 'post_failed'] AS outcomes,
  arrayConcat(
    ['loom.ratification.posted', 'loom.ratification.withheld'],
    arrayFlatten(arrayMap(k -> arrayMap(o -> concat('loom.ratification.', k, '.', o),
                                        outcomes), review_keys)),
    ['loom.ratification.capped', 'loom.ratification.deferred']
  ) AS fields,
  per_tick AS (
    SELECT argMax(attributes_string, timestamp) AS attrs
    FROM signoz_traces.signoz_index_v3
    WHERE name = 'ratification_lane.tick'
      AND attributes_string['loom.daemon.revision'] = '<deployed-sha>'
      AND timestamp >= toDateTime64('<start-utc>', 9, 'UTC')
      AND timestamp < toDateTime64('<end-utc>', 9, 'UTC')
    GROUP BY trace_id, span_id
  ),
  decoded AS (
    SELECT arrayMap(f -> toUInt64OrNull(attrs[f]), fields) AS parsed,
           arrayAll(x -> isNotNull(x), parsed) AS complete,
           arrayMap(x -> ifNull(x, toUInt64(0)), parsed) AS n
    FROM per_tick
  )
SELECT count() AS ticks,
       countIf(NOT complete) AS incomplete_ticks,
       countIf(complete AND (n[1] != n[3] + n[10]
         OR n[2] != arraySum(arraySlice(n, 3, 14)) - n[3] - n[10])) AS unreconciled_ticks,
       mapFromArrays(fields,
         arrayResize(sumForEach(arrayMap(x -> if(complete, x, toUInt64(0)), n)),
                     length(fields), toUInt64(0))) AS totals
FROM decoded;
```

## Marker format

```
<!-- ratify:key=<ee|market> sha=<40-hex head> -->
reviewer=<ratification-ee|ratification-market>
```

The first line is the wire format `keys_marked` parses; `reviewer=` is a visible
line beside it and is not part of the regex. The `sha` is the head captured at
selection, not re-read at post time: a push mid-review leaves the new head
without markers, so the PR is selected again. After the marker the comment
carries a `Verdict:` section: the extracted review text, never the session's
log. The review is delimited by the reviewer's own heading — `## EE-key
review:` or `## Market-key review:` — and runs from that heading's **last**
occurrence in the session output to the first launcher/wrapper line after it
(a timestamped `[<ISO-8601>] [INFO|WARN|ERROR]` log line or a
`[claude-wrapper]` diagnostic), so launcher or wrapper noise on either side
of the review is never read as a verdict or posted — no auth-mode line, no
wrapper tail.
A marker is posted only when the review also carries an `**Overall verdict:**`
line whose first token is one of the key's verdicts: `approve` /
`request-changes` for `ee`; `competitive` / `adequate-for-catalog` /
`uncompetitive` / `escalate` for `market`. The label may also be written
`**Overall verdict**:`, in any letter case, or unbolded (`Overall verdict:`)
at the start of a line; beside a bold verdict line, an unbolded one whose
first word is not a verdict word is a sentence and is not read. The first
word after the label decides. That word is normalized before it is compared
(#15315): trailing `.` `;` `:` `!` are dropped, one balanced wrapping of
`**`, `__`, `` ` ``, `*` or `_` is removed (repeatedly, and only around a
non-empty word), and ASCII case is folded — so `**approve**.`,
`` `Competitive` `` and `APPROVE;` all read as their token. The verdict's
first clause must not name a second allowed token: after a bare word the
clause ends at the next comma, sentence punctuation, parenthesis or dash
(`approve — no request-changes needed` reads as `approve`), but after a word
that carried one of those marks it runs to the first comma or the end of the
line, so normalization never hides a second verdict behind the mark it
dropped (`approve. request-changes` is withheld). Text after the first comma
is the "one sentence why" and is not read for a verdict. Still withheld: a
placeholder heading (its title starts with `<`) or verdict in the template's
shapes — a leading `<`, a `<a|b>` list anywhere on the line, or a `|` beside
a verdict word (`approve | request-changes`), counted as
`verdict_placeholder`; a comparison (`Vth < 0.4 V`) or a magnitude (`|S11|`)
in the reasoning is not one — a first clause naming two allowed tokens
(`approve or request-changes`), a `?` or any other unknown word, unbalanced
emphasis, and two lines that normalize to different tokens. Anything else — the
reviewer declined, authored the PR or reported a disclosure problem (all no
verdict), or no heading — also posts no marker and is logged as withheld. The text is at most 4000 characters — the head is
kept on truncation, so the heading and verdict line survive — with an
explicit notice when cut; its `<!--` and `-->` are escaped, so it cannot
form or alter a marker.

## Reading the markers: `ratification-lane status`

A held PR can carry dozens of key-marker comments at one head — daemons
older than the missing-key retry fix (#14797/#14800) re-dispatched both keys
every tick — and the markers exist in two formats. The read-only report

```
loom-daemon ratification-lane status <PR> [--repo owner/name] [--root PATH]
```

fetches the PR's current head and its complete, paginated comment history
(trusted authors only, `.loom/docs/comment-trust.md`), then prints the
reviewed head and one row per key: the **effective** marker — the review to
read first — with a link, one indented `superseded` link for every other
eligible marker at the same head, and an explicit `MISSING` line when a
complete read finds no marker for a key. An on-demand report also works for
historical PR threads where both keys are already present and the lane has
stopped posting. The command performs only reads: it never dispatches,
posts, edits, minimises or deletes anything, and it never releases a hold —
the human retains the ratification decision. If the PR head or the comment
history cannot be read (including a torn later page or a malformed record),
it reports the error and exits non-zero rather than guessing.

After the key rows it prints this host's [attempt bound](#attempt-bound-15315)
for that head, read from the workspace's ledger with the same rule the lane
applies:

```
Attempts on this host (/path/to/repo/.loom/state/ratification-lane/attempts.json):
  ee: CAPPED until 2026-11-23T10:00:00Z — 3/3 counted withheld at this head in the last 24h
    2026-11-22T10:00:00Z verdict_placeholder
    2026-11-22T11:00:00Z verdict_invalid
    2026-11-22T12:00:00Z verdict_invalid
  market: 0/3 counted withheld at this head in the last 24h
  (launch refusals and host failures are not counted; see loom.ratification.<key>.pre_launch_failed. Delete the file to reset.)
```

A capped key is dispatched again at the printed time, or at once on a new
head. An unreadable ledger is reported on that block (the lane treats it as
empty) and does not make the command fail. Backoffs live in daemon memory
and are not shown. Run it on the host whose lane you are asking about.

### Which marker is effective

- A marker is **eligible** for a head when a trusted author posted it, it
  names the PR's current head with a full 40-hex SHA, and — when the marker
  carries a `block=owner/repo#<n>` identity — that identity names exactly
  this PR. Markers for another head or another PR are neither effective nor
  superseded: they are not this head's markers.
- The **effective** marker for a key is the newest eligible one by the forge
  comment's **creation time**, with the numeric comment id as the
  tie-breaker. Both formats compete on exactly the same terms; an edit, a
  date written inside the review, or a more favourable verdict gives a
  comment no priority. Every other eligible comment for that key is listed
  as **superseded** and linked once. No review text is deleted.
- A marker inside a fenced code block or on a block-quote line is a quoted
  example, not a marker: review prose cannot establish a head or a key.

Both wire formats are recognized:

| Format | Shape | Posted by |
|--------|-------|-----------|
| Lane | `<!-- ratify:key=<ee\|market> sha=<40-hex head> -->` | the lane (`marker_body`) |
| Hand | `<!-- RATIFY-KEY: <ee\|market> verdict=… block=owner/repo#<pr> sha=<40-hex head> reviewer=… date=… -->` | a hand-run key-apply |

### A mixed-format example

Suppose a part repo's PR #98 carried both Market formats at head `6a3cd2c4…`
(#14834): the lane posted
`<!-- ratify:key=market sha=6a3cd2c43c31821fe93bbc18828c0eb6e06aa063 -->`
(created 19:00:37Z), then a hand-applied
`<!-- RATIFY-KEY: market verdict=adequate-for-catalog
block=example-org/part-repo#98 sha=6a3cd2c… -->`
followed (created 19:14:25Z). The report prints:

```
example-org/part-repo#98
Reviewed head: 6a3cd2c43c31821fe93bbc18828c0eb6e06aa063

ee: MISSING — no trusted ee marker names this head

market: effective 2026-11-20T19:14:25Z https://github.com/example-org/part-repo/pull/98#issuecomment-7048390167 (RATIFY-KEY)
  superseded 2026-11-20T19:00:37Z https://github.com/example-org/part-repo/pull/98#issuecomment-7048105117 (ratify:key)
```

The RATIFY-KEY review is the one to read first; the earlier `ratify:key`
review is still linked, still complete, just superseded.

## Trust model

No signing key, broker or per-key App. The daemon posts each marker as the fleet
identity and readers trust it under `.loom/docs/comment-trust.md`, as they do a
Judge verdict; a marker from an untrusted author is prose.

## Independence rule

The two keys are separate sessions, and neither is the session that built the
PR. Author login is not the test: in a fleet the Builder opens the PR as the
same identity that dispatches the lane. The builder's sweep id is read from the
PR's `<!-- loom:pr-lease[-released] role=builder ... sweep=<id> -->` comment;
a lane session (`LOOM_SWEEP_ID`) equal to it is refused before any session is
launched or comment posted. Nothing crosses between the sessions.
