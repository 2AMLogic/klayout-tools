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
}

#[derive(Debug, Clone, Copy, Deserialize, Serialize)]
pub struct Limits {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub min: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub max: Option<f64>,
    /// Optional yield the design is claimed to meet. When present, the
    /// measurement passes only if the **lower** confidence bound of the
    /// empirical yield reaches it -- a claim at the stated confidence, not a
    /// point estimate that happened to clear the bar.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub target_yield: Option<f64>,
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
    pub status: String,
    pub warnings: Vec<String>,
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
}
