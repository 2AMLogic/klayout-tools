//! A tiny Boolean-expression parser/evaluator over liberty `function`
//! attribute text (`"(A&B)"`, `"(!A) | (!B)"`, `"(A0&!S) | (A1&S)"`, ...).
//!
//! `celllib.rs` uses this to classify an arbitrary standard-cell's output
//! function against the generic-cell vocabulary
//! (`docs/design/synth-techmap-stage-contract.md` section 2.2) **by truth
//! table**, not by string matching against a hand-picked set of literal
//! function strings -- so a cell whose `function` text spells the same
//! Boolean function differently (`"(A&B)"` vs `"A & B"` vs `"!(!A | !B)"`)
//! still classifies correctly. This is deliberately a small, general
//! recursive-descent grammar (identifiers, `!`/`&`/`|`/`^`, parens),
//! matching liberty's own function-attribute grammar rather than a
//! sky130-specific subset.

use std::collections::HashMap;
use std::fmt;

#[derive(Debug, Clone, PartialEq)]
enum Tok {
    Ident(String),
    /// A bare `0`/`1` constant -- liberty's `function` grammar allows a
    /// constant-output cell's function to be the literal digit `"0"` or
    /// `"1"` rather than a Boolean expression over pin names (sky130's own
    /// `conb` uses exactly this: `pin ("HI") { function : "1"; }`).
    /// Tokenized distinctly from [`Tok::Ident`] so a lone `"1"`/`"0"`
    /// never gets treated as a free variable named `"1"`/`"0"` -- see
    /// [`Expr::Const`].
    Const(bool),
    Not,
    And,
    Or,
    Xor,
    LParen,
    RParen,
}

#[derive(Debug)]
pub struct BoolFnError(pub String);

impl fmt::Display for BoolFnError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.0)
    }
}

fn tokenize(src: &str) -> Result<Vec<Tok>, BoolFnError> {
    let mut toks = Vec::new();
    let chars: Vec<char> = src.chars().collect();
    let n = chars.len();
    let mut i = 0;
    while i < n {
        let c = chars[i];
        if c.is_whitespace() {
            i += 1;
            continue;
        }
        match c {
            '!' | '\'' => {
                toks.push(Tok::Not);
                i += 1;
            }
            '&' | '*' => {
                toks.push(Tok::And);
                i += 1;
            }
            '|' | '+' => {
                toks.push(Tok::Or);
                i += 1;
            }
            '^' => {
                toks.push(Tok::Xor);
                i += 1;
            }
            '(' => {
                toks.push(Tok::LParen);
                i += 1;
            }
            ')' => {
                toks.push(Tok::RParen);
                i += 1;
            }
            _ if c.is_alphanumeric() || c == '_' => {
                let start = i;
                while i < n && (chars[i].is_alphanumeric() || chars[i] == '_') {
                    i += 1;
                }
                let text: String = chars[start..i].iter().collect();
                match text.as_str() {
                    "0" => toks.push(Tok::Const(false)),
                    "1" => toks.push(Tok::Const(true)),
                    _ => toks.push(Tok::Ident(text)),
                }
            }
            other => {
                return Err(BoolFnError(format!(
                    "unexpected character '{other}' in function"
                )));
            }
        }
    }
    Ok(toks)
}

#[derive(Debug, Clone)]
pub enum Expr {
    Var(String),
    /// A bare `0`/`1` liberty constant -- see [`Tok::Const`].
    Const(bool),
    Not(Box<Expr>),
    And(Box<Expr>, Box<Expr>),
    Or(Box<Expr>, Box<Expr>),
    Xor(Box<Expr>, Box<Expr>),
}

struct EParser {
    toks: Vec<Tok>,
    pos: usize,
}

impl EParser {
    fn peek(&self) -> Option<&Tok> {
        self.toks.get(self.pos)
    }

    fn bump(&mut self) -> Option<Tok> {
        let t = self.toks.get(self.pos).cloned();
        self.pos += 1;
        t
    }

