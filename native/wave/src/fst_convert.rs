// Adapted from boldaxolotl/booley, crates/bwave/src/fst.rs
// (commit 0c3b4cd7b66a0f793de605ce58e82dc1bc0ac892,
// https://github.com/boldaxolotl/booley), specifically the small,
// write-side helper functions `timescale_to_exponent`,
// `timescale_str_from_exponent`, `var_type_of`,
// `canon_bits_into`/`canon_bits_append`, and `transition_scopes` that
// `FstBuildHandler` (also in `fst.rs`) uses to convert a parsed VCD into
// an FST file.
// Copyright (c) the boldaxolotl/booley contributors.
// Licensed under the Apache License, Version 2.0; see /NOTICE.
//
// Modifications from upstream: `transition_scopes` is ported unchanged
// (it already only calls stock `fst_writer::FstHeaderWriter`'s public
// `scope`/`up_scope` methods -- no dependency on booley's vendored,
// modified `fst-writer` fork). `timescale_to_exponent`/`var_type_of`/
// `canon_bits_into` are ported unchanged as free functions.
// `timescale_from_exponent` adapts `timescale_str_from_exponent` to
// return a split `(unit, value)` pair instead of a formatted string (see
// its own doc comment below). NOT ported:
// `fst.rs`'s `ColumnCache` FST *read* path, its `FstBuildHandler` struct
// itself (which drives these helpers via booley's vendored parallel
// `FstSectionEncoder`/`OrderedFstWriter` API -- not available from the
// stock crates.io `fst-writer` this crate depends on, decision record
// section 11), and its dense single/two/three-character VCD-id lookup
// tables (`SignalSchema`, `dense_vcd_id_index` -- a hot-path optimization
// this crate's `build.rs` does not need at first landing; it uses a plain
// `HashMap<String, u32>` VCD-id -> FST-signal-group lookup instead, see
// that module). The read-side `ColumnCache`/`cache.rs` query engine is
// issue #1599's (Phase 2a's) job, not this one's.

//! Small FST-conversion helpers shared by `build.rs`.

/// Parse a VCD `$timescale` string (e.g. "1ns", "10ps") into the FST
/// format's signed power-of-ten exponent (e.g. -9 for ns, -10 for 10ns).
pub fn timescale_to_exponent(ts: &str) -> i8 {
    let ts = ts.trim();
    let digits: String = ts.chars().take_while(|c| c.is_ascii_digit()).collect();
    let unit = ts[digits.len()..].trim();
    let factor_exp: i8 = match digits.as_str() {
        "1" | "" => 0,
        "10" => 1,
        "100" => 2,
        _ => 0,
    };
    let unit_exp: i8 = match unit {
        "s" => 0,
        "ms" => -3,
        "us" => -6,
        "ns" => -9,
        "ps" => -12,
        "fs" => -15,
        _ => -9,
    };
    unit_exp + factor_exp
}

/// Map a VCD `$var` type keyword (e.g. "reg", "wire", "real") to the FST
/// format's own `FstVarType` enum. Unrecognised keywords fall back to
/// `Wire` -- matching upstream's own fallback.
pub fn var_type_of(vt: &str) -> fst_writer::FstVarType {
    use fst_writer::FstVarType as W;
    match vt {
        "reg" => W::Reg,
        "integer" => W::Integer,
        "parameter" => W::Parameter,
        "real" => W::Real,
        "realtime" => W::RealTime,
        "time" => W::Time,
        "logic" => W::Logic,
        "bit" => W::Bit,
        "supply0" => W::Supply0,
        "supply1" => W::Supply1,
        "tri" => W::Tri,
        "triand" => W::TriAnd,
        "trior" => W::TriOr,
        "trireg" => W::TriReg,
        "tri0" => W::Tri0,
        "tri1" => W::Tri1,
        "wand" => W::Wand,
        "wor" => W::Wor,
        "event" => W::Event,
        "port" => W::Port,
        "int" => W::Int,
        _ => W::Wire,
    }
}

