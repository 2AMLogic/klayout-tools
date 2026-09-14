// Ported and adapted from boldaxolotl/booley, crates/bwave/src/format.rs
// (commit 0c3b4cd7b66a0f793de605ce58e82dc1bc0ac892,
// https://github.com/boldaxolotl/booley).
// Copyright (c) the boldaxolotl/booley contributors.
// Licensed under the Apache License, Version 2.0; see /NOTICE.
//
// Modifications from upstream:
// - Only the typed-time-token parsing (`TimeToken`, `parse_time_range`),
//   the `Radix` display type and hex/dec/bin value formatting
//   (`format_value_with_radix`, `format_value`, and the nibble/bin<->hex
//   helpers they depend on), and value/edge matching (`is_edge_keyword`,
//   `values_match`) are ported. Upstream's `format.rs` also holds the
//   Verilog-literal parser (`parse_verilog_literal`), bit-slicing helpers
//   (`hex_slice`/`slice_bits`/`expand_to_bits`/`hex_to_u128`), and the
//   hierarchical signal-tree text renderer (`print_signal_tree`/
//   `print_scope_tree`) -- none of those are used by any op in
//   docs/design/waveform-query-contract-spike.md section 5 (`value`,
//   `find`, `count`, `sample`, `stuck`, `diff`, `wave` address whole signal
//   values, never a bit slice, and this crate's response shapes are JSON
//   objects, not a rendered tree), so they are not ported.
// - `TimeToken`'s own CLI-string async-mode ambiguity check
//   (upstream rejected a bare integer outside "sync mode") is dropped: a
//   `klt wave query` request's time-point object (`crate::request::TimePointReq`)
//   is always a typed JSON field (`time_ns` or `cycle`), never a free-text
//   token, so that ambiguity cannot arise here.
// - `crate::query`'s own point/window resolution (`resolve_tick` in
//   `src/query.rs`) does **not** route through `TimeToken`: the contract's
//   `time_ns` is a JSON *float* (fractional nanoseconds are representable),
//   while `TimeToken`'s physical-time variants are `i64`-typed (sized for
//   parsing integer CLI tokens like `"100ns"`). Converting through `i64`
//   first would truncate precision `crate::query` does not need to lose.
//   `TimeToken`/`parse_time_range` are kept here, tested, as a faithful port
//   of the upstream dual-time-addressing design (contract section 3 asks
//   for exactly this cycle-vs-simulation-time duality) and as the natural
//   starting point for a future CLI-token surface, but are not load-bearing
//   for this issue's own `query` op vocabulary.
// - `#[allow(clippy::wrong_self_convention)]` added to `to_ns` to satisfy
//   this crate's own `cargo clippy -- -D warnings` CI gate (upstream's
//   workspace does not enable that lint as a hard error).

//! Value formatting (bin<->hex) and typed time-token parsing.
//!
//! The typed-time-token design (cycle vs. simulation-time addressing,
//! resolved once the store header is loaded) is exactly the dual time
//! addressing `docs/design/waveform-query-contract-spike.md` section 3
//! requires. `crate::query` resolves a request's `{"time_ns": ...}` /
//! `{"cycle": ...}` point objects directly against a loaded `ColumnCache`
//! (see that module's own `resolve_tick`) rather than through `TimeToken`
//! below -- see this file's own header comment for why.

// -- Typed time tokens -----------------------------------------------------

/// A time argument with an explicit unit. Resolution to a simulation tick or
/// cycle number is deferred until the store header is available, since
/// `ns`/`us`/`ms`/`ps` units must be converted via the FST timescale and `c`
/// (cycle) units need the clock period.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TimeToken {
    /// Cycle count (`100`, `100c`).
    Cycle(i64),
    /// Raw simulation tick (`100t`).
    Tick(i64),
    /// Picoseconds (`100ps`).
    Pico(i64),
    /// Nanoseconds (`100ns`).
    Nano(i64),
    /// Microseconds (`100us`).
    Micro(i64),
    /// Milliseconds (`100ms`).
    Milli(i64),
}

impl TimeToken {
    /// Parse a time token from a string. Grammar: `integer suffix?` where
    /// `suffix` in `{c, t, ns, us, ms, ps}`. A bare integer (no suffix)
    /// means cycles.
    pub fn parse(s: &str) -> Result<TimeToken, String> {
        let s = s.trim();
        if s.is_empty() {
            return Err("empty time token".to_string());
        }
        let (num_str, suffix) = split_time_suffix(s);
        if num_str.is_empty() {
            return Err(format!("invalid time token: '{s}'"));
        }
        let n: i64 = num_str
            .parse()
            .map_err(|_| format!("invalid integer in time token: '{s}'"))?;
        match suffix {
            "" | "c" => Ok(TimeToken::Cycle(n)),
            "t" => Ok(TimeToken::Tick(n)),
            "ps" => Ok(TimeToken::Pico(n)),
            "ns" => Ok(TimeToken::Nano(n)),
            "us" => Ok(TimeToken::Micro(n)),
            "ms" => Ok(TimeToken::Milli(n)),
            other => Err(format!(
                "unknown time-unit suffix '{other}' in '{s}' (expected c / t / ps / ns / us / ms)"
            )),
        }
    }

