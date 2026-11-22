- **`klt sim --attach` / `--resume` no longer report a credentials or
  throttling failure as "never submitted"** (#5613, 2026-11-19): the
  `job.json` existence probe behind the `--attach` precondition, shard-resume
  reconciliation and the deck-split attach paths read any non-zero `aws s3 cp`
  exit as "no such job", so an `AccessDenied`, an expired credential or an S3
  throttle surfaced as `batch_unknown_job` and sent the user looking for a job
  that exists. It is now the same `s3api head-object` probe `--job-status`
  already used: only a 404 / `Not Found` / `NoSuchKey` means "absent"; any
  other failure, timeout or spawn error raises a `batch backend failed: …`
  error carrying the AWS CLI's message (no `batch_unknown_job` code). The probe
  is bounded to 60 s (was 900 s) and no longer downloads the object body. A
  shard-resume that cannot tell whether a `pending` shard's job was uploaded
  now stops instead of resubmitting it. **Behaviour change** for callers that
  keyed on `batch_unknown_job` to detect a transport failure: that was never
  its meaning, and those cases now carry no `batch_unknown_job` code.
