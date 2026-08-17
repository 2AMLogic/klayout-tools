//! The JSON request/response shapes `klt_yield_native` speaks.
//!
//! This is the Rust/Python boundary contract, not the CLI contract: the
//! Python layer (`src/klayout_tools/yield_analysis.py`) is what turns a `klt
//! sim` Monte Carlo report -- or a plain sample-set document -- into the
//! request below, and hands the response straight back out as `klt yield`'s
//! payload. See `docs/cli/yield.md` for the user-facing schema.

use serde::{Deserialize, Serialize};

/// Default two-sided confidence level for every interval this crate reports.
pub const DEFAULT_CONFIDENCE: f64 = 0.95;

/// Default target half-width for the sample-size verdict, in absolute yield
/// (`0.01` = "the interval should pin the yield to within +/- 1 percentage
/// point"). Deliberately a *yield* tolerance, not a sigma count: it is the
/// question a caller can actually answer ("how tight do I need this?").
pub const DEFAULT_TARGET_CI_HALFWIDTH: f64 = 0.01;

/// Hard floor on the sample count, independent of `min_samples`.
///
/// Two samples is the smallest draw from which a **sample** standard
/// deviation exists at all; below it there is no interval to report, only a
/// bare point estimate -- exactly what this tool refuses to emit (issue
/// #816, epic #710).
pub const ABSOLUTE_MIN_SAMPLES: usize = 2;

#[derive(Debug, Deserialize)]
pub struct YieldRequest {
    /// Two-sided confidence level for every reported interval, in `(0, 1)`.
    #[serde(default = "default_confidence")]
    pub confidence: f64,
    /// Target half-width for the sample-size verdict, in `(0, 1)`.
    #[serde(default = "default_target_ci_halfwidth")]
    pub target_ci_halfwidth: f64,
    /// Minimum usable samples per measurement; clamped up to
    /// [`ABSOLUTE_MIN_SAMPLES`].
    #[serde(default)]
    pub min_samples: Option<usize>,
    pub measurements: Vec<MeasurementRequest>,
}

fn default_confidence() -> f64 {
    DEFAULT_CONFIDENCE
}

fn default_target_ci_halfwidth() -> f64 {
    DEFAULT_TARGET_CI_HALFWIDTH
}

#[derive(Debug, Deserialize)]
pub struct MeasurementRequest {
    pub name: String,
    #[serde(default)]
    pub unit: Option<String>,
    /// The usable Monte Carlo sample values (nulls already dropped by the
    /// Python layer, which counts them into `errored`).
    pub samples: Vec<f64>,
    /// Samples whose value was unextractable and therefore excluded.
    #[serde(default)]
    pub errored: u64,
    pub limits: Limits,
    /// Originating (pre-sampling) corner ids this measurement's samples were
    /// pooled from -- informational, echoed back so a pooled multi-corner
    /// draw is visible in the output.
    #[serde(default)]
    pub source_corners: Vec<String>,
    /// A seeded, known-bad variant of this measurement -- issue #817's
    /// self-check discipline. When present, its own yield is estimated
    /// against these same `limits` and checked for the degradation its
    /// deliberate defect is supposed to produce.
    #[serde(default)]
    pub negative_control: Option<NegativeControlRequest>,
    /// A closed-form distribution to check this measurement's empirical
    /// mean/stddev against (issue #817's analytic cross-check).
    #[serde(default)]
    pub analytic_cross_check: Option<AnalyticCrossCheckRequest>,
    /// The strategy `samples` were drawn with -- `None` (the default) means
    /// Phase 1's plain random Monte Carlo, unaffected by any of this. A
    /// variance-reduction strategy (issue #907, Phase 2b of epic #710) adds a
    /// second, strategy-aware yield estimate (`yield.variance_reduced`)
    /// alongside the plain empirical/normal ones.
    #[serde(default)]
    pub sampling: Option<SamplingRequest>,
}

