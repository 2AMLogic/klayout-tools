- **`klt sim` can cap paid fleet launches per batch job** (#7950).
  Set `batch.max_launch_attempts` on corner or deck-list requests to bound the
  initial launch and unattended replacements for each job ID. The client checks
  fleet support before launching each capped job.
