"""Liberty unit normalisation in ``klt characterize --compare-to`` (#5063).

A ps/fF reference compared against our ns/pF file must give the same verdict
as the same data declared in ns/pF; missing or unparseable unit declarations
raise rather than default. Synthetic libraries only -- no vendor files.
"""

from __future__ import annotations

import re

import pytest

from klayout_tools import liberty_compare
from test_characterize_vendor_comparison import _inverter_lib, _write

# --------------------------------------------------------------------------- #
# Unit normalisation
# --------------------------------------------------------------------------- #


def _rescale(text: str, t: float, c: float, units: str) -> str:
    import re

    out = []
    kind = ""
    for line in text.splitlines():
        m = re.match(r"\s+(\w+) \(tmpl\)", line)
        if m:
            kind = m.group(1)
        m = re.match(r'(\s+)(index_[12]) \("([^"]*)"\);', line)
        if m:
            k = t if m.group(2) == "index_1" else c
            nums = ", ".join(f"{float(v) * k:g}" for v in m.group(3).split(","))
            line = f'{m.group(1)}{m.group(2)} ("{nums}");'
        elif line.strip().startswith('"') and kind:
            k = c if kind.endswith("_power") else t
            body = line.strip().rstrip("\\").rstrip().rstrip(",")
            nums = ", ".join(f"{float(v) * k:g}" for v in body.strip('"').split(","))
            tail = ", \\" if line.rstrip().endswith(", \\") else " \\"
            line = f'"{nums}"{tail}'
        out.append(line)
    text = "\n".join(out) + "\n"
    head, rest = text.split("  cell (inv)", 1)
    lines = [ln for ln in head.splitlines() if "_unit" not in ln]
    return "\n".join(lines) + "\n" + units + "  cell (inv)" + rest


def test_ps_ff_library_compares_like_ns_pf(tmp_path):
    ours = _write(tmp_path, "ours.lib", _inverter_lib())
    ref_text = _rescale(
        _inverter_lib(),
        1000.0,
        1000.0,
        '  time_unit : "1ps";\n  capacitive_load_unit (1, ff);\n'
        '  voltage_unit : "1V";\n  leakage_power_unit : "1pW";\n',
    )
    ref = _write(tmp_path, "ref.lib", ref_text)
    result = liberty_compare.compare_libraries(ours, ref)
    assert result["summary"]["within_tolerance"], result["summary"]
    for entry in result["summary"]["fields"].values():
        assert entry["violations"] == 0
        assert entry["max_abs_delta"] < 1e-9
    assert result["units"]["reference"]["time_unit"] == "1ps"
    assert result["units"]["ours"]["time_unit"] == "1ns"


def test_non_unit_scale_cap_and_time_units_convert(tmp_path):
    ours = _write(tmp_path, "ours.lib", _inverter_lib())
    # (0.1, pf) = 100 fF; "10ps" = 0.01 ns.
    ref_text = _rescale(
        _inverter_lib(),
        100.0,
        10.0,
        '  time_unit : "10ps";\n  capacitive_load_unit (0.1, pf);\n'
        '  voltage_unit : "1V";\n  leakage_power_unit : "1pW";\n',
    )
    ref = _write(tmp_path, "ref.lib", ref_text)
    result = liberty_compare.compare_libraries(ours, ref)
    assert result["summary"]["within_tolerance"], result["summary"]


def test_leakage_and_voltage_units_convert(tmp_path):
    ours = _write(tmp_path, "ours.lib", _inverter_lib())
    ref_text = _inverter_lib(leakage=(0.04, 0.08)).replace('"1pW"', '"1nW"')
    ref = _write(tmp_path, "ref.lib", ref_text)
    result = liberty_compare.compare_libraries(ours, ref)
    assert result["summary"]["fields"]["leakage_power"]["violations"] == 0
    assert result["summary"]["fields"]["cell_leakage_power"]["violations"] == 0
    # 2 V: energy unit is voltage^2 x cap = 4 pJ per table unit.
    ref2 = _write(
        tmp_path,
        "ref2.lib",
        _inverter_lib(power_scale=0.25).replace('"1V"', '"2V"'),
    )
    energy = liberty_compare.load_cells(ref2)["inv"].power[("Y", "A")]["rise_power"]
    assert energy.values[0][0] == pytest.approx(0.002 * 0.25 * 4)


@pytest.mark.parametrize(
    "attribute",
    ["time_unit", "capacitive_load_unit", "voltage_unit", "leakage_power_unit"],
)
def test_missing_unit_attribute_raises(tmp_path, attribute):
    text = re.sub(rf"{attribute}[^;]*;", "", _inverter_lib())
    ref = _write(tmp_path, "ref.lib", text)
    ours = _write(tmp_path, "ours.lib", _inverter_lib())
    with pytest.raises(liberty_compare.LibertyCompareError, match=attribute) as exc:
        liberty_compare.compare_libraries(ours, ref)
    assert "ref.lib" in str(exc.value)


def test_unparseable_unit_raises(tmp_path):
    ref = _write(tmp_path, "ref.lib", _inverter_lib().replace('"1ns"', '"fast"'))
    with pytest.raises(liberty_compare.LibertyCompareError, match="time_unit"):
        liberty_compare.load_cells(ref)