    /// Resolve this token to a **cycle** number, using `ticks_to_ns` and
    /// `clock_period_ticks` from the cache header.
    pub fn resolve_to_cycle(
        &self,
        ticks_to_ns: f64,
        clock_period_ticks: u64,
    ) -> Result<i64, String> {
        match *self {
            TimeToken::Cycle(n) => Ok(n),
            TimeToken::Tick(n) => {
                if clock_period_ticks == 0 {
                    return Err("cannot resolve tick-token to cycle: no clock detected".to_string());
                }
                Ok(n / clock_period_ticks as i64)
            }
            TimeToken::Pico(_) | TimeToken::Nano(_) | TimeToken::Micro(_) | TimeToken::Milli(_) => {
                let ns = self.to_ns().unwrap();
                if ticks_to_ns <= 0.0 {
                    return Err("no timescale available".to_string());
                }
                if clock_period_ticks == 0 {
                    return Err(
                        "cannot convert physical-time token to cycle: no clock detected"
                            .to_string(),
                    );
                }
                let ticks = (ns / ticks_to_ns) as i64;
                Ok(ticks / clock_period_ticks as i64)
            }
        }
    }

    /// Resolve this token to a raw **simulation tick**.
    pub fn resolve_to_tick(
        &self,
        ticks_to_ns: f64,
        clock_period_ticks: u64,
    ) -> Result<i64, String> {
        match *self {
            TimeToken::Tick(n) => Ok(n),
            TimeToken::Cycle(n) => {
                if clock_period_ticks == 0 {
                    return Err("cannot resolve cycle-token to tick: no clock detected".to_string());
                }
                Ok(n * clock_period_ticks as i64)
            }
            _ => {
                let ns = self.to_ns().unwrap();
                if ticks_to_ns <= 0.0 {
                    return Err("no timescale available".to_string());
                }
                Ok((ns / ticks_to_ns) as i64)
            }
        }
    }

    /// Convert physical-time tokens to nanoseconds. `None` for
    /// non-physical tokens (Cycle/Tick).
    #[allow(clippy::wrong_self_convention)]
    fn to_ns(&self) -> Option<f64> {
        match *self {
            TimeToken::Pico(n) => Some(n as f64 / 1_000.0),
            TimeToken::Nano(n) => Some(n as f64),
            TimeToken::Micro(n) => Some(n as f64 * 1_000.0),
            TimeToken::Milli(n) => Some(n as f64 * 1_000_000.0),
            _ => None,
        }
    }
}

/// Split a time-token string into (numeric_prefix, unit_suffix).
/// `"100ns"` -> `("100", "ns")`, `"100"` -> `("100", "")`, `"-5c"` -> `("-5", "c")`.
fn split_time_suffix(s: &str) -> (&str, &str) {
    let mut split_at = s.len();
    for (i, ch) in s.char_indices().rev() {
        if ch.is_ascii_alphabetic() {
            split_at = i;
        } else {
            break;
        }
    }
    (&s[..split_at], &s[split_at..])
}

// -- Radix display control --------------------------------------------------

/// Display radix for signal values.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Radix {
    Hex,
    Dec,
    Bin,
}

/// Format a hex value in the given radix for display.
/// `hex_val` is the internal uppercase-hex representation (may contain x/z).
pub fn format_value_with_radix(hex_val: &str, radix: Radix) -> String {
    match radix {
        Radix::Hex => hex_val.to_string(),
        Radix::Dec => {
            if hex_val
                .bytes()
                .any(|b| matches!(b, b'x' | b'X' | b'z' | b'Z'))
            {
                return hex_val.to_string();
            }
            hex_to_decimal(hex_val)
        }
        Radix::Bin => {
            if hex_val
                .bytes()
                .any(|b| matches!(b, b'x' | b'X' | b'z' | b'Z'))
            {
                return hex_val.to_string();
            }
            hex_to_binary(hex_val)
        }
    }
}

/// Convert uppercase hex string to decimal string (big-integer, no crate needed).
fn hex_to_decimal(hex: &str) -> String {
    let hex = hex.trim_start_matches('0');
    if hex.is_empty() {
        return "0".to_string();
    }
    let mut digits: Vec<u8> = vec![0];
    for &b in hex.as_bytes() {
        let nibble = HEX_DECODE_NIBBLE[b as usize];
        if nibble > 0x0F {
            return hex.to_string();
        }
        let mut carry = nibble as u16;
        for d in digits.iter_mut() {
            let v = (*d as u16) * 16 + carry;
            *d = (v % 10) as u8;
            carry = v / 10;
        }
        while carry > 0 {
            digits.push((carry % 10) as u8);
            carry /= 10;
        }
    }
    digits.iter().rev().map(|d| (b'0' + d) as char).collect()
}

