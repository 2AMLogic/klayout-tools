//! Special functions and sample statistics for the `klt yield` core.
//!
//! Everything here is dependency-free numerics (no `statrs`/`nalgebra`): the
//! set of functions a yield estimate actually needs is small, and each one
//! below is a published, citable algorithm rather than a hand-rolled
//! approximation, so the reported numbers are checkable against a closed form
//! (see this module's own tests and `estimate.rs`'s closed-form case).
//!
//! - `erf`/`erfc` -- W. J. Cody, "Rational Chebyshev approximation for the
//!   error function", Math. Comp. 23 (1969), 631-638 (the `CALERF` rational
//!   approximations; ~1e-16 relative accuracy in double precision).
//! - `norm_ppf` -- P. J. Acklam's inverse-normal-CDF rational approximation,
//!   refined by one Halley step against `erfc` (full double accuracy).
//! - `ln_gamma` -- Lanczos approximation, g = 7, 9 coefficients.
//! - `betainc_reg` -- the regularized incomplete beta function via the
//!   standard modified-Lentz continued fraction; `beta_ppf` inverts it by
//!   bisection, which is what makes the **exact (Clopper-Pearson)** binomial
//!   confidence interval available rather than only a Wald/Wilson
//!   approximation.

use std::f64::consts::{PI, SQRT_2};

/// `1 / sqrt(pi)` -- Cody's `CALERF` uses this constant directly.
const ONE_OVER_SQRT_PI: f64 = 0.564189583547756286948079451561;

/// The complementary error function, `1 - erf(x)`, computed without
/// cancellation in the tail (the whole reason a yield tool needs `erfc`
/// rather than `1.0 - erf`).
pub fn erfc(x: f64) -> f64 {
    // Guarded up front: Cody's `exp(-y*y)` split evaluates `(y - y) * (y + y)`
    // on an infinite argument, which is `inf * 0` -> NaN. A one-sided limit
    // at an infinite spec limit is an ordinary case for this crate (`klt
    // yield` supports a min-only or max-only spec), so this is a real path,
    // not a defensive nicety.
    if x.is_infinite() {
        return if x > 0.0 { 0.0 } else { 2.0 };
    }
    let y = x.abs();
    if y <= 0.46875 {
        return 1.0 - erf_small(x);
    }
    let r = if y <= 4.0 { erfc_mid(y) } else { erfc_large(y) };
    if x < 0.0 {
        2.0 - r
    } else {
        r
    }
}

fn erf_small(x: f64) -> f64 {
    const A: [f64; 5] = [
        3.161_123_743_870_565_6e0,
        1.138_641_541_510_501_6e2,
        3.774_852_376_853_02e2,
        3.209_377_589_138_469_5e3,
        1.857_777_061_846_031_5e-1,
    ];
    const B: [f64; 4] = [
        2.360_129_095_234_412_1e1,
        2.440_246_379_344_441_7e2,
        1.282_616_526_077_372_3e3,
        2.844_236_833_439_171e3,
    ];
    let z = x * x;
    let mut xnum = A[4] * z;
    let mut xden = z;
    for i in 0..3 {
        xnum = (xnum + A[i]) * z;
        xden = (xden + B[i]) * z;
    }
    x * (xnum + A[3]) / (xden + B[3])
}

/// `erfc(y)` for `0.46875 < y <= 4`.
fn erfc_mid(y: f64) -> f64 {
    const C: [f64; 9] = [
        5.641_884_969_886_701e-1,
        8.883_149_794_388_376e0,
        6.611_919_063_714_163e1,
        2.986_351_381_974_001e2,
        8.819_522_212_417_69e2,
        1.712_047_612_634_070_6e3,
        2.051_078_377_826_071_5e3,
        1.230_339_354_797_997_3e3,
        2.153_115_354_744_038_5e-8,
    ];
    const D: [f64; 8] = [
        1.574_492_611_070_983_5e1,
        1.176_939_508_913_125e2,
        5.371_811_018_620_099e2,
        1.621_389_574_566_690_2e3,
        3.290_799_235_733_46e3,
        4.362_619_090_143_247e3,
        3.439_367_674_143_722e3,
        1.230_339_354_803_749_4e3,
    ];
    let mut xnum = C[8] * y;
    let mut xden = y;
    for i in 0..7 {
        xnum = (xnum + C[i]) * y;
        xden = (xden + D[i]) * y;
    }
    let result = (xnum + C[7]) / (xden + D[7]);
    scale_by_gaussian(y, result)
}

