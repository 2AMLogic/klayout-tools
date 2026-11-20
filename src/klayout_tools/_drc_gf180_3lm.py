"""The packaged Metal4 fallback for the foundry's 3LM metal-slot checks."""

from __future__ import annotations

from pathlib import Path

from ._digest import sha256_file, sha256_file_required
from ._report_verify import get_path, hash_check

PRELUDE = Path(__file__).with_name("decks") / "gf180mcu_3lm_prelude.rb"


def configure_prelude(native: dict, deck_vars: dict[str, str]) -> None:
    """Select the macro only for an auto-resolved gf180mcu family script."""
    if Path(native["deck_file"]).name != "gf180mcu.drc":
        return
    # options.rb accepts both variant spellings. An explicit stack wins.
    level = deck_vars.get("metal_level")
    if level is None and deck_vars.get("variant") in ("A", "gf180mcuA"):
        level = "3LM"
    if level == "3LM":
        native["preludes"] = [
            {
                "name": PRELUDE.name,
                "sha256": sha256_file_required(str(PRELUDE)),
                "reason": "Supply empty metal4_drawn for native 3LM slot checks.",
            }
        ]


def prelude_files(native: dict | None) -> tuple[str, ...]:
    """Only the packaged macro can be launched, never a path from a report."""
    return (str(PRELUDE),) if native and native.get("preludes") else ()


def prelude_checks(committed: dict, prefix: str) -> list[dict]:
    """Compare recorded evidence with the installed macro, including its name."""
    recorded = get_path(committed, ("provenance", "deck", "preludes"))
    if recorded is None:
        return []
    entry = recorded[0] if isinstance(recorded, list) and len(recorded) == 1 else None
    entry = entry if isinstance(entry, dict) else {}
    actual = sha256_file(str(PRELUDE)) if entry.get("name") == PRELUDE.name else None
    return [
        hash_check(
            f"{prefix}provenance.deck.preludes[0].sha256", entry.get("sha256"), actual
        )
    ]


def replay_prelude_kwargs(committed: dict, deck_file: str) -> dict:
    """Replay a prelude run using the installed macro and the recorded stack."""
    recorded = get_path(committed, ("provenance", "deck", "preludes"))
    if recorded is None:
        return {}

    from .drc import DrcError
    from .pdk import tree_of

    options = get_path(committed, ("provenance", "deck", "options"))
    native = committed.get("native_deck")
    if (
        not isinstance(native, dict)
        or native.get("kind") != "pdk_main_script"
        or not isinstance(options, dict)
        or not all(isinstance(v, str) for v in options.values())
        or {"input", "report"} & options.keys()
    ):
        raise DrcError("cannot replay the recorded 3LM family deck configuration")
    native = {**native, "deck_file": deck_file}
    native.pop("preludes", None)
    configure_prelude(native, options)
    if "preludes" not in native:
        raise DrcError(
            "recorded prelude requires the native gf180mcu 3LM family script"
        )

    root = None
    if get_path(committed, ("provenance", "pdk", "source")) == "--pdk-root flag":
        tree = tree_of(deck_file)
        root = str(Path(tree).parent) if tree else None
    return {
        "native_deck": native,
        "deck_vars": options,
        "min_klayout_version": native.get("min_klayout_version"),
        "pdk_variant": get_path(committed, ("provenance", "pdk", "name")),
        "pdk_root": root,
    }
