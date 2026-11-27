# SG13G2 mirrored inductor terminal ordering

A label-ordered comparison accepted the correctly mirrored spiral and also accepted a physically unmirrored spiral whose two winding labels were exchanged. Treating the texts only as device markers preserved independently named VDD and OUT nets, but lost the names of the original, correctly wired LA/LB ports. Neither candidate supplies sufficient evidence by itself to grade signoff item 4 or 11 `met`. [Measurement issue](https://github.com/2AMLogic/klayout-tools/issues/6914) · [Evidence tiers](../design-evidence-tiers.md)

## Measurements

KLayout 0.30.12 ran the SG13G2 v0.3.0 runset in strict top-port mode on macOS arm64. The fixture contains one two-terminal inductor with a substrate connection. The four run-mode columns count strict top-port errors in the `.lvsdb`. The extracted-inductor census agreed in all four modes. The unresolved column counts extracted inductors whose labels the ordering step could not assign. Report and apply gave the same count. Only these two modes run the ordering step. The cross-reference column gives klt’s result from the database before applying those errors. It was identical in all four runs for each row. The KLayout process returned zero even on the crossed-port controls. [Fixture definitions](../../tests/fixtures/runset_inductor/README.md) · [Pinned deck](https://github.com/IHP-GmbH/IHP-Open-PDK/blob/5cccb161f7492697cfa52eb14dc03beb00bdca9e/ihp-sg13g2/libs.tech/klayout/tech/lvs/sg13g2.lvs)

| Layout and reference | Cross-reference | Extracted inductors | Unresolved | Shipped | Report only | Label order | Markers only |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| As drawn, correct reference | match | 1 | 0 | 0 | 0 | 0 | 2 |
| Mirrored, correct reference | match | 1 | 0 | 2 | 2 | 0 | 2 |
| As drawn, reversed reference winding | match | 1 | 0 | 2 | 2 | 2 | 2 |
| Both labels moved off their pins | inconclusive | 0 | 0 | 0 | 0 | 0 | 0 |
| As drawn, LA and LB exchanged on their pins | match | 1 | 0 | 2 | 2 | 0 | 2 |
| Mirrored, reversed reference winding | match | 1 | 0 | 0 | 0 | 2 | 2 |
| Independent VDD and OUT pin names | match | 1 | 0 | 2 | 2 | 2 | 0 |
| Independent pin names, reversed reference winding | match | 1 | 0 | 2 | 2 | 2 | 2 |

**Label ordering.** The pinned VCO step reads the label falling inside each extracted terminal shape. It swaps the winding nets before comparison when LA is on terminal 2. The report-only wrapper reproduced all eight shipped results. Applying it swapped exactly one device on each mirrored layout and on the swapped-label layout. It preserved the as-drawn result and still rejected reversed wiring and off-pin texts. However, correctly mirrored geometry and exchanged terminal labels gave it the same local evidence. Both became clean. On the off-pin layout the step reported zero unresolved devices because it extracted zero inductors. The other seven comparisons each extracted one inductor and reported zero unresolved devices. [Measured step](https://github.com/2AMLogic/sg13g2-vco/blob/612eccac9129c9a6b806ebad225c4a0fd44ef8cd/layout/scripts/lvs_label_order.rb)

**Marker-only extraction.** A disposable copy of the PDK omitted the single connection `connect(ind_pin, ind_text)`. Its port geometry still came from texts on layer 27/25, and the original strict check still ran. With independent metal labels, the as-shipped names `LA,VDD` and `LB,OUT` became `VDD` and `OUT`. The reversed reference still produced both errors. On the ordinary correct fixture, the same edit left anonymous layout nets `$2` and `$3` opposite schematic pins LA and LB. Using this candidate for all SG13G2 inductors would reject that known-good case. The edited include had a different digest despite the unchanged top-level deck. [Port derivation](https://github.com/IHP-GmbH/IHP-Open-PDK/blob/5cccb161f7492697cfa52eb14dc03beb00bdca9e/ihp-sg13g2/libs.tech/klayout/tech/lvs/rule_decks/ind_derivations.lvs) · [Pin connection](https://github.com/IHP-GmbH/IHP-Open-PDK/blob/5cccb161f7492697cfa52eb14dc03beb00bdca9e/ihp-sg13g2/libs.tech/klayout/tech/lvs/rule_decks/ind_connections.lvs#L28)

## Grading and removal

Use either modified run as supplementary diagnostic evidence. The operator can retain the shipped-runset requirement or commission a narrower evidence rule for layouts that independently establish winding identity and name both external nets. Such a rule would need to discriminate the swapped-label control and preserve the correct as-drawn case. A future comparison wrapper would also need to record the PDK revision, step and modified-include digests, strict-port findings and recognized-device counts. Re-run this matrix on the next proposed upstream pin. Retire a workaround once the unmodified pin accepts the correct controls and rejects the corresponding faulty controls. The proposal changes no signoff criterion. [Grading decision](https://github.com/2AMLogic/klayout-tools/issues/6914)

## Reproduce the comparison

Run from the klayout-tools checkout with its Python environment active and a `klayout` 0.30.12 executable on `PATH`. These commands fetch only the LVS directory from the pinned PDK checkout, verify both input digests and write the modified include and databases under a temporary directory.

```sh
set -eu
fixtures="$(pwd -P)/tests/fixtures/runset_inductor"
scratch=$(mktemp -d "${TMPDIR:-/tmp}/klt-inductor.XXXXXXXX")
pdk="$scratch/pdk"
git init -q "$pdk"
git -C "$pdk" remote add origin https://github.com/IHP-GmbH/IHP-Open-PDK.git
git -C "$pdk" sparse-checkout set ihp-sg13g2/libs.tech/klayout/tech/lvs
git -C "$pdk" fetch -q --depth=1 --filter=blob:none origin 5cccb161f7492697cfa52eb14dc03beb00bdca9e
git -C "$pdk" checkout -q --detach FETCH_HEAD
deck="$pdk/ihp-sg13g2/libs.tech/klayout/tech/lvs/sg13g2.lvs"
step="$scratch/lvs_label_order.rb"
curl -fsSL https://raw.githubusercontent.com/2AMLogic/sg13g2-vco/612eccac9129c9a6b806ebad225c4a0fd44ef8cd/layout/scripts/lvs_label_order.rb -o "$step"
printf '%s  %s\n' e01bcb1ec5858e8069bade8f7fd7ec28c1026fc2fd0590e8f611d3398a5bbbd7 "$deck" eaae5d1267265dfbc006b86ba765f3adf6eaf685a5a153807644f63eb6e26a8f "$step" | shasum -a 256 -c -
printf '# %%include %s\n# %%include %s\n' "$step" "$deck" > "$scratch/wrapper.lvs"
cp -R "$(dirname "$deck")" "$scratch/markers"
inc="$scratch/markers/rule_decks/ind_connections.lvs"
sed '/^connect(ind_pin, ind_text)$/d' "$inc" > "$inc.new"
mv "$inc.new" "$inc"
printf '%s  %s\n' a6e44b79730c8e99ec60a1078774cc55db5c2236e97bd6c5658675490fd97730 "$inc" | shasum -a 256 -c -
sed 's/^L1 VDD OUT /L1 OUT VDD /' "$fixtures/ind2_pinnet.cdl" > "$scratch/pin-reverse.cdl"

run_case() {
    name=$1 layout=$2 ref=$3
    for mode in plain report apply markers; do
        out="$scratch/$name-$mode"
        mkdir -p "$out"
        selected="$deck"
        set --
        case "$mode" in
            report|apply) selected="$scratch/wrapper.lvs"
                set -- -rd "label_order_mode=$mode" -rd "label_order_report=$out/order.json" ;;
            markers) selected="$scratch/markers/sg13g2.lvs" ;;
        esac
        (cd "$out" && klayout -b -r "$selected" -rd "input=$fixtures/$layout.gds" \
            -rd "schematic=$ref" -rd "report=$out/result.lvsdb" -rd top=IND2 -rd topcell=IND2 \
            "$@" > run.log 2>&1)
        python - "$out" "$ref" <<'PY'
import json, sys
from pathlib import Path
from klayout_tools.lvs_runset import read_lvsdb_verdict
from klayout_tools.lvs_runset_ports import strict_port_findings
out, reference = map(Path, sys.argv[1:])
db = out / "result.lvsdb"
v = read_lvsdb_verdict(str(db), str(reference), sg13g2=True)
errors = [e["message"] for e in strict_port_findings(db.read_text())]
step = out / "order.json"
result = {
    "case": out.name, "status": v["status"], "strict_port_errors": errors,
    "extracted_inductors": v["device_census"]["layout"].get("inductor", 0),
}
if step.exists():
    order = json.loads(step.read_text())
    result["label_order"] = {k: order[k] for k in ("inductor_devices", "unresolved", "reordered")}
print(json.dumps(result))
PY
    done
}
run_case drawn ind2_asdrawn "$fixtures/ind2.cdl"
run_case mirror ind2_mirrored "$fixtures/ind2.cdl"
run_case wired-reversed ind2_asdrawn "$fixtures/ind2_reversed.cdl"
run_case wrong-net ind2_wrongnet_labels "$fixtures/ind2.cdl"
run_case swapped ind2_swapped_labels "$fixtures/ind2.cdl"
run_case mirror-reverse ind2_mirrored "$fixtures/ind2_reversed.cdl"
run_case pin-net ind2_pinnet "$fixtures/ind2_pinnet.cdl"
run_case pin-net-reverse ind2_pinnet "$scratch/pin-reverse.cdl"
```
