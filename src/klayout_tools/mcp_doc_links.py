"""Manual URLs shared by the MCP tool catalog and schema resources.

Split out of :mod:`.mcp_tools` (issue #6793) so a verb whose manual section
lives under another verb's page can be mapped without growing that module past
its file-size ceiling. Issue #6819 moved the bare-pointer rewrite here too, so
both MCP surfaces rewrite ``docs/cli/<page>.md`` pointers through one helper.
"""

from __future__ import annotations

import re

DOCS_BASE_URL = "https://github.com/2AMLogic/klayout-tools/blob/main/docs/cli"

#: A standalone ``docs/cli/<page>.md`` pointer in shipped prose: the same
#: matching semantics as ``cli/_doc_paths._DOC_POINTER`` (an absolute URL, a
#: longer path or an embedded file name is left alone; a ``#fragment`` suffix
#: survives after the rewritten page).
_DOC_POINTER = re.compile(
    r"(?<![\w./\\-])docs/cli/([A-Za-z0-9_-]+\.md)(?![\w/\\-]|\.\w)"
)

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


def link_cli_docs(text: str) -> str:
    """Use URLs for bare CLI doc references so remote clients can follow them."""
    return _DOC_POINTER.sub(rf"{DOCS_BASE_URL}/\1", text)