#[derive(Debug, Deserialize)]
pub struct NegativeControlRequest {
    /// Samples drawn from the deliberately-degraded variant (e.g. a
    /// mismatch/offset injected well past spec, a corner seeded to fail).
    pub samples: Vec<f64>,
    #[serde(default)]
    pub errored: u64,
    /// Human description of what was deliberately broken, echoed back so the
    /// report is self-documenting (e.g. "vos forced to 3x spec via a fixed
    /// device offset").
    #[serde(default)]
    pub description: Option<String>,
}

/// One closed-form distribution `klt yield` can check a measurement's
/// empirical mean/stddev against. Deliberately data-driven rather than a
/// PDK-specific model lookup: the caller supplies the physical parameters (or
/// the sigma itself), and this crate does the closed-form arithmetic and the
/// statistical comparison.
#[derive(Debug, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum AnalyticCrossCheckRequest {
    /// Thermal (`kT/C`) sampled-capacitor noise: `sigma = sqrt(kB * T / C)`,
    /// zero-mean unless `analytic_mean` overrides it (e.g. a known offset
    /// riding on top of the noise floor).
    KtcNoise {
        /// Sampling capacitance, in farads.
        capacitance_f: f64,
        /// Absolute temperature, in kelvin.
        #[serde(default = "default_temperature_k")]
        temperature_k: f64,
        #[serde(default)]
        analytic_mean: Option<f64>,
    },
    /// A mismatch-dominated offset/spread with a known sigma (e.g. a
    /// Pelgrom-model prediction the caller has already evaluated), in the
    /// measurement's own units.
    MismatchOffset {
        sigma: f64,
        #[serde(default)]
        mean: Option<f64>,
    },
}

fn default_temperature_k() -> f64 {
    300.0
}

/// How a measurement's `samples` were drawn -- Phase 2b of the
/// statistical/yield epic #710 (issue #907). Makes a rare-event (high-sigma)
/// yield question answerable without brute-forcing millions of plain-random
/// draws: Latin-hypercube sampling (stratified coverage, replicated so its
/// own variance is estimable) and importance sampling (a biased proposal
/// distribution, reweighted back to the target) are both selectable
/// alternatives to Phase 1's plain random Monte Carlo.
#[derive(Debug, Deserialize)]
#[serde(tag = "strategy", rename_all = "snake_case")]
pub enum SamplingRequest {
    /// Phase 1's plain random Monte Carlo -- every sample is an independent,
    /// equally-weighted draw. Stating it explicitly (rather than just
    /// omitting `sampling`) lets a caller record its provenance even though
    /// it changes nothing about the analysis.
    PlainRandom,
    /// Latin-hypercube sampling, run as `replicates` independent LHS designs
    /// of `samples.len() / replicates` points each -- `samples` must be
    /// `replicates` consecutive equal-sized blocks, each one full LHS design,
    /// in that order. Replication (McKay, Conover & Beckman 1979) is what
    /// makes the design's own variance estimable at all: a single
    /// unreplicated LHS draw's within-sample proportion has a *smaller* true
    /// variance than an iid draw of the same size (that is the whole point of
    /// stratifying), so treating it as iid (the plain Clopper-Pearson
    /// formula) would silently overstate its uncertainty rather than
    /// understate it -- still honest, but not the tighter, correctly-sized
    /// interval the strategy earns. The mean and spread *across* replicate
    /// proportions is what actually captures that.
    LatinHypercube {
        /// Number of independent LHS designs `samples` is divided into; must
        /// be at least 2 (a single replicate has no across-replicate spread
        /// to estimate a variance from -- the same "never a bare point
        /// estimate" floor Phase 1 applies to the plain sample count).
        replicates: u32,
    },
    /// Importance sampling: `samples` were drawn from a biased proposal
    /// distribution (e.g. concentrated in the failure region for a
    /// rare-event tail), and `weights[i]` is the importance weight
    /// `f_target(x_i) / f_proposal(x_i)` that reweights draw `i` back to the
    /// target distribution -- same length and order as `samples`.
    Importance {
        /// One positive, finite weight per sample, same order as `samples`.
        weights: Vec<f64>,
    },
}