/// `erfc(y)` for `y > 4`.
fn erfc_large(y: f64) -> f64 {
    const P: [f64; 6] = [
        3.053_266_349_612_323_4e-1,
        3.603_448_999_498_044_4e-1,
        1.257_817_261_112_292_5e-1,
        1.608_378_514_874_228e-2,
        6.587_491_615_298_378e-4,
        1.631_538_713_730_209_8e-2,
    ];
    const Q: [f64; 5] = [
        2.568_520_192_289_822,
        1.872_952_849_923_460_5e0,
        5.279_051_029_514_284e-1,
        6.051_834_131_244_132e-2,
        2.335_204_976_268_691_9e-3,
    ];
    let z = 1.0 / (y * y);
    let mut xnum = P[5] * z;
    let mut xden = z;
    for i in 0..4 {
        xnum = (xnum + P[i]) * z;
        xden = (xden + Q[i]) * z;
    }
    let mut result = z * (xnum + P[4]) / (xden + Q[4]);
    result = (ONE_OVER_SQRT_PI - result) / y;
    scale_by_gaussian(y, result)
}

/// Cody's split of `exp(-y*y)` into an exactly-representable part and a
/// small correction, which is what keeps the tail accurate.
fn scale_by_gaussian(y: f64, result: f64) -> f64 {
    let ysq = (y * 16.0).trunc() / 16.0;
    let del = (y - ysq) * (y + ysq);
    (-ysq * ysq).exp() * (-del).exp() * result
}

/// Standard-normal CDF.
pub fn norm_cdf(x: f64) -> f64 {
    0.5 * erfc(-x / SQRT_2)
}

/// Standard-normal PDF.
pub fn norm_pdf(x: f64) -> f64 {
    (-0.5 * x * x).exp() / (2.0 * PI).sqrt()
}

/// Standard-normal quantile (inverse CDF) for `0 < p < 1`.
///
/// Returns `-inf`/`+inf` at `p == 0`/`p == 1`, and NaN outside `[0, 1]`.
pub fn norm_ppf(p: f64) -> f64 {
    if p.is_nan() || !(0.0..=1.0).contains(&p) {
        return f64::NAN;
    }
    if p == 0.0 {
        return f64::NEG_INFINITY;
    }
    if p == 1.0 {
        return f64::INFINITY;
    }

    const A: [f64; 6] = [
        -3.969_683_028_665_376e1,
        2.209_460_984_245_205e2,
        -2.759_285_104_469_687e2,
        1.383_577_518_672_69e2,
        -3.066_479_806_614_716e1,
        2.506_628_277_459_239e0,
    ];
    const B: [f64; 5] = [
        -5.447_609_879_822_406e1,
        1.615_858_368_580_409e2,
        -1.556_989_798_598_866e2,
        6.680_131_188_771_972e1,
        -1.328_068_155_288_572e1,
    ];
    const C: [f64; 6] = [
        -7.784_894_002_430_293e-3,
        -3.223_964_580_411_365e-1,
        -2.400_758_277_161_838e0,
        -2.549_732_539_343_734e0,
        4.374_664_141_464_968e0,
        2.938_163_982_698_783e0,
    ];
    const D: [f64; 4] = [
        7.784_695_709_041_462e-3,
        3.224_671_290_700_398e-1,
        2.445_134_137_142_996e0,
        3.754_408_661_907_416e0,
    ];
    const P_LOW: f64 = 0.02425;

    let mut x = if p < P_LOW {
        let q = (-2.0 * p.ln()).sqrt();
        (((((C[0] * q + C[1]) * q + C[2]) * q + C[3]) * q + C[4]) * q + C[5])
            / ((((D[0] * q + D[1]) * q + D[2]) * q + D[3]) * q + 1.0)
    } else if p <= 1.0 - P_LOW {
        let q = p - 0.5;
        let r = q * q;
        (((((A[0] * r + A[1]) * r + A[2]) * r + A[3]) * r + A[4]) * r + A[5]) * q
            / (((((B[0] * r + B[1]) * r + B[2]) * r + B[3]) * r + B[4]) * r + 1.0)
    } else {
        let q = (-2.0 * (1.0 - p).ln()).sqrt();
        -(((((C[0] * q + C[1]) * q + C[2]) * q + C[3]) * q + C[4]) * q + C[5])
            / ((((D[0] * q + D[1]) * q + D[2]) * q + D[3]) * q + 1.0)
    };

    // One Halley refinement step against the (full-accuracy) `erfc` above.
    let e = 0.5 * erfc(-x / SQRT_2) - p;
    let u = e * (2.0 * PI).sqrt() * (x * x / 2.0).exp();
    x -= u / (1.0 + x * u / 2.0);
    x
}