    // Grammar (lowest to highest precedence): or_expr := xor_expr (('|') xor_expr)*
    // xor_expr := and_expr (('^') and_expr)*
    // and_expr := unary (('&') unary)* -- also allows implicit juxtaposition-AND
    // unary := '!'* primary
    // primary := Ident | '(' or_expr ')'

    fn parse_or(&mut self) -> Result<Expr, BoolFnError> {
        let mut lhs = self.parse_xor()?;
        while matches!(self.peek(), Some(Tok::Or)) {
            self.bump();
            let rhs = self.parse_xor()?;
            lhs = Expr::Or(Box::new(lhs), Box::new(rhs));
        }
        Ok(lhs)
    }

    fn parse_xor(&mut self) -> Result<Expr, BoolFnError> {
        let mut lhs = self.parse_and()?;
        while matches!(self.peek(), Some(Tok::Xor)) {
            self.bump();
            let rhs = self.parse_and()?;
            lhs = Expr::Xor(Box::new(lhs), Box::new(rhs));
        }
        Ok(lhs)
    }

    fn parse_and(&mut self) -> Result<Expr, BoolFnError> {
        let mut lhs = self.parse_unary()?;
        loop {
            match self.peek() {
                Some(Tok::And) => {
                    self.bump();
                    let rhs = self.parse_unary()?;
                    lhs = Expr::And(Box::new(lhs), Box::new(rhs));
                }
                // Implicit AND via juxtaposition: "AB" or "A(B)" -- not used
                // by sky130's own function strings (which are always
                // explicit), but liberty's grammar technically allows it,
                // so a bare Ident/LParen/Not immediately following an
                // and_expr's operand also continues the chain.
                Some(Tok::Ident(_)) | Some(Tok::Const(_)) | Some(Tok::LParen) | Some(Tok::Not) => {
                    let rhs = self.parse_unary()?;
                    lhs = Expr::And(Box::new(lhs), Box::new(rhs));
                }
                _ => break,
            }
        }
        Ok(lhs)
    }

    fn parse_unary(&mut self) -> Result<Expr, BoolFnError> {
        if matches!(self.peek(), Some(Tok::Not)) {
            self.bump();
            let inner = self.parse_unary()?;
            return Ok(Expr::Not(Box::new(inner)));
        }
        self.parse_primary()
    }

    fn parse_primary(&mut self) -> Result<Expr, BoolFnError> {
        match self.bump() {
            Some(Tok::Ident(name)) => Ok(Expr::Var(name)),
            Some(Tok::Const(b)) => Ok(Expr::Const(b)),
            Some(Tok::LParen) => {
                let e = self.parse_or()?;
                match self.bump() {
                    Some(Tok::RParen) => Ok(e),
                    other => Err(BoolFnError(format!("expected ')', got {:?}", other))),
                }
            }
            other => Err(BoolFnError(format!(
                "expected identifier or '(', got {:?}",
                other
            ))),
        }
    }
}

/// Parse a liberty `function` attribute's text into an [`Expr`] tree.
pub fn parse(src: &str) -> Result<Expr, BoolFnError> {
    let toks = tokenize(src)?;
    if toks.is_empty() {
        return Err(BoolFnError("empty function expression".to_string()));
    }
    let mut p = EParser { toks, pos: 0 };
    let e = p.parse_or()?;
    if p.pos != p.toks.len() {
        return Err(BoolFnError(
            "trailing tokens after function expression".to_string(),
        ));
    }
    Ok(e)
}

/// Every free variable (pin name) an expression references, in
/// first-encountered order (deduplicated).
pub fn free_vars(expr: &Expr) -> Vec<String> {
    let mut out = Vec::new();
    fn walk(e: &Expr, out: &mut Vec<String>) {
        match e {
            Expr::Var(v) => {
                if !out.contains(v) {
                    out.push(v.clone());
                }
            }
            Expr::Const(_) => {}
            Expr::Not(a) => walk(a, out),
            Expr::And(a, b) | Expr::Or(a, b) | Expr::Xor(a, b) => {
                walk(a, out);
                walk(b, out);
            }
        }
    }
    walk(expr, &mut out);
    out
}

