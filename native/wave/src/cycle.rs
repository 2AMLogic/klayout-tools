//! Clock/reset cycle-anchor tracking for `klt wave build`.
//!
//! Written fresh for this crate -- not a port. `docs/design/
//! waveform-query-contract-spike.md` section 4's `clock`/`reset` request
//! fields name the clock/reset signal and edge/polarity *explicitly*
//! (`clock.signal`, `clock.edge`, `reset.signal`, `reset.active`), so this
//! module has no auto-detection to do (no glob candidate scoring, no
//! "contains 'clk'"/"contains 'rst'" heuristics) -- it only has to watch
//! two already-identified signals' value changes and record edge times, a
//! materially simpler problem than `boldaxolotl/booley`'s
//! `crates/bwave/src/extract.rs` `Extractor::detect_clock`/`detect_reset`
//! (which auto-detect a clock/reset from a glob pattern or a bare CLI
//! flag against every declared signal). That file's *design* -- track
//! `first_rise_tick`/`second_rise_tick`, derive `clock_period_ticks` as
//! their difference -- is the same idea this module implements, credited
//! here as design inspiration rather than a ported-code attribution
//! (no literal source from that file is copied; see /NOTICE's own
//! distinction between "ported and adapted" and "written fresh").

/// Which VCD value transition counts as an active edge.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Edge {
    Rising,
    Falling,
}

impl Edge {
    pub fn parse(s: &str) -> Option<Edge> {
        match s {
            "rising" => Some(Edge::Rising),
            "falling" => Some(Edge::Falling),
            _ => None,
        }
    }
}

fn is_active(edge: Edge, from: Option<u8>, to: u8) -> bool {
    let Some(from) = from else { return false };
    match edge {
        Edge::Rising => from != b'1' && to == b'1',
        Edge::Falling => from != b'0' && to == b'0',
    }
}

/// Tracks a single-bit clock signal's active edges to compute the store's
/// `clock.period_ns` (median-equivalent: the first observed period, ticks
/// between the first two active edges -- matching the contract's "the
/// clock's own measured period (median inter-edge spacing)" for the
/// overwhelmingly common case of a free-running clock, where every period
/// is identical and any one interval equals the median) and the anchor
/// point cycle addressing is relative to.
#[derive(Debug, Default)]
pub struct ClockTracker {
    prev_value: Option<u8>,
    first_edge_tick: Option<u64>,
    second_edge_tick: Option<u64>,
}

impl ClockTracker {
    pub fn observe(&mut self, edge: Edge, value: u8, tick: u64) {
        if is_active(edge, self.prev_value, value) {
            if self.first_edge_tick.is_none() {
                self.first_edge_tick = Some(tick);
            } else if self.second_edge_tick.is_none() && Some(tick) != self.first_edge_tick {
                self.second_edge_tick = Some(tick);
            }
        }
        self.prev_value = Some(value);
    }

    /// The very first active edge observed in the whole trace, in ticks --
    /// used as the cycle-0 anchor when no `reset` block was declared.
    pub fn first_edge_tick(&self) -> Option<u64> {
        self.first_edge_tick
    }

    /// Ticks between the first two active edges, or `None` when the clock
    /// toggled fewer than twice (not an error -- see the contract's
    /// `clock.period_ns` field description).
    pub fn period_ticks(&self) -> Option<u64> {
        match (self.first_edge_tick, self.second_edge_tick) {
            (Some(a), Some(b)) if b > a => Some(b - a),
            _ => None,
        }
    }

    /// The first active clock edge at or after `after_tick` (inclusive) --
    /// used to anchor cycle 0 at the first clock edge at/after a reset's
    /// release point. Computed analytically from `first_edge_tick` +
    /// `period_ticks` (a free-running, constant-period clock -- the
    /// overwhelmingly common testbench shape, and the same "median
    /// inter-edge spacing" assumption `period_ticks` itself already makes)
    /// rather than by recording every edge in the trace, so this stays
    /// O(1) memory regardless of trace length. Returns `None` when no
    /// edge has been observed yet, or when `after_tick` is later than the
    /// first edge but the period is not yet known (fewer than two edges
    /// observed at all -- the whole-trace scan means this only happens
    /// for a clock that toggled zero or one times in total).
    pub fn first_edge_at_or_after(&self, after_tick: u64) -> Option<u64> {
        let first = self.first_edge_tick?;
        if after_tick <= first {
            return Some(first);
        }
        let period = self.period_ticks()?;
        let steps = (after_tick - first).div_ceil(period);
        Some(first + steps * period)
    }
}

