// Ported and adapted from boldaxolotl/booley, crates/bwave/src/signal.rs
// (commit 0c3b4cd7b66a0f793de605ce58e82dc1bc0ac892,
// https://github.com/boldaxolotl/booley).
// Copyright (c) the boldaxolotl/booley contributors.
// Licensed under the Apache License, Version 2.0; see /NOTICE.
//
// Modifications from upstream: none at port time -- this module is small
// and self-contained (no CLI coupling), ported as-is including its own
// unit tests.

//! Signal metadata, glob matching, and scope prefix utilities.

use globset::{Glob, GlobMatcher};

/// Metadata for a single signal declaration (used by store-build tooling;
/// kept here for parity with upstream even though `klt wave query` reads
/// `crate::cache::CachedSignal` for its own directory).
#[derive(Debug, Clone)]
#[allow(dead_code)]
pub struct SignalMeta {
    /// Full hierarchical name (e.g. "tb.dut.data[7:0]")
    pub name: String,
    /// VCD identifier code (short ASCII string like "!" or "#")
    pub id: String,
    /// Bit width
    pub width: u32,
    /// Variable type from $var (wire, reg, etc.)
    pub var_type: String,
}

/// Strip bit-select brackets from a signal name.
/// "dut.state[3:0]" -> "dut.state"
pub fn strip_bit_select(name: &str) -> &str {
    match name.find('[') {
        Some(i) => &name[..i],
        None => name,
    }
}

/// Check if a signal name matches any glob pattern.
/// Strips bit-select brackets before matching (so "*state*" matches "dut.state[3:0]").
/// Matches against both stripped and original forms.
pub fn match_signal(name: &str, matchers: &[(GlobMatcher, GlobMatcher)]) -> bool {
    let stripped = strip_bit_select(name);
    for (orig_matcher, stripped_matcher) in matchers {
        if stripped_matcher.is_match(stripped) || orig_matcher.is_match(name) {
            return true;
        }
    }
    false
}

/// Byte offset of a trailing Verilog index / bit-range suffix -- `[56]`,
/// `[7:0]` -- or None when the trailing bracket group is a genuine glob
/// character class (`[0-3]`, `[abc]`) or absent.
///
/// This distinction is what makes a pattern like `"words[56]"` work at all:
/// to globset `[56]` reads as "one character, either 5 or 6", so an
/// element-indexed name looked like a wildcard pattern and matched nothing.
fn index_suffix_start(p: &str) -> Option<usize> {
    let inner_end = p.strip_suffix(']')?.len();
    let open = p[..inner_end].rfind('[')?;
    let inner = &p[open + 1..inner_end];
    let index_like = !inner.is_empty()
        && inner.bytes().all(|b| b.is_ascii_digit() || b == b':')
        && inner.bytes().any(|b| b.is_ascii_digit());
    index_like.then_some(open)
}

/// Escape a literal `[` for globset -- `[[]` is the character class whose one
/// member is `[` -- so a Verilog index matches itself instead of acting as a
/// class over its own digits.
fn escape_index_bracket(p: &str, open: usize) -> String {
    format!("{}[[]{}", &p[..open], &p[open + 1..])
}

/// Auto-wrap a pattern with a leading `*` if it contains no glob metacharacters.
/// This gives suffix matching for bare signal names.
///   "input_data"  -> "*input_data"  (matches "tb.dut.input_data", NOT "input_data_plain")
///   "*input_data" -> "*input_data"  (already has wildcards, keep as-is)
///   "*data*"      -> "*data*"       (explicit substring opt-in)
///   "words[56]"   -> "*words[56]"   (trailing index is a literal, not a class)
fn auto_wrap_pattern(p: &str) -> String {
    let meta_scan = match index_suffix_start(p) {
        Some(open) => &p[..open],
        None => p,
    };
    if meta_scan.contains('*') || meta_scan.contains('?') || meta_scan.contains('[') {
        p.to_string()
    } else {
        format!("*{p}")
    }
}

