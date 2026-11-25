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

Since issue #5005 fragments are never folded into `CHANGELOG.md` at release
time (the fleet publisher's bump commit changes only `pyproject.toml` and the
release heading). Instead, `scripts/release_notes.py` renders the fragments
ADDED since the previous `v*` tag into that release's GitHub Release notes,
in filename order, after the release's `CHANGELOG.md` section. The files stay
on disk; keep them -- they are the durable record of what each release
shipped, and the window (previous tag, this tag] is what keeps one release
from repeating an earlier one's entries.
