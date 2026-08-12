//! Turn a Monte Carlo sample set + spec limits into a yield estimate that
//! **always** carries its confidence interval and sample count.
//!
//! Two estimates are reported side by side for every measurement, because
//! they fail in opposite directions and disagreeing is the useful signal:
//!
//! - **Empirical** (`method: "clopper-pearson"`) -- the fraction of samples
//!   inside the limits, with an exact binomial interval. Makes no
//!   distributional assumption at all, but cannot see past the samples it
//!   has: with zero observed failures it can only say "the yield is at least
//!   `low`", never resolve a `1e-6` tail.
//! - **Parametric normal** (`method: "normal-delta"`) -- the fitted normal's
//!   probability mass inside the limits, with a delta-method interval
//!   propagating the sampling variance of both `mu` and `sigma`. Extrapolates
//!   into the tail, but is only as good as the normality assumption --
//!   which is exactly why an Anderson-Darling verdict ships alongside it and
//!   a rejection raises a warning.
//!
//! Issue #816 (Phase 1a of the statistical/yield epic #710).

use crate::contract::{
    AnalyticCrossCheckReport, AnalyticCrossCheckRequest, Capability, Distribution, Estimate,
    Interval, Limits, MeasurementReport, MeasurementRequest, NegativeControlReport,
    NegativeControlRequest, Normality, SampleSize, SamplingReport, SamplingRequest,
    VarianceReducedSampleSize, YieldBlock, YieldRequest, YieldResponse, ABSOLUTE_MIN_SAMPLES,
    SCHEMA_VERSION,
};
use crate::stats;

/// Boltzmann constant, J/K (SI 2019 exact definition) -- the `kT/C` analytic
/// cross-check's only physical constant.
const BOLTZMANN_J_PER_K: f64 = 1.380_649e-23;

/// Run the full analysis for a request, or return a human-readable error.
pub fn analyze(request: &YieldRequest) -> Result<YieldResponse, String> {
    let confidence = request.confidence;
    if !(confidence > 0.0 && confidence < 1.0) {
        return Err(format!(
            "confidence must be strictly between 0 and 1 (got {confidence}); a confidence of \
             0 or 1 admits no finite interval, and `klt yield` does not emit a yield number \
             without one"
        ));
    }
    let target_ci_halfwidth = request.target_ci_halfwidth;
    if !(target_ci_halfwidth > 0.0 && target_ci_halfwidth < 0.5) {
        return Err(format!(
            "target_ci_halfwidth must be strictly between 0 and 0.5 (got {target_ci_halfwidth})"
        ));
    }
    let min_samples = request.min_samples.unwrap_or(ABSOLUTE_MIN_SAMPLES);
    if min_samples < ABSOLUTE_MIN_SAMPLES {
        return Err(format!(
            "min_samples must be at least {ABSOLUTE_MIN_SAMPLES} (got {min_samples}): a single \
             sample has no standard deviation and no confidence interval, so it can only \
             produce the bare point estimate this tool refuses to emit"
        ));
    }
    if request.measurements.is_empty() {
        return Err("no measurements to analyze".to_string());
    }

    let mut reports = Vec::with_capacity(request.measurements.len());
    for m in &request.measurements {
        let report = analyze_measurement(m, confidence, target_ci_halfwidth, min_samples)?;
        guard_no_bare_point_estimate(&report)?;
        reports.push(report);
    }

    let status = if reports.iter().any(|r| r.status == "fail") {
        "fail"
    } else if reports.iter().any(|r| r.status == "pass") {
        "pass"
    } else {
        "reported"
    };

    let mut warnings = Vec::new();
    if reports.iter().all(|r| r.limits.target_yield.is_none()) {
        warnings.push(
            "no measurement declared a target_yield, so no yield claim was checked -- the \
             estimates below are reported, never failed"
                .to_string(),
        );
    }
    // Issue #817: a campaign with no negative control at all is flagged --
    // "reported" would let a self-check-free campaign pass silently.
    if reports.iter().all(|r| r.negative_control.is_none()) {
        warnings.push(
            "no measurement declared a negative_control -- this campaign has no seeded, \
             known-bad variant demonstrating that the statistics above can actually detect a \
             degraded design; see docs/cli/yield.md#negative-control"
                .to_string(),
        );
    } else if reports
        .iter()
        .any(|r| matches!(&r.negative_control, Some(nc) if nc.verdict == "not_detected"))
    {
        warnings.push(
            "at least one measurement's negative_control did not show the expected \
             degradation -- see that measurement's own warnings"
                .to_string(),
        );
    }

    Ok(YieldResponse {
        schema_version: SCHEMA_VERSION,
        confidence,
        target_ci_halfwidth,
        min_samples,
        status: status.to_string(),
        measurement_count: reports.len(),
        measurements: reports,
        warnings,
    })
}