/// Compile glob patterns into matchers. Returns (original_matcher, stripped_matcher) pairs.
/// The stripped matcher also has bit-select removed from the pattern itself.
/// Patterns without glob metacharacters are auto-wrapped with `*...*` for substring matching.
///
/// Returns `Err` with the original pattern text and underlying glob error if
/// compilation fails -- a request naming a syntactically invalid pattern is
/// an application error (contract section 5's "op naming a signal absent
/// from the store" case includes an unresolvable glob), not a silent
/// fallback to matching everything.
pub fn compile_patterns(patterns: &[String]) -> Result<Vec<(GlobMatcher, GlobMatcher)>, String> {
    patterns
        .iter()
        .map(|p| {
            let wrapped = auto_wrap_pattern(p);
            // A trailing Verilog index is compiled as a literal; the stripped
            // matcher below still covers the case where the simulator dumped
            // the whole vector under its base name.
            let orig_pat = match index_suffix_start(&wrapped) {
                Some(open) => escape_index_bracket(&wrapped, open),
                None => wrapped.clone(),
            };
            let orig = Glob::new(&orig_pat)
                .map_err(|e| format!("invalid glob pattern '{p}': {e}"))?
                .compile_matcher();
            let stripped_pat = strip_bit_select(&wrapped);
            let stripped = Glob::new(stripped_pat)
                .map_err(|e| format!("invalid glob pattern '{p}': {e}"))?
                .compile_matcher();
            Ok((orig, stripped))
        })
        .collect()
}

/// Filter signals to those within a hierarchical scope.
/// Auto-appends `.` if missing to prevent "tb.dut" matching "tb.dut_extra.x".
#[allow(dead_code)]
pub fn signals_in_scope(signals: &[SignalMeta], scope: &str) -> Vec<SignalMeta> {
    let scope_dot = if scope.ends_with('.') {
        scope.to_string()
    } else {
        format!("{scope}.")
    };
    signals
        .iter()
        .filter(|s| s.name.starts_with(&scope_dot))
        .cloned()
        .collect()
}

/// Find the deepest common hierarchical prefix shared by all signal names.
/// Returns prefix with trailing dot (e.g. "tb.dut.") or empty string.
/// Truncated to dot boundary so partial leaf names are never split.
#[allow(dead_code)]
pub fn common_scope_prefix(names: &[String]) -> String {
    if names.is_empty() {
        return String::new();
    }
    if names.len() == 1 {
        return match names[0].rfind('.') {
            Some(dot) if dot > 0 => names[0][..=dot].to_string(),
            _ => String::new(),
        };
    }
    let first = names.iter().min().unwrap();
    let last = names.iter().max().unwrap();
    let common_len = first
        .bytes()
        .zip(last.bytes())
        .take_while(|(a, b)| a == b)
        .count();
    let prefix = &first[..common_len];
    match prefix.rfind('.') {
        Some(dot) if dot > 0 => prefix[..=dot].to_string(),
        _ => String::new(),
    }
}