/// Convert hex string to binary string (no leading zeros except for "0").
fn hex_to_binary(hex: &str) -> String {
    let hex = hex.trim_start_matches('0');
    if hex.is_empty() {
        return "0".to_string();
    }
    let mut result = String::with_capacity(hex.len() * 4);
    let mut first = true;
    for &b in hex.as_bytes() {
        let nibble = HEX_DECODE_NIBBLE[b as usize];
        if nibble > 0x0F {
            return hex.to_string();
        }
        if first {
            result.push_str(&format!("{nibble:b}"));
            first = false;
        } else {
            result.push_str(&format!("{nibble:04b}"));
        }
    }
    if result.is_empty() {
        "0".to_string()
    } else {
        result
    }
}

/// Format a VCD value as hex (multi-bit) or raw (1-bit / x/z).
///
/// Input values (after stripping VCD prefixes):
///   - single char: '0', '1', 'x', 'z'
///   - raw binary string: "0110..." (no 'b' prefix)
///   - real number string: "1.5" (no 'r' prefix)
pub fn format_value(val: &str) -> String {
    if val.len() <= 1 {
        return val.to_string();
    }
    if val.contains(['x', 'z', 'X', 'Z']) {
        return val.to_string();
    }
    bin_to_hex(val).unwrap_or_else(|| val.to_string())
}

/// Fast nibble-based binary-to-uppercase-hex conversion.
/// Returns None if input is not a valid binary string.
fn bin_to_hex(bin: &str) -> Option<String> {
    if bin.is_empty() {
        return Some("0".to_string());
    }
    if !bin.bytes().all(|b| b == b'0' || b == b'1') {
        return None;
    }
    let mut hex = String::with_capacity(bin.len().div_ceil(4));
    let remainder = bin.len() % 4;
    let start = if remainder > 0 {
        let nibble = u8_from_bin(&bin[..remainder]);
        if nibble > 0 || bin.len() <= 4 {
            hex.push(nibble_to_hex(nibble));
        }
        remainder
    } else {
        0
    };
    let mut i = start;
    let skip_leading = hex.is_empty();
    while i + 4 <= bin.len() {
        let nibble = u8_from_bin(&bin[i..i + 4]);
        if !skip_leading || nibble > 0 || !hex.is_empty() || i + 4 >= bin.len() {
            hex.push(nibble_to_hex(nibble));
        }
        i += 4;
    }
    if hex.is_empty() {
        Some("0".to_string())
    } else {
        Some(hex)
    }
}

fn u8_from_bin(s: &str) -> u8 {
    let mut val = 0u8;
    for b in s.bytes() {
        val = (val << 1) | (b - b'0');
    }
    val
}

fn nibble_to_hex(n: u8) -> char {
    match n {
        0..=9 => (b'0' + n) as char,
        10..=15 => (b'A' + n - 10) as char,
        _ => unreachable!(),
    }
}

/// Hex ASCII -> nibble value. Values > 0x0F indicate invalid (non-hex) input.
const HEX_DECODE_NIBBLE: [u8; 256] = {
    let mut t = [0xFFu8; 256];
    t[b'0' as usize] = 0;
    t[b'1' as usize] = 1;
    t[b'2' as usize] = 2;
    t[b'3' as usize] = 3;
    t[b'4' as usize] = 4;
    t[b'5' as usize] = 5;
    t[b'6' as usize] = 6;
    t[b'7' as usize] = 7;
    t[b'8' as usize] = 8;
    t[b'9' as usize] = 9;
    t[b'A' as usize] = 10;
    t[b'B' as usize] = 11;
    t[b'C' as usize] = 12;
    t[b'D' as usize] = 13;
    t[b'E' as usize] = 14;
    t[b'F' as usize] = 15;
    t[b'a' as usize] = 10;
    t[b'b' as usize] = 11;
    t[b'c' as usize] = 12;
    t[b'd' as usize] = 13;
    t[b'e' as usize] = 14;
    t[b'f' as usize] = 15;
    t
};

/// Compare two hex strings for numeric equality: strip leading zeros, case-fold.
/// Works for arbitrarily wide values (no integer parsing).
fn canonical_hex_eq(a: &str, b: &str) -> bool {
    let a = a.trim_start_matches('0');
    let b = b.trim_start_matches('0');
    a.eq_ignore_ascii_case(b)
}