fn analyze_measurement(
    m: &MeasurementRequest,
    confidence: f64,
    target_ci_halfwidth: f64,
    min_samples: usize,
) -> Result<MeasurementReport, String> {
    let name = &m.name;
    if m.limits.min.is_none() && m.limits.max.is_none() {
        return Err(format!(
            "measurement '{name}' declares no spec limits (min and max are both absent): \
             there is no yield to estimate without a limit to estimate it against"
        ));
    }
    if let (Some(lo), Some(hi)) = (m.limits.min, m.limits.max) {
        if hi <= lo {
            return Err(format!(
                "measurement '{name}' has limits.max ({hi}) <= limits.min ({lo})"
            ));
        }
    }
    if let Some(t) = m.limits.target_yield {
        if !(t > 0.0 && t <= 1.0) {
            return Err(format!(
                "measurement '{name}' has target_yield {t}, which must be in (0, 1]"
            ));
        }
    }
    if m.samples.iter().any(|s| !s.is_finite()) {
        return Err(format!(
            "measurement '{name}' has a non-finite sample value (NaN or infinity)"
        ));
    }
    if let Some(check) = &m.analytic_cross_check {
        validate_analytic_cross_check(check, name)?;
    }
    if let Some(sampling) = &m.sampling {
        validate_sampling(sampling, m.samples.len(), name)?;
    }

    let n = m.samples.len();
    if n < min_samples {
        return Err(format!(
            "measurement '{name}' has {n} usable sample(s), below the minimum of {min_samples}: \
             refusing to report a yield number that cannot carry a confidence interval \
             (see docs/cli/yield.md, 'Never a bare point estimate')"
        ));
    }

    let mut sorted = m.samples.clone();
    sorted.sort_by(|a, b| {
        a.partial_cmp(b)
            .expect("non-finite sample already rejected")
    });

    let mean = stats::mean(&sorted);
    let stddev = stats::sample_stddev(&sorted).expect("n >= 2 checked above");
    let n_u = n as u64;
    let mut warnings: Vec<String> = Vec::new();

    // ---- distribution fit ------------------------------------------------ //
    let normality = normality_verdict(&sorted, n, &mut warnings, name);
    let distribution = Distribution {
        model: "normal".to_string(),
        mean,
        stddev,
        min: sorted[0],
        max: sorted[n - 1],
        median: stats::quantile_sorted(&sorted, 0.5),
        skewness: stats::skewness(&sorted),
        excess_kurtosis: stats::excess_kurtosis(&sorted),
        normality,
    };

    // ---- empirical yield ------------------------------------------------- //
    let passing = sorted.iter().filter(|x| within(**x, &m.limits)).count() as u64;
    let (cp_low, cp_high) = stats::clopper_pearson(passing, n_u, confidence);
    let empirical = Estimate {
        method: "clopper-pearson".to_string(),
        estimate: passing as f64 / n as f64,
        confidence,
        confidence_interval: Interval {
            low: cp_low,
            high: cp_high,
        },
        n: n_u,
    };
    if passing == n_u {
        warnings.push(format!(
            "measurement '{name}': no sample fell outside the limits, so the empirical yield is \
             bounded only from below -- \"at least {:.4}% at {:.0}% confidence, N = {n}\" is the \
             honest statement, not \"100% yield\"",
            cp_low * 100.0,
            confidence * 100.0
        ));
    }

    // ---- parametric (normal) yield --------------------------------------- //
    let normal = if stddev > 0.0 {
        Some(normal_estimate(mean, stddev, n, &m.limits, confidence))
    } else {
        warnings.push(format!(
            "measurement '{name}': every sample has the same value, so the normal fit, Cpk and \
             sigma-to-spec are undefined and are reported as null"
        ));
        None
    };

    // ---- capability ------------------------------------------------------ //
    let capability = capability(mean, stddev, n, &m.limits, confidence);

    // ---- sampling strategy + variance-reduced yield (issue #907) --------- //
    // Deliberately built from `m.samples` in its *original* (unsorted) order,
    // not `sorted` above: a Latin-hypercube design's replicate membership and
    // an importance sample's weight are positional, and sorting would scatter
    // both across replicates/weights that no longer correspond to anything.
    let (sampling, variance_reduced) = match &m.sampling {
        None | Some(SamplingRequest::PlainRandom) => (
            SamplingReport {
                strategy: "plain_random".to_string(),
                replicates: None,
                effective_sample_size: None,
            },
            None,
        ),
        Some(SamplingRequest::LatinHypercube { replicates }) => (
            SamplingReport {
                strategy: "latin_hypercube".to_string(),
                replicates: Some(*replicates),
                effective_sample_size: None,
            },
            Some(lhs_replicated_estimate(
                &m.samples,
                *replicates,
                &m.limits,
                confidence,
            )),
        ),
        Some(SamplingRequest::Importance { weights }) => {
            let ess = effective_sample_size(weights);
            if ess < 0.05 * n as f64 {
                warnings.push(format!(
                    "measurement '{name}': importance sampling weights are highly concentrated \
                     (effective sample size {ess:.1} of N = {n}) -- the reweighted estimate has \
                     much less statistical power than N draws would suggest; consider a proposal \
                     distribution closer to the target"
                ));
            }
            (
                SamplingReport {
                    strategy: "importance".to_string(),
                    replicates: None,
                    effective_sample_size: Some(ess),
                },
                Some(importance_estimate(
                    &m.samples, weights, &m.limits, confidence,
                )),
            )
        }
    };
    let variance_reduced_sample_size = variance_reduced
        .as_ref()
        .map(|e| variance_reduced_sample_size(e, target_ci_halfwidth));

    // ---- sample-size verdict --------------------------------------------- //
    let sample_size = sample_size_verdict(
        passing,
        n_u,
        confidence,
        target_ci_halfwidth,
        m.limits.target_yield,
        empirical.confidence_interval,
    );
    let sample_size = SampleSize {
        variance_reduced: variance_reduced_sample_size,
        ..sample_size
    };
    // Scoped to the *precision* half of the verdict specifically: the verdict
    // can also be "insufficient" purely because the claim needs more samples,
    // which the separate warning below states in its own terms.
    if sample_size.observed_ci_halfwidth > target_ci_halfwidth || n_u < sample_size.required_n {
        warnings.push(format!(
            "measurement '{name}': N = {n} resolves the yield only to +/-{:.4} at {:.0}% \
             confidence; {} samples are needed for the requested +/-{target_ci_halfwidth}",
            sample_size.observed_ci_halfwidth,
            confidence * 100.0,
            sample_size.required_n
        ));
    }
    if let Some(required) = sample_size.required_n_for_target {
        if n_u < required {
            warnings.push(format!(
                "measurement '{name}': the observed pass rate clears the {} target, but N = {n} \
                 is too small to *claim* it at {:.0}% confidence (the lower bound is {:.6}); \
                 {required} samples at this rate would support the claim",
                m.limits.target_yield.unwrap_or(f64::NAN),
                confidence * 100.0,
                cp_low
            ));
        }
    } else if let Some(target) = m.limits.target_yield {
        if empirical.estimate <= target {
            warnings.push(format!(
                "measurement '{name}': the observed pass rate ({:.6}) is at or below the {target} \
                 target, so no sample count can support the claim -- this is the design falling \
                 short, not the campaign",
                empirical.estimate
            ));
        }
    }
    if m.errored > 0 {
        warnings.push(format!(
            "measurement '{name}': {} sample(s) produced no usable value and were excluded from \
             every statistic below",
            m.errored
        ));
    }
    if m.source_corners.len() > 1 {
        warnings.push(format!(
            "measurement '{name}': samples were pooled across {} originating corners; each \
             corner is its own population, so the pooled estimate is a worst-case envelope \
             rather than a distribution anyone sampled",
            m.source_corners.len()
        ));
    }

    // ---- negative control (issue #817) ------------------------------------ //
    let negative_control = match &m.negative_control {
        Some(nc) => {
            let (report, warning) =
                negative_control_report(name, nc, &m.limits, confidence, min_samples, &empirical)?;
            if let Some(w) = warning {
                warnings.push(w);
            }
            Some(report)
        }
        None => None,
    };

    // ---- analytic cross-check (issue #817) --------------------------------- //
    let analytic_cross_check = m.analytic_cross_check.as_ref().map(|check| {
        let report = analytic_cross_check_report(check, mean, stddev, n, confidence);
        if report.verdict == "inconsistent" {
            warnings.push(format!(
                "measurement '{name}': the empirical distribution (mean={:.6}, stddev={:.6}) is \
                 not consistent with the {} analytic model's prediction (mean={:.6}, \
                 stddev={:.6}) at {:.0}% confidence -- relative stddev delta {:+.2}%",
                mean,
                stddev,
                report.kind,
                report.analytic_mean,
                report.analytic_stddev,
                confidence * 100.0,
                report.stddev_relative_delta * 100.0
            ));
        }
        report
    });

    // ---- verdict --------------------------------------------------------- //
    let status = match m.limits.target_yield {
        None => "reported",
        Some(target) => {
            if empirical.confidence_interval.low >= target {
                "pass"
            } else {
                "fail"
            }
        }
    };

    Ok(MeasurementReport {
        name: name.clone(),
        unit: m.unit.clone(),
        n: n_u,
        errored: m.errored,
        limits: m.limits,
        source_corners: m.source_corners.clone(),
        distribution,
        yield_: YieldBlock {
            empirical,
            normal,
            variance_reduced,
        },
        capability,
        negative_control,
        analytic_cross_check,
        sampling,
        sample_size,
        status: status.to_string(),
        warnings,
    })
}

fn within(x: f64, limits: &Limits) -> bool {
    limits.min.map(|lo| x >= lo).unwrap_or(true) && limits.max.map(|hi| x <= hi).unwrap_or(true)
}

fn normality_verdict(
    sorted: &[f64],
    n: usize,
    warnings: &mut Vec<String>,
    name: &str,
) -> Normality {
    let statistic = stats::anderson_darling_normal(sorted);
    let verdict = match statistic {
        None => "degenerate",
        Some(_) if n < stats::AD_MIN_SAMPLES => "insufficient_samples",
        Some(a2) if a2 > stats::AD_CRITICAL_VALUE_5PCT => "rejected",
        Some(_) => "consistent",
    };
    if verdict == "rejected" {
        warnings.push(format!(
            "measurement '{name}': the sample set is not consistent with a normal distribution \
             (Anderson-Darling A2* = {:.4} > {:.3}); treat the parametric yield estimate and \
             Cpk as indicative only and prefer the empirical estimate",
            statistic.unwrap_or(f64::NAN),
            stats::AD_CRITICAL_VALUE_5PCT
        ));
    } else if verdict == "insufficient_samples" {
        warnings.push(format!(
            "measurement '{name}': N = {n} is below the {} samples the Anderson-Darling critical \
             values assume, so normality was not tested",
            stats::AD_MIN_SAMPLES
        ));
    }
    Normality {
        test: "anderson-darling".to_string(),
        statistic,
        critical_value: stats::AD_CRITICAL_VALUE_5PCT,
        significance: 0.05,
        verdict: verdict.to_string(),
    }
}

/// `phi(z)`, treating an infinite `z` (a one-sided spec) as contributing
/// nothing rather than producing `inf * 0 = NaN`.
fn phi(z: f64) -> f64 {
    if z.is_finite() {
        stats::norm_pdf(z)
    } else {
        0.0
    }
}

/// `z * phi(z)`, with the same infinite-`z` guard as [`phi`].
fn z_phi(z: f64) -> f64 {
    if z.is_finite() {
        z * stats::norm_pdf(z)
    } else {
        0.0
    }
}

/// The fitted normal's yield, with a delta-method confidence interval.
///
/// `Y = Phi(z_u) - Phi(z_l)` with `z_l = (LSL - mu) / sigma`,
/// `z_u = (USL - mu) / sigma`. Propagating the (asymptotically independent)
/// sampling variances `Var(mu_hat) = sigma^2 / n` and
/// `Var(sigma_hat) = sigma^2 / (2n)`:
///
/// ```text
/// dY/dmu    = (phi(z_l) - phi(z_u)) / sigma
/// dY/dsigma = (z_l phi(z_l) - z_u phi(z_u)) / sigma
/// Var(Y)    = (dY/dmu)^2 sigma^2/n + (dY/dsigma)^2 sigma^2/(2n)
/// ```
///
/// The interval is clamped to `[0, 1]` (a probability cannot leave it); the
/// clamp is documented in `docs/cli/yield.md` rather than hidden, since a
/// clamped endpoint is a signal that the normal approximation to the
/// *interval* is being pushed past where it is informative.
fn normal_estimate(mean: f64, stddev: f64, n: usize, limits: &Limits, confidence: f64) -> Estimate {
    let z_l = limits
        .min
        .map(|lo| (lo - mean) / stddev)
        .unwrap_or(f64::NEG_INFINITY);
    let z_u = limits
        .max
        .map(|hi| (hi - mean) / stddev)
        .unwrap_or(f64::INFINITY);

    let y = stats::norm_cdf(z_u) - stats::norm_cdf(z_l);
    let d_mu = (phi(z_l) - phi(z_u)) / stddev;
    let d_sigma = (z_phi(z_l) - z_phi(z_u)) / stddev;
    let n_f = n as f64;
    let var =
        d_mu * d_mu * stddev * stddev / n_f + d_sigma * d_sigma * stddev * stddev / (2.0 * n_f);
    let z = stats::norm_ppf(0.5 + confidence / 2.0);
    let half = z * var.sqrt();

    Estimate {
        method: "normal-delta".to_string(),
        estimate: y.clamp(0.0, 1.0),
        confidence,
        confidence_interval: Interval {
            low: (y - half).clamp(0.0, 1.0),
            high: (y + half).clamp(0.0, 1.0),
        },
        n: n as u64,
    }
}

