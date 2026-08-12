//! Sensitivity ranking for `klt yield-sensitivity`: attribute an output
//! metric's variance across a completed campaign to the device/process
//! parameters that were drawn per sample.
//!
//! Issue #923, Phase 3 of the statistical/yield epic
//! ([#710](https://github.com/2AMLogic/klayout-tools/issues/710)). Phases 1a/1b
//! (`native/yield/src/estimate.rs`) turn a sample set into a yield estimate;
//! this module answers a different question over the *same kind* of
//! campaign data -- not "does it pass", but "which parameter draws actually
//! moved the number".
//!
//! **Method: correlation/regression-based, not variance-based.** Per issue
//! #923's own scope note, a full Sobol/variance-based decomposition is a
//! heavier, sample-hungrier method deliberately deferred; this module ranks
//! parameters by:
//!
//! - **Standardized regression coefficients** (primary, when solvable) --
//!   each parameter's coefficient in a multiple linear regression of the
//!   standardized output on every standardized parameter jointly, obtained
//!   by solving `R * beta = r` where `R` is the parameter-parameter
//!   correlation matrix and `r` is the vector of parameter-output
//!   correlations (the standard closed form for a regression on
//!   standardized variables -- Draper & Smith, *Applied Regression
//!   Analysis*, 3rd ed., ch. 6). This is a **linear, first-order** measure:
//!   it captures each parameter's contribution controlling for the others,
//!   but not interaction effects or curvature.
//! - **Pearson `r`** and **Spearman rank correlation** (`rho`), reported for
//!   every ranked parameter alongside the primary metric as single-parameter
//!   corroborating measures. Spearman also survives a monotonic but
//!   nonlinear response that would understate a Pearson/regression number.
//! - When the regression cannot be fit (too few samples relative to the
//!   parameter count, or a numerically singular parameter-correlation
//!   matrix -- e.g. two perfectly collinear parameters), ranking falls back
//!   to Pearson `r` alone, and `method.regression_solvable` says so.
//!
//! Dependency-free numerics throughout (`stats::solve_linear_system` is
//! plain Gaussian elimination), matching this crate's existing convention.

use crate::contract::{
    SensitivityMeasurementReport, SensitivityMeasurementRequest, SensitivityMethod,
    SensitivityRankingEntry, SensitivityRequest, SensitivityResponse, SENSITIVITY_MIN_SAMPLES,
    SENSITIVITY_SCHEMA_VERSION,
};
use crate::stats;

/// Run the full sensitivity ranking for a request, or return a
/// human-readable error.
pub fn analyze(request: &SensitivityRequest) -> Result<SensitivityResponse, String> {
    if request.measurements.is_empty() {
        return Err("no measurements to analyze".to_string());
    }

    let mut reports = Vec::with_capacity(request.measurements.len());
    for m in &request.measurements {
        reports.push(analyze_measurement(m)?);
    }

    Ok(SensitivityResponse {
        schema_version: SENSITIVITY_SCHEMA_VERSION,
        measurement_count: reports.len(),
        measurements: reports,
        warnings: Vec::new(),
    })
}

