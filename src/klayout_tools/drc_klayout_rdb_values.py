"""Per-finding evidence attached to KLayout report-database items."""

import re

# A value may begin with a tag, e.g. [#RATIO] or ['region] name']. KLayout
# single-quotes names with delimiters and backslash-escapes embedded quotes.
_GEOMETRY_VALUE = re.compile(
    r"\s*(?:\[#?(?:'(?:\\.|[^'\\])*'|[^\]']+)\]\s*)?"
    r"(?:polygon|box|edge|edge-pair|path):"
)


def rdb_item_evidence(values: list[str]) -> dict[str, list[str]]:
    """Keep every non-geometry value, including its tag, spelling and position."""
    evidence = [value for value in values if not _GEOMETRY_VALUE.match(value)]
    return {"values": evidence} if evidence else {}
