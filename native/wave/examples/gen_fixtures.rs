//! Regenerates the committed test fixtures under `tests/fixtures/`.
//!
//! Not part of the crate's own build; run on demand with
//! `cargo run --example gen_fixtures` from `native/wave/` whenever the
//! fixture timeline needs to change. The two fixtures share the same
//! `tb.clk`/`tb.rst_n` timeline (clock period 10 ticks, first rising edge
//! at tick 5, reset deasserts at tick 22 -- so cycle 0 anchors at tick 25,
//! the first clock rising edge at or after reset release) and differ only
//! in when `tb.dut.o_valid` first asserts, so `tests/query_ops.rs`'s `diff`
//! op test has a real second trace to diverge against.
//!
//! See `tests/fixtures/README.md` for the full timeline table this file
//! implements.

use fst_writer::{
    open_fst, FstFileType, FstInfo, FstScopeType, FstSignalType, FstVarDirection, FstVarType,
};

fn build(path: &str, o_valid_first_assert: u64) {
    let info = FstInfo {
        start_time: 0,
        timescale_exponent: -9, // 1ns
        version: "klt-wave-native fixture generator 0.1.0".to_string(),
        date: "2026-09-09".to_string(),
        file_type: FstFileType::Verilog,
    };
    let mut header = open_fst(path, &info).unwrap();
    header.scope("tb", "tb", FstScopeType::Module).unwrap();
    let clk = header
        .var(
            "clk",
            FstSignalType::bit_vec(1),
            FstVarType::Reg,
            FstVarDirection::Implicit,
            None,
        )
        .unwrap();
    let rst_n = header
        .var(
            "rst_n",
            FstSignalType::bit_vec(1),
            FstVarType::Reg,
            FstVarDirection::Implicit,
            None,
        )
        .unwrap();
    header.scope("dut", "dut", FstScopeType::Module).unwrap();
    let o_valid = header
        .var(
            "o_valid",
            FstSignalType::bit_vec(1),
            FstVarType::Reg,
            FstVarDirection::Implicit,
            None,
        )
        .unwrap();
    let data = header
        .var(
            "data",
            FstSignalType::bit_vec(8),
            FstVarType::Reg,
            FstVarDirection::Implicit,
            None,
        )
        .unwrap();
    header.up_scope().unwrap(); // dut
    header.up_scope().unwrap(); // tb

    let mut body = header.finish().unwrap();

    // t=0: initial values, reset asserted, clock low.
    body.signal_change(clk, b"0").unwrap();
    body.signal_change(rst_n, b"0").unwrap();
    body.signal_change(o_valid, b"0").unwrap();
    body.signal_change(data, b"00000000").unwrap();

    // Clock: toggles every 5 ticks (period 10) through t=100.
    // Reset: deasserts at t=22 (between clock edges).
    // o_valid: asserts at `o_valid_first_assert`, deasserts at +20, then
    // reasserts at +40 and holds through the end of the trace.
    // data: increments by one every 20 ticks starting at t=35.
    for t in 1..=100u64 {
        body.time_change(t).unwrap();
        if t % 5 == 0 {
            let level = if (t / 5) % 2 == 0 { b"0" } else { b"1" };
            body.signal_change(clk, level).unwrap();
        }
        if t == 22 {
            body.signal_change(rst_n, b"1").unwrap();
        }
        if t == o_valid_first_assert {
            body.signal_change(o_valid, b"1").unwrap();
        } else if t == o_valid_first_assert + 20 {
            body.signal_change(o_valid, b"0").unwrap();
        } else if t == o_valid_first_assert + 40 {
            body.signal_change(o_valid, b"1").unwrap();
        }
        if t == 35 {
            body.signal_change(data, b"00000001").unwrap();
        } else if t == 55 {
            body.signal_change(data, b"00000010").unwrap();
        } else if t == 75 {
            body.signal_change(data, b"00000011").unwrap();
        }
    }

    body.finish().unwrap();
}

fn main() {
    build("tests/fixtures/gcd_like_a.fst", 45);
    build("tests/fixtures/gcd_like_b.fst", 65);
    println!("wrote tests/fixtures/gcd_like_a.fst and tests/fixtures/gcd_like_b.fst");
}
