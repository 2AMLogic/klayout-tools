//! Minimal DEF reader/writer -- only `UNITS DISTANCE MICRONS`, `ROW`, and
//! `COMPONENTS` (the fields a row legalizer needs). Every other section
//! (`NETS`, `SPECIALNETS`, `PINS`, `TRACKS`, ...) is treated as opaque text
//! and passed through byte-for-byte unmodified on write -- this is
//! deliberate, not a shortcut: issue #784's own acceptance criteria require
//! proving legalization "changes geometry only, never connectivity", and
//! the simplest, strongest way to prove that is to never parse (and
//! therefore never risk mutating) the connectivity-bearing sections at
//! all.

#[derive(Debug, Clone)]
pub struct Row {
    pub name: String,
    pub site: String,
    pub x_dbu: i64,
    pub y_dbu: i64,
    pub orient: String,
    pub num_x: i64,
    pub step_x_dbu: i64,
    pub step_y_dbu: i64,
}

impl Row {
    pub fn x_min_dbu(&self) -> i64 {
        self.x_dbu
    }

    pub fn x_max_dbu(&self) -> i64 {
        self.x_dbu + self.step_x_dbu * self.num_x
    }
}

#[derive(Debug, Clone)]
pub struct Component {
    pub instance: String,
    pub macro_name: String,
    pub x_dbu: i64,
    pub y_dbu: i64,
    pub orient: String,
}

pub struct Def {
    pub dbu: i64,
    pub rows: Vec<Row>,
    pub components: Vec<Component>,
    /// The full source text, with the `COMPONENTS ... END COMPONENTS` block
    /// replaced by a `{COMPONENTS}` placeholder -- `write` substitutes the
    /// (possibly legalized) component list back in, leaving every other
    /// byte -- including `NETS`/`PINS`/`ROW` -- untouched.
    template: String,
}

#[derive(Debug)]
pub struct DefParseError(pub String);

impl std::fmt::Display for DefParseError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "DEF parse error: {}", self.0)
    }
}

impl Def {
    pub fn parse(text: &str) -> Result<Def, DefParseError> {
        let dbu = parse_dbu(text)
            .ok_or_else(|| DefParseError("no 'UNITS DISTANCE MICRONS <dbu> ;' line".into()))?;

        let rows = text.lines().filter_map(parse_row_line).collect::<Vec<_>>();

        let components_start = text
            .find("\nCOMPONENTS ")
            .ok_or_else(|| DefParseError("no COMPONENTS section".into()))?
            + 1;
        let components_header_end = text[components_start..]
            .find('\n')
            .map(|i| components_start + i + 1)
            .ok_or_else(|| DefParseError("malformed COMPONENTS header".into()))?;
        let end_marker = "END COMPONENTS";
        let components_end = text[components_start..]
            .find(end_marker)
            .map(|i| components_start + i)
            .ok_or_else(|| DefParseError("no END COMPONENTS".into()))?;

        let body = &text[components_header_end..components_end];
        let components = body.lines().filter_map(parse_component_line).collect();

        let mut template = String::with_capacity(text.len());
        template.push_str(&text[..components_start]);
        template.push_str("{COMPONENTS}\n");
        template.push_str(&text[components_end..]);

        Ok(Def {
            dbu,
            rows,
            components,
            template,
        })
    }

    /// Renders the DEF text with `components` substituted in place of the
    /// original `COMPONENTS` block's body (header/`END COMPONENTS` kept
    /// from the source template, so the emitted count is always correct).
    pub fn write(&self, components: &[Component]) -> String {
        let count = components.len();
        let mut body = format!("COMPONENTS {count} ;\n");
        for c in components {
            body.push_str(&format!(
                "    - {} {} + PLACED ( {} {} ) {} ;\n",
                c.instance, c.macro_name, c.x_dbu, c.y_dbu, c.orient
            ));
        }
        self.template
            .replacen("{COMPONENTS}", body.trim_end_matches('\n'), 1)
    }
}

fn parse_dbu(text: &str) -> Option<i64> {
    for line in text.lines() {
        let trimmed = line.trim();
        if let Some(rest) = trimmed.strip_prefix("UNITS DISTANCE MICRONS ") {
            let token = rest.trim_end_matches(" ;").trim_end_matches(';').trim();
            return token.parse().ok();
        }
    }
    None
}

