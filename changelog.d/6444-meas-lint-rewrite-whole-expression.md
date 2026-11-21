- **`.meas` signal-position refusal now suggests a rewrite the lint accepts, and
  one that computes the author's number** (#6444, 2026-11-18): for a group of
  references, a call, or a two-node `v(a,b)` with an outer operator beside it
  (`2*abs(v(a)-v(b))`, `v(a)-v(b)*2`, `2*(v(a)-v(b))`, `(v(a)-v(b))*2`,
  `-v(a)+v(b)`), the first suggested `par('...')` span left the operator
  outside, so the suggestion was itself refused and only the second pass
  converged. The span now holds the whole local expression on the first pass.
  The second pass also lost parentheses (`2*v(a,b)` became
  `par('2*v(a)-v(b)')`, a lint-clean card that ngspice evaluates to a different
  crossing time); a two-node reference or inner `par('...')` span bound to an
  outer operator is now quoted parenthesised (`par('2*(v(a)-v(b))')`) where the
  operator would otherwise rebind it. The set of refused cards is unchanged and
  no JSON field changed; only the text of the refusal message differs.
