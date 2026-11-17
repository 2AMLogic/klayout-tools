"""A vendor PCell that raises inside ``produce_impl`` is an application
error, not a silent success with an unreadable GDS (issue #5936).

KLayout does not propagate such an exception: ``add_pcell_variant`` returns
a valid index whose cell is unnamed and empty, and only an ``ERROR:`` line
on stderr hints anything went wrong. These tests pin the fix -- klt checks
the variant before writing -- with fabricated tmp_path vendor PCells, the
same fixture style as ``test_gen_pdk_pcell.py`` (unique package/library/
class names per the process-global registration rule; CI downloads no PDK).
"""

import itertools
import json

import pytest

from klayout_tools import pdk
from klayout_tools.cli import main
from klayout_tools.pdk_pcell import PdkPCellError, generate_pdk_pcell

_COUNTER = itertools.count()


def _unique(prefix: str) -> str:
    """A process-unique identifier -- see the module docstring."""
    return f"{prefix}_{next(_COUNTER)}"


#: The ``test_gen_pdk_pcell.py`` toy vendor PCell, except ``produce_impl``
#: raises the way a real vendor PCell does when its environment is wrong
#: (gf180mcuA's ``cap_mim`` with ``GF_PDK_OPTION`` unset: ``KeyError``).
_RAISING_PACKAGE = '''\
import pya


class {klass}(pya.PCellDeclarationHelper):
    """Toy vendor PCell whose produce always raises (issue #5936)."""

    def __init__(self):
        super().__init__()
        self.param("w_um", self.TypeDouble, "Box width (um)", default=2.0)

    def display_text_impl(self):
        return "{cell}"

    def produce_impl(self):
        raise KeyError("TOY_PDK_OPTION")


class {libklass}(pya.Library):
    def __init__(self):
        super().__init__()
        self.description = "toy raising vendor PCell library"
        self.layout().register_pcell("{cell}", {klass}())
        self.register("{library}")

{registration}
'''

#: A vendor PCell that draws no geometry but names itself properly: empty
#: output is a legitimate vendor outcome, so klt must not flag it -- the
#: failure signal is the *unnamed* variant a swallowed ``produce`` exception
#: leaves, never the geometry count (see the issue's edge-case note).
_EMPTY_PACKAGE = '''\
import pya


class {klass}(pya.PCellDeclarationHelper):
    """Toy vendor PCell that legitimately produces no geometry."""

    def __init__(self):
        super().__init__()
        self.param("w_um", self.TypeDouble, "Box width (um)", default=2.0)

    def display_text_impl(self):
        return "{cell}"

    def produce_impl(self):
        pass


class {libklass}(pya.Library):
    def __init__(self):
        super().__init__()
        self.description = "toy empty vendor PCell library"
        self.layout().register_pcell("{cell}", {klass}())
        self.register("{library}")

{registration}
'''


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Scrub PDK env vars and empty the host search space -- see test_pdk.py."""
    monkeypatch.delenv("PDK_ROOT", raising=False)
    monkeypatch.delenv("PDK", raising=False)
    monkeypatch.setattr(pdk, "STORE_DIRS", [])
    monkeypatch.setattr(pdk, "CONVENTIONAL_PREFIXES", [])


def _fabricated_pdk(tmp_path, template, prefix):
    """A fabricated install shipping one self-registering toy PyCell package.

    Returns ``(root, package, library, cell)`` -- the ``_toy_pdk`` shape from
    ``test_gen_pdk_pcell.py``, parameterized by the package template.
    """
    variant_dir = tmp_path / "pdk_install" / "sky130A"
    package_dir = variant_dir / "libs.tech" / "klayout" / "python"
    package_dir.mkdir(parents=True)
    package = _unique(prefix)
    library = _unique(f"{prefix}_lib")
    klass = _unique("ToyCell")
    libklass = _unique("ToyLibrary")
    package_dir = package_dir / package
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text(
        template.format(
            klass=klass,
            libklass=libklass,
            library=library,
            cell="ToyCell",
            registration=f"{libklass}()",
        )
    )
    return tmp_path / "pdk_install", package, library, "ToyCell"


def test_raising_produce_is_application_error(tmp_path):
    root, _package, library, cell = _fabricated_pdk(
        tmp_path, _RAISING_PACKAGE, "raisinglib"
    )
    output = tmp_path / "boom.gds"

    with pytest.raises(PdkPCellError, match=f"{library}/{cell}"):
        generate_pdk_pcell(
            {
                "pdk_pcell": f"{library}/{cell}",
                "pdk": {"variant": "sky130A", "root": str(root)},
                "options": {"output": str(output)},
            }
        )
    assert not output.exists()


def test_cli_raising_produce_exits_nonzero_with_error_envelope(tmp_path, capsys):
    root, _package, library, cell = _fabricated_pdk(
        tmp_path, _RAISING_PACKAGE, "raisinglib"
    )
    output = tmp_path / "boom.gds"

    exit_code = main(
        [
            "gen",
            "--pdk-pcell",
            f"{library}/{cell}",
            "--pdk",
            "sky130A",
            "--pdk-root",
            str(root),
            "-o",
            str(output),
            "--format",
            "json",
        ]
    )

    assert exit_code != 0
    assert not output.exists()
    data = json.loads(capsys.readouterr().err)
    assert data["schema_version"] == 1
    assert data["error"]["command"] == "gen"
    assert f"{library}/{cell}" in data["error"]["message"]


def test_named_but_empty_pcell_is_not_flagged(tmp_path):
    root, _package, library, cell = _fabricated_pdk(
        tmp_path, _EMPTY_PACKAGE, "emptylib"
    )
    output = tmp_path / "empty.gds"

    report = generate_pdk_pcell(
        {
            "pdk_pcell": f"{library}/{cell}",
            "pdk": {"variant": "sky130A", "root": str(root)},
            "options": {"output": str(output)},
        }
    )

    assert output.is_file()
    assert report["generator"] == f"{library}/{cell}"