#[derive(Debug, Clone, Copy, Default, Deserialize, Serialize)]
pub struct Limits {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub min: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub max: Option<f64>,
    /// When `true`, `min` is a *strict* lower bound (`x > min` passes, not
    /// `x >= min`) -- for spec rows ratified as strict inequalities ("must
    /// be strictly positive"), so the limits file can transcribe the spec
    /// literally instead of encoding the strictness as an epsilon baked
    /// into `min` (issue #1083). Ignored (and rejected as an error, see
    /// `analyze_measurement`) unless `min` is also present. Defaults to
    /// `false` -- the pre-existing inclusive comparison -- so it is
    /// backwards compatible with every limits document that predates it.
    #[serde(default, skip_serializing_if = "is_false")]
    pub exclusive_min: bool,
    /// The `max`-side counterpart of `exclusive_min`: `true` makes `max` a
    /// strict upper bound (`x < max`, not `x <= max`).
    #[serde(default, skip_serializing_if = "is_false")]
    pub exclusive_max: bool,
    /// Optional yield the design is claimed to meet. When present, the
    /// measurement passes only if the **lower** confidence bound of the
    /// empirical yield reaches it -- a claim at the stated confidence, not a
    /// point estimate that happened to clear the bar.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub target_yield: Option<f64>,
}

fn is_false(b: &bool) -> bool {
    !*b
}

// --------------------------------------------------------------------------- //
// Response
// --------------------------------------------------------------------------- //

/// `1` -- the initial shape (issue #816, Phase 1a of the yield epic #710).
pub const SCHEMA_VERSION: u32 = 1;

#[derive(Debug, Serialize)]
pub struct YieldResponse {
    pub schema_version: u32,
    pub confidence: f64,
    pub target_ci_halfwidth: f64,
    pub min_samples: usize,
    /// `"pass"` / `"fail"` / `"reported"` -- `"reported"` when no
    /// measurement declared a `target_yield`, so nothing could pass or fail.
    pub status: String,
    pub measurement_count: usize,
    pub measurements: Vec<MeasurementReport>,
    pub warnings: Vec<String>,
}

#[derive(Debug, Serialize)]
pub struct MeasurementReport {
    pub name: String,
    pub unit: Option<String>,
    pub n: u64,
    pub errored: u64,
    pub limits: Limits,
    pub source_corners: Vec<String>,
    pub distribution: Distribution,
    #[serde(rename = "yield")]
    pub yield_: YieldBlock,
    pub capability: Capability,
    pub sample_size: SampleSize,
    /// `null` when this measurement declared no `negative_control` -- issue
    /// #817's self-check requires a campaign to be flagged when it omits
    /// one, which happens at the warning level (see `estimate::analyze`)
    /// rather than here, so the absence itself is still visible in the
    /// payload.
    pub negative_control: Option<NegativeControlReport>,
    /// `null` when this measurement declared no `analytic_cross_check`.
    pub analytic_cross_check: Option<AnalyticCrossCheckReport>,
    /// The sampling strategy this measurement's `yield.variance_reduced`
    /// (if any) was computed from -- always present, even for
    /// `"plain_random"` (issue #907, Phase 2b of epic #710).
    pub sampling: SamplingReport,
    pub status: String,
    pub warnings: Vec<String>,
}

/// Echoes which sampling strategy produced `samples`, plus the numbers a
/// variance-reduction strategy needs beyond `n` itself (issue #907, Phase 2b
/// of epic #710).
#[derive(Debug, Serialize)]
pub struct SamplingReport {
    /// `"plain_random"` / `"latin_hypercube"` / `"importance"`.
    pub strategy: String,
    /// Number of replicate LHS designs `samples` was divided into. `null`
    /// unless `strategy == "latin_hypercube"`.
    pub replicates: Option<u32>,
    /// Kish's effective sample size for an importance-weighted draw --
    /// `(sum w)^2 / sum(w^2)` -- "how many equally-weighted samples this
    /// reweighted draw is worth". Equals `n` exactly when every weight is
    /// equal and falls toward `1` as the weight mass concentrates on a few
    /// draws. `null` unless `strategy == "importance"`, where every sample
    /// already carries equal weight and this number adds nothing.
    pub effective_sample_size: Option<f64>,
}