/// Lanczos log-gamma (`g = 7`, 9 coefficients), valid for `x > 0`.
pub fn ln_gamma(x: f64) -> f64 {
    const G: [f64; 9] = [
        0.999_999_999_999_809_9,
        676.520_368_121_885_1,
        -1_259.139_216_722_402_8,
        771.323_428_777_653_1,
        -176.615_029_162_140_6,
        12.507_343_278_686_905,
        -0.138_571_095_265_720_12,
        9.984_369_578_019_572e-6,
        1.505_632_735_149_311_6e-7,
    ];
    if x < 0.5 {
        // Reflection: not needed by any caller here (a, b are always > 0),
        // but keeping the function total avoids a silent NaN if it ever is.
        return (PI / (PI * x).sin()).ln() - ln_gamma(1.0 - x);
    }
    let x = x - 1.0;
    let mut a = G[0];
    let t = x + 7.5;
    for (i, g) in G.iter().enumerate().skip(1) {
        a += g / (x + i as f64);
    }
    0.5 * (2.0 * PI).ln() + (x + 0.5) * t.ln() - t + a.ln()
}

/// Regularized incomplete beta function `I_x(a, b)`.
pub fn betainc_reg(a: f64, b: f64, x: f64) -> f64 {
    if x <= 0.0 {
        return 0.0;
    }
    if x >= 1.0 {
        return 1.0;
    }
    let ln_bt = ln_gamma(a + b) - ln_gamma(a) - ln_gamma(b) + a * x.ln() + b * (1.0 - x).ln();
    let bt = ln_bt.exp();
    if x < (a + 1.0) / (a + b + 2.0) {
        bt * betacf(a, b, x) / a
    } else {
        1.0 - bt * betacf(b, a, 1.0 - x) / b
    }
}

/// Continued-fraction expansion for the incomplete beta function (modified
/// Lentz algorithm).
fn betacf(a: f64, b: f64, x: f64) -> f64 {
    const MAX_ITER: usize = 300;
    const EPS: f64 = 3.0e-16;
    const FPMIN: f64 = 1.0e-300;

    let qab = a + b;
    let qap = a + 1.0;
    let qam = a - 1.0;
    let mut c = 1.0;
    let mut d = 1.0 - qab * x / qap;
    if d.abs() < FPMIN {
        d = FPMIN;
    }
    d = 1.0 / d;
    let mut h = d;
    for m in 1..=MAX_ITER {
        let m_f = m as f64;
        let m2 = 2.0 * m_f;

        let aa = m_f * (b - m_f) * x / ((qam + m2) * (a + m2));
        d = 1.0 + aa * d;
        if d.abs() < FPMIN {
            d = FPMIN;
        }
        c = 1.0 + aa / c;
        if c.abs() < FPMIN {
            c = FPMIN;
        }
        d = 1.0 / d;
        h *= d * c;

        let aa = -(a + m_f) * (qab + m_f) * x / ((a + m2) * (qap + m2));
        d = 1.0 + aa * d;
        if d.abs() < FPMIN {
            d = FPMIN;
        }
        c = 1.0 + aa / c;
        if c.abs() < FPMIN {
            c = FPMIN;
        }
        d = 1.0 / d;
        let del = d * c;
        h *= del;
        if (del - 1.0).abs() < EPS {
            break;
        }
    }
    h
}

