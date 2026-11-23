`klt lvs --check` and `klt extract --check` report unavailable inputs as `unverifiable` (exit 4). Changed hashes still report `drifted` (exit 3), including when another input is missing.