/// Cp / Cpk / sigma-to-spec, with Bissell's normal-approximation interval
/// for Cpk: `Cpk +/- z * sqrt(1/(9n) + Cpk^2 / (2(n-1)))`.
fn capability(mean: f64, stddev: f64, n: usize, limits: &Limits, confidence: f64) -> Capability {
    if stddev <= 0.0 {
        return Capability {
            cp: None,
            cpk: None,
            cpk_confidence_interval: None,
            sigma_to_spec: None,
            sigma_to_spec_confidence_interval: None,
            limiting_side: None,
        };
    }
    let cp = match (limits.min, limits.max) {
        (Some(lo), Some(hi)) => Some((hi - lo) / (6.0 * stddev)),
        _ => None,
    };
    let cpl = limits.min.map(|lo| (mean - lo) / (3.0 * stddev));
    let cpu = limits.max.map(|hi| (hi - mean) / (3.0 * stddev));
    let (cpk, side) = match (cpl, cpu) {
        (Some(l), Some(u)) => {
            if l <= u {
                (Some(l), Some("lower"))
            } else {
                (Some(u), Some("upper"))
            }
        }
        (Some(l), None) => (Some(l), Some("lower")),
        (None, Some(u)) => (Some(u), Some("upper")),
        (None, None) => (None, None),
    };

    let z = stats::norm_ppf(0.5 + confidence / 2.0);
    let (cpk_ci, sigma_ci) = match cpk {
        Some(c) => {
            let se = (1.0 / (9.0 * n as f64) + c * c / (2.0 * (n as f64 - 1.0))).sqrt();
            let ci = Interval {
                low: c - z * se,
                high: c + z * se,
            };
            (
                Some(ci),
                Some(Interval {
                    low: 3.0 * ci.low,
                    high: 3.0 * ci.high,
                }),
            )
        }
        None => (None, None),
    };

    Capability {
        cp,
        cpk,
        cpk_confidence_interval: cpk_ci,
        sigma_to_spec: cpk.map(|c| 3.0 * c),
        sigma_to_spec_confidence_interval: sigma_ci,
        limiting_side: side.map(str::to_string),
    }
}

/// The largest sample count [`required_n_for_target`] will search up to
/// before giving up and reporting `null`. A campaign needing more than ten
/// million samples is not a sample-size problem a verdict can usefully state.
const MAX_SEARCHED_N: u64 = 10_000_000;

/// "Is N large enough?", answered twice -- for precision and for the claim.
///
/// **`required_n` (precision)** -- how many samples the interval needs to
/// reach `target_ci_halfwidth`. Two regimes, because the usual
/// `n = z^2 p(1-p) / h^2` formula collapses to `n = 0` exactly where a yield
/// campaign usually lands (`p_hat == 1`, zero observed failures):
///
/// - **Zero observed failures** (`k == n`, or symmetrically `k == 0`): the
///   exact Clopper-Pearson interval is `[(alpha/2)^(1/n), 1]`, so requiring a
///   half-width `<= h` gives `n >= ln(alpha/2) / ln(1 - 2h)`.
/// - **Otherwise**: the standard normal-approximation sample size at the
///   observed proportion.
///
/// **`required_n_for_target` (the claim)** -- how many samples the *lower*
/// bound needs to reach the declared `target_yield`. Searched directly
/// against the same exact interval the report publishes, rather than through
/// a normal approximation, so the two can never disagree.
fn sample_size_verdict(
    passing: u64,
    n: u64,
    confidence: f64,
    target: f64,
    target_yield: Option<f64>,
    observed: Interval,
) -> SampleSize {
    let alpha = 1.0 - confidence;
    let p_hat = passing as f64 / n as f64;
    let (required, method) = if passing == n || passing == 0 {
        let required = (alpha / 2.0).ln() / (1.0 - 2.0 * target).ln();
        (
            required.ceil().max(ABSOLUTE_MIN_SAMPLES as f64) as u64,
            "clopper-pearson-zero-failures",
        )
    } else {
        let z = stats::norm_ppf(0.5 + confidence / 2.0);
        let required = z * z * p_hat * (1.0 - p_hat) / (target * target);
        (
            required.ceil().max(ABSOLUTE_MIN_SAMPLES as f64) as u64,
            "normal-approximation",
        )
    };
    let required_for_target =
        target_yield.and_then(|t| required_n_for_target(p_hat, confidence, t));
    let observed_half = observed.half_width();
    let precision_ok = observed_half <= target && n >= required;
    let claim_ok = required_for_target.map(|r| n >= r).unwrap_or(true);
    SampleSize {
        n,
        observed_ci_halfwidth: observed_half,
        target_ci_halfwidth: target,
        required_n: required,
        required_n_for_target: required_for_target,
        verdict: if precision_ok && claim_ok {
            "sufficient".to_string()
        } else {
            "insufficient".to_string()
        },
        method: method.to_string(),
        // Filled in by the caller (`analyze_measurement`), which knows
        // whether a `variance_reduced` yield estimate exists at all.
        variance_reduced: None,
    }
}

/// Smallest `n` whose exact lower bound at the observed pass rate reaches
/// `target_yield`, or `None` when the observed rate is at/below the target
/// (unreachable at any `n`) or the search exceeds [`MAX_SEARCHED_N`].
fn required_n_for_target(p_hat: f64, confidence: f64, target_yield: f64) -> Option<u64> {
    if p_hat <= target_yield {
        return None;
    }
    let reaches = |n: u64| -> bool {
        // Round the observed rate back to a whole pass count at this `n` --
        // the interval is only defined on integer counts.
        let k = (p_hat * n as f64).round().min(n as f64) as u64;
        stats::clopper_pearson(k, n, confidence).0 >= target_yield
    };
    let mut hi = ABSOLUTE_MIN_SAMPLES as u64;
    while !reaches(hi) {
        hi = hi.saturating_mul(2);
        if hi > MAX_SEARCHED_N {
            return None;
        }
    }
    let mut lo = ABSOLUTE_MIN_SAMPLES as u64;
    while lo < hi {
        let mid = lo + (hi - lo) / 2;
        if reaches(mid) {
            hi = mid;
        } else {
            lo = mid + 1;
        }
    }
    Some(lo)
}

// --------------------------------------------------------------------------- //
// Variance-reduction sampling strategies (issue #907, Phase 2b of epic #710)
// --------------------------------------------------------------------------- //

/// Validate a [`SamplingRequest`] against the measurement's sample count
/// before any arithmetic runs on it -- an uneven LHS block split or a
/// mismatched weights array would otherwise surface as a silent panic or a
/// `NaN` deep in the report.
fn validate_sampling(sampling: &SamplingRequest, n: usize, name: &str) -> Result<(), String> {
    match sampling {
        SamplingRequest::PlainRandom => Ok(()),
        SamplingRequest::LatinHypercube { replicates } => {
            let r = *replicates as usize;
            if r < 2 {
                return Err(format!(
                    "measurement '{name}' sampling.replicates must be at least 2 (got \
                     {replicates}): the replicated-LHS estimator needs at least two independent \
                     LHS designs to have its own across-replicate spread, exactly like the plain \
                     sample count floor (see docs/cli/yield.md, 'Never a bare point estimate')"
                ));
            }
            if r == 0 || !n.is_multiple_of(r) {
                return Err(format!(
                    "measurement '{name}' has {n} samples, not evenly divisible by \
                     sampling.replicates ({r}): samples must be {r} consecutive equal-sized LHS \
                     designs, in that order"
                ));
            }
            if n / r < 1 {
                return Err(format!(
                    "measurement '{name}' has too few samples ({n}) for {r} replicates"
                ));
            }
            Ok(())
        }
        SamplingRequest::Importance { weights } => {
            if weights.len() != n {
                return Err(format!(
                    "measurement '{name}' sampling.weights has {} entries, but samples has {n} \
                     -- importance sampling needs exactly one weight per sample, in the same \
                     order",
                    weights.len()
                ));
            }
            if weights.iter().any(|w| !w.is_finite() || *w <= 0.0) {
                return Err(format!(
                    "measurement '{name}' sampling.weights must all be finite and positive"
                ));
            }
            Ok(())
        }
    }
}

/// Kish's effective sample size for a set of importance weights:
/// `(sum w)^2 / sum(w^2)` -- see [`crate::contract::SamplingReport::effective_sample_size`].
fn effective_sample_size(weights: &[f64]) -> f64 {
    let sum: f64 = weights.iter().sum();
    let sum_sq: f64 = weights.iter().map(|w| w * w).sum();
    if sum_sq <= 0.0 {
        0.0
    } else {
        sum * sum / sum_sq
    }
}

/// The importance-weighted (Horvitz-Thompson) yield estimate: the weighted
/// fraction of `samples` inside `limits`, reweighted by `weights` back to the
/// target distribution the proposal was biased away from.
///
/// The interval is a normal-approximation delta-method interval on the ratio
/// estimator -- `Var(Y_hat) ~= sum(w_i^2 (I_i - Y_hat)^2) / (sum w_i)^2`, the
/// standard sandwich/robust variance for a self-normalized importance-
/// sampling estimator (Hesterberg, "Weighted Average Importance Sampling and
/// Defensive Mixture Distributions", Technometrics 1995) -- consistent with
/// every other non-exact interval in this crate (`normal_estimate`,
/// `capability`) using a `z` multiplier rather than an exact form.
fn importance_estimate(
    samples: &[f64],
    weights: &[f64],
    limits: &Limits,
    confidence: f64,
) -> Estimate {
    let indicators: Vec<f64> = samples
        .iter()
        .map(|x| if within(*x, limits) { 1.0 } else { 0.0 })
        .collect();
    let sum_w: f64 = weights.iter().sum();
    let y_hat = weights
        .iter()
        .zip(&indicators)
        .map(|(w, i)| w * i)
        .sum::<f64>()
        / sum_w;
    let var = weights
        .iter()
        .zip(&indicators)
        .map(|(w, i)| {
            let d = i - y_hat;
            w * w * d * d
        })
        .sum::<f64>()
        / (sum_w * sum_w);
    let z = stats::norm_ppf(0.5 + confidence / 2.0);
    let half = z * var.max(0.0).sqrt();
    Estimate {
        method: "importance-weighted".to_string(),
        estimate: y_hat.clamp(0.0, 1.0),
        confidence,
        confidence_interval: Interval {
            low: (y_hat - half).clamp(0.0, 1.0),
            high: (y_hat + half).clamp(0.0, 1.0),
        },
        n: samples.len() as u64,
    }
}