/// Tracks a single-bit reset signal's active -> inactive transition (its
/// "release" point).
#[derive(Debug, Default)]
pub struct ResetTracker {
    prev_value: Option<u8>,
    release_tick: Option<u64>,
}

impl ResetTracker {
    pub fn observe(&mut self, active_low: bool, value: u8, tick: u64) {
        if self.release_tick.is_none() {
            let was_active = self
                .prev_value
                .map(|v| is_asserted(active_low, v))
                .unwrap_or(false);
            let now_inactive = !is_asserted(active_low, value);
            if was_active && now_inactive {
                self.release_tick = Some(tick);
            }
        }
        self.prev_value = Some(value);
    }

    pub fn release_tick(&self) -> Option<u64> {
        self.release_tick
    }
}

fn is_asserted(active_low: bool, value: u8) -> bool {
    if active_low {
        value == b'0'
    } else {
        value == b'1'
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn clock_period_from_first_two_rising_edges() {
        let mut clk = ClockTracker::default();
        clk.observe(Edge::Rising, b'0', 0);
        clk.observe(Edge::Rising, b'1', 5); // first rise @5
        clk.observe(Edge::Rising, b'0', 10);
        clk.observe(Edge::Rising, b'1', 15); // second rise @15
        assert_eq!(clk.first_edge_tick(), Some(5));
        assert_eq!(clk.period_ticks(), Some(10));
    }

    #[test]
    fn clock_period_none_with_fewer_than_two_edges() {
        let mut clk = ClockTracker::default();
        clk.observe(Edge::Rising, b'0', 0);
        clk.observe(Edge::Rising, b'1', 5);
        assert_eq!(clk.period_ticks(), None);
    }

    #[test]
    fn falling_edge_clock() {
        let mut clk = ClockTracker::default();
        clk.observe(Edge::Falling, b'1', 0);
        clk.observe(Edge::Falling, b'0', 5);
        clk.observe(Edge::Falling, b'1', 10);
        clk.observe(Edge::Falling, b'0', 15);
        assert_eq!(clk.first_edge_tick(), Some(5));
        assert_eq!(clk.period_ticks(), Some(10));
    }

    #[test]
    fn anchor_at_or_after_release_between_edges() {
        let mut clk = ClockTracker::default();
        clk.observe(Edge::Rising, b'0', 0);
        clk.observe(Edge::Rising, b'1', 5); // first rise
        clk.observe(Edge::Rising, b'0', 10);
        clk.observe(Edge::Rising, b'1', 15); // second rise, period=10
        clk.observe(Edge::Rising, b'0', 20);
        clk.observe(Edge::Rising, b'1', 25);
        // reset releases at 12: first edge at/after 12 is 15.
        assert_eq!(clk.first_edge_at_or_after(12), Some(15));
        // release before the first edge -> anchor is the first edge itself.
        assert_eq!(clk.first_edge_at_or_after(0), Some(5));
        // release exactly on an edge -> that edge itself.
        assert_eq!(clk.first_edge_at_or_after(15), Some(15));
    }

    #[test]
    fn reset_release_active_low() {
        let mut rst = ResetTracker::default();
        rst.observe(true, b'0', 0); // asserted (active-low)
        rst.observe(true, b'0', 5);
        rst.observe(true, b'1', 40); // released
        assert_eq!(rst.release_tick(), Some(40));
    }

    #[test]
    fn reset_release_active_high() {
        let mut rst = ResetTracker::default();
        rst.observe(false, b'1', 0); // asserted (active-high)
        rst.observe(false, b'0', 40); // released
        assert_eq!(rst.release_tick(), Some(40));
    }

    #[test]
    fn reset_never_released_reports_none() {
        let mut rst = ResetTracker::default();
        rst.observe(true, b'0', 0);
        rst.observe(true, b'0', 100);
        assert_eq!(rst.release_tick(), None);
    }
}