/// Inverse of [`betainc_reg`] in `x`, by bisection.
///
/// Bisection (not Newton) on purpose: `I_x(a, b)` is monotone in `x` on
/// `[0, 1]`, so 200 halvings pin `x` to well under 1 ulp with no derivative
/// evaluation and no convergence failure mode to handle. The cost is
/// irrelevant here -- this is called at most twice per measurement.
pub fn beta_ppf(p: f64, a: f64, b: f64) -> f64 {
    if p <= 0.0 {
        return 0.0;
    }
    if p >= 1.0 {
        return 1.0;
    }
    let (mut lo, mut hi) = (0.0f64, 1.0f64);
    for _ in 0..200 {
        let mid = 0.5 * (lo + hi);
        if betainc_reg(a, b, mid) < p {
            lo = mid;
        } else {
            hi = mid;
        }
    }
    0.5 * (lo + hi)
}

/// Arithmetic mean. Panics-free: caller guarantees a non-empty slice.
pub fn mean(xs: &[f64]) -> f64 {
    xs.iter().sum::<f64>() / xs.len() as f64
}

/// Bessel-corrected (`n - 1`) **sample** standard deviation, matching the
/// estimator `klt sim`'s own `monte_carlo.stddev` reports. `None` for
/// `n < 2`, where it is undefined -- never faked as `0.0`.
pub fn sample_stddev(xs: &[f64]) -> Option<f64> {
    if xs.len() < 2 {
        return None;
    }
    let m = mean(xs);
    let ss: f64 = xs.iter().map(|x| (x - m) * (x - m)).sum();
    Some((ss / (xs.len() as f64 - 1.0)).sqrt())
}

/// Central moment of order `k` (population form, divisor `n`).
fn central_moment(xs: &[f64], k: i32) -> f64 {
    let m = mean(xs);
    xs.iter().map(|x| (x - m).powi(k)).sum::<f64>() / xs.len() as f64
}

/// Fisher-Pearson sample skewness `g1` (population form). `None` when the
/// spread is zero (undefined).
pub fn skewness(xs: &[f64]) -> Option<f64> {
    let m2 = central_moment(xs, 2);
    if m2 <= 0.0 {
        return None;
    }
    Some(central_moment(xs, 3) / m2.powf(1.5))
}

/// Excess kurtosis `g2` (population form; `0` for a normal). `None` when the
/// spread is zero.
pub fn excess_kurtosis(xs: &[f64]) -> Option<f64> {
    let m2 = central_moment(xs, 2);
    if m2 <= 0.0 {
        return None;
    }
    Some(central_moment(xs, 4) / (m2 * m2) - 3.0)
}

/// Linearly interpolated quantile of an already-sorted slice, using the
/// same `(n - 1) * q` order-statistic interpolation `klt sim`'s
/// `monte_carlo.quantiles` documents.
pub fn quantile_sorted(sorted: &[f64], q: f64) -> f64 {
    let n = sorted.len();
    if n == 1 {
        return sorted[0];
    }
    let h = (n as f64 - 1.0) * q;
    let lo = h.floor() as usize;
    let hi = h.ceil() as usize;
    if lo == hi {
        return sorted[lo];
    }
    sorted[lo] + (h - lo as f64) * (sorted[hi] - sorted[lo])
}