/// Check if a value string is an edge keyword. Returns
/// Some("rising")/Some("falling")/Some("change")/None.
pub fn is_edge_keyword(value: &str) -> Option<&'static str> {
    match value.to_lowercase().as_str() {
        "rising" => Some("rising"),
        "falling" => Some("falling"),
        "change" => Some("change"),
        _ => None,
    }
}

fn contains_xz(s: &str) -> bool {
    s.bytes().any(|b| matches!(b, b'x' | b'X' | b'z' | b'Z'))
}

/// Compare two values for equality (safe for x/z).
/// Handles case-insensitive hex, leading zeros, binary targets, x/z containment.
pub fn values_match(actual: &str, target: &str) -> bool {
    if actual.eq_ignore_ascii_case(target) {
        return true;
    }
    if target.eq_ignore_ascii_case("Z") {
        return contains_xz(actual) && actual.bytes().any(|b| matches!(b, b'z' | b'Z'));
    }
    if target.eq_ignore_ascii_case("X") {
        return contains_xz(actual) && actual.bytes().any(|b| matches!(b, b'x' | b'X'));
    }
    if contains_xz(actual) || contains_xz(target) {
        return false;
    }
    // If target looks like binary (all 0/1, len>1), convert to hex and compare canonically.
    if target.len() > 1 && target.bytes().all(|b| b == b'0' || b == b'1') {
        if let Some(target_hex) = bin_to_hex(target) {
            return canonical_hex_eq(actual, &target_hex);
        }
    }
    canonical_hex_eq(actual, target)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_time_token_parse() {
        assert_eq!(TimeToken::parse("100").unwrap(), TimeToken::Cycle(100));
        assert_eq!(TimeToken::parse("100c").unwrap(), TimeToken::Cycle(100));
        assert_eq!(TimeToken::parse("100t").unwrap(), TimeToken::Tick(100));
        assert_eq!(TimeToken::parse("100ns").unwrap(), TimeToken::Nano(100));
        assert_eq!(TimeToken::parse("100us").unwrap(), TimeToken::Micro(100));
        assert_eq!(TimeToken::parse("100ms").unwrap(), TimeToken::Milli(100));
        assert_eq!(TimeToken::parse("100ps").unwrap(), TimeToken::Pico(100));
        assert!(TimeToken::parse("").is_err());
        assert!(TimeToken::parse("abc").is_err());
        assert!(TimeToken::parse("100xyz").is_err());
    }

    #[test]
    fn test_resolve_to_cycle() {
        let t = TimeToken::Nano(100);
        // 1ns ticks, 10-tick clock period -> 100 ticks -> cycle 10
        assert_eq!(t.resolve_to_cycle(1.0, 10).unwrap(), 10);
        let t = TimeToken::Cycle(7);
        assert_eq!(t.resolve_to_cycle(1.0, 10).unwrap(), 7);
        let t = TimeToken::Tick(55);
        assert_eq!(t.resolve_to_cycle(1.0, 10).unwrap(), 5);
    }

    #[test]
    fn test_resolve_to_tick() {
        let t = TimeToken::Cycle(5);
        assert_eq!(t.resolve_to_tick(1.0, 10).unwrap(), 50);
        let t = TimeToken::Nano(84.0 as i64);
        assert_eq!(t.resolve_to_tick(1.0, 10).unwrap(), 84);
    }

    #[test]
    fn test_format_value() {
        assert_eq!(format_value("1"), "1");
        assert_eq!(format_value("0"), "0");
        assert_eq!(format_value("x"), "x");
        assert_eq!(format_value("00001111"), "F");
        assert_eq!(format_value("1111"), "F");
        assert_eq!(format_value("00000000"), "0");
        assert_eq!(format_value("01x0"), "01x0");
    }

    #[test]
    fn test_format_value_with_radix() {
        assert_eq!(format_value_with_radix("F", Radix::Hex), "F");
        assert_eq!(format_value_with_radix("F", Radix::Dec), "15");
        assert_eq!(format_value_with_radix("F", Radix::Bin), "1111");
        assert_eq!(format_value_with_radix("X", Radix::Dec), "X");
    }

    #[test]
    fn test_values_match() {
        assert!(values_match("1", "1"));
        assert!(values_match("F", "f"));
        assert!(values_match("0F", "F"));
        assert!(values_match("2", "10")); // binary "10" == 2
        assert!(values_match("x1", "X"));
        assert!(!values_match("01", "X"));
        assert!(!values_match("1", "0"));
    }

    #[test]
    fn test_is_edge_keyword() {
        assert_eq!(is_edge_keyword("rising"), Some("rising"));
        assert_eq!(is_edge_keyword("FALLING"), Some("falling"));
        assert_eq!(is_edge_keyword("change"), Some("change"));
        assert_eq!(is_edge_keyword("1"), None);
    }
}
