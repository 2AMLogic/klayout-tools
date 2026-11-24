- **Unavailable LVS and extraction inputs** (#6983, 2026-11-22).
  `klt lvs --check` and `klt extract --check` report unavailable inputs as `unverifiable` (exit 4). Changed hashes still report `drifted` (exit 3), including when another input is missing.
