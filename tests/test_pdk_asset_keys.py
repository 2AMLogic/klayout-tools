"""The PDK asset-key universe is declared in two modules; pin them together.

``pdk._ASSET_LAYOUT`` is the path authority (probed installs),
``pdk_descriptor.ASSET_KEYS`` is the ordering authority (descriptor installs
and the text render order of ``klt pdk find``). Neither may gain or lose a
key alone.
"""

from klayout_tools import pdk, pdk_descriptor
from klayout_tools.cli import pdk_cmd


def test_asset_layout_keys_match_descriptor_asset_keys():
    assert set(pdk._ASSET_LAYOUT) == set(pdk_descriptor.ASSET_KEYS)


def test_find_text_renders_assets_in_asset_keys_order(capsys):
    report = {
        "pdk": "x",
        "variant": "v",
        "root": "/r",
        "version": None,
        "resolved_via": "test",
        "assets": {key: f"/r/{key}" for key in reversed(pdk_descriptor.ASSET_KEYS)},
    }
    pdk_cmd._print_find_text(report)
    lines = capsys.readouterr().out.splitlines()
    start = lines.index("assets:")
    rendered = [
        ln.split(":")[0].strip()
        for ln in lines[start + 1 : start + 1 + len(pdk_descriptor.ASSET_KEYS)]
    ]
    assert rendered == list(pdk_descriptor.ASSET_KEYS)