/// The negative control's own yield estimate against the nominal
/// measurement's limits, plus the self-check verdict: does the deliberately
/// degraded variant actually show worse yield than the nominal draw, and is
/// that difference more than sampling noise?
#[derive(Debug, Serialize)]
pub struct NegativeControlReport {
    pub description: Option<String>,
    pub n: u64,
    pub errored: u64,
    #[serde(rename = "yield")]
    pub yield_: YieldBlock,
    /// The nominal measurement's empirical yield estimate, echoed here so
    /// the comparison this verdict rests on is checkable without
    /// cross-referencing another part of the payload.
    pub nominal_empirical_estimate: f64,
    /// `true` only when the negative control's empirical yield is both lower
    /// than the nominal's *and* the two exact (Clopper-Pearson) intervals do
    /// not overlap -- a difference too large to be sampling noise, not just
    /// a lower point estimate.
    pub degradation_detected: bool,
    /// `"detected"` / `"not_detected"`.
    pub verdict: String,
}

/// The analytic model's prediction, the sample's own empirical mean/stddev,
/// and the discrepancy between them.
#[derive(Debug, Serialize)]
pub struct AnalyticCrossCheckReport {
    /// `"ktc_noise"` / `"mismatch_offset"`.
    pub kind: String,
    pub analytic_mean: f64,
    pub analytic_stddev: f64,
    pub empirical_mean: f64,
    pub empirical_stddev: f64,
    /// Confidence interval for the empirical stddev (same confidence level
    /// as the rest of the report), via the same asymptotic
    /// `Var(sigma_hat) = sigma^2/(2n)` approximation `yield.normal`'s
    /// delta-method interval uses.
    pub empirical_stddev_confidence_interval: Interval,
    /// Confidence interval for the empirical mean (`Var(mu_hat) = sigma^2/n`).
    pub empirical_mean_confidence_interval: Interval,
    /// `(empirical_stddev - analytic_stddev) / analytic_stddev`.
    pub stddev_relative_delta: f64,
    /// `empirical_mean - analytic_mean`, in the measurement's own units.
    pub mean_delta: f64,
    /// `"consistent"` when both the analytic mean and stddev fall inside
    /// their empirical confidence intervals; `"inconsistent"` otherwise.
    pub verdict: String,
}

#[derive(Debug, Serialize)]
pub struct Distribution {
    /// The fitted family. `"normal"` is the only model Phase 1a fits; the
    /// field exists so a later family is additive, not a shape change.
    pub model: String,
    pub mean: f64,
    /// Bessel-corrected sample standard deviation.
    pub stddev: f64,
    pub min: f64,
    pub max: f64,
    pub median: f64,
    pub skewness: Option<f64>,
    pub excess_kurtosis: Option<f64>,
    pub normality: Normality,
}

#[derive(Debug, Serialize)]
pub struct Normality {
    pub test: String,
    pub statistic: Option<f64>,
    pub critical_value: f64,
    pub significance: f64,
    /// `"consistent"` / `"rejected"` / `"insufficient_samples"` /
    /// `"degenerate"` (zero spread).
    pub verdict: String,
}