/// Distinct first-level scopes represented by the signal set.
#[allow(dead_code)]
pub fn top_scopes(names: &[String]) -> Vec<String> {
    let mut scopes = std::collections::BTreeSet::new();
    for name in names {
        if let Some((scope, _leaf)) = name.split_once('.') {
            if !scope.is_empty() {
                scopes.insert(scope.to_string());
            }
        }
    }
    scopes.into_iter().collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_strip_bit_select() {
        assert_eq!(strip_bit_select("dut.data[7:0]"), "dut.data");
        assert_eq!(strip_bit_select("clk"), "clk");
        assert_eq!(strip_bit_select("a[0]"), "a");
    }

    #[test]
    fn test_common_scope_prefix() {
        let names = vec!["tb.dut.a".into(), "tb.dut.b".into()];
        assert_eq!(common_scope_prefix(&names), "tb.dut.");

        let names = vec!["tb.dut.sub.flag".into()];
        assert_eq!(common_scope_prefix(&names), "tb.dut.sub.");

        let names: Vec<String> = vec![];
        assert_eq!(common_scope_prefix(&names), "");

        let names = vec!["a".into(), "b".into()];
        assert_eq!(common_scope_prefix(&names), "");
    }

    #[test]
    fn test_top_scopes_preserves_multiple_roots() {
        let names = vec![
            "$rootio.clk".into(),
            "uart16550.rx_state".into(),
            "uart16550.rx_data".into(),
        ];
        assert_eq!(top_scopes(&names), vec!["$rootio", "uart16550"]);
    }

    #[test]
    fn test_match_signal() {
        let pats = compile_patterns(&["*data*".into()]).unwrap();
        assert!(match_signal("tb.dut.data[7:0]", &pats));
        assert!(!match_signal("tb.dut.clk", &pats));

        let pats = compile_patterns(&["*".into()]).unwrap();
        assert!(match_signal("anything", &pats));
    }

    #[test]
    fn test_auto_wrap_bare_pattern() {
        let pats = compile_patterns(&["input_data".into()]).unwrap();
        assert!(match_signal("tb.dut.input_data", &pats));
        assert!(match_signal("tb.dut.input_data[7:0]", &pats));
        assert!(match_signal("input_data", &pats));
        assert!(!match_signal("tb.dut.output_data", &pats));

        let pats = compile_patterns(&["input_data*".into()]).unwrap();
        assert!(!match_signal("tb.dut.input_data", &pats)); // no leading *
        assert!(match_signal("input_data_bus", &pats));

        let pats = compile_patterns(&["dut.clk".into()]).unwrap();
        assert!(match_signal("tb.dut.clk", &pats));
    }

    #[test]
    fn bare_name_suffix_matches() {
        let pats = compile_patterns(&["dmem_addr".into()]).unwrap();
        assert!(match_signal("tb.dut.dmem_addr", &pats));
        assert!(!match_signal("tb.dut.dmem_addr_next", &pats));
    }

    #[test]
    fn bare_name_no_substring_match() {
        let pats = compile_patterns(&["dmem".into()]).unwrap();
        assert!(!match_signal("tb.dut.dmem_addr", &pats));
        assert!(match_signal("tb.dut.dmem", &pats));
    }

    #[test]
    fn wildcard_substring_still_works() {
        let pats = compile_patterns(&["*dmem*".into()]).unwrap();
        assert!(match_signal("tb.dut.dmem_addr", &pats));
        assert!(match_signal("tb.dut.dmem_addr_next", &pats));
        assert!(match_signal("tb.dut.dmem", &pats));
    }

    #[test]
    fn wildcard_prefix_anchor() {
        let pats = compile_patterns(&["*dmem_addr".into()]).unwrap();
        assert!(match_signal("tb.dut.dmem_addr", &pats));
        assert!(!match_signal("tb.dut.dmem_addr_next", &pats));
    }

    #[test]
    fn test_signals_in_scope() {
        let sigs = vec![
            SignalMeta {
                name: "tb.dut.a".into(),
                id: "!".into(),
                width: 1,
                var_type: "wire".into(),
            },
            SignalMeta {
                name: "tb.dut.b[7:0]".into(),
                id: "#".into(),
                width: 8,
                var_type: "reg".into(),
            },
            SignalMeta {
                name: "tb.dut_extra.c".into(),
                id: "$".into(),
                width: 1,
                var_type: "wire".into(),
            },
            SignalMeta {
                name: "tb.clk".into(),
                id: "%".into(),
                width: 1,
                var_type: "wire".into(),
            },
        ];
        let filtered = signals_in_scope(&sigs, "tb.dut");
        assert_eq!(filtered.len(), 2);
        assert_eq!(filtered[0].name, "tb.dut.a");
        assert_eq!(filtered[1].name, "tb.dut.b[7:0]");

        let filtered2 = signals_in_scope(&sigs, "tb.dut.");
        assert_eq!(filtered2.len(), 2);

        let filtered3 = signals_in_scope(&sigs, "tb.nonexistent");
        assert!(filtered3.is_empty());
    }

    #[test]
    fn test_invalid_glob_returns_err() {
        let result = compile_patterns(&["[bad".into()]);
        assert!(result.is_err());
        let msg = result.unwrap_err();
        assert!(
            msg.contains("[bad"),
            "error should name the offending pattern: {msg}"
        );
    }
}
