"""Opt-in per-net layer suggestion for ``klt gen-compose`` routing (#1962).

The ordinary router decides one net at a time, in ``connectivity[]`` order:
a leg rejected for crossing an already-accepted net reports *that* net and
*that* leg, and the caller iterates -- one ``gen-compose`` call per
layer/waypoint fix -- with no view of which *combination* of per-net planes
would route the whole net list. Once a composition has more crossing nets
than the caller has patience (issue #1962's ~10-net report, its 22- and
65-net follow-ups), the iterate-until-it-sticks loop does not terminate in
practice, and the per-leg ``blocking_net`` diagnostics (#6961) only make
each individual failure legible, not the assignment problem solvable.

``routing.suggest_layers: true`` (:func:`compose_with_layer_suggestions`,
entered from :func:`klayout_tools.gen_compose.compose` -- this module is
deliberately separate because ``gen_compose.py`` sits at its per-file
size-ratchet ceiling) turns the request into a bounded, deterministic
search over per-net ``connectivity[].layer_role`` assignments for the nets
the caller left unpinned:

* **Candidates are full ordinary ``compose()`` runs.** Each candidate is the
  caller's own request with ``layer_role`` injected on a subset of the
  unpinned nets -- the exact request a caller replaying the suggestion
  would run -- so every check the ordinary router applies (route-vs-route,
  via ladders and their landing pads, block geometry, deck width floors)
  evaluates the candidate. Bounding-box overlap alone cannot establish that
  a concrete route will fail, so nothing cheaper is trusted.
* **Caller pins are honoured.** A net that already names ``layer_role`` is
  never touched, and the search only ever adds ``layer_role`` keys: pins,
  ``waypoints_um``, placement and widths pass through verbatim.
* **Ordered by total rank.** Candidates are enumerated with every net left
  as-is first (the baseline, i.e. exactly today's behaviour), then
  assignments ranked by layer preference (higher planes first, then lower
  planes). The lowest total rank goes first, with ties broken in declaration
  order -- so earlier-declared nets stay low, matching the router's own
  first-routed-net-wins order. The first assignment that routes every net
  wins; the enumeration order is fixed, so the mapping is deterministic.
* **Bounded.** The search stops after ``routing.suggest_layers_budget``
  compose runs (default :data:`DEFAULT_SUGGESTION_BUDGET`, baseline
  included). Stopping at the budget reports ``budget_exhausted``; covering
  the entire candidate space without a winner reports ``conflict`` --
  the distinction the issue asks for. The selected assignment or original
  request is run once at the caller's output (outside the search budget),
  so the response and the written GDS are exactly what replaying the
  suggestion through the ordinary command produces.
"""

from __future__ import annotations

import copy
import os
import shutil
import tempfile
from collections.abc import Iterator
from typing import Any, NamedTuple

from ._jsonnum import is_int
from .gen_compose_base import GenComposeError, LayerRuleError
from .gen_compose_layer_rules import alternative_routing_roles
from .gen_compose_port_request import CODE_CONFLICT

#: Compose runs (baseline included) one suggestion request may spend before
#: the search stops and reports ``budget_exhausted``. Each unit is one full
#: ``compose()`` evaluation, so this is the knob that keeps the mode bounded
#: on the large compositions the issue describes (65 nets and up), where the
#: candidate space is astronomically larger than any practical budget.
DEFAULT_SUGGESTION_BUDGET = 32

_STATUS_RESOLVED = "resolved"
_STATUS_CONFLICT = "conflict"
_STATUS_BUDGET_EXHAUSTED = "budget_exhausted"