#[derive(Debug, Serialize)]
pub struct YieldBlock {
    /// Non-parametric: the fraction of samples inside the limits, with an
    /// exact (Clopper-Pearson) interval. Always present.
    pub empirical: Estimate,
    /// Parametric: the fitted normal's probability mass inside the limits,
    /// with a delta-method interval. `null` when the fit is degenerate
    /// (zero sample spread).
    pub normal: Option<Estimate>,
    /// The strategy-aware estimate: `"lhs-replicated"` (across-replicate mean
    /// and spread) or `"importance-weighted"` (the Horvitz-Thompson
    /// estimator, reweighted back to the target distribution). `null` for
    /// `"plain_random"` -- `empirical` above already is that estimate, so
    /// there is nothing to add (issue #907, Phase 2b of epic #710).
    pub variance_reduced: Option<Estimate>,
}

/// **Every** yield number this crate emits is one of these -- there is no
/// shape in which an estimate can travel without its interval and its `n`.
/// That is the structural half of issue #816's "never a bare point estimate"
/// requirement; `estimate::guard_no_bare_point_estimate` is the runtime half.
#[derive(Debug, Serialize)]
pub struct Estimate {
    pub method: String,
    pub estimate: f64,
    pub confidence: f64,
    pub confidence_interval: Interval,
    pub n: u64,
}

#[derive(Debug, Clone, Copy, Serialize)]
pub struct Interval {
    pub low: f64,
    pub high: f64,
}

impl Interval {
    pub fn half_width(&self) -> f64 {
        0.5 * (self.high - self.low)
    }

    pub fn is_finite(&self) -> bool {
        self.low.is_finite() && self.high.is_finite() && self.high >= self.low
    }
}