fn analyze_measurement(
    m: &SensitivityMeasurementRequest,
) -> Result<SensitivityMeasurementReport, String> {
    let n = m.output.len();
    if m.parameters.len() != n {
        return Err(format!(
            "measurement '{}': {} parameter draw(s) but {} output value(s) -- these must be \
             index-aligned, one parameter draw per output sample",
            m.name,
            m.parameters.len(),
            n
        ));
    }
    if n < SENSITIVITY_MIN_SAMPLES {
        return Err(format!(
            "measurement '{}': only {n} usable sample(s), but a ranking needs at least \
             {SENSITIVITY_MIN_SAMPLES} -- a correlation computed from fewer points is not a \
             ranking, it is noise",
            m.name
        ));
    }

    let names = parameter_names(m)?;

    let output = &m.output;
    if !has_spread(output) {
        return Err(format!(
            "measurement '{}': the output is constant across all {n} samples -- there is no \
             variance to attribute to any parameter",
            m.name
        ));
    }

    let mut warnings = Vec::new();
    let mut columns: Vec<(String, Vec<f64>)> = Vec::with_capacity(names.len());
    for name in &names {
        let col: Vec<f64> = m.parameters.iter().map(|draw| draw[name]).collect();
        if has_spread(&col) {
            columns.push((name.clone(), col));
        } else {
            warnings.push(format!(
                "parameter '{name}' is constant across all samples -- excluded from the \
                 ranking (nothing to correlate)"
            ));
        }
    }
    if columns.is_empty() {
        return Err(format!(
            "measurement '{}': every declared parameter is constant across all samples -- \
             nothing to rank",
            m.name
        ));
    }

    let p = columns.len();
    let pearson: Vec<f64> = columns
        .iter()
        .map(|(_, col)| stats::pearson_r(col, output).expect("has_spread checked above"))
        .collect();
    let spearman: Vec<f64> = columns
        .iter()
        .map(|(_, col)| stats::spearman_rho(col, output).expect("has_spread checked above"))
        .collect();

    let (std_coeffs, r_squared, regression_solvable, regression_note) =
        fit_standardized_regression(&columns, &pearson, n, p);

    if let Some(note) = &regression_note {
        warnings.push(format!(
            "standardized multiple regression was not solvable ({note}); ranking falls back to \
             single-parameter Pearson correlation, which does not control for correlation \
             between parameters"
        ));
    }

    let mut entries: Vec<SensitivityRankingEntry> = columns
        .iter()
        .enumerate()
        .map(|(i, (name, _))| {
            let coeff = std_coeffs.as_ref().map(|b| b[i]);
            SensitivityRankingEntry {
                rank: 0, // assigned below, after sorting
                parameter: name.clone(),
                contribution: coeff.unwrap_or(pearson[i]),
                standardized_coefficient: coeff,
                pearson_r: pearson[i],
                spearman_rho: spearman[i],
            }
        })
        .collect();
    entries.sort_by(|a, b| {
        b.contribution
            .abs()
            .partial_cmp(&a.contribution.abs())
            .unwrap()
            .then_with(|| a.parameter.cmp(&b.parameter))
    });
    for (i, entry) in entries.iter_mut().enumerate() {
        entry.rank = i + 1;
    }

    let method = build_method(regression_solvable);

    Ok(SensitivityMeasurementReport {
        name: m.name.clone(),
        unit: m.unit.clone(),
        n: n as u64,
        parameter_count: p,
        method,
        r_squared,
        ranking: entries,
        warnings,
    })
}

/// The parameter name set, taken from sample 0 and validated identical
/// (same key set) across every other sample -- a campaign where one sample
/// silently dropped a parameter is a data-quality error, not something to
/// paper over with a default.
fn parameter_names(m: &SensitivityMeasurementRequest) -> Result<Vec<String>, String> {
    let mut names: Vec<String> = m.parameters[0].keys().cloned().collect();
    names.sort();
    if names.is_empty() {
        return Err(format!(
            "measurement '{}': sample 0 declares no parameters",
            m.name
        ));
    }
    let name_refs: Vec<&String> = names.iter().collect();
    for (i, draw) in m.parameters.iter().enumerate() {
        let mut keys: Vec<&String> = draw.keys().collect();
        keys.sort();
        if keys != name_refs {
            return Err(format!(
                "measurement '{}': sample {i} declares a different parameter set than sample \
                 0 -- every sample must draw the same named parameters",
                m.name
            ));
        }
    }
    Ok(names)
}

fn has_spread(xs: &[f64]) -> bool {
    stats::sample_stddev(xs).map(|s| s > 0.0).unwrap_or(false)
}