pub fn eval(expr: &Expr, env: &HashMap<String, bool>) -> bool {
    match expr {
        Expr::Var(v) => *env.get(v).unwrap_or(&false),
        Expr::Const(b) => *b,
        Expr::Not(a) => !eval(a, env),
        Expr::And(a, b) => eval(a, env) && eval(b, env),
        Expr::Or(a, b) => eval(a, env) || eval(b, env),
        Expr::Xor(a, b) => eval(a, env) != eval(b, env),
    }
}

/// The full truth table of `expr` over `vars`, evaluated in the order
/// `vars` provides, MSB-first (i.e. `vars[0]` is the most significant bit
/// of the row index) -- `2^vars.len()` rows.
pub fn truth_table(expr: &Expr, vars: &[String]) -> Vec<bool> {
    let n = vars.len();
    let rows = 1usize << n;
    let mut out = Vec::with_capacity(rows);
    for row in 0..rows {
        let mut env = HashMap::new();
        for (i, v) in vars.iter().enumerate() {
            let bit = (row >> (n - 1 - i)) & 1;
            env.insert(v.clone(), bit == 1);
        }
        out.push(eval(expr, &env));
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_and_evaluates_and2() {
        let e = parse("(A&B)").unwrap();
        assert_eq!(free_vars(&e), vec!["A".to_string(), "B".to_string()]);
        let tt = truth_table(&e, &["A".to_string(), "B".to_string()]);
        assert_eq!(tt, vec![false, false, false, true]);
    }

    #[test]
    fn parses_nand2_written_as_or_of_negations() {
        let e = parse("(!A) | (!B)").unwrap();
        let tt = truth_table(&e, &["A".to_string(), "B".to_string()]);
        // NAND: 1,1,1,0
        assert_eq!(tt, vec![true, true, true, false]);
    }

    #[test]
    fn parses_xor2() {
        let e = parse("(A&!B) | (!A&B)").unwrap();
        let tt = truth_table(&e, &["A".to_string(), "B".to_string()]);
        assert_eq!(tt, vec![false, true, true, false]);
    }

    #[test]
    fn parses_mux() {
        let e = parse("(A0&!S) | (A1&S)").unwrap();
        let vars = vec!["A0".to_string(), "A1".to_string(), "S".to_string()];
        let tt = truth_table(&e, &vars);
        // rows ordered A0,A1,S (MSB..LSB): 000,001,010,011,100,101,110,111
        // Y = S? A1 : A0
        assert_eq!(tt, vec![false, false, false, true, true, false, true, true]);
    }

    #[test]
    fn parses_inverter() {
        let e = parse("(!A)").unwrap();
        let tt = truth_table(&e, &["A".to_string()]);
        assert_eq!(tt, vec![true, false]);
    }

    #[test]
    fn bare_constants_are_not_free_variables() {
        // sky130's `conb`-style single-output tie cells use a bare "0"/"1"
        // function -- these must be constants, not a free variable named
        // "0"/"1" that would let classify_combinational's 1-var $buf check
        // wrongly match a constant-output cell.
        let one = parse("1").unwrap();
        assert!(free_vars(&one).is_empty());
        assert!(eval(&one, &HashMap::new()));

        let zero = parse("0").unwrap();
        assert!(free_vars(&zero).is_empty());
        assert!(!eval(&zero, &HashMap::new()));
    }

    #[test]
    fn constant_mixed_with_a_real_variable_still_evaluates_correctly() {
        let e = parse("A & 1").unwrap();
        assert_eq!(free_vars(&e), vec!["A".to_string()]);
        let tt = truth_table(&e, &["A".to_string()]);
        assert_eq!(tt, vec![false, true]); // A & 1 == A
    }
}