def compose_with_layer_suggestions(
    request: dict[str, Any], request_dir: str | None = None
) -> dict[str, Any]:
    """Run ``request``'s composition as a per-net layer-role search (#1962).

    Entered from :func:`klayout_tools.gen_compose.compose` when the caller
    sets ``routing.suggest_layers``; the search contract is the module
    docstring's. Raises :class:`GenComposeError` for a malformed mode
    request (non-boolean flag, non-positive budget, an empty
    ``connectivity[]``, a routing spec carrying nothing but the suggestion
    keys, or nothing left to search); every other request error is the
    ordinary router's own, raised by the baseline run before the search
    starts. Returns the ordinary compose response plus one additive
    ``layer_suggestions`` block (``status``/``assignments`` on success,
    ``status``/``unresolved_nets``/``note`` on failure).
    """
    from .gen_compose import compose  # deferred: gen_compose imports this module

    connectivity = _validated_connectivity(request)
    budget = _parse_budget(
        request["routing"].get("suggest_layers_budget", DEFAULT_SUGGESTION_BUDGET)
    )

    # Trials never open the caller's layout. Only a completed search publishes
    # a result, using ordinary compose so output format and errors are preserved.
    plain = _assignment_request(request, (), (), ())
    with tempfile.TemporaryDirectory(prefix="klt-gen-compose-baseline-") as scratch:
        staged = _assignment_request(
            request, (), (), (), os.path.join(scratch, "baseline.gds")
        )
        baseline = compose(staged, request_dir)
    searchable = tuple(
        index
        for index, entry in enumerate(connectivity)
        if entry.get("layer_role") is None
    )
    if not baseline.get("unrouted_nets"):
        # Nothing to fix: the request already routes every net exactly as
        # written, so the answer is the empty mapping -- never a search.
        return _with_suggestions(
            compose(plain, request_dir),
            {
                "status": _STATUS_RESOLVED,
                "assignments": {},
                "nets_searched": len(searchable),
                "candidates_evaluated": 1,
                "note": "the request routes every net as written",
            },
        )
    if not searchable:
        raise GenComposeError(
            "request.routing.suggest_layers: every connectivity[] net already "
            "names its own layer_role -- there is nothing to search"
        )
    roles = alternative_routing_roles(
        baseline["pdk"]["variant"], request["routing"]["layer_role"]
    )
    if not roles:
        raise GenComposeError(
            "request.routing.suggest_layers: routing.layer_role resolves to no "
            "alternative plane in this PDK variant's routing-metals stack -- "
            "no layer assignment to search"
        )
    labels = [connectivity[index]["net"] for index in searchable]
    space = (len(roles) + 1) ** len(searchable)

    search = _search_for_assignment(
        compose,
        request,
        request_dir,
        searchable,
        roles,
        budget,
        list(baseline["unrouted_nets"]),
    )
    common = {
        "nets_searched": len(searchable),
        "candidates_evaluated": search.evaluated,
        "candidate_space": space,
        "roles": list(roles),
    }
    if search.winner is not None:
        # Re-run the winning assignment once at the caller's own output
        # path, so the returned report and the written GDS are exactly what
        # replaying the suggestion through the ordinary command produces.
        final = compose(
            _assignment_request(request, searchable, roles, search.winner),
            request_dir,
        )
        return _with_suggestions(
            final,
            {
                "status": _STATUS_RESOLVED,
                "assignments": _assignments(labels, roles, search.winner),
                **common,
            },
        )
    note = (
        "every candidate was evaluated and none routed every net. "
        "unresolved_nets lists the nets left open in the closest trial. "
        "The saved layout and per-leg diagnostics describe the original request."
        if search.covered_space
        else "the search stopped at routing.suggest_layers_budget "
        "before the candidate space was covered -- raise the budget or pin "
        "connectivity[].layer_role values by hand"
    )
    return _with_suggestions(
        compose(plain, request_dir),
        {
            "status": _STATUS_CONFLICT
            if search.covered_space
            else _STATUS_BUDGET_EXHAUSTED,
            "assignments": {},
            "unresolved_nets": search.best_unrouted,
            "note": note,
            **common,
        },
    )


