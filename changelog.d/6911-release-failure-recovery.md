- **The automated release pipeline now reports and recovers from failed
  prepare/tag runs** (#6911, 2026-11-21): a failed `release.yml` job files or
  refreshes one open "Automated release blocked" issue (failed step,
  diagnostic, run URL) using the workflow's own token, so the report works
  even when the release-PR credential is what failed (native-extras failures
  included); the prepare job checks the PR-creation permission *before*
  pushing anything — a readable `can_approve_pull_request_reviews: false`
  stops the run and names the administrator setting or the
  `RELEASE_BOT_TOKEN` alternative, an unreadable setting proceeds —; and a
  PR-creation failure after a successful push reconciles the exact branch
  that run pushed: an open PR on that head wins, an inconclusive lookup or a
  moved remote SHA keeps the branch, otherwise it is deleted under an
  expected-SHA lease so a retry pushes cleanly. The release-lag report
  additionally names the newest `release.yml` run when it failed (new
  additive JSON field `last_failed_run`; exit codes unchanged), and
  RELEASING.md's manual sequence now mirrors the prepare job's order
  (committed bump before the generators, amend before pushing).