#[derive(Debug, Serialize)]
pub struct Capability {
    /// Potential capability `(USL - LSL) / (6 sigma)`; `null` unless both
    /// limits are declared and the spread is non-zero.
    pub cp: Option<f64>,
    /// Actual capability `min(USL - mu, mu - LSL) / (3 sigma)`.
    pub cpk: Option<f64>,
    pub cpk_confidence_interval: Option<Interval>,
    /// `3 * cpk` -- the distance from the mean to the nearest limit, in
    /// sample standard deviations. The number an analog reviewer asks for.
    pub sigma_to_spec: Option<f64>,
    pub sigma_to_spec_confidence_interval: Option<Interval>,
    /// `"lower"` / `"upper"` -- which limit `cpk` is measured against.
    pub limiting_side: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct SampleSize {
    pub n: u64,
    pub observed_ci_halfwidth: f64,
    pub target_ci_halfwidth: f64,
    /// Samples needed for the interval to reach `target_ci_halfwidth` at the
    /// observed pass rate -- the *precision* question.
    pub required_n: u64,
    /// Samples needed for the **lower** confidence bound to reach the
    /// declared `target_yield` at the observed pass rate -- the *claim*
    /// question, and the one an evidence reviewer actually asks. `null` when
    /// no `target_yield` was declared, or when the observed pass rate is
    /// already at or below the target (in which case no sample count helps:
    /// the design, not the campaign, is what falls short).
    pub required_n_for_target: Option<u64>,
    /// `"sufficient"` / `"insufficient"`.
    pub verdict: String,
    /// How `required_n` was derived, so the number is checkable:
    /// `"clopper-pearson-zero-failures"` or `"normal-approximation"`.
    pub method: String,
    /// The same precision verdict as `observed_ci_halfwidth`/`required_n`
    /// above, but computed from `yield.variance_reduced` instead of the
    /// plain Clopper-Pearson empirical interval. `null` unless this
    /// measurement's `sampling.strategy` produced a `variance_reduced`
    /// estimate -- this is how a variance-reduction strategy's actual effect
    /// on the sample-size verdict is surfaced, not just its point estimate
    /// (issue #907, Phase 2b of epic #710).
    pub variance_reduced: Option<VarianceReducedSampleSize>,
}

/// The precision half of the sample-size verdict, recomputed from the
/// strategy-aware `yield.variance_reduced` estimate. There is no
/// `required_n`/`required_n_for_target` analog here in this phase -- only
/// the observed-width verdict, which is what a variance-reduction strategy's
/// convergence claim is checked against.
#[derive(Debug, Serialize)]
pub struct VarianceReducedSampleSize {
    pub observed_ci_halfwidth: f64,
    pub target_ci_halfwidth: f64,
    /// `"sufficient"` / `"insufficient"` -- precision only.
    pub verdict: String,
}

// --------------------------------------------------------------------------- //
// Sensitivity ranking (`klt yield-sensitivity`, issue #923, Phase 3 of the
// statistical/yield epic #710)
// --------------------------------------------------------------------------- //

/// Independently versioned from [`SCHEMA_VERSION`] above -- `klt
/// yield-sensitivity` is its own command per `docs/json-contract.md`
/// ("versioned per command, not globally").
pub const SENSITIVITY_SCHEMA_VERSION: u32 = 1;

/// Floor on usable samples per measurement. Below this, a correlation is not
/// so much noisy as undefined in spirit -- 4 points is the minimum this
/// crate considers worth ranking at all (2 would let a single pair of
/// samples dictate the entire ranking).
pub const SENSITIVITY_MIN_SAMPLES: usize = 4;

#[derive(Debug, Deserialize)]
pub struct SensitivityRequest {
    pub measurements: Vec<SensitivityMeasurementRequest>,
}

#[derive(Debug, Deserialize)]
pub struct SensitivityMeasurementRequest {
    pub name: String,
    #[serde(default)]
    pub unit: Option<String>,
    /// Per-sample parameter draws, one inner map per sample -- every sample
    /// must declare the same parameter name set (the Python reader enforces
    /// this before the request ever reaches this crate).
    pub parameters: Vec<std::collections::BTreeMap<String, f64>>,
    /// The output metric value for each sample, index-aligned with
    /// `parameters`.
    pub output: Vec<f64>,
}

#[derive(Debug, Serialize)]
pub struct SensitivityResponse {
    pub schema_version: u32,
    pub measurement_count: usize,
    pub measurements: Vec<SensitivityMeasurementReport>,
    pub warnings: Vec<String>,
}

#[derive(Debug, Serialize)]
pub struct SensitivityMeasurementReport {
    pub name: String,
    pub unit: Option<String>,
    pub n: u64,
    pub parameter_count: usize,
    pub method: SensitivityMethod,
    /// `null` when the regression could not be fit (see
    /// `method.regression_solvable`) -- the standardized-coefficient
    /// ranking's `R^2` has nothing to report in that case.
    pub r_squared: Option<f64>,
    /// Descending by `|contribution|`; ties broken by parameter name so the
    /// order is stable and checkable.
    pub ranking: Vec<SensitivityRankingEntry>,
    pub warnings: Vec<String>,
}

#[derive(Debug, Serialize)]
pub struct SensitivityMethod {
    /// `"standardized_regression_coefficient"` or `"pearson_correlation"`
    /// (the fallback used when the regression is not solvable -- see
    /// `regression_solvable`).
    pub primary: String,
    pub description: String,
    pub limitations: Vec<String>,
    /// `false` when `n` was too small relative to `parameter_count`, or the
    /// parameter-correlation matrix was numerically singular (e.g. two
    /// perfectly collinear parameters) -- `ranking[].standardized_coefficient`
    /// is `null` for every entry in that case, and `primary` falls back to
    /// `"pearson_correlation"`.
    pub regression_solvable: bool,
}

#[derive(Debug, Serialize)]
pub struct SensitivityRankingEntry {
    /// 1-indexed position in the ranking.
    pub rank: usize,
    pub parameter: String,
    /// The value `ranking` is sorted (descending, by absolute value) on --
    /// mirrors `standardized_coefficient` when the regression is solvable,
    /// `pearson_r` otherwise. Always present, so a consumer never has to
    /// branch on `method.primary` just to sort or thereshold.
    pub contribution: f64,
    /// `null` when `method.regression_solvable` is `false`.
    pub standardized_coefficient: Option<f64>,
    pub pearson_r: f64,
    pub spearman_rho: f64,
}
