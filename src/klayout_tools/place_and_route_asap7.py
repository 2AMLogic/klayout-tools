"""Powered-route inputs for the two installed names of the pinned ASAP7 RVT cells."""

RVT_LIBRARIES = ("asap7sc7p5t_rvt", "asap7sc7p5t_28_R")

# lambdapdk v0.2.17, asap7/libs/asap7sc7p5t_rvt: the LEF supplies cell
# masters, and apr/openroad/{global_connect,tapcells}.tcl supplies pins and
# tap spacing. The clock buffer and RC arguments match the captured powered
# route in tests/fixtures/place_and_route_drcfill/request.json.
RVT_LIBRARY_DEFAULTS = {
    "cts_buffer": "BUFx4_ASAP7_75t_R",
    "power_pins": [
        {"net_role": "power", "pattern": "VDD", "primary": True},
        {"net_role": "ground", "pattern": "VSS", "primary": True},
    ],
    "wire_rc": {"resistance_ohm_per_um": 4.32e-05, "capacitance_pf_per_um": 0.00016579},
    "route_layers": {"low": "M2", "high": "M7"},
    "antenna_diode": None,
    "tapcell": {
        "master": "TAPCELL_ASAP7_75t_R",
        "endcap": "TAPCELL_ASAP7_75t_R",
        "distance_um": 25,
    },
    "filler": ["FILLERxp5_ASAP7_75t_R", "FILLER_ASAP7_75t_R"],
}
