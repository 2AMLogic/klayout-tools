"""SPICE card placement and cluster identity in the merged runset leg."""

from klayout_tools.pex_merged_leg import merge_parasitics


def test_merge_writer_preserves_pin_order_and_untouched_nets(tmp_path):
    child = ".SUBCKT CHILD \\$1 A vsubs\nXsub \\$1 A vsubs nmos\n.ENDS CHILD\n"
    other = ".subckt OTHER \\$1\nXo \\$1 G nmos\n.ends\n"
    leg = tmp_path / "leg.spice"
    leg.write_text(
        "* extracted by klt extract --engine klayout-runset --deck-file p.lvs\n"
        "* no parasitics: this engine extracts devices and connectivity only\n"
        + child
        + "* cell TOP\n"
        ".SUBCKT TOP \\$1 \\$2 keepme\n"
        "X$1 \\$1 \\$2 keepme nmos\n"
        "Xsub \\$1\n+ keepme \\$1 p=1\n"
        ".ENDS TOP\n" + other
    )
    cards = merge_parasitics(
        str(leg),
        top="TOP",
        substrate="vsubs",
        curated_nets=[{"name": "A", "net_id": 1}],
        all_runset_nets=["\\$1", "\\$2", "keepme"],
        parasitics={
            "nets": [
                {
                    "net": "A",
                    "net_id": 1,
                    "resistance_ohm": 0.5,
                    "capacitance_ff": 2.0,
                    "coupled": [],
                },
                {  # unmatched curated net: its card is dropped, not guessed
                    "net": "B",
                    "net_id": 2,
                    "resistance_ohm": 9.0,
                    "capacitance_ff": 9.0,
                    "coupled": [],
                },
            ],
            "substrate_dc_tie": {
                "resistance_ohm": 1e12,
                "nets": [{"net": "vsubs", "device": "Rvsubs_dctie"}],
            },
        },
        deck_name="sg13cmos5l",
        report_block={
            "matched": [{"runset": "\\$1", "curated": "A", "cluster_id": 1}],
            "unmatched_runset_nets": ["\\$2"],
            "disagreements": [],
        },
    )
    text = leg.read_text()
    assert child in text and other in text
    assert text.index(child) < text.index("RA_plr") < text.index(".ENDS TOP")
    assert cards == 3  # R + C + the substrate tie; B contributed none
    assert ".SUBCKT TOP A \\$2 keepme\n" in text  # renamed in place, order kept
    assert "X$1 A \\$2 keepme nmos" in text
    assert "Xsub A\n+ keepme \\$1 p=1\n" in text
    assert "RA_plr A A__kltph 0.5" in text
    assert "CA_plc A__kltph vsubs_1 2e-15" in text
    assert "Rvsubs_1_dctie vsubs_1 0 1e+12" in text
    assert ".GLOBAL vsubs_1\n" in text
    assert not any(
        line.startswith(("R", "C")) and " 9" in line for line in text.splitlines()
    )


def test_writer_uses_cluster_rc_and_only_unambiguous_couplings(tmp_path):
    leg = tmp_path / "leg.spice"
    leg.write_text(".subckt TOP body left right a b\n.ends\n")
    matches = [
        ("right", "Y", 12),
        ("left", "Y", 11),
        ("body", "vsubs", 13),
        ("a", "A", 14),
        ("b", "B", 15),
    ]
    corr = {
        "matched": [
            {"runset": node, "curated": label, "cluster_id": i}
            for node, label, i in matches
        ],
        "unmatched_runset_nets": [],
        "disagreements": [],
    }
    nets = [
        {
            "net": label,
            "net_id": i,
            "resistance_ohm": i,
            "capacitance_ff": 10 * i,
            "coupled": [{"net": "B", "capacitance_ff": 5}]
            if label in ("A", "Y")
            else [],
        }
        for _, label, i in reversed(matches)
        if label != "vsubs"
    ]
    count = merge_parasitics(
        str(leg),
        top="TOP",
        all_runset_nets=[m[0] for m in matches],
        curated_nets=[{"name": label, "net_id": i} for _, label, i in matches],
        substrate="vsubs",
        deck_name="fixture",
        report_block=corr,
        parasitics={
            "nets": nets,
            "substrate_dc_tie": {"nets": [{"net": "vsubs"}], "resistance_ohm": 1e12},
        },
    )
    text = leg.read_text()
    assert count == 10  # four R/C pairs, one coupling and one substrate tie
    assert "Rleft_plr left left__kltph 11\n" in text
    assert "Rright_plr right right__kltph 12\n" in text
    assert "Cleft_plc left__kltph body 1.1e-13\n" in text
    assert "Cright_plc right__kltph body 1.2e-13\n" in text
    assert "Ca_b_plcc a__kltph b__kltph 5e-15\n" in text
    assert corr["omitted_couplings"] == [
        {"curated_nets": ["B", "Y"], "reason": "ambiguous_label"}
    ]
    assert corr["substrate_return"]["runset"] == "body"
    assert "Rbody_dctie body 0 1e+12" in text
    assert ".GLOBAL" not in text
