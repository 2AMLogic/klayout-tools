//! The structural half of the liberty parser, split out of `liberty.rs`
//! (issue #5629 review: that file sits at its file-size ratchet ceiling,
//! so the fallible-grammar port lands here instead). This module owns the
//! tokenizer and the recursive-descent grammar for liberty's
//! group/attribute syntax (`group_name (args) { ... }`,
//! `simple_attr : value ;`, `complex_attr (v1, v2, ...) ;`) -- any
//! well-formed `.lib` file parses structurally here, with no semantics
//! attached. `liberty.rs` keeps the semantic-extraction half (units,
//! cells, pins, NLDM tables) and the public `parse`/`LibertyError` API.
//!
//! The grammar is fallible (issue #5629, the hand-port of techmap's own
//! grammar): a truncated or malformed `.lib` surfaces as
//! `Err(LibertyError)` rather than a panic, so every direct caller of
//! `parse` (the standalone `klt-statime` CLI and `lib.rs`'s
//! `run_critical_path`) receives a diagnostic instead of an unwind.

use std::fmt;

use super::LibertyError;

// ---------------------------------------------------------------------
// Tokenizer
// ---------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq)]
enum Token {
    Ident(String),
    Str(String),
    LParen,
    RParen,
    LBrace,
    RBrace,
    Comma,
    Colon,
    Semi,
}

fn tokenize(src: &str) -> Vec<Token> {
    // Liberty allows a line-continuation backslash immediately before a
    // newline inside `values(...)` lists that span many lines -- join those
    // first so the tokenizer never has to special-case it.
    let joined = src.replace("\\\r\n", "").replace("\\\n", "");

    let mut tokens = Vec::new();
    let bytes = joined.as_bytes();
    let mut i = 0;
    let n = bytes.len();
    while i < n {
        let c = bytes[i] as char;
        if c.is_whitespace() {
            i += 1;
            continue;
        }
        // Comments
        if c == '/' && i + 1 < n && bytes[i + 1] as char == '*' {
            i += 2;
            while i + 1 < n && !(bytes[i] as char == '*' && bytes[i + 1] as char == '/') {
                i += 1;
            }
            i += 2;
            continue;
        }
        if c == '/' && i + 1 < n && bytes[i + 1] as char == '/' {
            while i < n && bytes[i] as char != '\n' {
                i += 1;
            }
            continue;
        }
        match c {
            '(' => {
                tokens.push(Token::LParen);
                i += 1;
            }
            ')' => {
                tokens.push(Token::RParen);
                i += 1;
            }
            '{' => {
                tokens.push(Token::LBrace);
                i += 1;
            }
            '}' => {
                tokens.push(Token::RBrace);
                i += 1;
            }
            ',' => {
                tokens.push(Token::Comma);
                i += 1;
            }
            ':' => {
                tokens.push(Token::Colon);
                i += 1;
            }
            ';' => {
                tokens.push(Token::Semi);
                i += 1;
            }
            '"' => {
                let start = i + 1;
                let mut j = start;
                while j < n && bytes[j] as char != '"' {
                    j += 1;
                }
                let s = joined[start..j].to_string();
                tokens.push(Token::Str(s));
                i = j + 1;
            }
            _ => {
                let start = i;
                while i < n {
                    let ch = bytes[i] as char;
                    if ch.is_whitespace()
                        || matches!(ch, '(' | ')' | '{' | '}' | ',' | ':' | ';' | '"')
                    {
                        break;
                    }
                    i += 1;
                }
                tokens.push(Token::Ident(joined[start..i].to_string()));
            }
        }
    }
    tokens
}

// ---------------------------------------------------------------------
// Parse tree
// ---------------------------------------------------------------------

/// A single liberty "value" -- either a bare identifier/number or a quoted
/// string. Liberty does not distinguish these at the grammar level; the
/// distinction only matters when the caller pulls a specific attribute out.
#[derive(Debug, Clone)]
pub(super) enum Value {
    Ident(String),
    Str(String),
}

impl Value {
    pub(super) fn as_str(&self) -> &str {
        match self {
            Value::Ident(s) | Value::Str(s) => s,
        }
    }
}

impl fmt::Display for Value {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.as_str())
    }
}

/// A parsed group: `name (args) { members }`.
#[derive(Debug, Clone)]
pub(super) struct Group {
    pub(super) name: String,
    pub(super) args: Vec<Value>,
    pub(super) members: Vec<Member>,
}

#[derive(Debug, Clone)]
pub(super) enum Member {
    Group(Group),
    /// `name : value ;`
    SimpleAttr(String, Value),
    /// `name (v1, v2, ...) ;` -- used for e.g. `index_1(...)`/`values(...)`.
    ComplexAttr(String, Vec<Value>),
}

struct Parser {
    tokens: Vec<Token>,
    pos: usize,
}

impl Parser {
    fn peek(&self) -> Option<&Token> {
        self.tokens.get(self.pos)
    }

    fn next(&mut self) -> Option<Token> {
        let t = self.tokens.get(self.pos).cloned();
        self.pos += 1;
        t
    }