fn parse_row_line(line: &str) -> Option<Row> {
    let trimmed = line.trim();
    let rest = trimmed.strip_prefix("ROW ")?;
    let rest = rest.trim_end_matches(" ;").trim_end_matches(';');
    let tokens: Vec<&str> = rest.split_whitespace().collect();
    // ROW <name> <site> <x> <y> <orient> DO <numX> BY 1 STEP <stepX> <stepY>
    if tokens.len() < 12 || tokens[5] != "DO" || tokens[7] != "BY" || tokens[9] != "STEP" {
        return None;
    }
    Some(Row {
        name: tokens[0].to_string(),
        site: tokens[1].to_string(),
        x_dbu: tokens[2].parse().ok()?,
        y_dbu: tokens[3].parse().ok()?,
        orient: tokens[4].to_string(),
        num_x: tokens[6].parse().ok()?,
        step_x_dbu: tokens[9 + 1].parse().ok()?,
        step_y_dbu: tokens[9 + 2].parse().ok()?,
    })
}

fn parse_component_line(line: &str) -> Option<Component> {
    let trimmed = line.trim();
    if !trimmed.starts_with("- ") {
        return None;
    }
    let rest = trimmed.trim_start_matches("- ");
    let rest = rest.trim_end_matches(" ;").trim_end_matches(';');
    // `<inst> <macro> + PLACED ( x y ) <orient>` (or `+ FIXED`/`+ UNPLACED`
    // -- UNPLACED components have no `( x y )` and are skipped here, since
    // an unplaced component has nothing for a row legalizer to legalize).
    let tokens: Vec<&str> = rest.split_whitespace().collect();
    if tokens.len() < 2 {
        return None;
    }
    let instance = tokens[0].to_string();
    let macro_name = tokens[1].to_string();

    let paren_open = tokens.iter().position(|t| *t == "(")?;
    let x: i64 = tokens.get(paren_open + 1)?.parse().ok()?;
    let y: i64 = tokens.get(paren_open + 2)?.parse().ok()?;
    let orient = tokens.get(paren_open + 4)?.to_string();

    Some(Component {
        instance,
        macro_name,
        x_dbu: x,
        y_dbu: y,
        orient,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    const SAMPLE: &str = "\
VERSION 5.8 ;
DESIGN top ;
UNITS DISTANCE MICRONS 1000 ;
DIEAREA ( 0 0 ) ( 10000 5440 ) ;
ROW ROW_0 unithd 0 0 N DO 20 BY 1 STEP 460 0 ;
ROW ROW_1 unithd 0 2720 FS DO 20 BY 1 STEP 460 0 ;
COMPONENTS 2 ;
    - u1 sky130_fd_sc_hd__inv_1 + PLACED ( 100 50 ) N ;
    - u2 sky130_fd_sc_hd__buf_4 + PLACED ( 2000 2800 ) FS ;
END COMPONENTS
NETS 1 ;
    - n1 ( u1 A ) ( u2 X ) + USE SIGNAL ;
END NETS
END DESIGN
";

    #[test]
    fn parses_dbu_rows_components() {
        let def = Def::parse(SAMPLE).unwrap();
        assert_eq!(def.dbu, 1000);
        assert_eq!(def.rows.len(), 2);
        assert_eq!(def.rows[0].step_x_dbu, 460);
        assert_eq!(def.rows[1].orient, "FS");
        assert_eq!(def.components.len(), 2);
        assert_eq!(def.components[1].x_dbu, 2000);
    }

    #[test]
    fn write_preserves_everything_outside_components() {
        let def = Def::parse(SAMPLE).unwrap();
        let mut moved = def.components.clone();
        moved[0].x_dbu = 460;
        let out = def.write(&moved);
        assert!(out.contains("- u1 sky130_fd_sc_hd__inv_1 + PLACED ( 460 50 ) N ;"));
        // NETS untouched, byte for byte.
        assert!(out.contains("- n1 ( u1 A ) ( u2 X ) + USE SIGNAL ;"));
        assert!(out.contains("DIEAREA ( 0 0 ) ( 10000 5440 ) ;"));
    }
}
