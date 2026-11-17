# Changelog fragments

Use one UTF-8 Markdown file per change, named `<issue>-<slug>.md`. Use the
same bullet format as `CHANGELOG.md`, including the issue number and date.
For example, `1234-fix-lvs-count.md` could contain

```markdown
- **LVS counts repeated mismatches once** (#1234, 2026-11-17).
```

`scripts/release_prep.py prepare` appends the fragments in filename order
after any existing `## Unreleased` entries in the new release section of
`CHANGELOG.md` and removes the files it consumed. It skips this README,
empty files, and files with other extensions.