    /// Parse one top-level group (liberty files are a single top-level
    /// `library (...) { ... }` group). `Ok(None)` means end-of-input
    /// *before* any group started -- distinct from `Err`, which covers
    /// end-of-input inside an open group and every other syntax break
    /// (issue #5629: this is the hand-port of techmap's fallible grammar;
    /// a malformed `.lib` must reach callers as `LibertyError`, never as
    /// a panic).
    fn parse_group(&mut self) -> Result<Option<Group>, LibertyError> {
        let name = match self.next() {
            None => return Ok(None),
            Some(Token::Ident(s)) => s,
            Some(other) => {
                return Err(LibertyError(format!(
                    "expected group/attr name, got {:?}",
                    other
                )))
            }
        };
        // Args in parens (may be empty).
        let mut args = Vec::new();
        if self.peek() == Some(&Token::LParen) {
            self.next();
            loop {
                match self.peek() {
                    Some(Token::RParen) => {
                        self.next();
                        break;
                    }
                    Some(Token::Comma) => {
                        self.next();
                    }
                    Some(Token::Ident(_)) | Some(Token::Str(_)) => {
                        let v = match self.next().unwrap() {
                            Token::Ident(s) => Value::Ident(s),
                            Token::Str(s) => Value::Str(s),
                            _ => unreachable!(),
                        };
                        args.push(v);
                    }
                    other => {
                        return Err(LibertyError(format!(
                            "unexpected token in arg list: {:?}",
                            other
                        )))
                    }
                }
            }
        }
        // Is this a group (has `{`) or an attribute (ends at `;`)?
        match self.peek() {
            Some(Token::LBrace) => {
                self.next();
                let mut members = Vec::new();
                loop {
                    match self.peek() {
                        Some(Token::RBrace) => {
                            self.next();
                            break;
                        }
                        Some(Token::Semi) => {
                            // stray semicolons between members
                            self.next();
                        }
                        Some(Token::Ident(_)) => {
                            members.push(self.parse_member()?);
                        }
                        other => {
                            return Err(LibertyError(format!(
                                "unexpected token in group body: {:?}",
                                other
                            )))
                        }
                    }
                }
                Ok(Some(Group {
                    name,
                    args,
                    members,
                }))
            }
            Some(Token::Semi) => {
                self.next();
                // A complex attribute at top level -- shouldn't normally
                // happen for the outermost call, but handle gracefully by
                // wrapping it as a group with no members.
                Ok(Some(Group {
                    name,
                    args,
                    members: Vec::new(),
                }))
            }
            other => Err(LibertyError(format!(
                "expected '{{' or ';' after group header, got {:?}",
                other
            ))),
        }
    }

    fn parse_member(&mut self) -> Result<Member, LibertyError> {
        let name = match self.next() {
            Some(Token::Ident(s)) => s,
            other => {
                return Err(LibertyError(format!(
                    "expected member name, got {:?}",
                    other
                )))
            }
        };
        match self.peek() {
            Some(Token::Colon) => {
                self.next();
                let v = match self.next() {
                    Some(Token::Ident(s)) => Value::Ident(s),
                    Some(Token::Str(s)) => Value::Str(s),
                    other => {
                        return Err(LibertyError(format!(
                            "expected value after ':', got {:?}",
                            other
                        )))
                    }
                };
                // consume trailing ';'
                if self.peek() == Some(&Token::Semi) {
                    self.next();
                }
                Ok(Member::SimpleAttr(name, v))
            }
            Some(Token::LParen) => {
                self.next();
                let mut args = Vec::new();
                loop {
                    match self.peek() {
                        Some(Token::RParen) => {
                            self.next();
                            break;
                        }
                        Some(Token::Comma) => {
                            self.next();
                        }
                        Some(Token::Ident(_)) | Some(Token::Str(_)) => {
                            let v = match self.next().unwrap() {
                                Token::Ident(s) => Value::Ident(s),
                                Token::Str(s) => Value::Str(s),
                                _ => unreachable!(),
                            };
                            args.push(v);
                        }
                        other => {
                            return Err(LibertyError(format!(
                                "unexpected token in complex-attr args: {:?}",
                                other
                            )))
                        }
                    }
                }
                match self.peek() {
                    Some(Token::LBrace) => {
                        // It's actually a nested group.
                        self.next();
                        let mut members = Vec::new();
                        loop {
                            match self.peek() {
                                Some(Token::RBrace) => {
                                    self.next();
                                    break;
                                }
                                Some(Token::Semi) => {
                                    self.next();
                                }
                                Some(Token::Ident(_)) => {
                                    members.push(self.parse_member()?);
                                }
                                other => {
                                    return Err(LibertyError(format!(
                                        "unexpected token in group body: {:?}",
                                        other
                                    )))
                                }
                            }
                        }
                        Ok(Member::Group(Group {
                            name,
                            args,
                            members,
                        }))
                    }
                    Some(Token::Semi) => {
                        self.next();
                        Ok(Member::ComplexAttr(name, args))
                    }
                    other => Err(LibertyError(format!(
                        "expected '{{' or ';' after complex attr, got {:?}",
                        other
                    ))),
                }
            }
            other => Err(LibertyError(format!(
                "expected ':' or '(' after member name '{}', got {:?}",
                name, other
            ))),
        }
    }
}

pub(super) fn parse_top(src: &str) -> Result<Group, LibertyError> {
    let tokens = tokenize(src);
    let mut p = Parser { tokens, pos: 0 };
    p.parse_group()?
        .ok_or_else(|| LibertyError("empty liberty file".to_string()))
}
