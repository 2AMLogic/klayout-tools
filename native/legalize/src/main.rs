//! `klt-legalize` -- issue #784's spike CLI. Two subcommands:
//!
//!   klt-legalize legalize <gp.def[.gz]> <tech.lef[.gz]> <cell.lef[.gz]> \
//!       <site> <out.def> [--json]
//!       Runs the Abacus legalizer, writes the legalized DEF, prints a
//!       displacement/wall-clock JSON summary.
//!
//!   klt-legalize metrics <def[.gz]> <tech.lef[.gz]> <cell.lef[.gz]> <site>
//!       Reports overlap count + hpwl_um for any DEF (the GP input, this
//!       crate's own legalized output, or OpenROAD's oracle-legalized
//!       output) -- the common yardstick issue #784's comparison table
//!       uses across all three.
//!
//! Never a shipped `klt` command -- this crate has no `pyo3`/CLI-contract
//! wiring (see `Cargo.toml`'s own comment); this binary exists purely to
//! run the spike's own measurements, invoked directly by
//! `tests/corpus/legalize/compare.py`.

use klt_legalize_native::{abacus, def::Def, lef, metrics};
use std::collections::HashMap;
use std::fs::File;
use std::io::Read;
use std::time::Instant;

fn read_maybe_gz(path: &str) -> Result<String, String> {
    let mut file = File::open(path).map_err(|e| format!("could not open '{path}': {e}"))?;
    let mut bytes = Vec::new();
    file.read_to_end(&mut bytes)
        .map_err(|e| format!("could not read '{path}': {e}"))?;

    if path.ends_with(".gz") {
        let mut decoder = flate2::read::GzDecoder::new(&bytes[..]);
        let mut text = String::new();
        decoder
            .read_to_string(&mut text)
            .map_err(|e| format!("could not gunzip '{path}': {e}"))?;
        Ok(text)
    } else {
        String::from_utf8(bytes).map_err(|e| format!("'{path}' is not valid UTF-8: {e}"))
    }
}

/// `(site_width_um, site_height_um)`, and macro name -> `(width_um,
/// height_um)`.
type SiteAndMacroSizes = ((f64, f64), HashMap<String, (f64, f64)>);

fn load_widths(
    tech_lef_path: &str,
    cell_lef_path: &str,
    site: &str,
) -> Result<SiteAndMacroSizes, String> {
    let tech_text = read_maybe_gz(tech_lef_path)?;
    let cell_text = read_maybe_gz(cell_lef_path)?;
    let site_size = lef::find_site_size_um(&tech_text, site)
        .ok_or_else(|| format!("site '{site}' not found in '{tech_lef_path}'"))?;
    let widths = lef::parse_macro_sizes_um(&cell_text);
    Ok((site_size, widths))
}

fn cmd_legalize(args: &[String]) -> Result<(), String> {
    let [gp_def_path, tech_lef_path, cell_lef_path, site, out_def_path] = args else {
        return Err(
            "usage: klt-legalize legalize <gp.def> <tech.lef> <cell.lef> <site> <out.def>"
                .to_string(),
        );
    };

    let (site_size, widths) = load_widths(tech_lef_path, cell_lef_path, site)?;
    let def_text = read_maybe_gz(gp_def_path)?;
    let def = Def::parse(&def_text).map_err(|e| e.to_string())?;

    let t0 = Instant::now();
    let result = abacus::legalize(&def, site_size, &widths)?;
    let legalize_wallclock_ms = t0.elapsed().as_secs_f64() * 1000.0;

    let out_text = def.write(&result.components);
    std::fs::write(out_def_path, &out_text)
        .map_err(|e| format!("could not write '{out_def_path}': {e}"))?;

    let overlaps = metrics::find_overlaps(&result.components, &widths, def.dbu as f64)?;

    let summary = serde_json::json!({
        "component_count": def.components.len(),
        "total_displacement_um": result.total_displacement_dbu as f64 / def.dbu as f64,
        "max_displacement_um": result.max_displacement_dbu as f64 / def.dbu as f64,
        "safety_net_moves": result.safety_net_moves,
        "overlap_count": overlaps.len(),
        "legalize_wallclock_ms": legalize_wallclock_ms,
        "out_def": out_def_path,
    });
    println!("{}", serde_json::to_string_pretty(&summary).unwrap());
    Ok(())
}

fn cmd_metrics(args: &[String]) -> Result<(), String> {
    let [def_path, tech_lef_path, cell_lef_path, site] = args else {
        return Err("usage: klt-legalize metrics <def> <tech.lef> <cell.lef> <site>".to_string());
    };

    let (_site_size, widths) = load_widths(tech_lef_path, cell_lef_path, site)?;
    let def_text = read_maybe_gz(def_path)?;
    let def = Def::parse(&def_text).map_err(|e| e.to_string())?;

    let overlaps = metrics::find_overlaps(&def.components, &widths, def.dbu as f64)?;
    let hpwl = metrics::hpwl_um(&def_text, &def.components, &widths, def.dbu as f64)?;

    let summary = serde_json::json!({
        "component_count": def.components.len(),
        "overlap_count": overlaps.len(),
        "overlaps": overlaps.iter().map(|o| format!("{}<->{}", o.a, o.b)).collect::<Vec<_>>(),
        "hpwl_um": hpwl,
    });
    println!("{}", serde_json::to_string_pretty(&summary).unwrap());
    Ok(())
}

fn main() {
    let all_args: Vec<String> = std::env::args().collect();
    let result = match all_args.get(1).map(String::as_str) {
        Some("legalize") => cmd_legalize(&all_args[2..]),
        Some("metrics") => cmd_metrics(&all_args[2..]),
        _ => Err("usage: klt-legalize <legalize|metrics> ...".to_string()),
    };
    if let Err(e) = result {
        eprintln!("error: {e}");
        std::process::exit(1);
    }
}