def ordinary_routing(routing: dict[str, Any]) -> dict[str, Any]:
    """Return the routing specification passed to an ordinary compose call."""
    return {
        key: value
        for key, value in routing.items()
        if key not in ("suggest_layers", "suggest_layers_budget")
    }


def suggestions_enabled(routing: dict[str, Any]) -> bool:
    enabled = routing.get("suggest_layers", False)
    if not isinstance(enabled, bool):
        raise GenComposeError(
            "request.routing.suggest_layers must be a boolean when given"
        )
    return enabled


def _validated_connectivity(request: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate the mode's own request shape, returning ``connectivity``.

    Every check here guards a caller error only this mode introduces (the
    flag's type, the budget, a routing object carrying nothing but the
    suggestion keys, an empty ``connectivity[]``); every *ordinary* request
    error is left to the baseline run, which raises the canonical message.
    """
    routing = request.get("routing") or {}
    if not set(routing) - {"suggest_layers", "suggest_layers_budget"}:
        raise GenComposeError(
            "request.routing.suggest_layers requires an ordinary routing spec "
            "(routing.layer_role and routing.width_um) to search around -- "
            "request.routing carries nothing else"
        )
    connectivity = request.get("connectivity")
    if not isinstance(connectivity, list) or not connectivity:
        raise GenComposeError(
            "request.routing.suggest_layers requires a non-empty connectivity[] "
            "to suggest layer roles for"
        )
    if not all(isinstance(entry, dict) for entry in connectivity):
        raise GenComposeError("request.connectivity must be an array of objects")
    names: set[str] = set()
    for entry in connectivity:
        name = entry.get("net")
        if isinstance(name, str):
            if name in names:
                raise GenComposeError(
                    "request.routing.suggest_layers requires unique connectivity[].net "
                    f"names to replay assignments; repeated net: {name!r}"
                )
            names.add(name)
    return connectivity


class _SearchOutcome(NamedTuple):
    """What one bounded candidate search found."""

    #: The first climb tuple that routed every net, or ``None``.
    winner: tuple[int, ...] | None
    #: Compose runs spent, the baseline included.
    evaluated: int
    #: Unrouted nets of the closest candidate (the baseline's at worst).
    best_unrouted: list[str]
    #: True when the generator ran out: the whole space was evaluated.
    covered_space: bool


def _search_for_assignment(
    compose: Any,
    request: dict[str, Any],
    request_dir: str | None,
    searchable: tuple[int, ...],
    roles: tuple[str, ...],
    budget: int,
    baseline_unrouted: list[str],
) -> _SearchOutcome:
    """Evaluate candidates until a win, the budget, or the space runs out.

    Each candidate runs against a throwaway output. A deck floor violation
    or a collision between a candidate route and a requested pin stub
    rules out that assignment. Fixed pin conflicts and I/O errors propagate.
    """
    evaluated = 1  # the baseline run, whose unrouted nets seed best_unrouted
    best_unrouted = baseline_unrouted
    winner: tuple[int, ...] | None = None
    covered_space = False
    workdir = tempfile.mkdtemp(prefix="klt-gen-compose-suggest-")
    try:
        for climbs in _candidate_climbs(len(searchable), len(roles)):
            if evaluated >= budget:
                break
            candidate = _assignment_request(
                request,
                searchable,
                roles,
                climbs,
                output=os.path.join(workdir, f"candidate_{evaluated}.gds"),
            )
            evaluated += 1
            try:
                report = compose(candidate, request_dir)
            except LayerRuleError:
                continue
            except GenComposeError as exc:
                obstacle = (exc.details or {}).get("conflicts_with", {})
                if exc.code == CODE_CONFLICT and "net" in obstacle:
                    continue
                raise
            unrouted = list(report.get("unrouted_nets") or [])
            if not unrouted:
                winner = climbs
                break
            if len(unrouted) < len(best_unrouted):
                best_unrouted = unrouted
        else:
            # The generator ran out: every assignment in the space was
            # evaluated (budget permitting -- a budget break above leaves
            # covered_space False), and none routed every net.
            covered_space = True
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return _SearchOutcome(
        winner=winner,
        evaluated=evaluated,
        best_unrouted=best_unrouted,
        covered_space=covered_space,
    )


def _parse_budget(raw: Any) -> int:
    """``routing.suggest_layers_budget`` -- a positive integer, defaulting
    to :data:`DEFAULT_SUGGESTION_BUDGET` when absent (bools are ints in
    Python and are rejected explicitly)."""
    if not is_int(raw) or raw < 1:
        raise GenComposeError(
            "request.routing.suggest_layers_budget must be a positive "
            "integer when given"
        )
    return raw


def _assignment_request(
    request: dict[str, Any],
    searchable: tuple[int, ...],
    roles: tuple[str, ...],
    climbs: tuple[int, ...],
    output: str | None = None,
) -> dict[str, Any]:
    """One candidate as a plain request: the caller's document with the
    suggestion keys stripped and ``layer_role`` injected on the nets this
    assignment moves (climb 0 = leave the net as written).

    Deep-copied, so no evaluation can touch the caller's request.
    ``output`` overrides ``options.output`` for search-time evaluations;
    ``None`` keeps the caller's own output path, which is what the baseline
    and the winning re-run use.
    """
    clone = copy.deepcopy(request)
    clone["routing"] = ordinary_routing(clone.get("routing") or {})
    if output is not None:
        raw_options = clone.get("options") or {}
        if not isinstance(raw_options, dict):
            raise GenComposeError("request.options must be a JSON object")
        options = dict(raw_options)
        options["output"] = output
        clone["options"] = options
    for index, climb in zip(searchable, climbs, strict=True):
        if climb:
            clone["connectivity"][index]["layer_role"] = roles[climb - 1]
    return clone


def _candidate_climbs(net_count: int, max_climb: int) -> Iterator[tuple[int, ...]]:
    """Climb tuples (one climb per searchable net, declaration order),
    ordered by total climb then lexicographically.

    Distance 0 -- every net left as-is -- is the caller's own request,
    already evaluated as the baseline, so enumeration starts at 1.
    Lexicographic ties keep earlier-declared nets lowest, matching the
    router's own first-routed-net-wins order: the net committed first is
    the one a later net should move for.
    """
    for distance in range(1, net_count * max_climb + 1):
        yield from _climbs_at(net_count, max_climb, distance)


def _climbs_at(
    net_count: int, max_climb: int, distance: int
) -> Iterator[tuple[int, ...]]:
    """Climb tuples summing to exactly ``distance``, lexicographic order."""
    climbs = [0] * net_count

    def fill_tail(start: int, total: int) -> None:
        # Put as much climb as possible on the last nets (lexicographic minimum).
        for pos in range(net_count - 1, start - 1, -1):
            climbs[pos] = min(total, max_climb)
            total -= climbs[pos]

    fill_tail(0, distance)
    while True:
        yield tuple(climbs)
        available = climbs[-1]
        for pos in range(net_count - 2, -1, -1):
            if available and climbs[pos] < max_climb:
                climbs[pos] += 1
                fill_tail(pos + 1, available - 1)
                break
            available += climbs[pos]
        else:
            return


def _assignments(
    labels: list[str], roles: tuple[str, ...], climbs: tuple[int, ...]
) -> dict[str, str]:
    """The replayable ``{net: layer_role}`` mapping one climb tuple encodes:
    only the nets that actually moved, in declaration order."""
    return {
        label: roles[climb - 1]
        for label, climb in zip(labels, climbs, strict=True)
        if climb
    }


def _with_suggestions(report: dict[str, Any], block: dict[str, Any]) -> dict[str, Any]:
    """The compose response with one additive ``layer_suggestions`` block."""
    result = dict(report)
    result["layer_suggestions"] = block
    return result