/// The replicated-Latin-Hypercube yield estimate: `samples` is `replicates`
/// consecutive equal-sized LHS designs; each replicate's own empirical pass
/// fraction is an independent, unbiased estimate of the yield (McKay,
/// Conover & Beckman, "A Comparison of Three Methods for Selecting Values of
/// Input Variables in the Analysis of Output from a Computer Code",
/// Technometrics 1979), so the mean and sample standard deviation *across*
/// replicates -- not the plain within-sample Clopper-Pearson formula, which
/// assumes iid draws LHS deliberately is not -- is what actually captures the
/// variance the design removed.
fn lhs_replicated_estimate(
    samples: &[f64],
    replicates: u32,
    limits: &Limits,
    confidence: f64,
) -> Estimate {
    let r = replicates as usize;
    let block = samples.len() / r;
    let proportions: Vec<f64> = samples
        .chunks(block)
        .map(|chunk| {
            let passing = chunk.iter().filter(|x| within(**x, limits)).count();
            passing as f64 / chunk.len() as f64
        })
        .collect();
    let mean_p = stats::mean(&proportions);
    let se = stats::sample_stddev(&proportions)
        .expect("validate_sampling already checked replicates >= 2")
        / (r as f64).sqrt();
    let z = stats::norm_ppf(0.5 + confidence / 2.0);
    let half = z * se;
    Estimate {
        method: "lhs-replicated".to_string(),
        estimate: mean_p.clamp(0.0, 1.0),
        confidence,
        confidence_interval: Interval {
            low: (mean_p - half).clamp(0.0, 1.0),
            high: (mean_p + half).clamp(0.0, 1.0),
        },
        n: samples.len() as u64,
    }
}

/// The precision half of the sample-size verdict, recomputed from a
/// strategy-aware `variance_reduced` estimate instead of the plain empirical
/// interval -- see [`crate::contract::VarianceReducedSampleSize`].
fn variance_reduced_sample_size(estimate: &Estimate, target: f64) -> VarianceReducedSampleSize {
    let half = estimate.confidence_interval.half_width();
    VarianceReducedSampleSize {
        observed_ci_halfwidth: half,
        target_ci_halfwidth: target,
        verdict: if half <= target {
            "sufficient"
        } else {
            "insufficient"
        }
        .to_string(),
    }
}

/// Validate an [`AnalyticCrossCheckRequest`]'s physical parameters before any
/// arithmetic runs on them -- a non-positive capacitance, temperature, or
/// sigma would otherwise surface as a silent `NaN` deep in the report.
fn validate_analytic_cross_check(
    check: &AnalyticCrossCheckRequest,
    name: &str,
) -> Result<(), String> {
    match check {
        AnalyticCrossCheckRequest::KtcNoise {
            capacitance_f,
            temperature_k,
            ..
        } => {
            if *capacitance_f <= 0.0 {
                return Err(format!(
                    "measurement '{name}' analytic_cross_check.capacitance_f must be positive \
                     (got {capacitance_f})"
                ));
            }
            if *temperature_k <= 0.0 {
                return Err(format!(
                    "measurement '{name}' analytic_cross_check.temperature_k must be positive \
                     (got {temperature_k})"
                ));
            }
        }
        AnalyticCrossCheckRequest::MismatchOffset { sigma, .. } => {
            if *sigma <= 0.0 {
                return Err(format!(
                    "measurement '{name}' analytic_cross_check.sigma must be positive (got \
                     {sigma})"
                ));
            }
        }
    }
    Ok(())
}

/// The analytic model's prediction against this measurement's own empirical
/// mean/stddev, with confidence intervals for both (the same asymptotic
/// approximations `normal_estimate`'s delta method already relies on:
/// `Var(mu_hat) = sigma^2/n`, `Var(sigma_hat) = sigma^2/(2n)`).
///
/// `"consistent"` requires **both** the analytic mean and the analytic
/// stddev to fall inside their respective empirical confidence intervals --
/// either one alone would let a noise-floor-only or offset-only model pass
/// a check it should fail.
fn analytic_cross_check_report(
    check: &AnalyticCrossCheckRequest,
    mean: f64,
    stddev: f64,
    n: usize,
    confidence: f64,
) -> AnalyticCrossCheckReport {
    let (kind, analytic_mean, analytic_stddev) = match check {
        AnalyticCrossCheckRequest::KtcNoise {
            capacitance_f,
            temperature_k,
            analytic_mean,
        } => {
            let sigma = (BOLTZMANN_J_PER_K * temperature_k / capacitance_f).sqrt();
            ("ktc_noise".to_string(), analytic_mean.unwrap_or(0.0), sigma)
        }
        AnalyticCrossCheckRequest::MismatchOffset { sigma, mean } => {
            ("mismatch_offset".to_string(), mean.unwrap_or(0.0), *sigma)
        }
    };

    let n_f = n as f64;
    let z = stats::norm_ppf(0.5 + confidence / 2.0);
    let se_mean = stddev / n_f.sqrt();
    let se_stddev = stddev / (2.0 * n_f).sqrt();
    let mean_ci = Interval {
        low: mean - z * se_mean,
        high: mean + z * se_mean,
    };
    let stddev_ci = Interval {
        low: (stddev - z * se_stddev).max(0.0),
        high: stddev + z * se_stddev,
    };

    let mean_ok = analytic_mean >= mean_ci.low && analytic_mean <= mean_ci.high;
    let stddev_ok = analytic_stddev >= stddev_ci.low && analytic_stddev <= stddev_ci.high;

    AnalyticCrossCheckReport {
        kind,
        analytic_mean,
        analytic_stddev,
        empirical_mean: mean,
        empirical_stddev: stddev,
        empirical_stddev_confidence_interval: stddev_ci,
        empirical_mean_confidence_interval: mean_ci,
        stddev_relative_delta: if analytic_stddev != 0.0 {
            (stddev - analytic_stddev) / analytic_stddev
        } else {
            f64::NAN
        },
        mean_delta: mean - analytic_mean,
        verdict: if mean_ok && stddev_ok {
            "consistent"
        } else {
            "inconsistent"
        }
        .to_string(),
    }
}

/// Analyze the negative control's own samples against the nominal
/// measurement's limits, and decide whether the deliberate defect actually
/// shows up as a **statistically distinguishable** degradation -- not merely
/// a lower point estimate, which sampling noise alone could produce.
///
/// Returns the report plus an optional warning (when the degradation was not
/// detected). Errors exactly like the nominal measurement does: a
/// non-finite sample, or fewer than `min_samples` usable draws (issue #816's
/// "never a bare point estimate" rule applies here too -- a negative control
/// with no interval of its own could not support this comparison).
#[allow(clippy::too_many_arguments)]
fn negative_control_report(
    name: &str,
    nc: &NegativeControlRequest,
    limits: &Limits,
    confidence: f64,
    min_samples: usize,
    nominal_empirical: &Estimate,
) -> Result<(NegativeControlReport, Option<String>), String> {
    if nc.samples.iter().any(|s| !s.is_finite()) {
        return Err(format!(
            "measurement '{name}' negative_control has a non-finite sample value (NaN or \
             infinity)"
        ));
    }
    let n = nc.samples.len();
    if n < min_samples {
        return Err(format!(
            "measurement '{name}' negative_control has {n} usable sample(s), below the minimum \
             of {min_samples}: it needs its own confidence interval to be checkable, exactly \
             like the nominal measurement (see docs/cli/yield.md, 'Never a bare point estimate')"
        ));
    }
    let n_u = n as u64;
    let mut sorted = nc.samples.clone();
    sorted.sort_by(|a, b| {
        a.partial_cmp(b)
            .expect("non-finite sample already rejected")
    });
    let mean = stats::mean(&sorted);
    let stddev = stats::sample_stddev(&sorted).expect("n >= 2 checked above");

    let passing = sorted.iter().filter(|x| within(**x, limits)).count() as u64;
    let (cp_low, cp_high) = stats::clopper_pearson(passing, n_u, confidence);
    let empirical = Estimate {
        method: "clopper-pearson".to_string(),
        estimate: passing as f64 / n as f64,
        confidence,
        confidence_interval: Interval {
            low: cp_low,
            high: cp_high,
        },
        n: n_u,
    };
    let normal = if stddev > 0.0 {
        Some(normal_estimate(mean, stddev, n, limits, confidence))
    } else {
        None
    };

    // Non-overlapping exact intervals, in the degraded direction: sampling
    // noise alone cannot produce this, so it is the signal the self-check
    // exists to require.
    let degradation_detected = empirical.estimate < nominal_empirical.estimate
        && empirical.confidence_interval.high < nominal_empirical.confidence_interval.low;
    let verdict = if degradation_detected {
        "detected"
    } else {
        "not_detected"
    };

    let warning = if degradation_detected {
        None
    } else {
        Some(format!(
            "measurement '{name}': the negative_control's empirical yield ({:.6}, CI [{:.6}, \
             {:.6}]) is not statistically distinguishable from the nominal draw's ({:.6}, CI \
             [{:.6}, {:.6}]) -- the deliberate defect did not show up as the degradation the \
             statistics claim to detect",
            empirical.estimate,
            empirical.confidence_interval.low,
            empirical.confidence_interval.high,
            nominal_empirical.estimate,
            nominal_empirical.confidence_interval.low,
            nominal_empirical.confidence_interval.high
        ))
    };

    Ok((
        NegativeControlReport {
            description: nc.description.clone(),
            n: n_u,
            errored: nc.errored,
            // A negative control's own samples never declare a sampling
            // strategy in this phase -- it is always analysed as a plain
            // draw against the nominal measurement's limits.
            yield_: YieldBlock {
                empirical,
                normal,
                variance_reduced: None,
            },
            nominal_empirical_estimate: nominal_empirical.estimate,
            degradation_detected,
            verdict: verdict.to_string(),
        },
        warning,
    ))
}