/// Attempt the standardized-regression fit. Returns
/// `(coefficients, r_squared, solvable, failure_note)` -- `failure_note` is
/// `Some` exactly when `solvable` is `false`, describing why (for the
/// caller's warning message).
fn fit_standardized_regression(
    columns: &[(String, Vec<f64>)],
    pearson: &[f64],
    n: usize,
    p: usize,
) -> (Option<Vec<f64>>, Option<f64>, bool, Option<String>) {
    // At least one residual degree of freedom beyond the p coefficients
    // being estimated -- otherwise the fit is either impossible (n <= p) or
    // a guaranteed R^2 == 1 that reports nothing (n == p + 1).
    if n <= p + 1 {
        return (
            None,
            None,
            false,
            Some(format!(
                "only {n} usable sample(s) for {p} parameter(s) -- multiple regression needs \
                 more samples than parameters to be solvable"
            )),
        );
    }

    let mut r_matrix = vec![vec![0.0; p]; p];
    for i in 0..p {
        r_matrix[i][i] = 1.0;
        for j in (i + 1)..p {
            let r = stats::pearson_r(&columns[i].1, &columns[j].1).unwrap_or(0.0);
            r_matrix[i][j] = r;
            r_matrix[j][i] = r;
        }
    }

    match stats::solve_linear_system(&r_matrix, pearson) {
        Some(beta) => {
            let r2: f64 = beta.iter().zip(pearson.iter()).map(|(b, r)| b * r).sum();
            (Some(beta), Some(r2.clamp(0.0, 1.0)), true, None)
        }
        None => (
            None,
            None,
            false,
            Some(
                "the parameter-correlation matrix was numerically singular -- likely two or \
                 more perfectly (or near-perfectly) collinear parameters"
                    .to_string(),
            ),
        ),
    }
}