/// Canonicalize a VCD bit-vector value to exactly `width` lowercase chars
/// in `out` (fst-writer panics on over-wide values and self-extends short
/// ones, but VCD extension rules for x/z need to be ours). Returns `true`
/// when the value arrived wider than the declared width (a simulator
/// dialect quirk; least-significant bits are kept, matching GTKWave).
pub fn canon_bits_into(bits: &[u8], width: usize, out: &mut Vec<u8>) -> bool {
    out.clear();
    let len = bits.len();
    if len < width {
        let fill = match bits.first().copied().unwrap_or(b'0').to_ascii_lowercase() {
            b'x' => b'x',
            b'z' => b'z',
            _ => b'0',
        };
        out.resize(width - len, fill);
    }
    let src = if len > width {
        &bits[len - width..]
    } else {
        bits
    };
    out.extend(src.iter().map(|b| b.to_ascii_lowercase()));
    len > width
}

/// Decompose an FST timescale exponent (e.g. -9 for "1ns") into the
/// `{unit, value}` pair `docs/design/waveform-query-contract-spike.md`
/// section 4's `timescale` response field contracts. Ported from
/// upstream `fst.rs::timescale_str_from_exponent`, adapted to return the
/// split `(unit, value)` pair this crate's JSON response needs instead of
/// a formatted `"1ns"`-style string.
pub fn timescale_from_exponent(exp: i8) -> (&'static str, i64) {
    const UNITS: [(i8, &str); 6] = [
        (0, "s"),
        (-3, "ms"),
        (-6, "us"),
        (-9, "ns"),
        (-12, "ps"),
        (-15, "fs"),
    ];
    for (unit_exp, name) in UNITS {
        let delta = exp as i32 - unit_exp as i32;
        if (0..=2).contains(&delta) {
            return (name, 10i64.pow(delta as u32));
        }
    }
    ("ns", 1)
}

/// Emit scope transitions between two hierarchical paths: pop to the
/// common prefix, then push the new scopes. Preserving VCD declaration
/// order means a re-entered scope becomes a duplicate FST scope entry --
/// exactly what GTKWave's `vcd2fst` produces.
pub fn transition_scopes<W: std::io::Write + std::io::Seek>(
    hw: &mut fst_writer::FstHeaderWriter<W>,
    current: &mut Vec<String>,
    target: &[&str],
) -> Result<(), String> {
    let common = current
        .iter()
        .zip(target.iter())
        .take_while(|(a, b)| a.as_str() == **b)
        .count();
    while current.len() > common {
        hw.up_scope().map_err(|e| format!("fst up_scope: {e}"))?;
        current.pop();
    }
    for name in &target[common..] {
        hw.scope(*name, "", fst_writer::FstScopeType::Module)
            .map_err(|e| format!("fst scope '{name}': {e}"))?;
        current.push((*name).to_string());
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn timescale_exponent_ns() {
        assert_eq!(timescale_to_exponent("1ns"), -9);
        assert_eq!(timescale_to_exponent("10ns"), -8);
        assert_eq!(timescale_to_exponent("1ps"), -12);
        assert_eq!(timescale_to_exponent("100ps"), -10);
        assert_eq!(timescale_to_exponent("1s"), 0);
    }

    #[test]
    fn canon_bits_pads_short_values() {
        let mut out = Vec::new();
        let overwide = canon_bits_into(b"1", 4, &mut out);
        assert_eq!(&out, b"0001");
        assert!(!overwide);
    }

    #[test]
    fn canon_bits_pads_x_and_z() {
        let mut out = Vec::new();
        canon_bits_into(b"x", 4, &mut out);
        assert_eq!(&out, b"xxxx");
        canon_bits_into(b"z", 4, &mut out);
        assert_eq!(&out, b"zzzz");
    }

    #[test]
    fn timescale_from_exponent_roundtrip() {
        assert_eq!(timescale_from_exponent(-9), ("ns", 1));
        assert_eq!(timescale_from_exponent(-8), ("ns", 10));
        assert_eq!(timescale_from_exponent(-7), ("ns", 100));
        assert_eq!(timescale_from_exponent(-12), ("ps", 1));
        assert_eq!(timescale_from_exponent(0), ("s", 1));
    }

    #[test]
    fn canon_bits_truncates_overwide_values() {
        let mut out = Vec::new();
        let overwide = canon_bits_into(b"101010", 4, &mut out);
        assert_eq!(&out, b"1010");
        assert!(overwide);
    }
}
