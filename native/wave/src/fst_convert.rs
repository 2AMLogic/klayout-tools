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
// its own doc comment below). `var_type_str` (the inverse of
// `var_type_of`) moved here from this crate's `fst.rs` read path -- see
// that file's header for its provenance -- so the FST build and read
// paths share one type table; `ticks_to_ns_from_exponent` is original to
// this crate. NOT ported:
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

//! Small FST-conversion helpers shared by the build path (`build.rs`,
//! `build_fst.rs`, `store_writer.rs`) and the FST read path (`fst.rs`).

/// The FST producer string identifies stores that reserve the writer's canonical
/// NaN for unwritten real slots and use a different quiet NaN for explicit writes.
pub(crate) const REAL_NAN_ENCODING: &str = " (explicit NaN v1)";
pub(crate) const EXPLICIT_NAN: f64 = f64::from_bits(0x7ff8_0000_0000_0001);

pub(crate) struct RealFrameFill {
    tick: u64,
    bits: u64,
}

impl RealFrameFill {
    pub(crate) fn from_header(header: &fst_reader::FstHeader) -> Self {
        let bits = if header.version.starts_with("klt-wave ")
            && header.version.ends_with(REAL_NAN_ENCODING)
        {
            f64::NAN.to_bits()
        } else {
            // fst-writer 0.3.1 initialized real slots with eight ASCII 'x' bytes.
            u64::from_le_bytes([b'x'; 8])
        };
        Self {
            tick: header.start_time,
            bits,
        }
    }

    pub(crate) fn matches(&self, tick: u64, value: f64) -> bool {
        tick == self.tick && value.to_bits() == self.bits
    }
}

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

/// Inverse of `var_type_of`: map the FST format's own `FstVarType` back to
/// the VCD `$var` type keyword. Shared by the FST ingestion frontend
/// (`build_fst.rs` -- `StoreWriter::create` maps the keyword back through
/// `var_type_of`, so VCD and FST inputs produce one `DeclSignal` shape) and
/// by the FST reader (`fst.rs`), so a type cannot map one way on build and
/// another way on query. `RealParameter` and `ShortReal` have no keyword of
/// their own and map to `"real"` -- never to the `"wire"` fallback, which
/// would re-read a real signal as a bit vector.
pub fn var_type_str(tpe: fst_reader::FstVarType) -> &'static str {
    use fst_reader::FstVarType as R;
    match tpe {
        R::Wire => "wire",
        R::Reg => "reg",
        R::Integer => "integer",
        R::Parameter => "parameter",
        R::Real | R::RealParameter | R::ShortReal => "real",
        R::RealTime => "realtime",
        R::Time => "time",
        R::Logic => "logic",
        R::Bit => "bit",
        R::Supply0 => "supply0",
        R::Supply1 => "supply1",
        R::Tri => "tri",
        R::TriAnd => "triand",
        R::TriOr => "trior",
        R::TriReg => "trireg",
        R::Tri0 => "tri0",
        R::Tri1 => "tri1",
        R::Wand => "wand",
        R::Wor => "wor",
        R::Event => "event",
        R::Port => "port",
        R::Int => "int",
        _ => "wire",
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

/// Nanoseconds per tick for an FST timescale exponent: -9 ("1ns") is
/// `1.0`, -10 ("100ps") is `0.1`, 0 ("1s") is `1e9`.
pub fn ticks_to_ns_from_exponent(exp: i8) -> f64 {
    10f64.powi(exp as i32 + 9)
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
    fn var_type_str_keeps_real_variants_real() {
        use fst_reader::FstVarType as R;
        use fst_writer::FstVarType as W;

        for tpe in [R::Real, R::RealParameter, R::ShortReal] {
            assert_eq!(var_type_str(tpe), "real", "{tpe:?}");
            assert_eq!(var_type_of(var_type_str(tpe)), W::Real, "{tpe:?}");
        }
    }

    #[test]
    fn var_type_str_round_trips_through_var_type_of() {
        use fst_reader::FstVarType as R;
        use fst_writer::FstVarType as W;

        let cases = [
            (R::Event, "event", W::Event),
            (R::Integer, "integer", W::Integer),
            (R::Parameter, "parameter", W::Parameter),
            (R::Real, "real", W::Real),
            (R::RealParameter, "real", W::Real),
            (R::Reg, "reg", W::Reg),
            (R::Supply0, "supply0", W::Supply0),
            (R::Supply1, "supply1", W::Supply1),
            (R::Time, "time", W::Time),
            (R::Tri, "tri", W::Tri),
            (R::TriAnd, "triand", W::TriAnd),
            (R::TriOr, "trior", W::TriOr),
            (R::TriReg, "trireg", W::TriReg),
            (R::Tri0, "tri0", W::Tri0),
            (R::Tri1, "tri1", W::Tri1),
            (R::Wand, "wand", W::Wand),
            (R::Wire, "wire", W::Wire),
            (R::Wor, "wor", W::Wor),
            (R::Port, "port", W::Port),
            (R::SparseArray, "wire", W::Wire),
            (R::RealTime, "realtime", W::RealTime),
            (R::GenericString, "wire", W::Wire),
            (R::Bit, "bit", W::Bit),
            (R::Logic, "logic", W::Logic),
            (R::Int, "int", W::Int),
            (R::ShortInt, "wire", W::Wire),
            (R::LongInt, "wire", W::Wire),
            (R::Byte, "wire", W::Wire),
            (R::Enum, "wire", W::Wire),
            (R::ShortReal, "real", W::Real),
        ];
        for (tpe, keyword, written) in cases {
            assert_eq!(var_type_str(tpe), keyword, "{tpe:?}");
            assert_eq!(var_type_of(keyword), written, "{tpe:?}");
        }
    }

    #[test]
    fn ticks_to_ns_from_exponent_is_ns_per_tick() {
        for (exp, ns) in [
            (-15, 1e-6),
            (-12, 1e-3),
            (-9, 1.0),
            (-8, 10.0),
            (-6, 1e3),
            (-3, 1e6),
            (0, 1e9),
        ] {
            let got = ticks_to_ns_from_exponent(exp);
            assert!((got - ns).abs() < ns * 1e-12, "{exp}: {got} != {ns}");
        }
    }

    #[test]
    fn canon_bits_truncates_overwide_values() {
        let mut out = Vec::new();
        let overwide = canon_bits_into(b"101010", 4, &mut out);
        assert_eq!(&out, b"1010");
        assert!(overwide);
    }
}