/// Exact (Clopper-Pearson) two-sided confidence interval for a binomial
/// proportion `k / n` at confidence `conf`.
///
/// Exact rather than Wald/Wilson because a yield estimate lives where the
/// normal approximation is worst: `p` near 1, often with **zero** observed
/// failures, where a Wald interval degenerates to the useless `[1, 1]`.
/// Clopper-Pearson stays honest there (`[(alpha/2)^(1/n), 1]`).
pub fn clopper_pearson(k: u64, n: u64, conf: f64) -> (f64, f64) {
    let alpha = 1.0 - conf;
    let (k_f, n_f) = (k as f64, n as f64);
    let low = if k == 0 {
        0.0
    } else {
        beta_ppf(alpha / 2.0, k_f, n_f - k_f + 1.0)
    };
    let high = if k == n {
        1.0
    } else {
        beta_ppf(1.0 - alpha / 2.0, k_f + 1.0, n_f - k_f)
    };
    (low, high)
}

/// The Anderson-Darling normality statistic with **both** parameters
/// estimated from the sample, adjusted for finite `n` (`A2*` in the
/// Stephens 1974 convention).
///
/// `None` when the sample has no spread (the statistic is undefined).
pub fn anderson_darling_normal(sorted: &[f64]) -> Option<f64> {
    let n = sorted.len();
    if n < 2 {
        return None;
    }
    let m = mean(sorted);
    let s = sample_stddev(sorted)?;
    if s <= 0.0 {
        return None;
    }
    let mut acc = 0.0;
    for i in 0..n {
        let p_i = norm_cdf((sorted[i] - m) / s).clamp(1e-300, 1.0 - 1e-16);
        let p_rev = norm_cdf((sorted[n - 1 - i] - m) / s).clamp(1e-300, 1.0 - 1e-16);
        acc += (2.0 * (i as f64 + 1.0) - 1.0) * (p_i.ln() + (1.0 - p_rev).ln());
    }
    let a2 = -(n as f64) - acc / n as f64;
    let n_f = n as f64;
    Some(a2 * (1.0 + 0.75 / n_f + 2.25 / (n_f * n_f)))
}

/// Stephens' 5% critical value for [`anderson_darling_normal`]'s adjusted
/// statistic when mean and variance are both estimated.
pub const AD_CRITICAL_VALUE_5PCT: f64 = 0.787;

/// Below this sample count the Anderson-Darling critical values are not
/// considered applicable, and the verdict is reported as
/// `"insufficient_samples"` rather than a false "consistent".
pub const AD_MIN_SAMPLES: usize = 8;

#[cfg(test)]
mod tests {
    use super::*;

    fn close(a: f64, b: f64, tol: f64) {
        assert!((a - b).abs() <= tol, "{a} != {b} (tol {tol})");
    }

    /// `erfc` against `erf` reference values from the DLMF / standard
    /// tables, as `erf(x) == 1 - erfc(x)` (exact to within rounding for the
    /// moderate arguments used here).
    #[test]
    fn erfc_matches_reference_erf_values() {
        close(1.0 - erfc(0.0), 0.0, 1e-15);
        close(1.0 - erfc(0.5), 0.520_499_877_813_046_5, 1e-14);
        close(1.0 - erfc(1.0), 0.842_700_792_949_714_9, 1e-14);
        close(1.0 - erfc(2.0), 0.995_322_265_018_952_7, 1e-14);
        close(1.0 - erfc(-1.5), -0.966_105_146_475_310_7, 1e-14);
    }

    #[test]
    fn erfc_stays_accurate_in_the_far_tail() {
        // 1 - erf(6) would be 0 in double precision; erfc must not be.
        close(erfc(6.0), 2.151_973_671_249_891e-17, 1e-30);
        close(erfc(10.0), 2.088_487_583_762_545e-45, 1e-58);
    }

    #[test]
    fn norm_cdf_matches_the_three_sigma_points() {
        close(norm_cdf(0.0), 0.5, 1e-15);
        close(norm_cdf(1.0), 0.841_344_746_068_542_9, 1e-14);
        close(norm_cdf(-3.0), 1.349_898_031_630_095e-3, 1e-16);
        // The classic 6-sigma one-sided tail.
        close(norm_cdf(-6.0), 9.865_876_450_376_946e-10, 1e-22);
    }