/// The runtime half of issue #816's "never a bare point estimate" rule.
///
/// The response *types* already make an interval-less estimate
/// unrepresentable (`Estimate` has no optional-interval variant), but a
/// non-finite or inverted interval would be a bare number wearing an
/// interval's clothes. This rejects that before anything is serialised, so
/// the failure is a clean error rather than a published yield number nobody
/// can check.
pub fn guard_no_bare_point_estimate(report: &MeasurementReport) -> Result<(), String> {
    let check = |estimate: &Estimate, which: &str| -> Result<(), String> {
        if estimate.n == 0 {
            return Err(format!(
                "internal invariant violated: measurement '{}' would have emitted a {which} \
                 yield estimate with no sample count",
                report.name
            ));
        }
        if !estimate.confidence_interval.is_finite() {
            return Err(format!(
                "internal invariant violated: measurement '{}' would have emitted a {which} \
                 yield estimate ({}) without a usable confidence interval [{}, {}] -- `klt \
                 yield` does not publish a bare point estimate",
                report.name,
                estimate.estimate,
                estimate.confidence_interval.low,
                estimate.confidence_interval.high
            ));
        }
        Ok(())
    };
    check(&report.yield_.empirical, "empirical")?;
    if let Some(normal) = &report.yield_.normal {
        check(normal, "normal")?;
    }
    if let Some(variance_reduced) = &report.yield_.variance_reduced {
        check(variance_reduced, "variance-reduced")?;
    }
    if let Some(nc) = &report.negative_control {
        check(&nc.yield_.empirical, "negative-control empirical")?;
        if let Some(normal) = &nc.yield_.normal {
            check(normal, "negative-control normal")?;
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::contract::DEFAULT_TARGET_CI_HALFWIDTH;
    use crate::stats::norm_ppf;

    fn close(a: f64, b: f64, tol: f64) {
        assert!((a - b).abs() <= tol, "{a} != {b} (tol {tol})");
    }

    /// `n` inverse-CDF-spaced draws from `N(mu, sigma)` -- a deterministic
    /// stand-in for an infinite Monte Carlo campaign, with no RNG and no
    /// seed to pin. The empirical CDF of this set matches the analytic one
    /// to `O(1/n)` by construction, which is what makes a closed-form
    /// comparison meaningful at all.
    fn normal_grid(n: usize, mu: f64, sigma: f64) -> Vec<f64> {
        (1..=n)
            .map(|i| mu + sigma * norm_ppf((i as f64 - 0.5) / n as f64))
            .collect()
    }

    // ----------------------------------------------------------------- //
    // Test-only RNG + sample generators for the variance-reduction tests
    // below (issue #907, Phase 2b of epic #710). `normal_grid` above is a
    // deliberately noise-free stand-in for "an infinite MC campaign"; the
    // tests here instead need genuinely random (but reproducible) draws to
    // demonstrate that a variance-reduction *strategy*, not just a
    // favorably-arranged sample set, is what narrows the interval.
    // ----------------------------------------------------------------- //

    /// SplitMix64 (Vigna 2015) -- a small, dependency-free, deterministic
    /// PRNG, matching this crate's "dependency-free numerics" convention
    /// (see `stats.rs`'s module doc). Test-only: never used at runtime.
    struct SplitMix64(u64);

    impl SplitMix64 {
        fn next_u64(&mut self) -> u64 {
            self.0 = self.0.wrapping_add(0x9E37_79B9_7F4A_7C15);
            let mut z = self.0;
            z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
            z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
            z ^ (z >> 31)
        }

        /// Uniform in the open interval `(0, 1)` -- never exactly `0` or `1`,
        /// so `norm_ppf` never sees an infinite input.
        fn next_open01(&mut self) -> f64 {
            loop {
                let u = (self.next_u64() >> 11) as f64 / 9_007_199_254_740_992.0; // 2^53
                if u > 0.0 && u < 1.0 {
                    return u;
                }
            }
        }
    }

    /// `n` iid draws from `N(mu, sigma)` via inverse-CDF sampling -- plain
    /// random Monte Carlo, the Phase 1 baseline every strategy here is
    /// compared against.
    fn plain_random_normal_samples(
        rng: &mut SplitMix64,
        n: usize,
        mu: f64,
        sigma: f64,
    ) -> Vec<f64> {
        (0..n)
            .map(|_| mu + sigma * norm_ppf(rng.next_open01()))
            .collect()
    }

    /// A replicated Latin-Hypercube design for `N(mu, sigma)`: `replicates`
    /// independent LHS designs of `block` points each, flattened in
    /// replicate order (matching `SamplingRequest::LatinHypercube`'s
    /// `samples` layout) -- one stratified, jittered point per bin per
    /// replicate. 1-D LHS needs no cross-dimension permutation.
    fn lhs_replicated_normal_samples(
        rng: &mut SplitMix64,
        replicates: usize,
        block: usize,
        mu: f64,
        sigma: f64,
    ) -> Vec<f64> {
        let mut out = Vec::with_capacity(replicates * block);
        for _ in 0..replicates {
            for i in 0..block {
                let u = (i as f64 + rng.next_open01()) / block as f64;
                out.push(mu + sigma * norm_ppf(u));
            }
        }
        out
    }

    fn request(samples: Vec<f64>, limits: Limits) -> YieldRequest {
        YieldRequest {
            confidence: 0.95,
            target_ci_halfwidth: DEFAULT_TARGET_CI_HALFWIDTH,
            min_samples: None,
            measurements: vec![MeasurementRequest {
                name: "m".to_string(),
                unit: None,
                samples,
                errored: 0,
                limits,
                source_corners: vec![],
                negative_control: None,
                analytic_cross_check: None,
                sampling: None,
            }],
        }
    }

    /// Same shape as [`request`], but with an explicit `sampling` strategy --
    /// issue #907, Phase 2b of epic #710.
    fn request_with_sampling(
        samples: Vec<f64>,
        limits: Limits,
        sampling: Option<SamplingRequest>,
    ) -> YieldRequest {
        YieldRequest {
            confidence: 0.95,
            target_ci_halfwidth: DEFAULT_TARGET_CI_HALFWIDTH,
            min_samples: None,
            measurements: vec![MeasurementRequest {
                name: "m".to_string(),
                unit: None,
                samples,
                errored: 0,
                limits,
                source_corners: vec![],
                negative_control: None,
                analytic_cross_check: None,
                sampling,
            }],
        }
    }

    /// The closed-form case issue #816's fourth acceptance criterion asks
    /// for: a standard normal against symmetric +/-2 sigma limits has an
    /// analytic yield of `erf(sqrt(2)) = 0.9544997...`.
    #[test]
    fn normal_yield_matches_the_closed_form_two_sigma_case() {
        let analytic = stats::norm_cdf(2.0) - stats::norm_cdf(-2.0);
        close(analytic, 0.954_499_736_103_641_6, 1e-14);

        let req = request(
            normal_grid(4000, 0.0, 1.0),
            Limits {
                min: Some(-2.0),
                max: Some(2.0),
                target_yield: None,
            },
        );
        let resp = analyze(&req).unwrap();
        let m = &resp.measurements[0];

        // The fitted normal reproduces the closed form to well under a
        // tenth of a percent.
        let parametric = m.yield_.normal.as_ref().unwrap();
        close(parametric.estimate, analytic, 5e-4);
        assert!(parametric.confidence_interval.low <= parametric.estimate);
        assert!(parametric.confidence_interval.high >= parametric.estimate);

        // The non-parametric estimate agrees, and its exact interval covers
        // the analytic truth.
        let empirical = &m.yield_.empirical;
        close(empirical.estimate, analytic, 2e-3);
        assert!(
            empirical.confidence_interval.low <= analytic
                && analytic <= empirical.confidence_interval.high,
            "Clopper-Pearson interval {:?} misses the analytic yield {analytic}",
            empirical.confidence_interval
        );

        // Cpk for a symmetric +/-2 sigma window is exactly 2/3, and
        // sigma-to-spec is exactly 2.
        close(m.capability.cpk.unwrap(), 2.0 / 3.0, 5e-3);
        close(m.capability.sigma_to_spec.unwrap(), 2.0, 1.5e-2);
        close(m.capability.cp.unwrap(), 2.0 / 3.0, 5e-3);
        assert_eq!(m.distribution.normality.verdict, "consistent");
    }

    /// A one-sided spec exercises the infinite-`z` paths in both the CDF and
    /// the delta-method derivative. Closed form: `Phi(3) = 0.99865010...`.
    #[test]
    fn one_sided_spec_matches_the_closed_form_three_sigma_tail() {
        let analytic = stats::norm_cdf(3.0);
        close(analytic, 0.998_650_101_968_369_9, 1e-14);

        let req = request(
            normal_grid(4000, 0.0, 1.0),
            Limits {
                min: None,
                max: Some(3.0),
                target_yield: None,
            },
        );
        let resp = analyze(&req).unwrap();
        let m = &resp.measurements[0];
        let parametric = m.yield_.normal.as_ref().unwrap();
        close(parametric.estimate, analytic, 5e-4);
        assert!(parametric.confidence_interval.low.is_finite());
        assert_eq!(m.capability.limiting_side.as_deref(), Some("upper"));
        close(m.capability.sigma_to_spec.unwrap(), 3.0, 2e-2);
        assert!(m.capability.cp.is_none());
    }

    #[test]
    fn a_shifted_mean_fails_a_declared_target_yield() {
        // Mean sits right on the upper limit: ~50% yield, nowhere near 99%.
        let req = request(
            normal_grid(500, 1.0, 0.1),
            Limits {
                min: Some(0.5),
                max: Some(1.0),
                target_yield: Some(0.99),
            },
        );
        let resp = analyze(&req).unwrap();
        assert_eq!(resp.status, "fail");
        assert_eq!(resp.measurements[0].status, "fail");
        assert!(resp.measurements[0].yield_.empirical.estimate < 0.6);
    }

    #[test]
    fn a_centered_distribution_passes_a_declared_target_yield() {
        let req = request(
            normal_grid(2000, 1.2, 0.005),
            Limits {
                min: Some(1.15),
                max: Some(1.25),
                target_yield: Some(0.99),
            },
        );
        let resp = analyze(&req).unwrap();
        assert_eq!(resp.status, "pass");
        assert_eq!(resp.measurements[0].status, "pass");
        assert!(
            resp.measurements[0]
                .yield_
                .empirical
                .confidence_interval
                .low
                >= 0.99
        );
    }

    #[test]
    fn every_estimate_carries_an_interval_and_a_sample_count() {
        let req = request(
            normal_grid(64, 0.0, 1.0),
            Limits {
                min: Some(-3.0),
                max: Some(3.0),
                target_yield: None,
            },
        );
        let resp = analyze(&req).unwrap();
        let m = &resp.measurements[0];
        assert_eq!(m.yield_.empirical.n, 64);
        assert!(m.yield_.empirical.confidence_interval.is_finite());
        let parametric = m.yield_.normal.as_ref().unwrap();
        assert_eq!(parametric.n, 64);
        assert!(parametric.confidence_interval.is_finite());
        guard_no_bare_point_estimate(m).unwrap();
    }

    #[test]
    fn a_single_sample_is_an_error_not_a_point_estimate() {
        let req = request(
            vec![1.0],
            Limits {
                min: Some(0.0),
                max: Some(2.0),
                target_yield: None,
            },
        );
        let err = analyze(&req).unwrap_err();
        assert!(err.contains("confidence interval"), "{err}");
    }

    #[test]
    fn a_degenerate_confidence_is_an_error() {
        let mut req = request(
            normal_grid(100, 0.0, 1.0),
            Limits {
                min: Some(-1.0),
                max: Some(1.0),
                target_yield: None,
            },
        );
        req.confidence = 1.0;
        let err = analyze(&req).unwrap_err();
        assert!(err.contains("no finite interval"), "{err}");
    }

    #[test]
    fn missing_spec_limits_is_an_error() {
        let req = request(
            normal_grid(100, 0.0, 1.0),
            Limits {
                min: None,
                max: None,
                target_yield: None,
            },
        );
        let err = analyze(&req).unwrap_err();
        assert!(err.contains("no spec limits"), "{err}");
    }

    #[test]
    fn zero_failures_reports_a_one_sided_interval_and_a_sample_size_requirement() {
        // 30 samples, limits wide enough that nothing fails.
        let req = request(
            normal_grid(30, 0.0, 1.0),
            Limits {
                min: Some(-10.0),
                max: Some(10.0),
                target_yield: None,
            },
        );
        let resp = analyze(&req).unwrap();
        let m = &resp.measurements[0];
        assert_eq!(m.yield_.empirical.estimate, 1.0);
        assert_eq!(m.yield_.empirical.confidence_interval.high, 1.0);
        assert!(m.yield_.empirical.confidence_interval.low < 1.0);
        assert_eq!(m.sample_size.method, "clopper-pearson-zero-failures");
        assert_eq!(m.sample_size.verdict, "insufficient");
        // ln(0.025) / ln(0.98) = 182.6 -> 183
        assert_eq!(m.sample_size.required_n, 183);
    }

    #[test]
    fn a_zero_failure_run_reports_how_many_samples_the_claim_would_need() {
        // 300 clean samples cannot support a 99% claim at 95% confidence:
        // the exact lower bound for k = n is (alpha/2)^(1/n), which reaches
        // 0.99 only at n = ln(0.025)/ln(0.99) = 367.03 -> 368.
        let req = request(
            normal_grid(300, 0.0, 1.0),
            Limits {
                min: Some(-10.0),
                max: Some(10.0),
                target_yield: Some(0.99),
            },
        );
        let resp = analyze(&req).unwrap();
        let m = &resp.measurements[0];
        assert_eq!(m.status, "fail");
        assert_eq!(m.yield_.empirical.estimate, 1.0);
        assert_eq!(m.sample_size.required_n_for_target, Some(368));
        assert_eq!(m.sample_size.verdict, "insufficient");
        assert!(m
            .warnings
            .iter()
            .any(|w| w.contains("too small to *claim*")));

        // At 368 samples the same clean run does support the claim.
        let req = request(
            normal_grid(368, 0.0, 1.0),
            Limits {
                min: Some(-10.0),
                max: Some(10.0),
                target_yield: Some(0.99),
            },
        );
        let resp = analyze(&req).unwrap();
        assert_eq!(resp.measurements[0].status, "pass");
        assert_eq!(resp.measurements[0].sample_size.verdict, "sufficient");
    }

    #[test]
    fn an_unreachable_target_reports_no_required_n_and_blames_the_design() {
        // Mean on the upper limit: ~50% pass rate, so no sample count can
        // ever support a 99% claim.
        let req = request(
            normal_grid(400, 1.0, 0.1),
            Limits {
                min: Some(0.5),
                max: Some(1.0),
                target_yield: Some(0.99),
            },
        );
        let resp = analyze(&req).unwrap();
        let m = &resp.measurements[0];
        assert_eq!(m.sample_size.required_n_for_target, None);
        assert!(m.warnings.iter().any(|w| w.contains("no sample count")));
    }

    #[test]
    fn a_zero_spread_sample_set_drops_the_parametric_fit_but_keeps_the_interval() {
        let req = request(
            vec![1.0; 20],
            Limits {
                min: Some(0.0),
                max: Some(2.0),
                target_yield: None,
            },
        );
        let resp = analyze(&req).unwrap();
        let m = &resp.measurements[0];
        assert!(m.yield_.normal.is_none());
        assert!(m.capability.cpk.is_none());
        assert_eq!(m.distribution.normality.verdict, "degenerate");
        assert!(m.yield_.empirical.confidence_interval.is_finite());
    }

    #[test]
    fn a_non_normal_sample_set_is_flagged() {
        // A flat uniform grid: normality must be rejected, and the warning
        // must say so rather than letting the parametric number stand alone.
        let xs: Vec<f64> = (0..300).map(|i| i as f64 / 299.0).collect();
        let req = request(
            xs,
            Limits {
                min: Some(0.0),
                max: Some(1.0),
                target_yield: None,
            },
        );
        let resp = analyze(&req).unwrap();
        let m = &resp.measurements[0];
        assert_eq!(m.distribution.normality.verdict, "rejected");
        assert!(m.warnings.iter().any(|w| w.contains("not consistent")));
    }

    // ----------------------------------------------------------------- //
    // Negative control (issue #817)
    // ----------------------------------------------------------------- //

    /// A request whose one measurement optionally carries a negative
    /// control and/or an analytic cross-check -- the `request()` helper
    /// above deliberately leaves both `None` so every pre-#817 test stays
    /// unaffected.
    fn request_with_self_checks(
        samples: Vec<f64>,
        limits: Limits,
        negative_control: Option<NegativeControlRequest>,
        analytic_cross_check: Option<AnalyticCrossCheckRequest>,
    ) -> YieldRequest {
        YieldRequest {
            confidence: 0.95,
            target_ci_halfwidth: DEFAULT_TARGET_CI_HALFWIDTH,
            min_samples: None,
            measurements: vec![MeasurementRequest {
                name: "m".to_string(),
                unit: None,
                samples,
                errored: 0,
                limits,
                source_corners: vec![],
                negative_control,
                analytic_cross_check,
                sampling: None,
            }],
        }
    }

    #[test]
    fn a_seeded_known_bad_negative_control_is_detected() {
        // Nominal: tightly centered, comfortably inside +/-0.5 -- a clean
        // campaign. Negative control: deliberately shifted to straddle the
        // upper limit, mirroring a seeded known-bad variant (e.g. a forced
        // offset injection) that should show up as materially worse yield.
        let nominal = normal_grid(300, 0.0, 0.05);
        let bad = normal_grid(300, 0.6, 0.05);
        let req = request_with_self_checks(
            nominal,
            Limits {
                min: Some(-0.5),
                max: Some(0.5),
                target_yield: None,
            },
            Some(NegativeControlRequest {
                samples: bad,
                errored: 0,
                description: Some("vos forced to 0.6 (12x the 0.05 spec sigma)".to_string()),
            }),
            None,
        );
        let resp = analyze(&req).unwrap();
        let m = &resp.measurements[0];
        let nc = m.negative_control.as_ref().unwrap();
        assert_eq!(nc.verdict, "detected");
        assert!(nc.degradation_detected);
        assert!(nc.yield_.empirical.estimate < m.yield_.empirical.estimate);
        assert_eq!(
            nc.description.as_deref(),
            Some("vos forced to 0.6 (12x the 0.05 spec sigma)")
        );
        // A detected negative control raises no "not distinguishable" or
        // "no measurement declared" warning anywhere in the payload.
        assert!(!m.warnings.iter().any(|w| w.contains("not statistically")));
        assert!(!resp
            .warnings
            .iter()
            .any(|w| w.contains("no measurement declared a negative_control")));
    }

    #[test]
    fn a_negative_control_that_does_not_degrade_is_flagged_not_detected() {
        // The "negative control" here is drawn from the *same* distribution
        // as the nominal measurement -- a broken/no-op self-check (e.g. the
        // deliberate defect was never actually injected). The tool must
        // flag this rather than silently accept it.
        let nominal = normal_grid(300, 0.0, 0.05);
        let not_actually_bad = normal_grid(300, 0.0, 0.05);
        let req = request_with_self_checks(
            nominal,
            Limits {
                min: Some(-0.5),
                max: Some(0.5),
                target_yield: None,
            },
            Some(NegativeControlRequest {
                samples: not_actually_bad,
                errored: 0,
                description: None,
            }),
            None,
        );
        let resp = analyze(&req).unwrap();
        let m = &resp.measurements[0];
        let nc = m.negative_control.as_ref().unwrap();
        assert_eq!(nc.verdict, "not_detected");
        assert!(!nc.degradation_detected);
        assert!(m
            .warnings
            .iter()
            .any(|w| w.contains("not statistically distinguishable")));
        assert!(resp
            .warnings
            .iter()
            .any(|w| w.contains("did not show the expected degradation")));
    }

    #[test]
    fn a_campaign_with_no_negative_control_at_all_is_flagged() {
        let req = request(
            normal_grid(200, 0.0, 1.0),
            Limits {
                min: Some(-3.0),
                max: Some(3.0),
                target_yield: None,
            },
        );
        let resp = analyze(&req).unwrap();
        assert!(resp.measurements[0].negative_control.is_none());
        assert!(resp
            .warnings
            .iter()
            .any(|w| w.contains("no measurement declared a negative_control")));
    }

    #[test]
    fn a_negative_control_below_the_sample_floor_is_an_error() {
        let req = request_with_self_checks(
            normal_grid(50, 0.0, 1.0),
            Limits {
                min: Some(-3.0),
                max: Some(3.0),
                target_yield: None,
            },
            Some(NegativeControlRequest {
                samples: vec![9.0],
                errored: 0,
                description: None,
            }),
            None,
        );
        let err = analyze(&req).unwrap_err();
        assert!(err.contains("negative_control"), "{err}");
        assert!(err.contains("confidence interval"), "{err}");
    }

    // ----------------------------------------------------------------- //
    // Analytic cross-check (issue #817)
    // ----------------------------------------------------------------- //

    #[test]
    fn ktc_noise_cross_check_matches_a_synthetic_thermal_noise_draw() {
        // sigma = sqrt(kT/C) for a 1 pF sampling cap at 300 K.
        let capacitance_f = 1.0e-12_f64;
        let temperature_k = 300.0_f64;
        let sigma = (1.380_649e-23 * temperature_k / capacitance_f).sqrt();
        let samples = normal_grid(2000, 0.0, sigma);
        let req = request_with_self_checks(
            samples,
            Limits {
                min: Some(-5.0 * sigma),
                max: Some(5.0 * sigma),
                target_yield: None,
            },
            None,
            Some(AnalyticCrossCheckRequest::KtcNoise {
                capacitance_f,
                temperature_k,
                analytic_mean: Some(0.0),
            }),
        );
        let resp = analyze(&req).unwrap();
        let check = resp.measurements[0].analytic_cross_check.as_ref().unwrap();
        assert_eq!(check.kind, "ktc_noise");
        close(check.analytic_stddev, sigma, 1e-15 * sigma);
        assert_eq!(check.verdict, "consistent");
        close(check.stddev_relative_delta, 0.0, 5e-3);
        assert!(!resp.measurements[0]
            .warnings
            .iter()
            .any(|w| w.contains("not consistent with the ktc_noise")));
    }

    #[test]
    fn mismatch_offset_cross_check_flags_a_draw_that_disagrees_with_the_claimed_sigma() {
        // The draw's actual spread (1 mV) is nowhere near the claimed
        // Pelgrom-model sigma (10 mV) -- the discrepancy an analytic
        // cross-check exists to catch.
        let samples = normal_grid(2000, 0.0, 0.001);
        let req = request_with_self_checks(
            samples,
            Limits {
                min: Some(-0.05),
                max: Some(0.05),
                target_yield: None,
            },
            None,
            Some(AnalyticCrossCheckRequest::MismatchOffset {
                sigma: 0.010,
                mean: Some(0.0),
            }),
        );
        let resp = analyze(&req).unwrap();
        let m = &resp.measurements[0];
        let check = m.analytic_cross_check.as_ref().unwrap();
        assert_eq!(check.kind, "mismatch_offset");
        assert_eq!(check.verdict, "inconsistent");
        close(check.mean_delta, 0.0, 1e-3);
        assert!(check.stddev_relative_delta < -0.5);
        assert!(m
            .warnings
            .iter()
            .any(|w| w.contains("not consistent with the mismatch_offset")));
    }

    #[test]
    fn a_non_positive_analytic_parameter_is_an_error() {
        let req = request_with_self_checks(
            normal_grid(50, 0.0, 1.0),
            Limits {
                min: Some(-3.0),
                max: Some(3.0),
                target_yield: None,
            },
            None,
            Some(AnalyticCrossCheckRequest::MismatchOffset {
                sigma: -1.0,
                mean: None,
            }),
        );
        let err = analyze(&req).unwrap_err();
        assert!(err.contains("sigma must be positive"), "{err}");
    }

    // ----------------------------------------------------------------- //
    // Variance-reduction sampling strategies (issue #907, Phase 2b of
    // epic #710)
    // ----------------------------------------------------------------- //

    /// Omitting `sampling` (Phase 1's default) is unaffected: `strategy`
    /// reports `"plain_random"`, and there is no `variance_reduced` estimate
    /// or sample-size verdict to add beyond what Phase 1 already reports.
    #[test]
    fn omitting_sampling_reports_plain_random_with_no_variance_reduced_estimate() {
        let req = request(
            normal_grid(300, 0.0, 1.0),
            Limits {
                min: Some(-3.0),
                max: Some(3.0),
                target_yield: None,
            },
        );
        let resp = analyze(&req).unwrap();
        let m = &resp.measurements[0];
        assert_eq!(m.sampling.strategy, "plain_random");
        assert!(m.sampling.replicates.is_none());
        assert!(m.sampling.effective_sample_size.is_none());
        assert!(m.yield_.variance_reduced.is_none());
        assert!(m.sample_size.variance_reduced.is_none());
    }

    /// Declaring `plain_random` explicitly is identical to omitting it.
    #[test]
    fn an_explicit_plain_random_strategy_behaves_identically_to_omitting_it() {
        let samples = normal_grid(300, 0.0, 1.0);
        let limits = Limits {
            min: Some(-3.0),
            max: Some(3.0),
            target_yield: None,
        };
        let implicit = analyze(&request(samples.clone(), limits)).unwrap();
        let explicit = analyze(&request_with_sampling(
            samples,
            limits,
            Some(SamplingRequest::PlainRandom),
        ))
        .unwrap();
        assert_eq!(
            implicit.measurements[0].yield_.empirical.estimate,
            explicit.measurements[0].yield_.empirical.estimate
        );
        assert_eq!(explicit.measurements[0].sampling.strategy, "plain_random");
    }

    #[test]
    fn latin_hypercube_replicates_below_two_is_an_error() {
        let req = request_with_sampling(
            normal_grid(20, 0.0, 1.0),
            Limits {
                min: Some(-3.0),
                max: Some(3.0),
                target_yield: None,
            },
            Some(SamplingRequest::LatinHypercube { replicates: 1 }),
        );
        let err = analyze(&req).unwrap_err();
        assert!(err.contains("replicates must be at least 2"), "{err}");
    }

    #[test]
    fn latin_hypercube_samples_not_evenly_divisible_by_replicates_is_an_error() {
        let req = request_with_sampling(
            normal_grid(21, 0.0, 1.0),
            Limits {
                min: Some(-3.0),
                max: Some(3.0),
                target_yield: None,
            },
            Some(SamplingRequest::LatinHypercube { replicates: 4 }),
        );
        let err = analyze(&req).unwrap_err();
        assert!(err.contains("not evenly divisible"), "{err}");
    }

    #[test]
    fn importance_weights_length_mismatch_is_an_error() {
        let req = request_with_sampling(
            normal_grid(20, 0.0, 1.0),
            Limits {
                min: Some(-3.0),
                max: Some(3.0),
                target_yield: None,
            },
            Some(SamplingRequest::Importance {
                weights: vec![1.0; 10],
            }),
        );
        let err = analyze(&req).unwrap_err();
        assert!(err.contains("sampling.weights has 10 entries"), "{err}");
    }

    #[test]
    fn a_non_positive_importance_weight_is_an_error() {
        let n = 20;
        let mut weights = vec![1.0; n];
        weights[3] = -0.5;
        let req = request_with_sampling(
            normal_grid(n, 0.0, 1.0),
            Limits {
                min: Some(-3.0),
                max: Some(3.0),
                target_yield: None,
            },
            Some(SamplingRequest::Importance { weights }),
        );
        let err = analyze(&req).unwrap_err();
        assert!(err.contains("must all be finite and positive"), "{err}");
    }

    /// Equal importance weights must reproduce the plain empirical estimate
    /// exactly -- the Horvitz-Thompson estimator collapses to the ordinary
    /// sample proportion when every weight is the same, which is the sanity
    /// check that the reweighting arithmetic itself is correct before
    /// trusting it on a genuinely biased proposal.
    #[test]
    fn equal_importance_weights_reproduce_the_plain_empirical_estimate() {
        let samples = normal_grid(300, 0.0, 1.0);
        let limits = Limits {
            min: Some(-2.0),
            max: Some(2.0),
            target_yield: None,
        };
        let req = request_with_sampling(
            samples,
            limits,
            Some(SamplingRequest::Importance {
                weights: vec![3.0; 300], // any equal, non-unit constant
            }),
        );
        let resp = analyze(&req).unwrap();
        let m = &resp.measurements[0];
        let is_est = m.yield_.variance_reduced.as_ref().unwrap();
        close(is_est.estimate, m.yield_.empirical.estimate, 1e-12);
        assert_eq!(m.sampling.strategy, "importance");
        close(m.sampling.effective_sample_size.unwrap(), 300.0, 1e-6);
    }

    /// Highly concentrated importance weights (most of the weight mass on a
    /// handful of draws) are flagged -- Kish's effective sample size is much
    /// smaller than the raw `n`, so the reweighted estimate has far less
    /// statistical power than `n` would suggest.
    #[test]
    fn highly_concentrated_importance_weights_are_flagged() {
        let n = 200;
        let mut weights = vec![0.01; n];
        weights[0] = 1000.0; // almost all the weight mass on one draw
        let req = request_with_sampling(
            normal_grid(n, 0.0, 1.0),
            Limits {
                min: Some(-3.0),
                max: Some(3.0),
                target_yield: None,
            },
            Some(SamplingRequest::Importance { weights }),
        );
        let resp = analyze(&req).unwrap();
        let m = &resp.measurements[0];
        assert!(m.sampling.effective_sample_size.unwrap() < n as f64 * 0.05);
        assert!(m.warnings.iter().any(|w| w.contains("highly concentrated")));
    }

    /// Issue #907's core acceptance criterion: at a **matched total sample
    /// count**, replicated Latin-hypercube sampling reaches the known
    /// analytic yield with a measurably tighter confidence interval than
    /// plain random Monte Carlo -- i.e. LHS needs fewer samples than plain
    /// MC to reach the same CI width, not just a narrower one at equal N by
    /// coincidence of one seed.
    #[test]
    fn latin_hypercube_reaches_the_analytic_yield_with_a_tighter_interval_than_plain_mc_at_matched_n(
    ) {
        let analytic = stats::norm_cdf(2.0) - stats::norm_cdf(-2.0);
        let limits = Limits {
            min: Some(-2.0),
            max: Some(2.0),
            target_yield: None,
        };
        let n = 600;
        let replicates = 6;
        let block = n / replicates;

        let mut rng = SplitMix64(0x00C0_FFEE_2026);
        let mc_samples = plain_random_normal_samples(&mut rng, n, 0.0, 1.0);
        let lhs_samples = lhs_replicated_normal_samples(&mut rng, replicates, block, 0.0, 1.0);

        let mc_resp = analyze(&request_with_sampling(mc_samples, limits, None)).unwrap();
        let mc = &mc_resp.measurements[0];

        let lhs_resp = analyze(&request_with_sampling(
            lhs_samples,
            limits,
            Some(SamplingRequest::LatinHypercube {
                replicates: replicates as u32,
            }),
        ))
        .unwrap();
        let lhs = &lhs_resp.measurements[0];
        let lhs_est = lhs.yield_.variance_reduced.as_ref().unwrap();

        // Both are honest about the analytic truth...
        assert!(
            mc.yield_.empirical.confidence_interval.low <= analytic
                && analytic <= mc.yield_.empirical.confidence_interval.high
        );
        assert!(
            lhs_est.confidence_interval.low <= analytic
                && analytic <= lhs_est.confidence_interval.high
        );

        // ...but LHS's interval is measurably tighter at the SAME total
        // sample count -- the whole point of variance reduction: it would
        // reach any target CI width plain MC hits at N = 600 using fewer
        // than 600 samples of its own.
        let mc_half = mc.yield_.empirical.confidence_interval.half_width();
        let lhs_half = lhs_est.confidence_interval.half_width();
        assert!(
            lhs_half < mc_half * 0.5,
            "lhs half-width {lhs_half} not measurably tighter than plain MC's {mc_half}"
        );

        assert_eq!(lhs.sampling.strategy, "latin_hypercube");
        assert_eq!(lhs.sampling.replicates, Some(replicates as u32));
        assert_eq!(lhs_est.method, "lhs-replicated");
        let vr_size = lhs.sample_size.variance_reduced.as_ref().unwrap();
        close(vr_size.observed_ci_halfwidth, lhs_half, 1e-12);
    }

    /// Issue #907's rare-event motivating case: a one-sided 4-sigma limit
    /// (analytic yield `Phi(4) = 0.99996833...`, fail rate ~3.17e-5) is
    /// exactly the kind of question plain MC cannot resolve at a modest
    /// budget -- at N = 5000 it almost never observes a single failure, so
    /// its exact interval can only state a loose lower bound. Importance
    /// sampling from a proposal centered on the limit (standard exponential
    /// tilting) resolves the same question, correctly centered on the
    /// analytic answer, using FEWER total samples.
    #[test]
    fn importance_sampling_resolves_a_high_sigma_tail_plain_mc_cannot_at_a_smaller_budget() {
        let limit = 4.0;
        let analytic = stats::norm_cdf(limit);
        let limits = Limits {
            min: None,
            max: Some(limit),
            target_yield: None,
        };

        let mut rng = SplitMix64(0x000B_ADC0_FFEE_2026);

        // Plain MC: a realistic budget that essentially never sees a failure
        // at this tail probability (expected failures ~= 5000 * 3.17e-5).
        let mc_n = 5000;
        let mc_samples = plain_random_normal_samples(&mut rng, mc_n, 0.0, 1.0);
        let mc_resp = analyze(&request_with_sampling(mc_samples, limits, None)).unwrap();
        let mc = &mc_resp.measurements[0];
        assert_eq!(mc.yield_.empirical.estimate, 1.0); // zero observed failures

        // Importance sampling: proposal N(limit, 1), reweighted back to
        // N(0, 1) -- fewer total samples than the plain MC run above.
        let is_n = 2000;
        let is_samples: Vec<f64> = (0..is_n)
            .map(|_| limit + norm_ppf(rng.next_open01()))
            .collect();
        let weights: Vec<f64> = is_samples
            .iter()
            .map(|x| (-limit * x + 0.5 * limit * limit).exp())
            .collect();
        let is_resp = analyze(&request_with_sampling(
            is_samples,
            limits,
            Some(SamplingRequest::Importance { weights }),
        ))
        .unwrap();
        let is_m = &is_resp.measurements[0];
        let is_est = is_m.yield_.variance_reduced.as_ref().unwrap();

        // Importance sampling's estimate is centered close to the true,
        // tiny-tail analytic answer, and its interval covers it...
        close(is_est.estimate, analytic, 5e-4);
        assert!(
            is_est.confidence_interval.low <= analytic
                && analytic <= is_est.confidence_interval.high
        );

        // ...while using FEWER total samples than plain MC's budget above
        // (2000 vs 5000) to get there. Plain MC's zero-failure bound only
        // says "no worse than X"; it does not confirm the true rate is
        // anywhere near the analytic answer the way importance sampling's
        // direct, reweighted estimate does.
        let mc_half = mc.yield_.empirical.confidence_interval.half_width();
        let is_half = is_est.confidence_interval.half_width();
        assert!(
            is_half < mc_half,
            "importance-sampling half-width {is_half} not tighter than plain MC's {mc_half} \
             despite using fewer samples ({is_n} vs {mc_n})"
        );
        assert_eq!(is_m.sampling.strategy, "importance");
        assert_eq!(is_est.method, "importance-weighted");
        assert!(is_m.sampling.effective_sample_size.unwrap() > 0.0);
    }
}
