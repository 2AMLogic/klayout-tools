//! JSON in/out shapes for the FLUTE/RUDY-family post-placement congestion
//! pre-check core (issue #785, Epic #700 Phase 1 §3.6 of
//! `docs/design/place-and-route-improvements-survey.md`).
//!
//! Mirrors `native/mom/src/contract.rs`'s split: this module only defines
//! the solver's own request/response payload. Turning a placement DEF into
//! this request shape (parsing `COMPONENTS`/`NETS`/`PINS`) is the Python
//! layer's job (`src/klayout_tools/congestion.py`), matching every other
//! `klt` native engine's Rust/Python boundary.

use serde::{Deserialize, Serialize};

/// Bumped only on a non-additive (breaking) change to this crate's own JSON
/// shape.
pub const RESPONSE_SCHEMA_VERSION: u32 = 1;

/// Default assumed usable signal-routing layer count when a request omits
/// `layer_count`. Not tied to any one PDK -- sky130hd's own
/// `set_routing_layers -signal met1-met5` range (see
/// `place_and_route.py`'s `_ROUTING_LAYER_RANGE`) has 5 layers, but met1 is
/// heavily used for standard-cell pin access/power rails rather than free
/// signal routing -- 3 is a conservative, documented default, not a
/// technology-calibrated constant. Callers building a request from a real
/// PDK should pass an explicit value.
pub const DEFAULT_LAYER_COUNT: f64 = 3.0;

/// Default assumed routing track pitch (micrometers) when a request omits
/// `track_pitch_um`. Matches sky130hd's own met2 vertical-track pitch (DEF
/// `STEP 460` = 0.46um, confirmed against this issue's own worked-example
/// placement DEF) -- a reasonable default for that PDK family, not a
/// universal constant. Callers targeting a different PDK should pass an
/// explicit value derived from its own tech LEF.
pub const DEFAULT_TRACK_PITCH_UM: f64 = 0.46;

#[derive(Debug, Deserialize)]
pub struct CongestionRequest {
    /// The placement region this estimate covers, in micrometers -- the
    /// core area is the natural choice (matches OpenROAD's own
    /// `core_area_um2` metric already in `klt place-and-route`'s response),
    /// but any axis-aligned region containing every net's pins is valid.
    pub die_box_um: BoxUm,
    /// Grid resolution for the RUDY-style demand map. Larger grids resolve
    /// congestion hot-spots more precisely at proportionally higher cost;
    /// this is a genuinely hot loop (the survey's own framing), so both
    /// dimensions are caller-controlled rather than fixed.
    pub grid_cols: u32,
    pub grid_rows: u32,
    /// Assumed usable signal-routing layer count for the capacity model
    /// (see `DEFAULT_LAYER_COUNT`). Optional -- omit to use the default.
    #[serde(default)]
    pub layer_count: Option<f64>,
    /// Assumed routing track pitch in micrometers for the capacity model
    /// (see `DEFAULT_TRACK_PITCH_UM`). Optional -- omit to use the default.
    #[serde(default)]
    pub track_pitch_um: Option<f64>,
    pub nets: Vec<NetRequest>,
}

#[derive(Debug, Deserialize, Clone, Copy)]
pub struct BoxUm {
    pub x0_um: f64,
    pub y0_um: f64,
    pub x1_um: f64,
    pub y1_um: f64,
}

#[derive(Debug, Deserialize)]
pub struct NetRequest {
    // Not read by the estimator itself (nets are anonymous to the RUDY/RSMT
    // math) -- carried through the request/`Debug` shape only so a caller
    // building/debugging a request (or a future per-net diagnostic) can
    // identify which net a given entry came from.
    #[allow(dead_code)]
    pub name: String,
    /// Pin locations in micrometers -- one point per net terminal
    /// (component-center approximation is the caller's choice, e.g. a
    /// standard cell's `COMPONENTS ... PLACED` location rather than its
    /// exact pin geometry; see `congestion.py`'s own docstring for why that
    /// approximation is appropriate at this stage). Nets with fewer than 2
    /// pins carry no routing demand and are skipped (reported in
    /// `skipped_single_pin_nets`).
    pub pins: Vec<PointUm>,
}

#[derive(Debug, Deserialize, Clone, Copy)]
pub struct PointUm {
    pub x_um: f64,
    pub y_um: f64,
}

#[derive(Debug, Serialize)]
pub struct CongestionResponse {
    pub schema_version: u32,
    pub grid_cols: u32,
    pub grid_rows: u32,
    pub tile_w_um: f64,
    pub tile_h_um: f64,
    /// Assumed wire-length capacity per tile, in micrometers, used to
    /// normalize the density map into `overflow_tile_count`/
    /// `congestion_score` (see `grid::tile_capacity_um`).
    pub capacity_um_per_tile: f64,
    /// Nets actually contributing routing demand (>= 2 pins).
    pub net_count: usize,
    /// Nets with 0 or 1 pins -- no routing demand, excluded from every
    /// statistic below.
    pub skipped_single_pin_nets: usize,
    /// Sum of every net's RSMT-family length estimate (see `rsmt.rs`),
    /// informational -- lets a caller sanity-check against a pure-HPWL
    /// total independently of the grid/capacity choice.
    pub total_rsmt_length_um: f64,
    /// Per-tile wire-demand density (um of estimated wire per um^2 of tile
    /// area), summary statistics across the whole grid.
    pub mean_density_um_per_um2: f64,
    pub max_density_um_per_um2: f64,
    pub p95_density_um_per_um2: f64,
    /// Tiles whose demand exceeds `capacity_um_per_tile` (grid cells over
    /// their assumed routing budget).
    pub overflow_tile_count: u32,
    pub overflow_tile_fraction: f64,
    /// Headline scalar summarizing the whole map for candidate ranking:
    /// `max(overflow_tile_fraction, max_density_um_per_um2 / (capacity_um_per_tile / tile_area_um2))`,
    /// i.e. whichever of "how much of the design is over-budget" or "how
    /// bad is the worst spot" is more pessimistic. Never the only signal a
    /// caller should look at -- the correlation study
    /// (`scripts/research/flute_congestion_correlation.py`) evaluates this
    /// alongside the raw density/overflow fields to see which single
    /// number (if any) is worth using for real gating.
    pub congestion_score: f64,
}
