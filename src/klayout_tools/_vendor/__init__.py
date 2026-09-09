"""Third-party source ported into this repo rather than depended on.

Every module in this package carries its own upstream attribution header
(origin repository, file path, fetch date, license) at the top of the file --
the mechanism ``docs/design/mutation-testing-spike.md`` -> "Open questions" ->
"Attribution mechanics" settled on for this repo, which has no ``NOTICE`` file
and (per that spike's own live check) ports from projects that ship none
either. Per-file headers satisfy Apache-2.0 section 4's notice-preservation
requirement directly, and are fully compatible with this repo's own MIT
license for its original code.

Rules for anything added here:

- **Port, don't fork silently.** Keep the upstream shape recognisable so a
  reviewer can diff against the origin file; list every deliberate change in
  the header's own "Changes from upstream" block.
- **No `klt` imports.** A vendored module must stay standalone -- it is the
  first-party wrapper's job to adapt it to this repo's error classes and
  request/response contracts, not the vendored module's job to know about
  them.
"""
