"""Manual URLs the MCP tool catalog appends to each tool description.

Split out of :mod:`.mcp_tools` (issue #6793) so a verb whose manual section
lives under another verb's page can be mapped without growing that module past
its file-size ceiling.
"""

from __future__ import annotations

DOCS_BASE_URL = "https://github.com/2AMLogic/klayout-tools/blob/main/docs/cli"

#: Verbs with no ``docs/cli/<verb>.md`` page: the manual covers them as a
#: section of ``sim.md``. The fragments are GitHub's heading anchors, which
#: include the issue number the heading cites.
_SECTION_TARGETS = {
    "sim-merge": "sim.md#merging-per-request-reports-klt-sim-merge-3045",
    "sim-plan": "sim.md#planning-shards-klt-sim-plan-3423",
}


def cli_docs_url(verb: str) -> str:
    """URL of the manual page (or section) documenting top-level ``verb``."""
    return f"{DOCS_BASE_URL}/{_SECTION_TARGETS.get(verb, f'{verb}.md')}"
