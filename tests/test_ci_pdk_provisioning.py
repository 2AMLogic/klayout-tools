"""Static regression tests for the sky130A PDK provisioning composite action
(issue #5658).

Before #5658 the seven workflows that provision the pinned sky130A PDK all
cached `~/.ciel` under one key (`sky130A-ciel-f6eeac7...`) while installing
two different payloads: `-l sky130_fd_pr` (the four sim-canary sites) versus
`-l sky130_fd_pr -l sky130_fd_sc_hd` (the three std-cell consumers). The
fetch was gated solely on the cache-hit output and the key carried no
library input, so whichever payload populated the key first was handed to
every consumer -- and the nightly device-models-only entry won by
construction, leaving equiv-canary / place-and-route-smoke /
post-route-sta-live to run synthesis, P&R and STA against a sky130A tree
with no liberty/LEF.

The fix replaces the seven copy-pasted Install-ciel/Cache/Fetch triples with
one composite action (`.github/actions/setup-sky130a-pdk`) whose library
list is an input folded into the cache key, so a differing library list
cannot share a key by construction. These tests pin that structure the same
way `test_artifact_determinism.py` pins ci.yml: by reading the workflow text
(the dev extra has no YAML library, and none is needed for these
assertions).
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
ACTION_DIR = REPO_ROOT / ".github" / "actions" / "setup-sky130a-pdk"
ACTION = ACTION_DIR / "action.yml"

#: The workflows that provision the sky130A PDK, and how many provisioning
#: sites each carries. equiv-canary, place-and-route-smoke and
#: post-route-sta-live consume the standard-cell library (they synthesize,
#: place-and-route, or run STA), so they must request both library families;
#: the sim canaries need only the device models.
SC_HD_CONSUMERS = (
    "equiv-canary.yml",
    "place-and-route-smoke.yml",
    "post-route-sta-live.yml",
)
DEVICE_MODEL_SITES = {
    "design-agent-benchmark.yml": 3,
    "kb-drift-canary.yml": 1,
}

#: The using-step id every call site keeps, so `steps.pdk-cache.outputs.*`
#: resolves at the call site exactly as the inline triple's cache step did.
CALL_SITE_ID = "id: pdk-cache"

#: Both library families, as the std-cell consumers must request them. The
#: composite action passes each element of this string as a `-l` flag.
BOTH_LIBS = "libs: sky130_fd_pr sky130_fd_sc_hd"

#: The pinned open_pdks commit every site fetched before the composite action
#: (re-verified in the workflows at the time of #5658).
PINNED_COMMIT = "f6eeac7dad085ffcc829ccfd721f7b4ce39edcf7"


def _read(path: Path) -> str:
    return path.read_text()


def _call_sites(workflow: str) -> list[str]:
    """Each `uses:` step block of the composite action in one workflow.

    A block runs from its `- name:`/`- id:`/`- uses:` line to the next
    sibling step or end of file, mirroring test_artifact_determinism.py's
    block-scoped-assertion style.
    """
    blocks: list[str] = []
    for match in re.finditer(r"^(?=      - )", workflow, re.MULTILINE):
        blocks.append(workflow[match.start() :])
    return [
        block.split("\n      - ", 1)[0]
        for block in blocks
        if "./.github/actions/setup-sky130a-pdk" in block.split("\n      - ", 1)[0]
    ]


def test_composite_action_exists() -> None:
    assert ACTION.is_file(), f"missing {ACTION.relative_to(REPO_ROOT)}"


def test_action_cache_key_folds_in_the_library_slug() -> None:
    """The one assertion that makes the collision structurally impossible:
    the cache key must be derived from the library list, not a constant."""
    text = _read(ACTION)
    keys = [
        line.strip() for line in text.splitlines() if line.strip().startswith("key:")
    ]
    assert len(keys) == 1, f"expected exactly one cache key in the action, got {keys}"
    assert "inputs.pdk-commit" in keys[0] or PINNED_COMMIT in keys[0], (
        f"key does not carry the pinned commit: {keys[0]}"
    )
    assert "steps.libs-slug.outputs.slug" in keys[0], (
        f"key does not embed the library slug (issue #5658's fix): {keys[0]}"
    )
    # ...the pinned commit reaches the key through the input's default, so a
    # silent default drift would silently move every consumer's cache.
    commit_default = re.search(r"pdk-commit:.*?default:\s*(\S+)", text, re.DOTALL)
    assert commit_default is not None, "pdk-commit input has no default"
    assert commit_default.group(1) == PINNED_COMMIT, commit_default.group(1)
    # ...and the slug must actually be a function of the libs input.
    slug_step = re.search(
        r"id: libs-slug.*?run:.*?\n(.*?)(?=\n      - |\Z)", text, re.DOTALL
    )
    assert slug_step is not None, "action has no libs-slug step"
    assert "inputs.libs" in slug_step.group(1), (
        "libs-slug is not derived from the libs input"
    )


def test_action_defaults_to_the_device_models_only_library_list() -> None:
    text = _read(ACTION)
    default = re.search(r"^\s+default:\s*(\S+)", text, re.MULTILINE)
    assert default is not None, "libs input has no default"
    assert default.group(1) == "sky130_fd_pr", default.group(1)


def test_action_fetch_is_gated_on_cache_miss() -> None:
    """Preserves the inline triple's cache-miss gate. Composite-action steps
    cannot carry `timeout-minutes` (the runner rejects the manifest with
    "Unexpected value 'timeout-minutes'" -- observed live on the first
    equiv-canary dispatch), so the 20-minute fetch cap the triples carried
    lives on the `uses:` step at each call site instead (asserted below)."""
    text = _read(ACTION)
    fetch = re.search(r"- name: Fetch full sky130A PDK.*?(?=\Z)", text, re.DOTALL)
    assert fetch is not None, "action has no fetch step"
    body = fetch.group(0)
    assert "steps.pdk-cache.outputs.cache-hit != 'true'" in body, (
        "fetch step is not gated on a cache miss"
    )
    assert not re.search(r"^\s*timeout-minutes:", body, re.MULTILINE), (
        "composite steps must not carry timeout-minutes (runner rejects it)"
    )
    assert "ciel enable --pdk-family sky130" in body
    assert PINNED_COMMIT in body or "inputs.pdk-commit" in body


def test_every_call_site_carries_the_20_minute_cap() -> None:
    """The fetch cap the inline triples carried (`timeout-minutes: 20` on the
    fetch step) must survive at every call site."""
    expected: dict[str, int] = {**DEVICE_MODEL_SITES, **{k: 1 for k in SC_HD_CONSUMERS}}
    for name in expected:
        workflow = _read(WORKFLOWS / name)
        calls = _call_sites(workflow)
        assert calls, f"{name} has no setup-sky130a-pdk call site"
        for call in calls:
            assert re.search(r"^ +timeout-minutes: 20$", call, re.MULTILINE), (
                f"{name}: setup-sky130a-pdk call site lost the 20-minute cap"
            )


def test_action_has_no_restore_keys_fallback() -> None:
    """A restore-keys fallback would let a device-models-only entry satisfy a
    std-cell request and silently reintroduce the collision through the
    restore path (issue #5658). The inline triple had none either."""
    cache_step = re.search(
        r"- name: Cache full sky130A PDK.*?(?=\n      - |\Z)",
        _read(ACTION),
        re.DOTALL,
    )
    assert cache_step is not None
    # Match the mapping key, not prose (the step's comment says "do not add
    # restore-keys").
    assert not re.search(r"^\s*restore-keys:", cache_step.group(0), re.MULTILINE), (
        "cache step carries a restore-keys fallback"
    )


def test_no_workflow_defines_its_own_pdk_cache_key() -> None:
    """The composite action is the single source of the sky130A PDK cache
    key. A workflow that still carries an inline `sky130A-ciel` key is a
    site the migration missed -- the exact copy-paste drift #5658 fixes."""
    offenders: list[str] = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        if "sky130A-ciel" in _read(path):
            offenders.append(path.name)
    assert not offenders, (
        f"workflows still defining an inline sky130A PDK cache key: {offenders}"
    )


def test_every_pdk_provisioning_site_calls_the_composite_action() -> None:
    """Seven sites across five workflows, each keeping the pdk-cache step id."""
    expected: dict[str, int] = {**DEVICE_MODEL_SITES, **{k: 1 for k in SC_HD_CONSUMERS}}
    for name, count in expected.items():
        workflow = _read(WORKFLOWS / name)
        calls = _call_sites(workflow)
        assert len(calls) == count, (
            f"{name}: expected {count} setup-sky130a-pdk sites, found {len(calls)}"
        )
        for call in calls:
            assert CALL_SITE_ID in call, f"{name}: call site lost `{CALL_SITE_ID}`"


def test_std_cell_consumers_request_both_library_families() -> None:
    for name in SC_HD_CONSUMERS:
        workflow = _read(WORKFLOWS / name)
        calls = _call_sites(workflow)
        assert calls, f"{name} has no setup-sky130a-pdk call site"
        for call in calls:
            assert BOTH_LIBS in call, (
                f"{name}: std-cell consumer does not request both library "
                f"families ({BOTH_LIBS!r} missing)"
            )


def test_device_model_sites_stay_device_models_only() -> None:
    """The sim canaries must not silently grow a standard-cell dependency (a
    multi-GB payload change hiding in a `with:` would also change what their
    cache entry means for every other consumer of that key)."""
    for name in DEVICE_MODEL_SITES:
        workflow = _read(WORKFLOWS / name)
        calls = _call_sites(workflow)
        assert calls, f"{name} has no setup-sky130a-pdk call site"
        for call in calls:
            assert "sky130_fd_sc_hd" not in call, (
                f"{name}: device-models-only site now requests sky130_fd_sc_hd"
            )
