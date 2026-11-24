# Changelog fragments

Use one UTF-8 Markdown file per change, named `<issue>-<slug>.md`. Write the
entry as a Markdown bullet, like the entries in `CHANGELOG.md`, and include
the issue number as `#<issue>`. A date is optional: the release heading already
carries it. For example, `1234-fix-lvs-count.md` could contain

```markdown
- **LVS counts repeated mismatches once** (#1234).
```

CI enforces this with `scripts/check_changelog_fragments.py`. A fragment fails
unless its first non-blank line starts with `- ` and its text contains a
`#<digits>` issue reference anywhere (the reference may be on a continuation
line of a multi-line bullet). Run `python scripts/check_changelog_fragments.py`
before pushing; it names each offending file and why.

`scripts/release_prep.py prepare` appends the fragments in filename order
after any existing `## Unreleased` entries in the new release section of
`CHANGELOG.md` and removes the files it consumed. It copies each fragment as
written and does not check or repair its shape, which is why the check above
runs at PR time. It skips this README, empty files, and files with other
extensions; the check skips the same files.
