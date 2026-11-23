"""The base-module re-exports remain available after the module split."""

import inspect

from klayout_tools import functional_verification, functional_verification_base

_BASE_PUBLIC_NAMES = (
    "FunctionalVerificationError",
    "SDF_ALIAS_PORT_DROPPED_CLASS",
    "SDF_ANNOTATE_MODULE",
    "SDF_BENIGN_DIAGNOSTIC_SUBSTRINGS",
    "SDF_CORNERS",
    "SDF_DIAGNOSTIC_LINE_RE",
    "SDF_DIAGNOSTIC_MARKERS",
    "SDF_ENGINES",
    "SDF_ENTRIES_MODES",
    "SDF_MIN_ICARUS_MAJOR",
    "SDF_OMITTED_ANNOTATION_MARKER",
    "SDF_SPLICED_LOG_HEADER_RE",
    "SDF_WRAPPER_DUT_INSTANCE",
    "SDF_WRAPPER_MODULE",
    "DEFAULT_SDF_CORNER",
    "DEFAULT_SDF_ENTRIES",
)

# Imported into the base module for its own use, not part of the re-export
# contract: ``re``, the ``Any`` typing helper, and the ``annotations``
# future-import feature object.
_BASE_IMPORTED_NAMES = frozenset({"re", "Any", "annotations"})


def test_base_reexports_each_moved_object():
    for name in _BASE_PUBLIC_NAMES:
        assert getattr(functional_verification, name) is getattr(
            functional_verification_base, name
        )


def test_base_reexports_cover_every_public_base_name():
    public = {
        name
        for name, obj in vars(functional_verification_base).items()
        if not name.startswith("_")
        and name not in _BASE_IMPORTED_NAMES
        and not inspect.ismodule(obj)
    }
    assert public == set(_BASE_PUBLIC_NAMES)
    for name in public:
        assert getattr(functional_verification, name) is getattr(
            functional_verification_base, name
        )