fn build_method(regression_solvable: bool) -> SensitivityMethod {
    let common_limitation = "linear/monotonic effects only -- captures neither interaction \
        effects between parameters nor curvature in the response. A full variance-based (Sobol) \
        decomposition would; issue #923 deliberately defers it as a heavier, sample-hungrier \
        method than this campaign size needs to justify"
        .to_string();
    let no_ci_limitation = "point estimates only -- no confidence interval on the ranking or its \
        coefficients (unlike `klt yield`'s yield/Cpk numbers). A close 1st/2nd contribution gap \
        can reorder under a different campaign seed; re-run with more samples before treating it \
        as decisive"
        .to_string();

    if regression_solvable {
        SensitivityMethod {
            primary: "standardized_regression_coefficient".to_string(),
            description: "standardized OLS regression coefficients: each parameter's \
                coefficient in a multiple linear regression of the (z-scored) output on every \
                (z-scored) parameter jointly, solved via the parameter-parameter correlation \
                matrix (the closed form for a regression on standardized variables -- Draper & \
                Smith, 'Applied Regression Analysis', 3rd ed., ch. 6). Unlike a bare pairwise \
                correlation, this controls for correlation with the other parameters. pearson_r \
                and spearman_rho are reported per parameter alongside it as single-parameter \
                corroborating measures."
                .to_string(),
            limitations: vec![
                common_limitation,
                "standardized coefficients can become unstable (large magnitude, unstable sign) \
                 under strong collinearity between parameters, even when each is individually \
                 well-behaved -- cross-check against pearson_r when parameters are known to be \
                 correlated"
                    .to_string(),
                no_ci_limitation,
            ],
            regression_solvable: true,
        }
    } else {
        SensitivityMethod {
            primary: "pearson_correlation".to_string(),
            description: "pearson_r (linear correlation) between the output and each parameter \
                taken one at a time. The standardized multiple regression could not be fit for \
                this sample set (see this measurement's warnings for why), so the ranking falls \
                back to this single-parameter measure. spearman_rho is reported alongside as a \
                monotonic (non-linear) corroborating measure."
                .to_string(),
            limitations: vec![
                common_limitation,
                "single-parameter correlation only -- does not control for correlation between \
                 parameters, so two correlated parameters can both rank highly even when only \
                 one actually drives the output"
                    .to_string(),
                no_ci_limitation,
            ],
            regression_solvable: false,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::BTreeMap;

    fn draw(pairs: &[(&str, f64)]) -> BTreeMap<String, f64> {
        pairs.iter().map(|(k, v)| (k.to_string(), *v)).collect()
    }

    /// A deterministic linear-response synthetic campaign: `y = 10*x1 + x2 +
    /// x3 + noise`, with `x1`, `x2`, `x3` independent (unit-spaced grids
    /// shuffled against each other so they are uncorrelated by
    /// construction) -- `x1`'s coefficient is exactly 10x the others', so
    /// it must surface first regardless of which corroborating metric is
    /// read. Mirrors issue #923's own acceptance criterion.
    fn dominant_parameter_campaign(n: usize) -> SensitivityMeasurementRequest {
        let mut parameters = Vec::with_capacity(n);
        let mut output = Vec::with_capacity(n);
        for i in 0..n {
            let t = i as f64;
            // Three independent-ish deterministic sequences (distinct
            // periods so they do not co-vary) standing in for three
            // mismatch-term draws.
            let x1 = (t * 0.7).sin();
            let x2 = (t * 1.9 + 1.0).sin();
            let x3 = (t * 3.3 + 2.0).sin();
            let y = 10.0 * x1 + 1.0 * x2 + 1.0 * x3;
            parameters.push(draw(&[("x1", x1), ("x2", x2), ("x3", x3)]));
            output.push(y);
        }
        SensitivityMeasurementRequest {
            name: "gain_db".to_string(),
            unit: Some("dB".to_string()),
            parameters,
            output,
        }
    }

    #[test]
    fn dominant_parameter_is_ranked_first_by_standardized_coefficient() {
        let report = analyze_measurement(&dominant_parameter_campaign(60)).unwrap();
        assert!(report.method.regression_solvable, "{:?}", report.warnings);
        assert_eq!(report.method.primary, "standardized_regression_coefficient");
        assert_eq!(report.ranking[0].parameter, "x1");
        assert_eq!(report.ranking[0].rank, 1);
        assert!(
            report.ranking[0].contribution.abs() > report.ranking[1].contribution.abs(),
            "{:?}",
            report.ranking
        );
        assert!(report.r_squared.unwrap() > 0.9, "{:?}", report.r_squared);
    }

    #[test]
    fn dominant_parameter_is_also_ranked_first_by_the_pearson_fallback() {
        // Same campaign, but with too few samples relative to the
        // parameter count for the regression to be solvable (n <= p + 1) --
        // the fallback must still surface the 10x term first.
        let mut request = dominant_parameter_campaign(4);
        request.parameters.truncate(4);
        request.output.truncate(4);
        let report = analyze_measurement(&request).unwrap();
        assert!(!report.method.regression_solvable);
        assert_eq!(report.method.primary, "pearson_correlation");
        assert_eq!(report.ranking[0].parameter, "x1");
        assert!(report.ranking[0].standardized_coefficient.is_none());
        assert!(!report.warnings.is_empty());
    }

    #[test]
    fn rejects_a_constant_output() {
        let mut request = dominant_parameter_campaign(10);
        for v in request.output.iter_mut() {
            *v = 42.0;
        }
        let err = analyze_measurement(&request).unwrap_err();
        assert!(err.contains("no variance to attribute"), "{err}");
    }

    #[test]
    fn excludes_a_constant_parameter_with_a_warning() {
        let mut request = dominant_parameter_campaign(10);
        for d in request.parameters.iter_mut() {
            d.insert("frozen".to_string(), 1.0);
        }
        let report = analyze_measurement(&request).unwrap();
        assert!(report.ranking.iter().all(|e| e.parameter != "frozen"));
        assert!(
            report.warnings.iter().any(|w| w.contains("'frozen'")),
            "{:?}",
            report.warnings
        );
    }

    #[test]
    fn rejects_a_sample_set_below_the_minimum() {
        let mut request = dominant_parameter_campaign(3);
        request.parameters.truncate(3);
        request.output.truncate(3);
        let err = analyze_measurement(&request).unwrap_err();
        assert!(err.contains("at least"), "{err}");
    }

    #[test]
    fn rejects_a_sample_with_a_mismatched_parameter_set() {
        let mut request = dominant_parameter_campaign(10);
        request.parameters[3].remove("x2");
        let err = analyze_measurement(&request).unwrap_err();
        assert!(err.contains("sample 3"), "{err}");
    }

    #[test]
    fn ranking_ties_break_alphabetically_by_parameter_name() {
        // Two parameters with identical (exact) correlation to the output
        // -- x2 == x3 as sequences, so their contribution is identical.
        let n = 12;
        let mut parameters = Vec::with_capacity(n);
        let mut output = Vec::with_capacity(n);
        for i in 0..n {
            let t = i as f64;
            let x = (t * 0.5).sin();
            parameters.push(draw(&[("bravo", x), ("alpha", x)]));
            output.push(x);
        }
        let request = SensitivityMeasurementRequest {
            name: "m".to_string(),
            unit: None,
            parameters,
            output,
        };
        let report = analyze_measurement(&request).unwrap();
        assert_eq!(report.ranking[0].parameter, "alpha");
        assert_eq!(report.ranking[1].parameter, "bravo");
    }
}
