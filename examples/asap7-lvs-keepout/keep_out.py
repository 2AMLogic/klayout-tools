import json
import sys
from pathlib import Path

from klayout_tools.lvs_known_limits import KNOWN_UNSUPPORTED_ASAP7_CELLS

keep_out = sorted(KNOWN_UNSUPPORTED_ASAP7_CELLS)
for argument in sys.argv[1:]:
    path, section = argument.rsplit(":", 1)
    document = json.loads(Path(path).read_text())
    settings = dict(document.get(section) or {})
    settings["dont_use"] = list(
        dict.fromkeys([*settings.get("dont_use", []), *keep_out])
    )
    document[section] = settings
    Path(path).write_text(json.dumps(document, indent=2) + "\n")
