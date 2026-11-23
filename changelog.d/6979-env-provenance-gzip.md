- **Provenance checks for compressed files** (#6979, 2026-11-22).
  `klt env-provenance scan` and `lint-envelope` inspect gzip-compressed files and report damaged streams as read errors.