    #[test]
    fn norm_ppf_inverts_norm_cdf() {
        for p in [
            1e-9,
            1e-4,
            0.01,
            0.025,
            0.1,
            0.5,
            0.9,
            0.975,
            0.999,
            1.0 - 1e-9,
        ] {
            let x = norm_ppf(p);
            close(norm_cdf(x), p, 1e-13 * p.max(1e-9));
        }
        // The two-sided 95% z multiplier every CI in this crate uses.
        close(norm_ppf(0.975), 1.959_963_984_540_054, 1e-12);
    }

    #[test]
    fn ln_gamma_matches_factorials() {
        close(ln_gamma(1.0), 0.0, 1e-12);
        close(ln_gamma(5.0), 24.0f64.ln(), 1e-12);
        close(ln_gamma(0.5), PI.sqrt().ln(), 1e-12);
    }

    #[test]
    fn betainc_reg_matches_closed_forms() {
        // I_x(1, 1) == x.
        close(betainc_reg(1.0, 1.0, 0.25), 0.25, 1e-14);
        // I_x(2, 1) == x^2.
        close(betainc_reg(2.0, 1.0, 0.3), 0.09, 1e-14);
        // Symmetry: I_x(a, b) == 1 - I_{1-x}(b, a).
        close(
            betainc_reg(3.5, 2.5, 0.4),
            1.0 - betainc_reg(2.5, 3.5, 0.6),
            1e-13,
        );
    }

    #[test]
    fn beta_ppf_inverts_betainc_reg() {
        for p in [0.001, 0.025, 0.5, 0.975, 0.999] {
            let x = beta_ppf(p, 3.0, 7.0);
            close(betainc_reg(3.0, 7.0, x), p, 1e-12);
        }
    }

    #[test]
    fn clopper_pearson_matches_the_rule_of_three() {
        // Zero failures in n: the exact upper bound on the failure rate is
        // 1 - (alpha/2)^(1/n), whose small-alpha approximation is the
        // textbook "rule of three" (~3/n at 95%).
        let (low, high) = clopper_pearson(100, 100, 0.95);
        assert_eq!(high, 1.0);
        close(low, 0.025f64.powf(1.0 / 100.0), 1e-12);
        assert!((1.0 - low) < 3.7 / 100.0 && (1.0 - low) > 3.0 / 100.0);

        // 2 failures in 100 at 95%. Reference values obtained by solving
        // the defining binomial tail equations directly --
        // `P(X >= 98 | n = 100, p_low) = 0.025` and
        // `P(X <= 98 | n = 100, p_high) = 0.025` -- by bisection over an
        // exact `math.comb`-based sum, independent of this crate's
        // incomplete-beta route to the same numbers.
        let (low, high) = clopper_pearson(98, 100, 0.95);
        close(low, 0.929_616_067_528_929_8, 1e-12);
        close(high, 0.997_568_663_176_057_4, 1e-12);
    }

    #[test]
    fn sample_stddev_is_bessel_corrected_and_undefined_below_two() {
        assert!(sample_stddev(&[1.0]).is_none());
        close(sample_stddev(&[1.0, 3.0]).unwrap(), 2.0f64.sqrt(), 1e-15);
    }

    #[test]
    fn anderson_darling_rejects_a_flat_uniform_sample() {
        // A perfectly uniform grid is decidedly not normal: with n = 200 the
        // adjusted statistic must exceed the 5% critical value.
        let xs: Vec<f64> = (0..200).map(|i| i as f64 / 199.0).collect();
        let a2 = anderson_darling_normal(&xs).unwrap();
        assert!(a2 > AD_CRITICAL_VALUE_5PCT, "A2* = {a2}");
    }

    #[test]
    fn anderson_darling_accepts_an_exact_normal_grid() {
        // Inverse-CDF-spaced samples are as normal as a finite sample gets.
        let n = 200;
        let xs: Vec<f64> = (1..=n)
            .map(|i| norm_ppf((i as f64 - 0.5) / n as f64))
            .collect();
        let a2 = anderson_darling_normal(&xs).unwrap();
        assert!(a2 < AD_CRITICAL_VALUE_5PCT, "A2* = {a2}");
    }
}
