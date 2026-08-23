"""``klt size``: solve a single device's operating point from a gm/Id target
and a current budget, with ``ngspice`` on the real PDK models as the
evaluator -- never a closed-form surrogate as the final word.

Phase 0 (issue #721) of the analog-sizing epic #705: define the request/
response interface, wire ``ngspice`` in as the in-loop evaluator (reusing
the model-library resolution and subprocess-invocation conventions
``sim.py`` already established, per that module's own docstring and
``docs/cli/sim.md``'s "Model library resolution"/"Engine" sections), and
solve the single-device gm/Id MVP: given a fixed channel length, a target
current, and a target gm/Id, find the channel width that hits it.

Why this is not a thin wrapper around :func:`klayout_tools.sim.run_sim`
------------------------------------------------------------------------
``run_sim`` evaluates a **caller-supplied netlist** against a **caller-
supplied ``.meas`` card list** over a PVT corner matrix -- it is not shaped
for what this module needs: a bespoke, dynamically generated single-device
testbench (diode-connected bias, swept across many candidate widths) whose
"measurement" is the MOSFET's own internal small-signal operating-point
state (``gm``, ``id``, ``vgs``, ``vth``, ``vdsat``), which ngspice exposes
only via ``@<device>[<param>]`` vector access inside a ``.control`` block --
there is no ``.MEASURE OP`` (``sim.py``'s own ``_validate_meas_card`` already
documents why) and no sweep variable for a ``.meas dc``/``.meas tran`` card
to search over here. What *is* reused, rather than reimplemented, is the
model-library resolution (:func:`klayout_tools.sim._resolve_models_lib`,
PDK-aware, identical ``models.pdk``/``models.lib``/``models.pdk_root``
shape) and the ``ngspice -b <deck> -o <log>`` subprocess-invocation/timeout/
engine-version-extraction pattern ``sim.py``'s own ``_run_corner`` uses.

Device convention (sky130-first, per CLAUDE.md)
------------------------------------------------
A request names a PDK subcircuit (e.g. ``sky130_fd_pr__nfet_01v8``), called
as an ``X`` element the way every sky130/gf180 PDK deck this repo already
targets expects (see ``sim.py``'s ``_SUBCKT_FAMILY_KEYWORDS`` comment) --
never a bare SPICE-native ``M`` element bound straight to a ``.model`` card.
The subcircuit must accept ``l``/``w``/``nf``/``mult`` parameters, matching
the ``sky130_fd_pr__*`` convention (confirmed against the installed sky130A
PDK while building this module). ngspice's internal op-point vector for a
device instantiated this way lives at ``@m.<instance>.<inner-name>[<param>]``,
where ``<inner-name>`` is, by the same sky130 convention, ``m`` prefixed
onto the subcircuit's own name (e.g. ``msky130_fd_pr__nfet_01v8``) --
:func:`_default_op_point_element` derives this default; ``device.
op_point_element`` overrides it for a PDK that does not follow the
convention.

Method: gm/Id lookup via a diode-connected bias sweep
------------------------------------------------------
The classical gm/Id sizing methodology (Silveira/Flandre/Jespers) looks up
current density (``Id/W``) for a target gm/Id from a pre-characterized curve
at a *fixed* ``Vds``. This MVP uses the simpler, closely related
diode-connected variant instead (``Vds = Vgs``, gate tied to drain): an ideal
current source fixes ``Id`` exactly, ngspice's own DC operating-point solver
finds the ``Vgs`` (and therefore ``gm``) the device settles at for a given
width -- so the only free variable driving the search is ``W``, and no
feedback/regulation loop is needed to hold an independently chosen ``Vds``
against the swept device. ``gm/Id`` at fixed ``Id`` is monotonically
increasing in ``W`` (larger ``W`` -> lower current density -> weaker
inversion -> higher ``gm/Id``, verified empirically against the installed
sky130A PDK's ``sky130_fd_pr__nfet_01v8``/``__pfet_01v8`` while building this
module), which is what makes a bracket-and-interpolate search well-posed.
Decoupling ``Vds`` from ``Vgs`` (the fully general fixed-``Vds`` lookup-table
method) is available as an opt-in mode -- see "Fixed-``Vds`` bias mode"
below.

Fixed-``Vds`` bias mode (issue #1015)
----------------------------------------
Setting ``request.target.vds_v`` switches the generated deck's bias topology
from the diode-connected tie above to the fully general fixed-``Vds``
lookup-table method: an ideal voltage source holds the device's ``Vds`` at
exactly the requested value, and a **feedback-regulated gate bias** --
implemented as an ngspice behavioral (``B``) source expressing the gate
voltage as a clamped, high-loop-gain function of the measured drain current
error, ``V(gate) = clip(Vdd/2 + K*(Id_measured - Id_target)/Id_target, 0,
Vdd)`` -- servos ``Vgs`` until the device settles at the target current,
*at that fixed Vds*. This is a single nonlinear algebraic constraint ngspice's
own DC Newton-Raphson solver resolves directly inside each ``op`` point,
exactly the same way SPICE resolves any other closed-loop feedback network
(e.g. an op-amp macro-model) at DC -- no extra ngspice invocation or outer
Python-level search loop is needed versus the diode-connected mode's own
one-invocation-per-sweep design (see "Two-invocation-per-request design"
below): the identical bracket-and-interpolate-then-confirm width search
(:func:`_find_bracket`, :func:`_log_interp`, then a fresh single-point
confirmation run) runs unchanged, only :func:`_write_sweep_deck`'s generated
bias topology differs.

The loop-gain constant ``K`` (:data:`_FIXED_VDS_FEEDBACK_GAIN`) is
*normalized* by the target current (dividing the raw current error by
``Id_target`` before multiplying by ``K``) rather than an absolute
volts-per-amp gain, so the same constant gives comparable *relative*
precision on the confirmed current across many decades of target current
-- verified empirically against this module's own synthetic square-law
device library (see ``examples/size/``) from sub-nanoamp to milliamp targets
while building this feature: an absolute (non-normalized) gain converges
each candidate width's confirmed current to a roughly *constant absolute*
residual (set by the solver's own voltage precision divided by the gain),
which is a negligible relative error at a typical ~10uA target but a huge
one at a sub-nanoamp target; normalizing by ``Id_target`` fixes the
confirmed current's *relative* precision at ~1e-4 (0.01%) regardless of
scale, comfortably inside every default ``tolerance.gm_id_rel`` this module
documents. ``K`` itself was picked empirically 100x below the value
(``3e6``) at which the normalized loop stopped converging in that same
sweep -- a comfortable margin below realistic PVT/model nonlinearity for a
real PDK's own compact model.

Because the target current is enforced by *feedback*, not an ideal current
source, ``margins.id_rel_error`` in this mode is a genuinely measured
quantity (not "near-zero by construction" the way the diode-connected
mode's ideal current source guarantees) -- a caller whose declared
``target.vds_v``/``target.id_a`` combination cannot be reached by any gate
bias inside ``[0, Vdd]`` (an over-constrained request, e.g. too little
``Vds`` headroom for the requested current at the narrowest allowed width)
sees the feedback bias saturate against a supply rail and
``margins.id_rel_error`` report the resulting mismatch honestly, rather
than silently reporting an unreachable point as met. See
``docs/cli/size.md``'s "Fixed-Vds bias mode" section for the request/
response shape and a worked cascode-device example.

Two-invocation-per-request design (performance)
-------------------------------------------------
A real PDK's combined-corner ``ngspice`` model library is large (every
device family in one file) -- parsing it costs the overwhelming majority of
a single ``ngspice -b`` invocation's wall-clock time (tens of seconds,
independent of how many operating points are then evaluated; measured
against the installed sky130A PDK while building this module). Naively
re-invoking ``ngspice`` once per candidate width (a classic bisection) would
pay that parse cost on every iteration. Instead, this module pays it
**once**: the whole coarse, log-spaced width sweep (:data:`DEFAULT_SWEEP_POINTS`
points by default) runs inside a *single* ``ngspice`` invocation, using
``alterparam``/``reset`` (re-elaborate the already-parsed circuit with a new
``.param`` value -- cheap, no re-read from disk) between each ``op`` point.
A second, single-point invocation then *confirms* the interpolated answer
(never trusts the coarse grid or the interpolation alone as the final
word) -- see :func:`run_size`.

Corner *set* input, one sizing corner (issue #729)
----------------------------------------------------
A request declares either the original single ``request.corner`` object
(``process``/``vdd_v``/``temperature_c`` scalars, unchanged) or a corner
*set* via ``request.corners``, reusing ``sim.py``'s own
``corners.process``/``corners.temperature_c`` axis semantics verbatim
(:func:`klayout_tools.sim._expand_corners`) rather than inventing a third
spelling -- ``vdd_v`` stays a single scalar across the whole set, since this
command biases one fixed supply rather than sweeping it. Exactly **one**
declared point, ``corners.sizing`` (defaulting to the first point on each
axis), is the *sizing* corner: the width search (the sweep + confirm
described above) runs only there, because re-solving per corner would
return a different width per corner, which is not a device. The confirmed
width is then *verified* -- a fresh single-point ngspice confirmation, no
further search -- at every other declared corner, reporting each corner's
own operating point, margins, and ``pass``/``fail``/``error`` status
(:func:`_parse_corner_set`, :func:`run_size`). The sizing corner's status
alone sets the response's aggregate ``status`` unless the request opts in
via ``targets.hold_across_corners: true``; an evaluator error at *any*
declared corner always makes the aggregate ``status`` ``"error"``, mirroring
``klt sim``'s own error > fail > pass precedence -- see
``docs/cli/size.md``'s "Corner sets" section for the full response shape.

Worst-corner margin objective (issue #769)
--------------------------------------------
The above ("sizing_corner", the default) optimizes for exactly one nominal
corner and only *reports* the spread elsewhere -- a device sized that way
can still be the worst-margin choice across the declared PVT set. Setting
``request.corners.objective: "worst_case_margin"`` instead searches for the
single width that maximizes the *worst* per-corner gm/Id margin across
*every* declared corner (:func:`_run_worst_case_margin`).

Every declared corner's ``gm_id(w)`` curve is monotonically increasing in
width at fixed current (this module's own documented, empirically verified
assumption -- see "Method" above), so each corner's relative error
``error_c(w) = (gm_id_c(w) - target) / target`` is monotonically increasing
too, which makes both ``U(w) = max_c error_c(w)`` and ``L(w) = min_c
error_c(w)`` monotonically non-decreasing in ``w`` (the max/min of a finite
family of non-decreasing functions is itself non-decreasing). The width
that minimizes the worst-case absolute error, ``max_c |error_c(w)| =
max(U(w), -L(w))``, is therefore exactly the unique zero crossing of
``h(w) = U(w) + L(w)`` (or a bound of ``[w_min_um, w_max_um]`` when ``h``
never crosses zero within it -- every declared corner sits on the same side
of the target throughout the searchable range). A plain bisection on
``h(ln w)`` finds that crossing to :data:`_WORST_CASE_BISECTION_ITERS`
digits of precision in pure Python against each corner's own already-swept
curve -- no additional ngspice invocation per bisection step.

Unlike the ``"sizing_corner"`` objective (one full-grid sweep, plus one
single-point verification per *other* declared corner: ``N+1`` invocations
total for ``N`` corners), this objective needs every corner's own full-grid
curve to search against, so it costs one full-grid sweep invocation *per
declared corner* plus one single-point confirmation invocation per corner
at the converged width: ``2N`` invocations. The top-level ``corner``/
``operating_point``/``margins`` fields mirror the *worst-margin* corner
among those confirmations under this objective (not the declared
``corners.sizing`` corner, which is still echoed and still flagged
``is_sizing`` in ``corners.results`` for reference) -- see
``docs/cli/size.md``'s "Worst-corner margin objective" section.

A single declared corner makes ``"sizing_corner"`` and ``"worst_case_margin"``
mathematically equivalent, but this module never routes a single-corner
request through the new code path unless the request explicitly opts in via
``corners.objective`` -- the legacy ``request.corner`` shape has no
``objective`` field at all, so a single-corner request is bit-for-bit
unaffected by this feature (issue #769's regression requirement).

Design-centering objective (issue #1326)
-------------------------------------------
The objectives above all optimize *this device's own width* against a
gm/Id target -- they have no notion of which device, among several, is the
one a mismatch/yield analysis says is actually worth growing.
``design_centering.py`` (issue #924, Phase 3 of the statistical/yield epic
#710) already defines that contract -- a ``klt yield-sensitivity``-shaped
``ranking[].parameter``/``ranking[].contribution`` array plus a
``parameter_map`` bridging each ranked mismatch parameter to a sized-device
instance -- but shipped with no consumer: its own docstring explicitly
reserved this wiring for #705 once it grew a real design-centering stage.

Setting ``request.design_centering: {"ranking": [...], "parameter_map":
{...}}`` opts a sizing run into that stage: **additive** to
``corners.objective`` above (an independent request field a caller may set
alongside any objective, not a new ``corners.objective`` value), it grows
whichever instance(s) the ranking flags as the dominant mismatch
contributor(s) and re-solves at the grown geometry, all inside this one
``klt size`` invocation -- rather than requiring a caller to run ``klt
size``, then ``klt design-centering`` on the result, then hand-edit and
re-run ``klt size`` a second time.

Method: reuse the ranking bridge, grow by MULT, re-verify with ngspice
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
1. This module's own just-solved geometry stands in for the ``sized_device``
   payload a standalone ``klt design-centering`` call would need supplied
   separately -- there is nothing to load, the run already has it.
2. :func:`design_centering.rank_mapped_parameters` and
   :func:`design_centering.suggested_area_multiplier` -- the same pure
   ranking-bridge and Pelgrom-law area-multiplier helpers
   ``design_centering.py``'s own reference consumer uses -- resolve each
   mapped candidate's suggested area multiplier against the sized geometry
   from step 1 (:func:`_apply_design_centering`).
3. Every candidate whose multiplier exceeds ``1x`` grows that instance's
   ``mult`` (parallel-device count) -- never its ``W``, so the growth never
   perturbs the gm/Id target the search already met -- rounded up to the
   next whole device (:func:`_grow_mult`). Single-device mode has exactly
   one instance to grow; topology mode (see below) may grow more than one
   role at once when the ranking flags instances in different roles.
4. The **whole sizing search re-runs** at the grown geometry (a second full
   sweep+confirm, or a second full joint solve in topology mode) -- turning
   Pelgrom's law's own "first-pass, order-of-magnitude heuristic, re-verify
   with a fresh sizing pass" caveat (``design_centering.py``'s docstring)
   into an actual ngspice-confirmed operating point, not an unverified
   number. The response's top-level device/operating-point/margins fields
   are this **grown** run's own result; a ``design_centering`` block
   reports what was flagged, what was grown, and by how much.

Cost: one extra full solve (the same cost as the base objective already
ran) only when ``request.design_centering`` opts in and at least one mapped
candidate's multiplier exceeds ``1x`` -- a request that does not set this
field pays nothing extra, and a request that sets it but whose ranking's
top mapped candidate is already the weakest (multiplier ``1.0``, nothing to
grow) also pays nothing extra. See ``docs/cli/size.md``'s "Design-centering
objective" section for the full request/response shape.

Coupled multi-device topology sizing (issue #768)
--------------------------------------------------
Everything above sizes **one** device. A request may instead declare a
coupled multi-device topology via ``request.topology`` -- Phase 1a of the
analog-sizing epic (#705) -- and get back a sized operating point for every
device in it, solved *jointly*. The shipped topology is
``"diff_pair_mirror_tail"``: an NMOS differential input pair, a PMOS
current-mirror load, and an NMOS tail current source with its
diode-connected bias replica -- six device instances, three solved widths,
the same structure as this repo's own hand-sized 5T OTA canary
(``examples/design-pipeline/ota_5t.spice``).

Why this cannot be three single-device solves
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
The three roles' operating points are *coupled*, along two axes the request
declares under ``request.budget`` rather than per device:

- **Tail current split.** The tail device sources the whole bias current;
  each input-pair leg carries half of it. Widening the tail changes both
  legs' current, hence both legs' gm/Id *and* the mirror's.
- **Mirror ratio.** ``budget.tail_mirror_ratio`` sets how much the tail
  device multiplies the diode-connected replica's reference current, and
  the PMOS load mirror then carries whatever branch current the input pair
  settles at.

On top of that, none of the six devices actually sits at ``Vds = Vgs`` in
the real circuit (only the two diode-connected ones do), and the input
pair's sources sit at a raised tail node, so its body effect is real.
Sizing each device alone against a diode-connected surrogate and stopping
there would report an operating point the assembled circuit never reaches.

Method: seed from per-role curves, then solve on the assembled circuit
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
1. **Characterize** (one ngspice invocation per role): the same
   diode-connected, log-spaced width sweep single-device mode uses, at each
   role's nominal current, producing that role's ``gm_id(W)`` curve. This
   only *seeds* the search and supplies its local slope -- it is never the
   final word.
2. **Solve jointly** (:func:`_run_joint_solve`, one ngspice DC
   operating-point invocation per candidate point): write the *whole*
   topology at the candidate ``(W_tail, W_input, W_mirror)``, read back
   every instance's real in-circuit ``gm``/``Id``/``Vgs``/``Vth``/``Vdsat``
   at its actual coupled ``Vds``, and correct **all three widths at once**
   from that single coupled measurement by inverting each role's own curve
   shifted by its measured offset::

       offset_r   = gm_id_measured_r(W_k) - curve_r(W_k)
       W_{k+1, r} = curve_r^-1(target_r - offset_r)

   The offset absorbs whatever the diode-connected surrogate could not model
   (real ``Vds``, the current the circuit actually settled at, body effect);
   the curve supplies the slope. Iteration stops when every role's control
   device is inside ``tolerance.gm_id_rel``, when the correction step stops
   moving, or at ``options.max_joint_iterations``.

The assembled deck holds its output at the common-mode point with the same
DC-only feedback network (short at DC, open at AC) the canary's own netlist
uses, rather than letting a single-ended output float to a fragile,
corner-dependent balance point -- see :func:`_write_topology_deck`.

Every instance's response entry carries its own measured gm/Id, inversion
level, and a rationale sentence explaining both (issue #768's third
acceptance criterion); ``budget.measured`` reports the coupled quantities
the circuit actually delivered (real tail current, the two legs' split, the
realized mirror ratio). Corner *sets* are not yet supported in topology
mode. See ``docs/cli/size.md``'s "Coupled multi-device topology sizing"
section for the full request/response shape and worked example.
"""

from __future__ import annotations

import copy
import math
import os
import re
import subprocess
import tempfile
from typing import Any

from . import design_centering, env_provenance
from ._paths import _load_request_json, validate_request_shape
from ._provenance import build_provenance
from ._text import line_containing as _line_containing
from .pdk import PdkNotFoundError, find_pdk
from .sim import SimError, _expand_corners, _resolve_models_lib

#: Bumped only on a non-additive (breaking) change to this command's own
#: JSON shape -- see docs/json-contract.md.
#:
#: Bumped 1 -> 2 (issue #1274): `environment.models_lib` changed from the
#: resolved *absolute* model-library path to the `{path, scope}` shape
#: `env_provenance.repo_relative_path` defines, the same normalization
#: `klt sim`/`klt pex` already apply to their own path fields (issue #1261).
#: On a normal install the PDK lives under the user's home directory, so
#: that field leaked a home path into every committed report. Nothing is
#: lost: the library is still pinned by identity via `provenance.deck`.
SCHEMA_VERSION = 2

#: ``ngspice`` is the only implemented engine, mirroring ``sim.py``.
SUPPORTED_ENGINES = ("ngspice",)

#: ``response.design_centering``'s own schema version (issue #1326) --
#: independently versioned from `SCHEMA_VERSION` per docs/json-contract.md's
#: "per command, not globally" convention, mirroring how `design_centering.py`
#: versions its own payload independently of every other `klt` command's.
DESIGN_CENTERING_SCHEMA_VERSION = 1

#: A request names which terminal the target current flows through and
#: which node ties to the supply -- see this module's docstring's "Method"
#: section for the diode-connected bias each kind implies.
SUPPORTED_DEVICE_KINDS = ("nmos", "pmos")

#: ``request.corners.objective`` -- ``"sizing_corner"`` (default, unchanged
#: since issue #729) solves the width at exactly one declared corner and
#: verifies elsewhere; ``"worst_case_margin"`` (issue #769) instead searches
#: for the width that maximizes the worst per-corner gm/Id margin across
#: every declared corner -- see this module's docstring's "Worst-corner
#: margin objective" section.
SUPPORTED_OBJECTIVES = ("sizing_corner", "worst_case_margin")
DEFAULT_OBJECTIVE = "sizing_corner"

#: Bisection iterations for the worst-case-margin width search. Each step
#: only interpolates against already-swept per-corner curves (no additional
#: ngspice invocation), so this is cheap to make tight -- 60 steps gives
#: ~2^-60 relative precision on the search interval, far finer than any
#: physically meaningful width resolution.
_WORST_CASE_BISECTION_ITERS = 60

#: Coupled multi-device topologies ``request.topology`` accepts (issue
#: #768, Phase 1a of epic #705) -- an alternative to the single-``device``
#: request shape. See this module's "Coupled multi-device topology sizing"
#: docstring section below.
SUPPORTED_TOPOLOGIES = ("diff_pair_mirror_tail",)

#: The three coupled sizing roles a ``diff_pair_mirror_tail`` topology
#: declares under ``request.devices``/``request.target`` -- fixed order
#: used everywhere a role is iterated, so response field order (and the
#: joint deck's instance order) is deterministic. Six device *instances*
#: share these three widths -- see :data:`TOPOLOGY_INSTANCES`.
TOPOLOGY_ROLES = ("tail", "input_pair", "mirror")

#: Required ``device.kind`` per role for the shipped ``diff_pair_mirror_tail``
#: orientation: an NMOS input pair, a PMOS current-mirror load, and an NMOS
#: tail current source -- the same orientation as this repo's own hand-sized
#: 5T OTA canary (``examples/design-pipeline/ota_5t.spice``). The
#: complementary PMOS-input orientation is documented future work -- see
#: ``docs/cli/size.md``'s "Known limitations".
_TOPOLOGY_ROLE_KIND = {
    "input_pair": "nmos",
    "mirror": "pmos",
    "tail": "nmos",
}

#: Points in the coarse, log-spaced width sweep run inside the single
#: sweep invocation (see this module's docstring). Overridable via
#: ``request.options.sweep_points``.
DEFAULT_SWEEP_POINTS = 25

#: Minimum sweep points accepted -- fewer than this and a monotonic bracket
#: search has too little resolution to be meaningful.
MIN_SWEEP_POINTS = 5

#: Joint-solve iteration cap for a coupled topology request (issue #768),
#: overridable via ``request.options.max_joint_iterations``. Each iteration
#: costs one full ngspice invocation of the assembled circuit (the model
#: library is re-parsed every time, since the next candidate point depends
#: on the previous one's result and cannot be batched into one deck the way
#: the single-device width sweep is). The curve-shift correction converges
#: like a Newton step -- 2-3 iterations is typical against the real sky130A
#: models, measured while building this module -- so 6 is a generous
#: ceiling, not an expected cost.
DEFAULT_MAX_JOINT_ITERATIONS = 6

#: A joint solve must run at least one candidate evaluation to have
#: measured anything at all.
MIN_JOINT_ITERATIONS = 1

#: Stop the joint iteration when every role's next width would move by less
#: than this in ``ln(W)`` (~0.1%) -- the correction has stopped moving (each
#: role is pinned at a bound, or its curve simply cannot reach the shifted
#: target), so another ngspice invocation would only repeat the same run.
_JOINT_WIDTH_STEP_EPS = 1e-3

#: Per-invocation ngspice wall-clock budget. Generous by default because
#: the sweep invocation pays a real PDK's full model-library parse cost
#: once (tens of seconds) plus one cheap ``op`` per sweep point -- see this
#: module's docstring.
DEFAULT_TIMEOUT_S = 180

#: Default relative tolerance on the confirmed operating point's ``gm/Id``
#: against ``request.target.gm_id`` -- overridable via
#: ``request.tolerance.gm_id_rel``.
DEFAULT_GM_ID_TOLERANCE = 0.03

#: Loop-gain constant for the fixed-``Vds`` mode's feedback-regulated gate
#: bias (issue #1015, see this module's docstring's "Fixed-Vds bias mode"
#: section) -- normalized by the target current, not an absolute
#: volts-per-amp gain, so the confirmed current's *relative* precision is
#: roughly constant (~1e-4) regardless of the target current's absolute
#: scale. Chosen empirically 100x below the value (``3e6``) at which
#: ngspice's DC operating-point solver stops converging the normalized loop
#: against this module's own synthetic square-law device library
#: (``examples/size/``), swept from sub-nanoamp to milliamp targets and
#: from ``w_min_um`` to ``w_max_um``.
_FIXED_VDS_FEEDBACK_GAIN = 1e4

_MARKER_RE = re.compile(r"^KLT_SIZE_POINT\s+(\d+)\s+(\S+)\s*$")
_OP_VALUE_RE = re.compile(r"^@\S+\[(\w+)\]\s*=\s*([-+0-9.eEgGnN]+)\s*$")
_ENGINE_VERSION_RE = re.compile(r"ngspice-([\w.]+)")
_FATAL_LOG_RE = re.compile(
    r"fatal error|could not find a valid modelname|no simulations run|"
    r"simulation\(s\)\s+aborted",
    re.IGNORECASE,
)

#: Op-point vectors requested from ngspice for every point -- ``vth``/
#: ``vdsat`` are not exposed by every compact model (e.g. a bare SPICE
#: level=1 device omits them, verified while building this module's test
#: fixtures), so parsing tolerates their absence; ``gm``/``id`` are required
#: for the point to be usable at all (see ``run_size``'s ``valid_points``
#: filter).
_OP_PARAMS = ("gm", "id", "vgs", "vth", "vdsat")

#: Response-field name each ``_OP_PARAMS`` entry parses into on a point dict.
_OP_PARAM_KEYS = {
    "gm": "gm_s",
    "id": "id_a",
    "vgs": "vgs_v",
    "vth": "vth_v",
    "vdsat": "vdsat_v",
}


class SizeError(Exception):
    """Raised when a sizing request cannot even be attempted: a missing/
    malformed request file, an unresolvable model library, an unsupported
    engine/device kind, or a request whose device bounds are nonsensical.

    Distinct from a request that ran but could not meet its target (reported
    as ``status: "fail"``) or whose evaluator itself errored/timed out
    (``status: "error"``) -- both of those are documented outcomes, not
    exceptions, mirroring ``sim.py``'s ``SimError`` split.
    """


def load_request(request_path: str) -> dict[str, Any]:
    """Read and minimally validate a ``klt size`` request JSON file.

    Raises :class:`SizeError` if the file is missing/unreadable, not valid
    JSON, or missing a required top-level field (``models``, ``target``,
    plus exactly one of ``device`` (single-device mode) or ``topology``
    (coupled multi-device mode, issue #768) -- the two modes are mutually
    exclusive, mirroring ``corner``/``corners``' own XOR rule).
    """
    request = _load_request_json(request_path, SizeError)
    request = validate_request_shape(
        request,
        "request file",
        error_cls=SizeError,
        required_fields=("models", "target"),
    )
    has_device = "device" in request
    has_topology = "topology" in request
    if has_device and has_topology:
        raise SizeError(
            "request must declare either 'device' (single-device mode) or "
            "'topology' (coupled multi-device mode), not both"
        )
    if not has_device and not has_topology:
        raise SizeError(
            "request is missing required field: 'device' (single-device "
            "mode) or 'topology' (coupled multi-device mode)"
        )
    return request


def _report_path(path: str | None, *, repo_root: str | None) -> dict[str, Any]:
    """Normalise one resolved input path for this module's own JSON response
    (issue #1274) -- a thin, module-local wrapper over
    :func:`~klayout_tools.env_provenance.repo_relative_path`, matching
    ``sim.py``'s helper of the same name so ``klt size``, ``klt sim``,
    ``klt pex`` and ``klt env-provenance`` all agree on exactly one
    ``{path, scope}`` shape. Pass an already-resolved absolute path (e.g.
    ``_resolve_models_lib``'s answer); a bare relative string would be
    resolved against the current process's cwd, which is not necessarily
    what a caller meant.
    """
    return env_provenance.repo_relative_path(path, repo_root=repo_root)


def _require_positive_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SizeError(f"{field} must be a number")
    if value <= 0:
        raise SizeError(f"{field} must be positive")
    return float(value)


def _default_op_point_element(model: str) -> str:
    """The sky130-convention internal instance path suffix for a PDK
    subcircuit's own wrapped MOSFET -- ``m`` prefixed onto the subcircuit
    name (e.g. ``sky130_fd_pr__nfet_01v8`` -> ``msky130_fd_pr__nfet_01v8``),
    verified against the installed sky130A PDK's ``__nfet_01v8``/
    ``__pfet_01v8`` subcircuits while building this module. Override via
    ``device.op_point_element`` for a PDK that does not follow it.
    """
    return f"m{model}"


def _parse_device(device: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(device, dict):
        raise SizeError("request.device must be a JSON object")

    kind = device.get("kind")
    if kind not in SUPPORTED_DEVICE_KINDS:
        raise SizeError(
            f"request.device.kind must be one of {SUPPORTED_DEVICE_KINDS} "
            f"(got {kind!r})"
        )

    model = device.get("model")
    if not isinstance(model, str) or not model:
        raise SizeError("request.device.model is required (subcircuit name)")

    l_um = _require_positive_number(device.get("l_um"), "request.device.l_um")
    w_min_um = _require_positive_number(
        device.get("w_min_um"), "request.device.w_min_um"
    )
    w_max_um = _require_positive_number(
        device.get("w_max_um"), "request.device.w_max_um"
    )
    if w_max_um <= w_min_um:
        raise SizeError("request.device.w_max_um must be greater than w_min_um")

    nf = device.get("nf", 1)
    mult = device.get("mult", 1)
    for value, field in ((nf, "request.device.nf"), (mult, "request.device.mult")):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise SizeError(f"{field} must be a positive number")

    op_point_element = device.get("op_point_element") or _default_op_point_element(
        model
    )
    if not isinstance(op_point_element, str) or not op_point_element:
        raise SizeError("request.device.op_point_element must be a non-empty string")

    return {
        "kind": kind,
        "model": model,
        "l_um": l_um,
        "w_min_um": w_min_um,
        "w_max_um": w_max_um,
        "nf": nf,
        "mult": mult,
        "op_point_element": op_point_element,
    }


def _parse_corner(corner: dict[str, Any]) -> dict[str, Any]:
    corner = corner or {}
    if not isinstance(corner, dict):
        raise SizeError("request.corner must be a JSON object")
    process = corner.get("process", "tt")
    if not isinstance(process, str) or not process:
        raise SizeError("request.corner.process must be a non-empty string")
    vdd = corner.get("vdd_v")
    if vdd is None:
        raise SizeError("request.corner.vdd_v is required")
    vdd = _require_positive_number(vdd, "request.corner.vdd_v")
    temperature_c = corner.get("temperature_c", 27)
    if isinstance(temperature_c, bool) or not isinstance(temperature_c, (int, float)):
        raise SizeError("request.corner.temperature_c must be a number")
    return {"process": process, "vdd_v": vdd, "temperature_c": float(temperature_c)}


def _corner_label(process: str, temperature_c: float) -> str:
    """Deterministic display label for one corner point, e.g. ``"tt/27C"``
    -- mirrors ``klt sim``'s own ``CornerPoint.corner_id`` (this module's
    docstring's "Corner set input" section), minus the supply-voltage
    segment (``vdd_v`` is a single scalar across the whole set here, never
    swept per corner)."""
    temp_label = f"{temperature_c:g}"
    return f"{process}/{temp_label}C"


def _parse_corner_set(request: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    """Parse the request's corner declaration into a list of expanded
    corner-point dicts (each shaped like :func:`_parse_corner`'s output,
    plus a ``corner_id`` label and, for a process *bundle* entry, a
    ``process_sections`` list) and the index of the declared sizing corner
    within that list.

    Accepts either the original single-corner ``request.corner`` object
    (unchanged) or the new corner-*set* ``request.corners`` object -- never
    both. See this module's docstring's "Corner set input" section.
    """
    legacy_corner = request.get("corner")
    corner_set = request.get("corners")
    if legacy_corner is not None and corner_set is not None:
        raise SizeError(
            "request must declare either 'corner' (single) or 'corners' "
            "(a set), not both"
        )

    if corner_set is None:
        parsed = _parse_corner(legacy_corner or {})
        parsed["corner_id"] = _corner_label(parsed["process"], parsed["temperature_c"])
        return [parsed], 0

    if not isinstance(corner_set, dict):
        raise SizeError("request.corners must be a JSON object")

    vdd = corner_set.get("vdd_v")
    if vdd is None:
        raise SizeError("request.corners.vdd_v is required")
    vdd = _require_positive_number(vdd, "request.corners.vdd_v")

    exclude_spec = corner_set.get("exclude") or []
    if not isinstance(exclude_spec, list):
        raise SizeError("request.corners.exclude must be a list")

    axes_spec = {
        "process": corner_set.get("process"),
        "temperature_c": corner_set.get("temperature_c"),
    }
    try:
        points = _expand_corners(axes_spec, exclude_spec)
    except SimError as exc:
        raise SizeError(str(exc)) from exc
    if not points:
        raise SizeError("request.corners produced no corner points (check 'exclude')")

    parsed_points: list[dict[str, Any]] = []
    for point in points:
        process = point.process if point.process is not None else "tt"
        entry: dict[str, Any] = {
            "process": process,
            "vdd_v": vdd,
            "temperature_c": float(point.temperature_c),
        }
        if point.process_sections is not None:
            entry["process_sections"] = list(point.process_sections)
        entry["corner_id"] = _corner_label(process, entry["temperature_c"])
        parsed_points.append(entry)

    sizing_index = _resolve_sizing_index(parsed_points, corner_set.get("sizing"))
    return parsed_points, sizing_index


def _resolve_sizing_index(points: list[dict[str, Any]], sizing_spec: Any) -> int:
    """Resolve ``corners.sizing`` to an index into ``points``. Defaults to
    the first declared point on each axis (index 0 -- :func:`_expand_corners`
    expands process (outer) x temperature (inner), in declaration order, so
    the first expanded point already *is* "first point on each axis")."""
    if sizing_spec is None:
        return 0
    if not isinstance(sizing_spec, dict):
        raise SizeError("request.corners.sizing must be a JSON object")

    candidates = list(range(len(points)))
    process = sizing_spec.get("process")
    if process is not None:
        candidates = [i for i in candidates if points[i]["process"] == process]
    temperature_c = sizing_spec.get("temperature_c")
    if temperature_c is not None:
        if isinstance(temperature_c, bool) or not isinstance(
            temperature_c, (int, float)
        ):
            raise SizeError("request.corners.sizing.temperature_c must be a number")
        candidates = [
            i for i in candidates if points[i]["temperature_c"] == float(temperature_c)
        ]
    if not candidates:
        raise SizeError(
            "request.corners.sizing does not match any declared corner point"
        )
    return candidates[0]


def _corner_public(corner: dict[str, Any]) -> dict[str, Any]:
    """The response-facing shape for one corner-point dict: the original
    ``process``/``vdd_v``/``temperature_c`` fields (unchanged, so the
    top-level ``corner`` field stays backward compatible) plus the additive
    ``corner_id`` label and, for a process bundle, ``process_sections``."""
    out = {
        "corner_id": corner["corner_id"],
        "process": corner["process"],
        "vdd_v": corner["vdd_v"],
        "temperature_c": corner["temperature_c"],
    }
    if "process_sections" in corner:
        out["process_sections"] = corner["process_sections"]
    return out


def _corner_set_echo(
    corner_points: list[dict[str, Any]],
    sizing_index: int,
    hold_across_corners: bool,
    objective: str,
) -> dict[str, Any]:
    """The ``corners`` response block's input-echo portion (``declared``/
    ``sizing``/``objective``/``hold_across_corners``) -- present on every
    response, including an early evaluator-error one, per this module's "no
    stated method is rejected" precedent. ``results`` is filled in by the
    caller once (if) the per-corner verification pass actually runs;
    ``worst_case`` is filled in only by the ``"worst_case_margin"``
    objective's own search (see :func:`_run_worst_case_margin`) -- always
    ``None`` under the default ``"sizing_corner"`` objective."""
    return {
        "declared": [_corner_public(c) for c in corner_points],
        "sizing": _corner_public(corner_points[sizing_index]),
        "objective": objective,
        "hold_across_corners": hold_across_corners,
        "results": None,
        "worst_case": None,
    }


def _parse_hold_across_corners(request: dict[str, Any]) -> bool:
    """``request.targets.hold_across_corners`` -- opts a non-sizing declared
    corner's ``fail`` into the aggregate ``status`` (see this module's
    docstring's "Corner set input" section). Defaults to ``False``: a
    multi-corner request does not fail by construction just because
    ``gm/Id`` genuinely drifts with process/temperature away from the
    sizing corner."""
    targets_block = request.get("targets")
    if targets_block is None:
        return False
    if not isinstance(targets_block, dict):
        raise SizeError("request.targets must be a JSON object")
    value = targets_block.get("hold_across_corners", False)
    if not isinstance(value, bool):
        raise SizeError("request.targets.hold_across_corners must be a boolean")
    return value


def _parse_objective(request: dict[str, Any]) -> str:
    """``request.corners.objective`` -- selects the width-search strategy
    across a declared corner set (see this module's docstring's "Worst-
    corner margin objective" section). Only meaningful inside
    ``request.corners`` (a corner *set*) -- the legacy single-``corner``
    request shape has no equivalent field, so it always resolves to the
    default (issue #769's regression requirement: a single declared corner
    behaves exactly as it did before this field existed)."""
    corners_spec = request.get("corners")
    if not isinstance(corners_spec, dict):
        return DEFAULT_OBJECTIVE
    objective = corners_spec.get("objective", DEFAULT_OBJECTIVE)
    if objective not in SUPPORTED_OBJECTIVES:
        raise SizeError(
            f"request.corners.objective must be one of {SUPPORTED_OBJECTIVES} "
            f"(got {objective!r})"
        )
    return objective


def _parse_target(target: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(target, dict):
        raise SizeError("request.target must be a JSON object")
    id_a = _require_positive_number(target.get("id_a"), "request.target.id_a")
    gm_id = _require_positive_number(target.get("gm_id"), "request.target.gm_id")
    parsed = {"id_a": id_a, "gm_id": gm_id}
    vds_v = target.get("vds_v")
    if vds_v is not None:
        # Fixed-Vds sizing mode (issue #1015) -- see this module's
        # docstring's "Fixed-Vds bias mode" section. Absent (the default),
        # `_write_sweep_deck` keeps the original diode-connected bias
        # (Vds=Vgs) unchanged.
        parsed["vds_v"] = _require_positive_number(vds_v, "request.target.vds_v")
    return parsed


#: ``method.name``/``method.bias`` text for the two single-device bias
#: modes (issue #1015) -- shared by every response-building site
#: (:func:`run_size`'s own success/fail path, :func:`_error_payload`, and
#: :func:`_run_worst_case_margin`) so the reported method always matches
#: the deck :func:`_write_sweep_deck` actually generated.
def _bias_method_name(target: dict[str, Any]) -> str:
    if target.get("vds_v") is not None:
        return "gm/Id lookup via fixed-Vds bias sweep (feedback-regulated)"
    return "gm/Id lookup via diode-connected bias sweep"


def _bias_label(target: dict[str, Any]) -> str:
    vds_v = target.get("vds_v")
    if vds_v is not None:
        return (
            f"fixed-Vds (Vds regulated to {vds_v:.6g}V via a feedback-"
            "controlled gate bias; Vgs solved to hit the target current)"
        )
    return "diode-connected (gate tied to drain, Vds=Vgs)"


#: Every device-shaped entry in a ``klt size`` response (single-device
#: mode's top-level payload, or one instance in topology mode's
#: ``devices.<instance>`` map) must carry these three keys -- issue #770's
#: extension of the epic's "a result with no stated method is not accepted"
#: bar (issue #721) to gm/Id itself: a device result is not just method-
#: documented, its achieved gm/Id and inversion level must be present too,
#: never silently omitted. Values may be ``None`` (e.g. ``status: "error"``,
#: where no operating point was ever confirmed) -- what is refused is the
#: *key* being absent, not the value being unknown.
_DEVICE_RATIONALE_FIELDS = ("gm_id_target", "gm_id_achieved", "inversion_level")


def run_size(
    request_path: str,
    *,
    artifacts_dir: str | None = None,
    timeout_s: float | None = None,
) -> dict[str, Any]:
    """Solve ``request.device``'s width from ``request.target``'s gm/Id and
    current budget, at exactly one declared *sizing* corner, with ``ngspice``
    scoring every candidate against the real PDK models named by
    ``request.models`` -- then verify the solved width's operating point
    and per-target margins across every corner declared in
    ``request.corner``/``request.corners`` (see this module's docstring's
    "Corner set input" section).

    ``artifacts_dir`` overrides where the generated decks/logs are written
    when ``options.keep_artifacts`` is true; defaults to a ``.klt/size/``
    directory next to the request file, mirroring ``klt sim``'s ``.klt/sim/``
    convention. ``timeout_s`` overrides ``options.timeout_s`` (the ``--
    timeout-s`` CLI flag path), same precedence rule as ``klt sim``'s own
    CLI-overrides-request-field fields.

    Returns a dict matching the documented JSON schema (see
    ``docs/cli/size.md``). Raises :class:`SizeError` for anything that
    prevents the search from starting at all (bad request, unresolvable
    model library, unsupported engine/device kind) -- once the sweep starts,
    the response always carries a ``status`` and a populated ``method``
    (see this module's docstring and the "no stated method is rejected"
    acceptance criterion, issue #721). Before returning, every device-shaped
    entry in the response is checked for the gm/Id rationale fields issue
    #770 requires -- see :func:`_validate_device_rationale`.
    """
    payload = _run_size_impl(
        request_path, artifacts_dir=artifacts_dir, timeout_s=timeout_s
    )
    _validate_device_rationale(payload)
    return payload


def _validate_device_rationale(payload: dict[str, Any]) -> None:
    """Refuse to emit a response where a sized device's gm/Id rationale is
    missing (issue #770): every device-shaped entry must carry
    ``gm_id_target``/``gm_id_achieved``/``inversion_level`` -- the
    hoisted-to-top-level counterparts of ``target.gm_id``/
    ``operating_point.gm_id``/``operating_point.inversion_level`` that a
    caller would otherwise have to reconstruct by hand. This mirrors
    ``method``'s own "always populated, even on `status: 'error'`"
    precedent (this module's docstring): the *keys* are always present
    (values may be ``None`` when nothing was ever confirmed), so "missing"
    can only mean a response-building code path forgot to set them --
    a structural bug in this module, not a condition a caller's request can
    trigger, hence :class:`SizeError` rather than a silent gap.

    Topology mode's ``status: "error"`` is the one documented exception:
    ``devices`` itself is ``None`` there (the evaluator never produced any
    per-instance result to attach rationale to -- see
    :func:`_topology_error_payload`), so there is nothing to check.
    """
    if "devices" in payload:
        devices = payload["devices"]
        if devices is None:
            return
        for key, entry in devices.items():
            _require_device_rationale_fields(entry, f"devices.{key!r}")
        return
    _require_device_rationale_fields(payload, "response")


def _require_device_rationale_fields(entry: dict[str, Any], where: str) -> None:
    missing = [field for field in _DEVICE_RATIONALE_FIELDS if field not in entry]
    if missing:
        raise SizeError(
            f"internal error: {where} is missing required gm/Id rationale "
            f"field(s) {missing} -- refusing to emit an unexplained number "
            "(issue #770)"
        )


def _run_size_impl(
    request_path: str,
    *,
    artifacts_dir: str | None = None,
    timeout_s: float | None = None,
) -> dict[str, Any]:
    """The former body of :func:`run_size` -- split out so
    :func:`_validate_device_rationale` has exactly one call site to guard,
    regardless of which of this function's several return points fired.

    Dispatches to single-device or topology mode, then layers the
    additive, opt-in design-centering objective (issue #1326,
    :func:`_apply_design_centering`) on top of whichever payload that
    solve produced -- see this module's docstring's "Design-centering
    objective" section.
    """
    request = load_request(request_path)
    request_dir = os.path.dirname(os.path.abspath(request_path))
    # Issue #1274: `environment.models_lib` is normalised against this one
    # repo root, resolved once from the request file's own location -- the
    # same "walk up from the input" default `env_provenance.find_repo_root`
    # uses for its own caller, and the same convention `sim.py` follows.
    repo_root = env_provenance.find_repo_root(request_dir)

    engine = request.get("engine", "ngspice")
    if engine not in SUPPORTED_ENGINES:
        raise SizeError(
            f"unsupported engine '{engine}' (supported: {', '.join(SUPPORTED_ENGINES)})"
        )

    # Fail fast on a malformed `request.design_centering` block before
    # paying for any ngspice invocation -- see `_parse_design_centering`'s
    # own docstring. The result is discarded here; `_apply_design_centering`
    # (below) parses it again once it actually has a payload to act on.
    _parse_design_centering(request)

    if "topology" in request:
        payload = _run_topology_size(
            request,
            request_dir,
            repo_root=repo_root,
            artifacts_dir=artifacts_dir,
            timeout_s=timeout_s,
        )
    else:
        payload = _run_single_device_size(
            request,
            request_dir,
            repo_root=repo_root,
            artifacts_dir=artifacts_dir,
            timeout_s=timeout_s,
        )

    return _apply_design_centering(
        request,
        request_dir,
        payload,
        repo_root=repo_root,
        artifacts_dir=artifacts_dir,
        timeout_s=timeout_s,
    )


def _run_single_device_size(
    request: dict[str, Any],
    request_dir: str,
    *,
    repo_root: str | None,
    artifacts_dir: str | None,
    timeout_s: float | None,
) -> dict[str, Any]:
    """Single-device mode's own solve (the ``"sizing_corner"``/
    ``"worst_case_margin"`` objectives, issues #721/#729/#769) -- split out
    of :func:`_run_size_impl` so :func:`_apply_design_centering` (issue
    #1326) can re-invoke exactly this solve a second time, at a grown
    device geometry, without re-parsing the request from a file path a
    second time.
    """
    device = _parse_device(request["device"])
    corner_points, sizing_index = _parse_corner_set(request)
    sizing_corner = corner_points[sizing_index]
    target = _parse_target(request["target"])
    hold_across_corners = _parse_hold_across_corners(request)
    objective = _parse_objective(request)
    corners_echo = _corner_set_echo(
        corner_points, sizing_index, hold_across_corners, objective
    )

    models = request.get("models") or {}
    try:
        models_lib = _resolve_models_lib(models, request_dir)
    except SimError as exc:
        raise SizeError(str(exc)) from exc

    provenance_pdk: dict[str, Any] | None = None
    if models.get("pdk") or models.get("pdk_root"):
        try:
            provenance_pdk = find_pdk(
                variant=models.get("pdk"), root=models.get("pdk_root")
            )
        except PdkNotFoundError:
            provenance_pdk = None

    tolerance = request.get("tolerance") or {}
    gm_id_rel_tol = tolerance.get("gm_id_rel", DEFAULT_GM_ID_TOLERANCE)
    gm_id_rel_tol = _require_positive_number(
        gm_id_rel_tol, "request.tolerance.gm_id_rel"
    )

    options = request.get("options") or {}
    sweep_points = options.get("sweep_points", DEFAULT_SWEEP_POINTS)
    if (
        isinstance(sweep_points, bool)
        or not isinstance(sweep_points, int)
        or sweep_points < MIN_SWEEP_POINTS
    ):
        raise SizeError(
            f"request.options.sweep_points must be an integer >= {MIN_SWEEP_POINTS}"
        )

    timeout_s = timeout_s if timeout_s is not None else options.get("timeout_s")
    timeout_s = DEFAULT_TIMEOUT_S if timeout_s is None else timeout_s
    timeout_s = _require_positive_number(timeout_s, "request.options.timeout_s")

    keep_artifacts = bool(options.get("keep_artifacts", False))
    if artifacts_dir is None:
        artifacts_dir = os.path.join(request_dir, ".klt", "size")

    if keep_artifacts:
        work_dir = artifacts_dir
        os.makedirs(work_dir, exist_ok=True)
    else:
        work_dir = tempfile.mkdtemp(prefix="klt-size-")

    w_grid = _log_space(device["w_min_um"], device["w_max_um"], sweep_points)

    provenance = build_provenance(
        deck_name=os.path.basename(models_lib),
        deck_path=models_lib,
        pdk=provenance_pdk,
    )

    if objective == "worst_case_margin":
        payload = _run_worst_case_margin(
            device=device,
            corner_points=corner_points,
            sizing_index=sizing_index,
            target=target,
            models_lib=models_lib,
            repo_root=repo_root,
            gm_id_rel_tol=gm_id_rel_tol,
            w_grid=w_grid,
            work_dir=work_dir,
            timeout_s=timeout_s,
            corners_echo=corners_echo,
            provenance=provenance,
        )
        if keep_artifacts:
            payload["environment"]["artifacts_dir"] = work_dir
        else:
            _cleanup_dir(work_dir)
        return payload

    sweep_points_result, sweep_engine_version, sweep_log, sweep_diagnostic = _run_sweep(
        device=device,
        corner=sizing_corner,
        target=target,
        models_lib=models_lib,
        w_values=w_grid,
        work_dir=work_dir,
        deck_name="sweep.cir",
        log_name="sweep.log",
        timeout_s=timeout_s,
    )

    valid_points = [
        p
        for p in sweep_points_result
        if p["gm_s"] is not None and p["id_a"] not in (None, 0.0)
    ]
    for p in valid_points:
        p["gm_id"] = abs(p["gm_s"]) / abs(p["id_a"])

    engine_version = sweep_engine_version
    environment = {
        "engine": "ngspice",
        "engine_version": engine_version,
        # Issue #1274: `{path, scope}`, never the resolved absolute path --
        # a PDK model library normally lives outside the repo (under the
        # user's home directory) and would leak that layout into every
        # committed report. `provenance.deck` still pins which library ran.
        "models_lib": _report_path(models_lib, repo_root=repo_root),
    }

    if len(valid_points) < 2:
        payload = _error_payload(
            device=device,
            corner=sizing_corner,
            corners=corners_echo,
            target=target,
            environment=environment,
            provenance=provenance,
            reason=(
                "ngspice did not return usable operating-point data for at "
                "least 2 of the swept widths"
                + (f": {sweep_diagnostic}" if sweep_diagnostic else "")
                + (f" (see sweep log: {sweep_log})" if keep_artifacts else "")
            ),
        )
        if not keep_artifacts:
            _cleanup_dir(work_dir)
        return payload

    bracket, feasibility_note = _find_bracket(valid_points, target["gm_id"])

    if bracket is None:
        # Infeasible within [w_min, w_max]: report the boundary point closest
        # to the target as the best achievable result, never silently "pass".
        boundary = min(valid_points, key=lambda p: abs(p["gm_id"] - target["gm_id"]))
        w_star = boundary["w_um"]
    else:
        lo, hi = bracket
        w_star = _log_interp(
            lo["w_um"], lo["gm_id"], hi["w_um"], hi["gm_id"], target["gm_id"]
        )

    confirm_points, confirm_engine_version, confirm_log, confirm_diagnostic = (
        _run_sweep(
            device=device,
            corner=sizing_corner,
            target=target,
            models_lib=models_lib,
            w_values=[w_star],
            work_dir=work_dir,
            deck_name="confirm.cir",
            log_name="confirm.log",
            timeout_s=timeout_s,
        )
    )
    if confirm_engine_version is not None:
        environment["engine_version"] = confirm_engine_version

    confirmed = confirm_points[0] if confirm_points else None
    if confirmed is None or confirmed["gm_s"] is None or not confirmed["id_a"]:
        payload = _error_payload(
            device=device,
            corner=sizing_corner,
            corners=corners_echo,
            target=target,
            environment=environment,
            provenance=provenance,
            reason=(
                "ngspice confirmation run at the interpolated width did not "
                "return usable operating-point data"
                + (f": {confirm_diagnostic}" if confirm_diagnostic else "")
                + (f" (see confirm log: {confirm_log})" if keep_artifacts else "")
            ),
        )
        if not keep_artifacts:
            _cleanup_dir(work_dir)
        return payload

    operating_point, confirmed_gm_id, vov, vov_is_approx = _op_point_from_confirmed(
        confirmed, device, vds_v=target.get("vds_v")
    )
    gm_id_rel_error = (confirmed_gm_id - target["gm_id"]) / target["gm_id"]
    id_rel_error = (abs(confirmed["id_a"]) - target["id_a"]) / target["id_a"]
    inversion_level = operating_point["inversion_level"]

    feasible = bracket is not None
    within_tolerance = abs(gm_id_rel_error) <= gm_id_rel_tol
    sizing_status = "pass" if (feasible and within_tolerance) else "fail"

    if target.get("vds_v") is not None:
        rationale_parts = [
            f"gm/Id lookup via a fixed-Vds bias sweep: {device['kind']} "
            f"'{device['model']}' at L={device['l_um']}um, Vds held at "
            f"{target['vds_v']:.6g}V by an ideal bias source while a "
            "feedback-regulated gate bias servos Vgs to hit the target "
            f"current {target['id_a']:.6g}A (the confirmed current is "
            "measured, not assumed exact the way the diode-connected mode's "
            "ideal current source guarantees -- see margins.id_rel_error), "
            f"width swept from {device['w_min_um']}um to "
            f"{device['w_max_um']}um ({len(sweep_points_result)} points) to "
            f"bracket the target gm/Id={target['gm_id']:.6g}, then "
            "confirmed by a fresh ngspice operating-point run at the "
            "interpolated width (never trusting the sweep grid or "
            "interpolation alone)."
        ]
    else:
        rationale_parts = [
            "gm/Id lookup via a diode-connected bias sweep (Vds=Vgs): "
            f"{device['kind']} '{device['model']}' at L={device['l_um']}um, "
            "current fixed at "
            f"{target['id_a']:.6g}A by an ideal bias source, width swept from "
            f"{device['w_min_um']}um to {device['w_max_um']}um "
            f"({len(sweep_points_result)} points) to bracket the target gm/Id="
            f"{target['gm_id']:.6g}, then confirmed by a fresh ngspice operating-"
            f"point run at the interpolated width (never trusting the sweep grid "
            f"or interpolation alone)."
        ]
    if not feasible:
        rationale_parts.append(
            f"Target gm/Id={target['gm_id']:.6g} is not reachable within "
            f"[{device['w_min_um']}, {device['w_max_um']}]um at this "
            f"current -- {feasibility_note}; reporting the closest boundary "
            "point actually achieved instead of extrapolating."
        )
    if vov is not None and not vov_is_approx:
        rationale_parts.append(
            f"Inversion level '{inversion_level}' from Vov=Vgs-Vth="
            f"{vov:.4g}V (Vov<=0: weak; 0<Vov<0.1V: moderate; "
            "Vov>=0.1V: strong -- a standard rule-of-thumb threshold, not a "
            "precise inversion-coefficient computation)."
        )
    elif vov is not None and vov_is_approx:
        rationale_parts.append(
            f"Inversion level '{inversion_level}' from Vov~=Vdsat="
            f"{vov:.4g}V (Vth not exposed by this device model; Vdsat "
            "approximates Vov for a square-law device, same thresholds as "
            "above -- less accurate for a model where the two genuinely "
            "diverge)."
        )
    else:
        rationale_parts.append(
            "Inversion level could not be classified: the device model did "
            "not expose Vth or Vdsat at this operating point."
        )

    method = {
        "name": _bias_method_name(target),
        "bias": _bias_label(target),
        "rationale": " ".join(rationale_parts),
        "sweep_points": len(sweep_points_result),
        "valid_sweep_points": len(valid_points),
        "bracket_w_um": (
            [bracket[0]["w_um"], bracket[1]["w_um"]] if bracket is not None else None
        ),
        "interpolated_w_um": w_star,
        "feasible": feasible,
    }

    # Verify the solved width across every OTHER declared corner -- a fresh
    # single-point confirmation each, no further search (issue #729). The
    # sizing corner's own entry is already fully computed above.
    corner_results: list[dict[str, Any]] = [None] * len(corner_points)  # type: ignore[list-item]
    corner_results[sizing_index] = {
        "corner_id": sizing_corner["corner_id"],
        "process": sizing_corner["process"],
        "temperature_c": sizing_corner["temperature_c"],
        "is_sizing": True,
        "status": sizing_status,
        "operating_point": operating_point,
        "margins": {
            "gm_id_rel_error": gm_id_rel_error,
            "id_rel_error": id_rel_error,
        },
        "diagnostic": None,
    }
    for index, other_corner in enumerate(corner_points):
        if index == sizing_index:
            continue
        corner_results[index] = _verify_corner(
            device=device,
            corner=other_corner,
            target=target,
            models_lib=models_lib,
            w_star=w_star,
            work_dir=work_dir,
            deck_name=f"verify_{index}.cir",
            log_name=f"verify_{index}.log",
            timeout_s=timeout_s,
            gm_id_rel_tol=gm_id_rel_tol,
        )
    corners_echo["results"] = corner_results

    # error > fail > pass, mirroring `klt sim`'s own aggregate precedence: an
    # evaluator error at *any* declared corner always wins. Otherwise the
    # sizing corner's own status sets the aggregate -- a non-sizing corner's
    # "fail" is reported spread, not aggregate-failing, unless the request
    # opts in via `targets.hold_across_corners`.
    if any(result["status"] == "error" for result in corner_results):
        status = "error"
    elif sizing_status == "fail":
        status = "fail"
    elif hold_across_corners and any(
        not result["is_sizing"] and result["status"] == "fail"
        for result in corner_results
    ):
        status = "fail"
    else:
        status = "pass"

    payload = {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "device": device,
        "corner": _corner_public(sizing_corner),
        "corners": corners_echo,
        "target": target,
        # Issue #770: hoisted rationale fields, so a caller can read a
        # device's gm/Id target/achieved/inversion-level without
        # reconstructing them from `target`/`operating_point` by hand.
        "gm_id_target": target["gm_id"],
        "gm_id_achieved": confirmed_gm_id,
        "inversion_level": inversion_level,
        "tolerance": {"gm_id_rel": gm_id_rel_tol},
        "operating_point": operating_point,
        "margins": {
            "gm_id_rel_error": gm_id_rel_error,
            "id_rel_error": id_rel_error,
        },
        "method": method,
        "environment": environment,
        "provenance": provenance,
    }

    if keep_artifacts:
        payload["environment"]["artifacts_dir"] = work_dir
    else:
        _cleanup_dir(work_dir)

    return payload


def _parse_design_centering(request: dict[str, Any]) -> dict[str, Any] | None:
    """``request.design_centering`` (issue #1326) --
    ``{"ranking": [...], "parameter_map": {...}}``, the same
    ``ranking[].parameter``/``ranking[].contribution`` contract
    ``design_centering.py`` defines, consumed directly rather than
    reshaped -- see this module's docstring's "Design-centering objective"
    section. Additive to ``corners.objective``: an independent request
    field, not a new ``corners.objective`` value, so it composes with any
    objective (or none).

    Returns ``None`` when the request does not opt in (the overwhelmingly
    common case). Raises :class:`SizeError` for a malformed block -- called
    once, up front, from :func:`_run_size_impl` purely to fail fast before
    any ngspice invocation runs (a malformed block would otherwise only
    surface after the first full solve, in :func:`_apply_design_centering`,
    which parses it again to actually act on it). ``design_centering.py``
    itself validates the ranking array's own per-entry shape once this
    module hands it off.
    """
    spec = request.get("design_centering")
    if spec is None:
        return None
    if not isinstance(spec, dict):
        raise SizeError("request.design_centering must be a JSON object")

    ranking = spec.get("ranking")
    if not isinstance(ranking, list) or not ranking:
        raise SizeError(
            "request.design_centering.ranking must be a non-empty array of "
            "{parameter, contribution} entries -- the same 'ranking' shape "
            "klt yield-sensitivity/klt design-centering produce"
        )

    parameter_map = spec.get("parameter_map")
    if not isinstance(parameter_map, dict) or not parameter_map:
        raise SizeError(
            "request.design_centering.parameter_map must be a non-empty "
            "JSON object mapping ranked parameter names to sized-device "
            "instance names"
        )
    for key, value in parameter_map.items():
        if not isinstance(value, str) or not value:
            raise SizeError(
                f"request.design_centering.parameter_map['{key}'] must be "
                "a non-empty instance name string"
            )
        if "topology" in request and value not in _INSTANCE_TO_ROLE:
            raise SizeError(
                f"request.design_centering.parameter_map['{key}'] names "
                f"sized-device instance '{value}', which is not one of "
                "this topology's own instances (available: "
                f"{', '.join(sorted(_INSTANCE_TO_ROLE))})"
            )

    return {"ranking": ranking, "parameter_map": parameter_map}


def _grow_mult(current_mult: float, area_multiplier: float) -> float:
    """Grow a device's ``mult`` (parallel-instance count, an ``X``-element
    param -- see :func:`_write_sweep_deck`/:func:`_write_topology_deck`) by
    ``area_multiplier``, rounded **up** to the next whole device: ``mult``
    is a literal parallel-device count, so a fractional value is not
    physically realizable. Growing ``mult`` rather than ``w_um`` is what
    lets the design-centering objective (issue #1326) add area without
    perturbing the gm/Id operating point the search already solved for --
    see this module's docstring's "Design-centering objective" section.
    Never shrinks below ``current_mult`` (the caller only reaches this
    helper when ``area_multiplier > 1.0``, but this stays a hard floor
    regardless).
    """
    grown = current_mult * area_multiplier
    return float(max(current_mult, math.ceil(grown - 1e-9)))


def _apply_design_centering(
    request: dict[str, Any],
    request_dir: str,
    payload: dict[str, Any],
    *,
    repo_root: str | None,
    artifacts_dir: str | None,
    timeout_s: float | None,
) -> dict[str, Any]:
    """Wire ``design_centering.py``'s ranking/parameter_map contract into a
    sizing run's own objective (issue #1326, epic #705 Phase 2): grow
    whichever instance(s) ``request.design_centering`` flags as the
    dominant mismatch contributor(s), then re-solve at the grown geometry
    so the returned payload's device/operating-point/margins fields are
    already a re-verified, grown design -- see this module's docstring's
    "Design-centering objective" section for the full method and cost.

    A no-op (returns ``payload`` unchanged) when the request did not
    declare ``request.design_centering`` -- the common case, and why this
    wrapping happens *after* the normal solve rather than folding into it.

    This function never re-solves more than once, regardless of how many
    ranked parameters are mapped: every candidate whose suggested area
    multiplier exceeds ``1x`` is applied in a single follow-up solve (one
    ``mult`` bump per affected role/device), not one re-solve per
    candidate -- "the same pass" the issue calls for, not an iterative
    re-centering loop.
    """
    spec = _parse_design_centering(request)
    if spec is None:
        return payload

    is_topology = "topology" in request
    try:
        mapped, unmapped = design_centering.rank_mapped_parameters(
            spec["ranking"], spec["parameter_map"]
        )
    except design_centering.DesignCenteringError as exc:
        raise SizeError(f"request.design_centering: {exc}") from exc

    if payload.get("status") == "error":
        payload["design_centering"] = {
            "schema_version": DESIGN_CENTERING_SCHEMA_VERSION,
            "requested": True,
            "applied": False,
            "candidates": [],
            "unmapped_parameters": unmapped,
            "grown": {},
            "note": (
                "sizing itself errored before any device geometry was "
                "confirmed -- no design-centering growth attempted"
            ),
        }
        return payload

    # `mapped` preserves `ranking`'s own descending-|contribution| order
    # (design_centering.py's own contract), so `mapped[i+1]` is always the
    # next-lower mapped contributor -- exactly what
    # `suggested_area_multiplier` needs.
    candidates: list[dict[str, Any]] = []
    growth: dict[str, float] = {}  # role (topology) or "device" (single) -> multiplier
    for i, item in enumerate(mapped):
        instance = item["instance"]
        multiplier = design_centering.suggested_area_multiplier(mapped, i)
        applied = False
        if multiplier is not None and multiplier > 1.0:
            # `_parse_design_centering` already checked every mapped
            # instance name against `_INSTANCE_TO_ROLE` in topology mode
            # (fail-fast, before any ngspice invocation) -- this lookup
            # cannot miss here.
            key = _INSTANCE_TO_ROLE[instance] if is_topology else "device"
            growth[key] = max(growth.get(key, 1.0), multiplier)
            applied = True
        candidates.append(
            {
                "rank": item["rank"],
                "parameter": item["parameter"],
                "contribution": item["contribution"],
                "instance": instance,
                "suggested_area_multiplier": multiplier,
                "applied": applied,
            }
        )

    if not growth:
        payload["design_centering"] = {
            "schema_version": DESIGN_CENTERING_SCHEMA_VERSION,
            "requested": True,
            "applied": False,
            "candidates": candidates,
            "unmapped_parameters": unmapped,
            "grown": {},
            "note": (
                "no mapped candidate outranked its next-lower mapped "
                "contributor by a finite area multiplier > 1x -- nothing "
                "to grow"
            ),
        }
        return payload

    # Growing only ever changes `mult` (never `w_um`/`nf`), so the second
    # solve is a fresh, independent search -- not a continuation of the
    # first -- against the same request otherwise unchanged. Re-runs
    # against a distinct artifacts directory when the request keeps
    # artifacts, so the grown pass's decks/logs never silently overwrite
    # the first pass's own (both would otherwise share the same file
    # names).
    grown_request = copy.deepcopy(request)
    grown_request.pop("design_centering", None)
    grown_artifacts_dir = artifacts_dir
    keep_artifacts = bool((request.get("options") or {}).get("keep_artifacts", False))
    if keep_artifacts:
        base = (
            artifacts_dir
            if artifacts_dir is not None
            else os.path.join(request_dir, ".klt", "size")
        )
        grown_artifacts_dir = base + "-design-centering-grown"

    grown_summary: dict[str, Any] = {}
    if is_topology:
        for role, multiplier in growth.items():
            role_device = grown_request["devices"][role]
            before = role_device.get("mult", 1)
            after = _grow_mult(before, multiplier)
            role_device["mult"] = after
            grown_summary[role] = {
                "mult_before": before,
                "mult_after": after,
                "area_multiplier_applied": multiplier,
            }
        grown_payload = _run_topology_size(
            grown_request,
            request_dir,
            repo_root=repo_root,
            artifacts_dir=grown_artifacts_dir,
            timeout_s=timeout_s,
        )
    else:
        multiplier = growth["device"]
        device_spec = grown_request["device"]
        before = device_spec.get("mult", 1)
        after = _grow_mult(before, multiplier)
        device_spec["mult"] = after
        grown_summary["device"] = {
            "mult_before": before,
            "mult_after": after,
            "area_multiplier_applied": multiplier,
        }
        grown_payload = _run_single_device_size(
            grown_request,
            request_dir,
            repo_root=repo_root,
            artifacts_dir=grown_artifacts_dir,
            timeout_s=timeout_s,
        )

    grown_payload["design_centering"] = {
        "schema_version": DESIGN_CENTERING_SCHEMA_VERSION,
        "requested": True,
        "applied": True,
        "candidates": candidates,
        "unmapped_parameters": unmapped,
        "grown": grown_summary,
    }
    return grown_payload


def _op_point_from_confirmed(
    confirmed: dict[str, Any], device: dict[str, Any], *, vds_v: float | None = None
) -> tuple[dict[str, Any], float, float | None, bool]:
    """Build the ``operating_point`` response shape from one confirmed
    sweep-log point -- shared by the sizing corner's own confirmation and
    every other declared corner's verification run (:func:`_verify_corner`).

    ``vds_v`` (issue #1015) echoes ``target.vds_v`` into
    ``operating_point.vds_v`` for the fixed-Vds bias mode, where it is
    enforced *exactly* by an ideal voltage source (see this module's
    docstring's "Fixed-Vds bias mode" section) -- not a measured quantity,
    so no op-point vector read is needed. Left ``None`` (the default) for
    diode-connected single-device mode and for every coupled-topology
    instance (issue #768), neither of which sits at a request-declared
    fixed Vds -- the topology's own non-diode-connected instances measure
    their real in-circuit Vds implicitly via their own Vgs/Vds node
    voltages, which this shared helper does not have a uniform way to
    surface without guessing.

    Returns ``(operating_point, gm_id, vov, vov_is_approx)`` -- the latter
    two are exposed separately because the sizing corner's rationale text
    (built by its caller) needs them past what ``operating_point`` itself
    carries.
    """
    gm_id = abs(confirmed["gm_s"]) / abs(confirmed["id_a"])
    vgs = confirmed["vgs_v"]
    vth = confirmed["vth_v"]
    vdsat = confirmed["vdsat_v"]
    # Overdrive is normalized to NMOS-style "how far above threshold", so
    # `_classify_inversion`'s thresholds are written once for both device
    # kinds. The polarity has to come from the *reported* Vth rather than
    # from `device["kind"]`, because the two model conventions this module
    # meets disagree: a bare SPICE `level=1` PMOS reports negative Vgs/Vth
    # (so its overdrive is Vth-Vgs), while sky130's `__pfet_01v8` subcircuit
    # reports both as positive magnitudes (so its overdrive is Vgs-Vth) --
    # both verified against the installed sky130A PDK and this module's own
    # synthetic test library while building the coupled-topology solver.
    # Keying off sign(Vth) is correct under either convention. Without this,
    # a PMOS on the negative-reporting convention reads back a negative Vov
    # and is misreported as weakly inverted regardless of its actual bias
    # (issue #768: the coupled topology's mirror load is a PMOS, and its
    # inversion-level rationale is part of that issue's acceptance criteria).
    vov_is_approx = False
    if vgs is not None and vth is not None and vth != 0:
        vov = math.copysign(1.0, vth) * (vgs - vth)
    elif vdsat is not None:
        # Fallback for a compact model that does not expose Vth via its
        # internal op-point vectors (e.g. a bare SPICE level=1 device, see
        # this module's test fixtures): Vdsat approximates Vov for a simple
        # square-law model in saturation. Flagged as approximate in the
        # rationale below -- not accurate for a model where Vdsat and Vov
        # genuinely diverge (e.g. velocity-saturated short-channel BSIM).
        # Magnitude only, same polarity normalization as above.
        vov = abs(vdsat)
        vov_is_approx = True
    else:
        vov = None
    operating_point = {
        "w_um": confirmed["w_um"],
        "l_um": device["l_um"],
        "nf": device["nf"],
        "mult": device["mult"],
        "id_a": abs(confirmed["id_a"]),
        "gm_s": abs(confirmed["gm_s"]),
        "gm_id": gm_id,
        "vgs_v": vgs,
        "vds_v": vds_v,
        "vth_v": vth,
        "vov_v": vov,
        "vdsat_v": vdsat,
        "inversion_level": _classify_inversion(vov),
    }
    return operating_point, gm_id, vov, vov_is_approx


def _verify_corner(
    *,
    device: dict[str, Any],
    corner: dict[str, Any],
    target: dict[str, Any],
    models_lib: str,
    w_star: float,
    work_dir: str,
    deck_name: str,
    log_name: str,
    timeout_s: float,
    gm_id_rel_tol: float,
    is_sizing: bool = False,
) -> dict[str, Any]:
    """Verify a solved width ``w_star`` at one declared ``corner``: a
    single-point confirmation, no further search (issue #729's "verify the
    solved width... across every declared corner"). Never raises -- an
    evaluator failure at this corner yields an ``"error"`` entry, exactly
    like the sizing corner's own confirmation failure does via
    :func:`_error_payload`.

    ``is_sizing`` defaults to ``False`` (the ``"sizing_corner"`` objective's
    own usage -- called once per *other* declared corner, the sizing
    corner's own entry is built inline by :func:`run_size`). The
    ``"worst_case_margin"`` objective (:func:`_run_worst_case_margin`) calls
    this uniformly for *every* declared corner instead, passing
    ``is_sizing=True`` only for the one matching the request's declared
    ``corners.sizing`` -- a purely informational echo under that objective,
    since the width search itself no longer targets one corner specially.
    """
    points, _engine_version, _log_path, diagnostic = _run_sweep(
        device=device,
        corner=corner,
        target=target,
        models_lib=models_lib,
        w_values=[w_star],
        work_dir=work_dir,
        deck_name=deck_name,
        log_name=log_name,
        timeout_s=timeout_s,
    )
    confirmed = points[0] if points else None
    if confirmed is None or confirmed["gm_s"] is None or not confirmed["id_a"]:
        return {
            "corner_id": corner["corner_id"],
            "process": corner["process"],
            "temperature_c": corner["temperature_c"],
            "is_sizing": is_sizing,
            "status": "error",
            "operating_point": None,
            "margins": None,
            "diagnostic": (
                "ngspice verification run at the sizing width did not "
                "return usable operating-point data"
                + (f": {diagnostic}" if diagnostic else "")
            ),
        }

    operating_point, gm_id, _vov, _vov_is_approx = _op_point_from_confirmed(
        confirmed, device, vds_v=target.get("vds_v")
    )
    gm_id_rel_error = (gm_id - target["gm_id"]) / target["gm_id"]
    id_rel_error = (abs(confirmed["id_a"]) - target["id_a"]) / target["id_a"]
    status = "pass" if abs(gm_id_rel_error) <= gm_id_rel_tol else "fail"
    return {
        "corner_id": corner["corner_id"],
        "process": corner["process"],
        "temperature_c": corner["temperature_c"],
        "is_sizing": is_sizing,
        "status": status,
        "operating_point": operating_point,
        "margins": {
            "gm_id_rel_error": gm_id_rel_error,
            "id_rel_error": id_rel_error,
        },
        "diagnostic": None,
    }


def _interp_gm_id_at_width(points: list[dict[str, Any]], w_um: float) -> float:
    """Log-linear interpolate (or clamp at a boundary) the ``gm_id`` value
    at an arbitrary width from one corner's own valid sweep points -- the
    forward counterpart of :func:`_log_interp` (which solves the inverse:
    the width at which a target ``gm_id`` is hit). ``points`` must be
    sorted by ``w_um`` ascending and non-empty.
    """
    if w_um <= points[0]["w_um"]:
        return points[0]["gm_id"]
    if w_um >= points[-1]["w_um"]:
        return points[-1]["gm_id"]
    for lo, hi in zip(points, points[1:], strict=False):
        if lo["w_um"] <= w_um <= hi["w_um"]:
            if hi["w_um"] == lo["w_um"]:
                return lo["gm_id"]
            frac = (math.log(w_um) - math.log(lo["w_um"])) / (
                math.log(hi["w_um"]) - math.log(lo["w_um"])
            )
            return lo["gm_id"] + frac * (hi["gm_id"] - lo["gm_id"])
    return points[-1][
        "gm_id"
    ]  # pragma: no cover -- unreachable given the bounds checks above


def _run_worst_case_margin(
    *,
    device: dict[str, Any],
    corner_points: list[dict[str, Any]],
    sizing_index: int,
    target: dict[str, Any],
    models_lib: str,
    repo_root: str | None,
    gm_id_rel_tol: float,
    w_grid: list[float],
    work_dir: str,
    timeout_s: float,
    corners_echo: dict[str, Any],
    provenance: dict[str, Any],
) -> dict[str, Any]:
    """``request.corners.objective: "worst_case_margin"`` (issue #769): find
    the single width that maximizes the *worst* per-corner gm/Id margin
    across every declared corner -- see this module's docstring's "Worst-
    corner margin objective" section for the minimax-bisection method and
    its cost relative to the default ``"sizing_corner"`` objective
    (:func:`run_size`, unchanged).
    """
    corner_curves: dict[int, list[dict[str, Any]]] = {}
    corner_diagnostics: dict[int, tuple[str, str | None]] = {}
    engine_version: str | None = None

    for index, corner in enumerate(corner_points):
        points, version, log_path, diagnostic = _run_sweep(
            device=device,
            corner=corner,
            target=target,
            models_lib=models_lib,
            w_values=w_grid,
            work_dir=work_dir,
            deck_name=f"sweep_corner{index}.cir",
            log_name=f"sweep_corner{index}.log",
            timeout_s=timeout_s,
        )
        if version is not None:
            engine_version = version
        valid = [
            p for p in points if p["gm_s"] is not None and p["id_a"] not in (None, 0.0)
        ]
        for p in valid:
            p["gm_id"] = abs(p["gm_s"]) / abs(p["id_a"])
        if len(valid) < 2:
            corner_diagnostics[index] = (log_path, diagnostic)
        else:
            corner_curves[index] = sorted(valid, key=lambda p: p["w_um"])

    environment = {
        "engine": "ngspice",
        "engine_version": engine_version,
        # Issue #1274 -- see `run_size`'s own `environment` block.
        "models_lib": _report_path(models_lib, repo_root=repo_root),
    }

    if not corner_curves:
        first_index = next(iter(corner_diagnostics))
        log_path, diagnostic = corner_diagnostics[first_index]
        reason = (
            "ngspice did not return usable operating-point data for at "
            "least 2 of the swept widths at any declared corner (first "
            f"seen at {corner_points[first_index]['corner_id']}"
            + (f": {diagnostic}" if diagnostic else "")
            + f", see sweep log: {log_path})"
        )
        return _error_payload(
            device=device,
            corner=corner_points[sizing_index],
            corners=corners_echo,
            target=target,
            environment=environment,
            provenance=provenance,
            reason=reason,
        )

    w_min_um, w_max_um = device["w_min_um"], device["w_max_um"]

    def _bounds_error(w_um: float) -> tuple[float, float]:
        errors = [
            (_interp_gm_id_at_width(corner_curves[i], w_um) - target["gm_id"])
            / target["gm_id"]
            for i in corner_curves
        ]
        return max(errors), min(errors)

    u_lo, l_lo = _bounds_error(w_min_um)
    u_hi, l_hi = _bounds_error(w_max_um)
    h_lo, h_hi = u_lo + l_lo, u_hi + l_hi

    if h_lo >= 0:
        # Every declared corner is already at or above target gm/Id even at
        # the narrowest allowed width -- widening only makes it worse, so
        # the least-bad achievable width is the lower bound.
        w_star, feasible = w_min_um, False
    elif h_hi <= 0:
        # The mirror image: every corner is still below target gm/Id even
        # at the widest allowed width.
        w_star, feasible = w_max_um, False
    else:
        lo_ln, hi_ln = math.log(w_min_um), math.log(w_max_um)
        for _ in range(_WORST_CASE_BISECTION_ITERS):
            mid_ln = (lo_ln + hi_ln) / 2
            u_mid, l_mid = _bounds_error(math.exp(mid_ln))
            if u_mid + l_mid < 0:
                lo_ln = mid_ln
            else:
                hi_ln = mid_ln
        w_star, feasible = math.exp((lo_ln + hi_ln) / 2), True

    corner_results: list[dict[str, Any] | None] = [None] * len(corner_points)
    for index, corner in enumerate(corner_points):
        if index in corner_curves:
            corner_results[index] = _verify_corner(
                device=device,
                corner=corner,
                target=target,
                models_lib=models_lib,
                w_star=w_star,
                work_dir=work_dir,
                deck_name=f"confirm_corner{index}.cir",
                log_name=f"confirm_corner{index}.log",
                timeout_s=timeout_s,
                gm_id_rel_tol=gm_id_rel_tol,
                is_sizing=(index == sizing_index),
            )
        else:
            log_path, diagnostic = corner_diagnostics[index]
            corner_results[index] = {
                "corner_id": corner["corner_id"],
                "process": corner["process"],
                "temperature_c": corner["temperature_c"],
                "is_sizing": index == sizing_index,
                "status": "error",
                "operating_point": None,
                "margins": None,
                "diagnostic": (
                    "ngspice did not return usable operating-point data for "
                    "this corner's own width sweep"
                    + (f": {diagnostic}" if diagnostic else "")
                ),
            }
    corners_echo["results"] = corner_results

    usable = [
        i for i, result in enumerate(corner_results) if result["margins"] is not None
    ]
    if not usable:
        return _error_payload(
            device=device,
            corner=corner_points[sizing_index],
            corners=corners_echo,
            target=target,
            environment=environment,
            provenance=provenance,
            reason=(
                "ngspice confirmation at the worst-case-margin width did "
                "not return usable operating-point data at any declared "
                "corner -- see corners.results for the per-corner detail"
            ),
        )

    worst_index = max(
        usable, key=lambda i: abs(corner_results[i]["margins"]["gm_id_rel_error"])
    )
    worst = corner_results[worst_index]
    corners_echo["worst_case"] = worst["corner_id"]

    status = (
        "error"
        if any(result["status"] == "error" for result in corner_results)
        else worst["status"]
    )

    corners_used = [corner_points[i]["corner_id"] for i in sorted(corner_curves)]
    rationale = (
        "Width chosen to maximize the worst per-corner gm/Id margin across "
        f"{len(corners_used)} declared corner(s) ({', '.join(corners_used)}) via "
        "a bisection on ln(W), balancing each corner's own gm/Id(W) curve "
        "(swept once per corner, confirmed by a fresh ngspice operating-point "
        f"run at the converged width): W={w_star:.6g}um, worst margin at "
        f"'{worst['corner_id']}' (gm_id_rel_error="
        f"{worst['margins']['gm_id_rel_error']:+.4g})."
    )
    if not feasible:
        rationale += (
            " Every declared corner sits on the same side of the target "
            "throughout [w_min_um, w_max_um] -- reporting the boundary "
            "width closest to equalizing the margin instead of "
            "extrapolating."
        )

    method = {
        "name": "worst-corner margin via minimax bisection across the declared PVT set",
        "bias": _bias_label(target),
        "rationale": rationale,
        "sweep_points": len(w_grid),
        "valid_sweep_points": min(len(corner_curves[i]) for i in corner_curves),
        "bracket_w_um": None,
        "interpolated_w_um": w_star,
        "feasible": feasible,
    }

    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "device": device,
        "corner": _corner_public(corner_points[worst_index]),
        "corners": corners_echo,
        "target": target,
        # Issue #770: hoisted from the worst corner's own confirmed
        # operating point -- see the default-objective payload above.
        "gm_id_target": target["gm_id"],
        "gm_id_achieved": worst["operating_point"]["gm_id"],
        "inversion_level": worst["operating_point"]["inversion_level"],
        "tolerance": {"gm_id_rel": gm_id_rel_tol},
        "operating_point": worst["operating_point"],
        "margins": worst["margins"],
        "method": method,
        "environment": environment,
        "provenance": provenance,
    }


def _error_payload(
    *,
    device: dict[str, Any],
    corner: dict[str, Any],
    corners: dict[str, Any],
    target: dict[str, Any],
    environment: dict[str, Any],
    provenance: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    """Build the ``status: "error"`` response shape -- the evaluator itself
    could not produce a trustworthy result at the sizing corner, so no other
    declared corner is verified either (there is no solved width to check).
    ``method`` is still populated (per this module's docstring, "no stated
    method is rejected" applies to every status, not just a successful
    one); ``corners.declared``/``corners.sizing`` are still echoed,
    ``corners.results`` stays ``None`` (never evaluated). ``gm_id_target``
    (issue #770) is still echoed from the request's own declared target --
    ``gm_id_achieved``/``inversion_level`` are ``None`` since no operating
    point was ever confirmed.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "error",
        "device": device,
        "corner": _corner_public(corner),
        "corners": corners,
        "target": target,
        "gm_id_target": target["gm_id"],
        "gm_id_achieved": None,
        "inversion_level": None,
        "tolerance": None,
        "operating_point": None,
        "margins": None,
        "method": {
            "name": _bias_method_name(target),
            "bias": _bias_label(target),
            "rationale": f"Evaluator error: {reason}",
            "sweep_points": None,
            "valid_sweep_points": None,
            "bracket_w_um": None,
            "interpolated_w_um": None,
            "feasible": None,
        },
        "environment": environment,
        "provenance": provenance,
    }


def _classify_inversion(vov: float | None) -> str:
    if vov is None:
        return "unknown"
    if vov <= 0:
        return "weak"
    if vov < 0.1:
        return "moderate"
    return "strong"


def _log_space(lo: float, hi: float, n: int) -> list[float]:
    """``n`` log-spaced points from ``lo`` to ``hi`` inclusive."""
    if n == 1:
        return [lo]
    log_lo, log_hi = math.log(lo), math.log(hi)
    step = (log_hi - log_lo) / (n - 1)
    return [math.exp(log_lo + i * step) for i in range(n)]


def _log_interp(
    w_lo: float, g_lo: float, w_hi: float, g_hi: float, target: float
) -> float:
    """Interpolate the width at which ``gm/Id`` crosses ``target``, linear in
    ``ln(w)`` between the two bracketing sweep points."""
    if g_hi == g_lo:
        return w_lo
    frac = (target - g_lo) / (g_hi - g_lo)
    log_w = math.log(w_lo) + frac * (math.log(w_hi) - math.log(w_lo))
    return math.exp(log_w)


def _find_bracket(
    points: list[dict[str, Any]], target_gm_id: float
) -> tuple[tuple[dict[str, Any], dict[str, Any]] | None, str]:
    """Find the adjacent pair of (width-sorted) points whose ``gm_id``
    brackets ``target_gm_id``. ``gm/Id`` is monotonically increasing in
    width at fixed current (see this module's docstring) -- returns
    ``(None, note)`` when the target falls outside the swept range instead
    of extrapolating.
    """
    ordered = sorted(points, key=lambda p: p["w_um"])
    if target_gm_id < ordered[0]["gm_id"]:
        return None, (
            f"target is below the lowest achievable gm/Id in the swept range "
            f"({ordered[0]['gm_id']:.4g} at w_min={ordered[0]['w_um']:.4g}um) -- "
            "gm/Id only increases with width at fixed current, so no width "
            "in bounds reaches a lower value; lower w_min_um or accept a "
            "larger gm/Id target"
        )
    if target_gm_id > ordered[-1]["gm_id"]:
        return None, (
            f"target exceeds the highest achievable gm/Id in the swept range "
            f"({ordered[-1]['gm_id']:.4g} at w_max={ordered[-1]['w_um']:.4g}um) "
            "-- gm/Id only increases with width at fixed current, so no "
            "width in bounds reaches a higher value; raise w_max_um or "
            "accept a smaller gm/Id target"
        )
    for lo, hi in zip(ordered, ordered[1:], strict=False):
        if lo["gm_id"] <= target_gm_id <= hi["gm_id"]:
            return (lo, hi), ""
    # Non-monotonic sweep data (a real but non-ideal model can wobble at the
    # edges of validity) -- fall back to "not bracketed" rather than
    # picking an arbitrary pair.
    return None, "sweep data was not monotonic across the requested range"


def _write_sweep_deck(
    *,
    deck_path: str,
    device: dict[str, Any],
    corner: dict[str, Any],
    target: dict[str, Any],
    models_lib: str,
    w_values: list[float],
) -> None:
    """Generate a single ngspice deck: one ``.lib`` parse, then one
    ``op``-analysis point per ``w_values`` entry, using ``alterparam``/
    ``reset`` between points (cheap re-elaboration, no re-parse of the model
    library -- see this module's docstring's "Two-invocation-per-request
    design").

    ``target.vds_v`` (issue #1015) switches the bias topology from the
    default diode-connected tie (``Vds=Vgs``) to a fixed-``Vds`` feedback
    bias -- see this module's docstring's "Fixed-Vds bias mode" section.
    Everything else (the ``alterparam``/``reset`` width sweep, the marker/
    print protocol :func:`_parse_sweep_log` reads back) is identical between
    the two modes.
    """
    kind = device["kind"]
    model = device["model"]
    op = device["op_point_element"]
    id_a = target["id_a"]
    vdd = corner["vdd_v"]
    l_um = device["l_um"]
    nf = device["nf"]
    mult = device["mult"]
    vds_v = target.get("vds_v")

    lines = ["* klt size -- generated device sweep deck, do not edit"]
    lines.append(f".param w_sweep={w_values[0]!r}")
    process_sections = corner.get("process_sections")
    if process_sections:
        # Multi-section corner bundle (e.g. gf180mcu's per-device-family
        # `.lib` cards) -- one line per declared section, in order, matching
        # `sim.py`'s own `_write_corner_deck` handling of the same shape.
        for section in process_sections:
            lines.append(f".lib {models_lib} {section}")
    else:
        lines.append(f".lib {models_lib} {corner['process']}")
    lines.append(f"Vdd vdd 0 DC {vdd!r}")
    x_params = f"l={l_um!r} w={{w_sweep}} nf={nf!r} mult={mult!r}"
    if vds_v is None:
        if kind == "nmos":
            lines.append(f"Idc vdd drain DC {id_a!r}")
            lines.append(f"X1 drain drain 0 0 {model} {x_params}")
        else:
            lines.append(f"Idc drain 0 DC {id_a!r}")
            lines.append(f"X1 drain drain vdd vdd {model} {x_params}")
    else:
        # Fixed-Vds bias (issue #1015): an ideal voltage source holds the
        # drain (nmos: referenced to the grounded source; pmos: referenced
        # to the vdd-tied source, so the drain sits vds_v *below* vdd) at
        # exactly the requested Vds. `Bgate` is a behavioral source
        # expressing the gate voltage as a clamped, high-loop-gain function
        # of the current error normalized by the target current (see this
        # module's docstring for why normalized, not absolute, gain) --
        # ngspice's own DC Newton-Raphson solver resolves this closed loop
        # directly at each `op` point, exactly like any other feedback
        # network (e.g. an op-amp macro-model) at DC. `i(Vds)` (the current
        # ngspice reports flowing through the `Vds` source) is the negative
        # of the device's drawn current for an nmos (the source *delivers*
        # current into the drain) and the positive for a pmos (the source
        # *sinks* the current the device pulls from vdd) -- both signs
        # verified empirically against this module's own synthetic device
        # library while building this feature.
        gain = _FIXED_VDS_FEEDBACK_GAIN
        if kind == "nmos":
            lines.append(f"Vds drain 0 DC {vds_v!r}")
            gate_error = f"(1+i(Vds)/{id_a!r})"
            lines.append(f"X1 drain gate 0 0 {model} {x_params}")
        else:
            lines.append(f"Vds drain 0 DC {(vdd - vds_v)!r}")
            gate_error = f"(i(Vds)/{id_a!r}-1)"
            lines.append(f"X1 drain gate vdd vdd {model} {x_params}")
        lines.append(
            f"Bgate gate 0 V=min(max({vdd / 2!r}+{gain!r}*{gate_error},0),{vdd!r})"
        )
    # A top-level dot-card, like `sim.py`'s own corner deck -- `.temp` is not
    # a `.control`-block command. `reset` (used between sweep points below)
    # re-elaborates the circuit from this same parsed source, so the
    # temperature carries through every point without repeating the card.
    lines.append(f".temp {corner['temperature_c']!r}")

    lines.append(".control")
    for index, w_value in enumerate(w_values):
        if index > 0:
            lines.append(f"alterparam w_sweep={w_value!r}")
            lines.append("reset")
        lines.append("op")
        lines.append(f"echo KLT_SIZE_POINT {index} {w_value!r}")
        for param in _OP_PARAMS:
            lines.append(f"print @m.x1.{op}[{param}]")
    lines.append("quit")
    lines.append(".endc")
    lines.append(".end")

    with open(deck_path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def _run_sweep(
    *,
    device: dict[str, Any],
    corner: dict[str, Any],
    target: dict[str, Any],
    models_lib: str,
    w_values: list[float],
    work_dir: str,
    deck_name: str,
    log_name: str,
    timeout_s: float,
) -> tuple[list[dict[str, Any]], str | None, str, str | None]:
    """Run one ngspice invocation sweeping ``w_values``, returning
    ``(points, engine_version, log_path, diagnostic)``. ``points`` always has
    exactly ``len(w_values)`` entries, in order; a value ngspice did not
    print for a given point is ``None`` in that point's dict rather than the
    point being dropped, so callers can tell "ran but incomplete" from
    "never ran". ``diagnostic`` is the first fatal-error line found in the
    log (see :data:`_FATAL_LOG_RE`), or ``None`` if none was found -- folded
    into the error payload's ``method.rationale`` when the run is otherwise
    unusable. Never raises -- a launch failure or timeout yields ``gm_s:
    None`` for every point and a synthetic ``diagnostic`` instead.
    """
    deck_path = os.path.join(work_dir, deck_name)
    log_path = os.path.join(work_dir, log_name)
    _write_sweep_deck(
        deck_path=deck_path,
        device=device,
        corner=corner,
        target=target,
        models_lib=models_lib,
        w_values=w_values,
    )

    log_text, engine_version, diagnostic = _invoke_ngspice(
        deck_path, log_path, timeout_s
    )
    points = _parse_sweep_log(log_text, len(w_values), w_values)
    return points, engine_version, log_path, diagnostic


def _invoke_ngspice(
    deck_path: str, log_path: str, timeout_s: float
) -> tuple[str, str | None, str | None]:
    """Launch ``ngspice -b <deck_path> -o <log_path>``, returning
    ``(log_text, engine_version, diagnostic)``. Shared by every ``klt size``
    ngspice invocation -- the single-device sweep/confirm decks
    (:func:`_run_sweep`) and the coupled-topology joint solve's per-candidate
    deck (:func:`_run_joint_solve`) alike -- so the subprocess-launch,
    timeout, and fatal-error-detection handling (:data:`_FATAL_LOG_RE`) is
    written once. Never raises -- a launch failure or timeout yields an
    empty/partial ``log_text`` and a synthetic ``diagnostic`` instead.
    """
    engine_version: str | None = None
    launch_diagnostic: str | None = None
    try:
        completed = subprocess.run(
            ["ngspice", "-b", deck_path, "-o", log_path],
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        match = _ENGINE_VERSION_RE.search(completed.stdout or "")
        engine_version = match.group(1) if match else None
    except subprocess.TimeoutExpired:
        launch_diagnostic = f"ngspice did not complete within {timeout_s}s, killed"
    except FileNotFoundError as exc:
        launch_diagnostic = f"could not launch ngspice: {exc}"

    log_text = ""
    if os.path.isfile(log_path):
        try:
            with open(log_path, encoding="utf-8", errors="replace") as handle:
                log_text = handle.read()
        except OSError:
            log_text = ""

    diagnostic = launch_diagnostic
    if diagnostic is None:
        fatal_match = _FATAL_LOG_RE.search(log_text)
        if fatal_match:
            diagnostic = _line_containing(log_text, fatal_match.start())

    return log_text, engine_version, diagnostic


def _parse_sweep_log(
    log_text: str, expected_count: int, w_values: list[float]
) -> list[dict[str, Any]]:
    points: list[dict[str, Any] | None] = [None] * expected_count
    current_index: int | None = None
    current_values: dict[str, float] = {}

    def _flush() -> None:
        if current_index is None:
            return
        entry: dict[str, Any] = {"w_um": w_values[current_index]}
        for param in _OP_PARAMS:
            key = _OP_PARAM_KEYS[param]
            entry[key] = current_values.get(param)
        points[current_index] = entry

    for raw_line in log_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        marker = _MARKER_RE.match(line)
        if marker:
            _flush()
            current_index = int(marker.group(1))
            current_values = {}
            continue
        value_match = _OP_VALUE_RE.match(line)
        if value_match and current_index is not None:
            key, raw_value = value_match.groups()
            try:
                current_values[key] = float(raw_value)
            except ValueError:
                continue
    _flush()

    return [
        p
        if p is not None
        else {
            "w_um": w_values[i],
            "gm_s": None,
            "id_a": None,
            "vgs_v": None,
            "vth_v": None,
            "vdsat_v": None,
        }
        for i, p in enumerate(points)
    ]


def _cleanup_dir(path: str) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Coupled multi-device topology sizing (issue #768, Phase 1a of epic #705)
# --------------------------------------------------------------------------- #
#
# See this module's docstring's "Coupled multi-device topology sizing"
# section for the method overview. Request/response shape is documented in
# ``docs/cli/size.md``'s "Coupled multi-device topology sizing" section.

_INSTANCE_MARKER_RE = re.compile(r"^KLT_SIZE_INSTANCE\s+(\S+)\s*$")
_NODE_VALUE_RE = re.compile(r"^v\((\w+)\)\s*=\s*([-+0-9.eEgGnN]+)\s*$")

#: Every device instance the assembled ``diff_pair_mirror_tail`` deck
#: contains, in report order: ``(key, spice_instance, role, is_control)``.
#: Six instances, three sizing roles -- the input pair and the mirror load
#: are each a matched pair sharing one solved width, and the tail is a
#: matched pair too (the diode-connected bias replica ``tail_ref`` plus the
#: tail device proper). ``is_control`` marks the one instance per role whose
#: *in-circuit* gm/Id drives that role's width update in the joint solve
#: (:func:`_run_joint_solve`) and gates the aggregate status; the other
#: instances of the same role are still fully reported (their own gm/Id,
#: inversion level, and rationale) but do not add a redundant degree of
#: freedom -- they share a width with their partner by construction.
TOPOLOGY_INSTANCES = (
    ("tail_ref", "xtailref", "tail", False),
    ("tail", "xtail", "tail", True),
    ("input_a", "xinputa", "input_pair", True),
    ("input_b", "xinputb", "input_pair", False),
    ("mirror_a", "xmirrora", "mirror", True),
    ("mirror_b", "xmirrorb", "mirror", False),
)

#: Human-readable placement of each instance in the topology, folded into
#: that instance's own rationale string so a response explains *where* the
#: device sits, not just what it measured (issue #768's third acceptance
#: criterion).
_INSTANCE_DESCRIPTION = {
    "tail_ref": (
        "diode-connected tail-bias replica (gate/drain tied to the reference "
        "current), Vds=Vgs"
    ),
    "tail": (
        "tail current source, gate driven by the bias replica, drain at the "
        "input pair's common source node"
    ),
    "input_a": (
        "differential-pair leg driving the mirror's diode leg (drain at the "
        "mirror node)"
    ),
    "input_b": (
        "differential-pair leg driving the output node (drain at the "
        "single-ended output)"
    ),
    "mirror_a": "diode-connected leg of the PMOS current-mirror load, Vds=Vgs",
    "mirror_b": (
        "output leg of the PMOS current-mirror load, drain at the single-ended output"
    ),
}

#: The instance whose measured gm/Id drives each role's width update.
_ROLE_CONTROL_INSTANCE = {
    role: key for key, _instance, role, is_control in TOPOLOGY_INSTANCES if is_control
}

#: Instance key (`design_centering.py`'s own geometry key, e.g. `"input_a"`)
#: -> the role that instance's width is solved under (issue #1326) -- the
#: bridge the design-centering objective mode needs to turn a
#: `request.design_centering.parameter_map` instance name into the
#: `request.devices` key it actually grows.
_INSTANCE_TO_ROLE = {
    key: role for key, _instance, role, _is_control in TOPOLOGY_INSTANCES
}


def _parse_topology_devices(devices: Any, topology: str) -> dict[str, dict[str, Any]]:
    """Parse ``request.devices`` for a coupled topology: one
    :func:`_parse_device`-shaped entry per role in :data:`TOPOLOGY_ROLES`,
    each required to declare the ``device.kind`` :data:`_TOPOLOGY_ROLE_KIND`
    fixes for the shipped topology orientation.
    """
    if not isinstance(devices, dict):
        raise SizeError("request.devices must be a JSON object")

    parsed: dict[str, dict[str, Any]] = {}
    for role in TOPOLOGY_ROLES:
        role_device = devices.get(role)
        if role_device is None:
            raise SizeError(
                f"request.devices.{role} is required for topology '{topology}'"
            )
        parsed_device = _parse_device(role_device)
        expected_kind = _TOPOLOGY_ROLE_KIND[role]
        if parsed_device["kind"] != expected_kind:
            raise SizeError(
                f"request.devices.{role}.kind must be '{expected_kind}' for "
                f"topology '{topology}' (got {parsed_device['kind']!r}) -- this "
                "MVP only supports the NMOS-input-pair/PMOS-mirror-load/"
                "NMOS-tail orientation (see docs/cli/size.md's 'Known "
                "limitations')"
            )
        parsed[role] = parsed_device
    return parsed


def _parse_topology_budget(budget: Any) -> dict[str, Any]:
    """Parse ``request.budget``: the shared tail-current budget and the
    tail mirror ratio, the two coupled degrees of freedom a
    ``diff_pair_mirror_tail`` request declares instead of a per-device
    current (see this module's docstring's "Coupled multi-device topology
    sizing" section).

    ``tail_mirror_ratio`` is the tail device's multiplier relative to the
    diode-connected bias replica: the deck injects ``tail_current_a /
    tail_mirror_ratio`` into the replica and lets the mirror multiply it
    back up, so a ratio of ``4`` costs a quarter of the reference current
    for the same tail current. Defaults to ``1`` (the 1:1 replica this
    repo's own 5T OTA canary uses).
    """
    if not isinstance(budget, dict):
        raise SizeError("request.budget must be a JSON object")
    tail_current_a = _require_positive_number(
        budget.get("tail_current_a"), "request.budget.tail_current_a"
    )
    tail_mirror_ratio = budget.get("tail_mirror_ratio", 1)
    tail_mirror_ratio = _require_positive_number(
        tail_mirror_ratio, "request.budget.tail_mirror_ratio"
    )
    return {
        "tail_current_a": tail_current_a,
        "tail_mirror_ratio": tail_mirror_ratio,
        "reference_current_a": tail_current_a / tail_mirror_ratio,
        "branch_current_a": tail_current_a / 2.0,
    }


def _parse_topology_targets(target: Any, topology: str) -> dict[str, dict[str, Any]]:
    """Parse ``request.target`` for a coupled topology: one
    ``{"gm_id": number}`` entry per role -- unlike single-device mode's
    ``target.id_a``, current is never declared per role here; it is derived
    from ``request.budget`` (see :func:`_parse_topology_budget`), since the
    whole point of coupled sizing is that a role's current is not an
    independent choice.
    """
    if not isinstance(target, dict):
        raise SizeError("request.target must be a JSON object")
    parsed: dict[str, dict[str, Any]] = {}
    for role in TOPOLOGY_ROLES:
        role_target = target.get(role)
        if not isinstance(role_target, dict):
            raise SizeError(
                f"request.target.{role} is required for topology '{topology}'"
            )
        gm_id = _require_positive_number(
            role_target.get("gm_id"), f"request.target.{role}.gm_id"
        )
        parsed[role] = {"gm_id": gm_id}
    return parsed


def _parse_topology_bias(bias: Any) -> dict[str, Any]:
    """Parse the optional ``request.bias`` block controlling the assembled
    topology's input common-mode voltage (defaults to ``Vdd/2`` -- a typical
    mid-supply operating point for a differential pair's gates)."""
    if bias is None:
        return {"vcm_v": None}
    if not isinstance(bias, dict):
        raise SizeError("request.bias must be a JSON object")
    vcm_v = bias.get("vcm_v")
    if vcm_v is not None:
        if isinstance(vcm_v, bool) or not isinstance(vcm_v, (int, float)):
            raise SizeError("request.bias.vcm_v must be a number")
        vcm_v = float(vcm_v)
    return {"vcm_v": vcm_v}


def _parse_max_joint_iterations(options: dict[str, Any]) -> int:
    value = options.get("max_joint_iterations", DEFAULT_MAX_JOINT_ITERATIONS)
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < MIN_JOINT_ITERATIONS
    ):
        raise SizeError(
            "request.options.max_joint_iterations must be an integer >= "
            f"{MIN_JOINT_ITERATIONS}"
        )
    return value


def _invert_curve(
    points: list[dict[str, Any]], target_gm_id: float
) -> tuple[float, bool, str]:
    """Solve one role's already-swept ``gm_id(W)`` curve for the width at
    which it hits ``target_gm_id``, returning ``(w_um, feasible, note)``.

    ``gm/Id`` is monotonically increasing in width at fixed current (this
    module's own documented, empirically verified assumption -- see the
    "Method" docstring section), so the inversion is a bracket search
    (:func:`_find_bracket`) plus a log-linear interpolation
    (:func:`_log_interp`) -- exactly what single-device mode does. A target
    outside the swept range is *not* extrapolated: the closest boundary
    point actually achieved is returned with ``feasible=False``.
    """
    ordered = sorted(points, key=lambda p: p["w_um"])
    bracket, note = _find_bracket(ordered, target_gm_id)
    if bracket is None:
        boundary = min(ordered, key=lambda p: abs(p["gm_id"] - target_gm_id))
        return boundary["w_um"], False, note
    lo, hi = bracket
    return (
        _log_interp(lo["w_um"], lo["gm_id"], hi["w_um"], hi["gm_id"], target_gm_id),
        True,
        "",
    )


def _run_topology_size(
    request: dict[str, Any],
    request_dir: str,
    *,
    repo_root: str | None,
    artifacts_dir: str | None,
    timeout_s: float | None,
) -> dict[str, Any]:
    """Solve a coupled ``diff_pair_mirror_tail`` topology's three widths
    jointly, against the *assembled* circuit in ngspice -- see this module's
    docstring's "Coupled multi-device topology sizing" section for the
    method and ``docs/cli/size.md`` for the full request/response schema.
    """
    topology = request.get("topology")
    if topology not in SUPPORTED_TOPOLOGIES:
        raise SizeError(
            f"unsupported topology '{topology}' (supported: "
            f"{', '.join(SUPPORTED_TOPOLOGIES)})"
        )
    if "corners" in request:
        raise SizeError(
            "topology mode ('request.topology') does not yet support a "
            "declared corner set ('request.corners') -- size at a single "
            "corner and re-verify with klt sim across corners; see "
            "docs/cli/size.md's 'Known limitations'"
        )

    devices = _parse_topology_devices(request.get("devices"), topology)
    corner = _parse_corner(request.get("corner") or {})
    corner["corner_id"] = _corner_label(corner["process"], corner["temperature_c"])
    budget = _parse_topology_budget(request.get("budget"))
    targets = _parse_topology_targets(request.get("target"), topology)
    bias = _parse_topology_bias(request.get("bias"))

    models = request.get("models") or {}
    try:
        models_lib = _resolve_models_lib(models, request_dir)
    except SimError as exc:
        raise SizeError(str(exc)) from exc

    provenance_pdk: dict[str, Any] | None = None
    if models.get("pdk") or models.get("pdk_root"):
        try:
            provenance_pdk = find_pdk(
                variant=models.get("pdk"), root=models.get("pdk_root")
            )
        except PdkNotFoundError:
            provenance_pdk = None

    tolerance = request.get("tolerance") or {}
    gm_id_rel_tol = tolerance.get("gm_id_rel", DEFAULT_GM_ID_TOLERANCE)
    gm_id_rel_tol = _require_positive_number(
        gm_id_rel_tol, "request.tolerance.gm_id_rel"
    )

    options = request.get("options") or {}
    sweep_points = options.get("sweep_points", DEFAULT_SWEEP_POINTS)
    if (
        isinstance(sweep_points, bool)
        or not isinstance(sweep_points, int)
        or sweep_points < MIN_SWEEP_POINTS
    ):
        raise SizeError(
            f"request.options.sweep_points must be an integer >= {MIN_SWEEP_POINTS}"
        )
    max_joint_iterations = _parse_max_joint_iterations(options)

    timeout_s = timeout_s if timeout_s is not None else options.get("timeout_s")
    timeout_s = DEFAULT_TIMEOUT_S if timeout_s is None else timeout_s
    timeout_s = _require_positive_number(timeout_s, "request.options.timeout_s")

    keep_artifacts = bool(options.get("keep_artifacts", False))
    if artifacts_dir is None:
        artifacts_dir = os.path.join(request_dir, ".klt", "size")

    if keep_artifacts:
        work_dir = artifacts_dir
        os.makedirs(work_dir, exist_ok=True)
    else:
        work_dir = tempfile.mkdtemp(prefix="klt-size-topology-")

    resolved_vcm_v = bias["vcm_v"] if bias["vcm_v"] is not None else corner["vdd_v"] / 2
    bias_echo = {"vcm_v": resolved_vcm_v, "vout_v": None, "vtail_v": None}

    provenance = build_provenance(
        deck_name=os.path.basename(models_lib),
        deck_path=models_lib,
        pdk=provenance_pdk,
    )
    environment: dict[str, Any] = {
        "engine": "ngspice",
        "engine_version": None,
        # Issue #1274 -- see `run_size`'s own `environment` block.
        "models_lib": _report_path(models_lib, repo_root=repo_root),
    }

    def _finish(payload: dict[str, Any]) -> dict[str, Any]:
        if keep_artifacts:
            payload["environment"]["artifacts_dir"] = work_dir
        else:
            _cleanup_dir(work_dir)
        return payload

    # ---- Phase A: characterize each role's own gm/Id(W) curve ------------ #
    #
    # One diode-connected width sweep per role, at that role's nominal
    # current -- the same single ngspice invocation per sweep single-device
    # mode uses (see the "Two-invocation-per-request design" docstring
    # section). These curves are what the joint solve inverts to turn a
    # measured in-circuit gm/Id error into a width correction, without
    # paying an extra ngspice invocation per correction step.
    nominal_currents = _topology_nominal_currents(budget)
    curves: dict[str, list[dict[str, Any]]] = {}
    for role in TOPOLOGY_ROLES:
        device = devices[role]
        w_grid = _log_space(device["w_min_um"], device["w_max_um"], sweep_points)
        points, version, log_path, diagnostic = _run_sweep(
            device=device,
            corner=corner,
            target={"id_a": nominal_currents[role], "gm_id": targets[role]["gm_id"]},
            models_lib=models_lib,
            w_values=w_grid,
            work_dir=work_dir,
            deck_name=f"{role}_sweep.cir",
            log_name=f"{role}_sweep.log",
            timeout_s=timeout_s,
        )
        if version is not None:
            environment["engine_version"] = version
        valid = [
            p for p in points if p["gm_s"] is not None and p["id_a"] not in (None, 0.0)
        ]
        for point in valid:
            point["gm_id"] = abs(point["gm_s"]) / abs(point["id_a"])
        if len(valid) < 2:
            return _finish(
                _topology_error_payload(
                    topology=topology,
                    corner=corner,
                    budget=budget,
                    bias=bias_echo,
                    targets=targets,
                    devices=devices,
                    gm_id_rel_tol=gm_id_rel_tol,
                    environment=environment,
                    provenance=provenance,
                    reason=(
                        f"the '{role}' role's characterization sweep did not "
                        "return usable operating-point data for at least 2 of "
                        "the swept widths"
                        + (f": {diagnostic}" if diagnostic else "")
                        + (f" (see sweep log: {log_path})" if keep_artifacts else "")
                    ),
                )
            )
        curves[role] = sorted(valid, key=lambda p: p["w_um"])

    # ---- Phase B: joint solve on the assembled topology ------------------ #
    widths_um: dict[str, float] = {}
    feasible: dict[str, bool] = {}
    feasibility_notes: dict[str, str] = {}
    for role in TOPOLOGY_ROLES:
        w_um, role_feasible, note = _invert_curve(curves[role], targets[role]["gm_id"])
        widths_um[role] = w_um
        feasible[role] = role_feasible
        feasibility_notes[role] = note

    solve = _run_joint_solve(
        devices=devices,
        curves=curves,
        widths_um=widths_um,
        targets=targets,
        corner=corner,
        budget=budget,
        vcm_v=resolved_vcm_v,
        models_lib=models_lib,
        work_dir=work_dir,
        timeout_s=timeout_s,
        gm_id_rel_tol=gm_id_rel_tol,
        max_iterations=max_joint_iterations,
    )
    if solve["engine_version"] is not None:
        environment["engine_version"] = solve["engine_version"]

    if solve["final"] is None:
        return _finish(
            _topology_error_payload(
                topology=topology,
                corner=corner,
                budget=budget,
                bias=bias_echo,
                targets=targets,
                devices=devices,
                gm_id_rel_tol=gm_id_rel_tol,
                environment=environment,
                provenance=provenance,
                reason=solve["diagnostic"] or "the joint topology solve failed",
                joint_solve=solve["report"],
            )
        )

    final = solve["final"]
    widths_um = final["widths_um"]
    bias_echo = {
        "vcm_v": resolved_vcm_v,
        "vout_v": final["nodes"].get("out"),
        "vtail_v": final["nodes"].get("tail"),
    }

    device_results = _build_topology_device_results(
        devices=devices,
        widths_um=widths_um,
        targets=targets,
        budget=budget,
        points=final["points"],
        feasible=feasible,
        feasibility_notes=feasibility_notes,
        gm_id_rel_tol=gm_id_rel_tol,
        corner=corner,
        sweep_points=sweep_points,
        iterations_run=solve["report"]["iterations_run"],
    )

    measured = _measured_bias(final["points"], budget)
    control_status = [
        entry["status"]
        for entry in device_results.values()
        if entry["is_control"] and entry["status"] != "error"
    ]
    all_feasible = all(feasible.values())
    status = (
        "pass"
        if (
            all_feasible and control_status and all(s == "pass" for s in control_status)
        )
        else "fail"
    )

    return _finish(
        {
            "schema_version": SCHEMA_VERSION,
            "status": status,
            "topology": topology,
            "corner": _corner_public(corner),
            "budget": {**budget, "measured": measured},
            "bias": bias_echo,
            "roles": {
                role: {
                    "w_um": widths_um[role],
                    "l_um": devices[role]["l_um"],
                    "target_gm_id": targets[role]["gm_id"],
                    "control_instance": _ROLE_CONTROL_INSTANCE[role],
                    "feasible": feasible[role],
                }
                for role in TOPOLOGY_ROLES
            },
            "devices": device_results,
            "tolerance": {"gm_id_rel": gm_id_rel_tol},
            "joint_solve": solve["report"],
            "method": _topology_method(
                topology=topology,
                solve_report=solve["report"],
                widths_um=widths_um,
                feasible=feasible,
                feasibility_notes=feasibility_notes,
                sweep_points=sweep_points,
            ),
            "environment": environment,
            "provenance": provenance,
        }
    )


def _topology_nominal_currents(budget: dict[str, Any]) -> dict[str, float]:
    """Each role's nominal current from the shared budget -- the starting
    point for the characterization sweep, not a constraint on the solve
    (the joint solve reads back whatever current the assembled circuit
    actually settles at, see :func:`_run_joint_solve`)."""
    return {
        "tail": budget["tail_current_a"],
        "input_pair": budget["branch_current_a"],
        "mirror": budget["branch_current_a"],
    }


def _instance_nominal_current(key: str, budget: dict[str, Any]) -> float:
    if key == "tail_ref":
        return budget["reference_current_a"]
    if key == "tail":
        return budget["tail_current_a"]
    return budget["branch_current_a"]


def _run_joint_solve(
    *,
    devices: dict[str, dict[str, Any]],
    curves: dict[str, list[dict[str, Any]]],
    widths_um: dict[str, float],
    targets: dict[str, dict[str, Any]],
    corner: dict[str, Any],
    budget: dict[str, Any],
    vcm_v: float,
    models_lib: str,
    work_dir: str,
    timeout_s: float,
    gm_id_rel_tol: float,
    max_iterations: int,
) -> dict[str, Any]:
    """The coupled solve proper: iterate the three widths against the
    **assembled** ``diff_pair_mirror_tail`` circuit, one ngspice DC
    operating-point invocation per candidate point.

    Each iteration writes the whole topology at the current candidate
    ``(W_tail, W_input, W_mirror)``, runs ngspice, and reads back every
    instance's *in-circuit* ``gm``/``Id``/``Vgs``/``Vth``/``Vdsat`` at its
    real coupled ``Vds`` -- not a per-device diode-connected surrogate. A
    role whose control instance misses its gm/Id target is corrected by
    inverting that role's own already-swept ``gm_id(W)`` curve *shifted by
    the measured in-circuit offset*:

        offset_r   = gm_id_measured_r(W_k) - curve_r(W_k)
        W_{k+1, r} = curve_r^-1(target_r - offset_r)

    The offset absorbs everything the diode-connected sweep cannot model --
    the real ``Vds``, the real current the circuit settles at (which is not
    exactly the declared budget), and the body effect on the input pair's
    raised source node -- while the curve supplies the local slope, so this
    converges like a Newton step without needing a per-step derivative
    evaluation. All three roles are corrected simultaneously from the *same*
    coupled evaluation, which is what makes this one joint solve rather than
    three independent single-device solves: a width change in any role moves
    every other role's operating point, and the next iteration measures that
    directly.

    Returns ``{"final", "report", "engine_version", "diagnostic"}``. Never
    raises -- an evaluator failure yields ``final: None`` and a populated
    ``diagnostic``, the same convention every other ngspice-invoking helper
    in this module follows.
    """
    engine_version: str | None = None
    iterations: list[dict[str, Any]] = []
    final: dict[str, Any] | None = None
    diagnostic: str | None = None
    converged = False
    candidate = dict(widths_um)

    for index in range(max_iterations):
        deck_path = os.path.join(work_dir, f"joint_{index}.cir")
        log_path = os.path.join(work_dir, f"joint_{index}.log")
        _write_topology_deck(
            deck_path=deck_path,
            devices=devices,
            widths_um=candidate,
            corner=corner,
            budget=budget,
            vcm_v=vcm_v,
            models_lib=models_lib,
        )
        log_text, version, run_diagnostic = _invoke_ngspice(
            deck_path, log_path, timeout_s
        )
        if version is not None:
            engine_version = version
        points, nodes = _parse_topology_log(log_text)

        missing = [
            key
            for key, _instance, _role, _control in TOPOLOGY_INSTANCES
            if points.get(key) is None
            or points[key].get("gm_s") is None
            or not points[key].get("id_a")
        ]
        if missing:
            diagnostic = (
                "the joint topology operating-point run did not return usable "
                f"data for instance(s): {', '.join(missing)}"
                + (f": {run_diagnostic}" if run_diagnostic else "")
                + f" (see log: {log_path})"
            )
            iterations.append(
                {
                    "index": index,
                    "widths_um": dict(candidate),
                    "gm_id": None,
                    "gm_id_rel_error": None,
                    "worst_abs_rel_error": None,
                    "status": "error",
                }
            )
            break

        measured_gm_id = {}
        rel_error = {}
        for role in TOPOLOGY_ROLES:
            point = points[_ROLE_CONTROL_INSTANCE[role]]
            gm_id = abs(point["gm_s"]) / abs(point["id_a"])
            measured_gm_id[role] = gm_id
            rel_error[role] = (gm_id - targets[role]["gm_id"]) / targets[role]["gm_id"]
        worst = max(abs(value) for value in rel_error.values())

        iterations.append(
            {
                "index": index,
                "widths_um": dict(candidate),
                "gm_id": dict(measured_gm_id),
                "gm_id_rel_error": dict(rel_error),
                "worst_abs_rel_error": worst,
                "status": "ok",
            }
        )
        final = {"widths_um": dict(candidate), "points": points, "nodes": nodes}

        if worst <= gm_id_rel_tol:
            converged = True
            break
        if index == max_iterations - 1:
            break

        next_candidate: dict[str, float] = {}
        for role in TOPOLOGY_ROLES:
            device = devices[role]
            offset = measured_gm_id[role] - _interp_gm_id_at_width(
                curves[role], candidate[role]
            )
            shifted_target = targets[role]["gm_id"] - offset
            w_next, _feasible, _note = _invert_curve(curves[role], shifted_target)
            next_candidate[role] = min(
                max(w_next, device["w_min_um"]), device["w_max_um"]
            )
        if all(
            abs(math.log(next_candidate[role]) - math.log(candidate[role]))
            < _JOINT_WIDTH_STEP_EPS
            for role in TOPOLOGY_ROLES
        ):
            # The correction step has stopped moving (every role is pinned at
            # a width bound, or the curve simply cannot reach its shifted
            # target) -- iterating further only repeats the same ngspice run.
            break
        candidate = next_candidate

    report = {
        "method": (
            "coupled fixed-point on the assembled topology: one ngspice DC "
            "operating-point run per candidate (W_tail, W_input, W_mirror), "
            "each role corrected by inverting its own swept gm/Id(W) curve "
            "shifted by the measured in-circuit offset"
        ),
        "max_iterations": max_iterations,
        "iterations_run": len(iterations),
        "converged": converged,
        "gm_id_rel_tol": gm_id_rel_tol,
        "iterations": iterations,
    }
    return {
        "final": final,
        "report": report,
        "engine_version": engine_version,
        "diagnostic": diagnostic,
    }


def _measured_bias(points: dict[str, Any], budget: dict[str, Any]) -> dict[str, Any]:
    """The coupled quantities the assembled circuit actually settled at --
    the real tail current, how it split across the two input-pair legs, and
    the mirror's realized current ratio. These are *outputs* of the joint
    solve, not request inputs: a 50/50 split and a 1:1 mirror are what the
    topology is supposed to deliver, and reporting the measured values is
    how a caller checks that it did (issue #768's "returns a sized operating
    point for all devices jointly")."""
    tail_id = abs(points["tail"]["id_a"])
    input_a_id = abs(points["input_a"]["id_a"])
    input_b_id = abs(points["input_b"]["id_a"])
    mirror_a_id = abs(points["mirror_a"]["id_a"])
    mirror_b_id = abs(points["mirror_b"]["id_a"])
    branch_total = input_a_id + input_b_id
    return {
        "tail_current_a": tail_id,
        "reference_current_a": abs(points["tail_ref"]["id_a"]),
        "branch_current_a": {"input_a": input_a_id, "input_b": input_b_id},
        "tail_split": (
            [input_a_id / branch_total, input_b_id / branch_total]
            if branch_total
            else None
        ),
        "tail_mirror_ratio": (
            tail_id / abs(points["tail_ref"]["id_a"])
            if points["tail_ref"]["id_a"]
            else None
        ),
        "mirror_ratio": (mirror_b_id / mirror_a_id if mirror_a_id else None),
        "tail_current_rel_error": (
            (tail_id - budget["tail_current_a"]) / budget["tail_current_a"]
        ),
    }


def _build_topology_device_results(
    *,
    devices: dict[str, dict[str, Any]],
    widths_um: dict[str, float],
    targets: dict[str, dict[str, Any]],
    budget: dict[str, Any],
    points: dict[str, Any],
    feasible: dict[str, bool],
    feasibility_notes: dict[str, str],
    gm_id_rel_tol: float,
    corner: dict[str, Any],
    sweep_points: int,
    iterations_run: int,
) -> dict[str, Any]:
    """One fully-explained result per *instance* in the assembled topology
    (six of them, three solved widths) -- each carrying its own in-circuit
    operating point, gm/Id, inversion level, and the rationale sentence
    issue #768's third acceptance criterion requires ("every device's result
    states its gm/Id and inversion-level rationale")."""
    results: dict[str, Any] = {}
    for key, instance, role, is_control in TOPOLOGY_INSTANCES:
        device = devices[role]
        point = dict(points[key])
        point["w_um"] = widths_um[role]
        operating_point, gm_id, vov, vov_is_approx = _op_point_from_confirmed(
            point, device
        )
        target_gm_id = targets[role]["gm_id"]
        nominal_id = _instance_nominal_current(key, budget)
        gm_id_rel_error = (gm_id - target_gm_id) / target_gm_id
        id_rel_error = (abs(point["id_a"]) - nominal_id) / nominal_id
        status = "pass" if abs(gm_id_rel_error) <= gm_id_rel_tol else "fail"
        results[key] = {
            "role": role,
            "instance": instance,
            "is_control": is_control,
            "placement": _INSTANCE_DESCRIPTION[key],
            "device": device,
            "status": status,
            "target": {"gm_id": target_gm_id, "id_a": nominal_id},
            # Issue #770: hoisted rationale fields -- see the single-device
            # payload's own copy of this comment.
            "gm_id_target": target_gm_id,
            "gm_id_achieved": gm_id,
            "inversion_level": operating_point["inversion_level"],
            "operating_point": operating_point,
            "margins": {
                "gm_id_rel_error": gm_id_rel_error,
                "id_rel_error": id_rel_error,
            },
            "rationale": _instance_rationale(
                key=key,
                role=role,
                is_control=is_control,
                device=device,
                w_um=widths_um[role],
                target_gm_id=target_gm_id,
                gm_id=gm_id,
                operating_point=operating_point,
                vov=vov,
                vov_is_approx=vov_is_approx,
                nominal_id=nominal_id,
                feasible=feasible[role],
                feasibility_note=feasibility_notes[role],
                corner=corner,
                sweep_points=sweep_points,
                iterations_run=iterations_run,
            ),
        }
    return results


def _instance_rationale(
    *,
    key: str,
    role: str,
    is_control: bool,
    device: dict[str, Any],
    w_um: float,
    target_gm_id: float,
    gm_id: float,
    operating_point: dict[str, Any],
    vov: float | None,
    vov_is_approx: bool,
    nominal_id: float,
    feasible: bool,
    feasibility_note: str,
    corner: dict[str, Any],
    sweep_points: int,
    iterations_run: int,
) -> str:
    """The per-device "why this size, why this inversion level" sentence.

    Deliberately states the *measured in-circuit* gm/Id (not the
    diode-connected surrogate the characterization sweep used) and the
    inversion level's own numeric basis, so a caller can audit the sizing
    decision without re-running the tool.
    """
    parts = [
        f"[{key}] {role} role, {_INSTANCE_DESCRIPTION[key]}: {device['kind']} "
        f"'{device['model']}' sized to W={w_um:.4g}um at L={device['l_um']}um "
        f"(nf={device['nf']}, mult={device['mult']})."
    ]
    if is_control:
        parts.append(
            f"This instance is the '{role}' role's control device: its "
            "in-circuit gm/Id is what the joint solve drove to the target by "
            f"correcting W ({iterations_run} coupled ngspice operating-point "
            f"iteration(s), seeded from a {sweep_points}-point diode-connected "
            f"gm/Id(W) sweep at the '{corner['corner_id']}' corner)."
        )
    else:
        parts.append(
            f"This instance shares the '{role}' role's solved width with its "
            "matched partner rather than adding a separate degree of freedom, "
            "so its operating point is reported as measured, not targeted "
            "independently."
        )
    parts.append(
        f"Measured in-circuit gm/Id={gm_id:.4g} S/A against the target "
        f"{target_gm_id:.4g} S/A "
        f"({(gm_id - target_gm_id) / target_gm_id:+.2%}), at "
        f"Id={abs(operating_point['id_a']):.4g}A "
        f"(nominal {nominal_id:.4g}A from the shared tail-current budget)."
    )
    if not feasible:
        parts.append(
            f"The '{role}' role's target gm/Id={target_gm_id:.4g} is not "
            f"reachable within [{device['w_min_um']}, {device['w_max_um']}]um "
            f"at this current -- {feasibility_note}; the closest achievable "
            "width was used instead of extrapolating."
        )
    inversion_level = operating_point["inversion_level"]
    if vov is not None and not vov_is_approx:
        parts.append(
            f"Inversion level '{inversion_level}' from Vov=Vgs-Vth={vov:.4g}V "
            "(Vov<=0: weak; 0<Vov<0.1V: moderate; Vov>=0.1V: strong -- a "
            "standard rule-of-thumb threshold, not a precise "
            "inversion-coefficient computation)."
        )
    elif vov is not None and vov_is_approx:
        parts.append(
            f"Inversion level '{inversion_level}' from Vov~=Vdsat={vov:.4g}V "
            "(Vth not exposed by this device model; Vdsat approximates Vov "
            "for a square-law device, same thresholds as above)."
        )
    else:
        parts.append(
            "Inversion level could not be classified: the device model did "
            "not expose Vth or Vdsat at this operating point."
        )
    return " ".join(parts)


def _topology_method(
    *,
    topology: str,
    solve_report: dict[str, Any],
    widths_um: dict[str, float],
    feasible: dict[str, bool],
    feasibility_notes: dict[str, str],
    sweep_points: int,
) -> dict[str, Any]:
    sized = ", ".join(f"{role}={widths_um[role]:.4g}um" for role in TOPOLOGY_ROLES)
    rationale = (
        f"Coupled '{topology}' sizing: the three widths ({sized}) were solved "
        "jointly against the assembled circuit, not device by device. Each "
        f"role's gm/Id(W) curve was first characterized by a {sweep_points}-"
        "point diode-connected width sweep at its nominal current (one "
        "ngspice invocation per role) to seed the search and supply the local "
        "slope; the solve itself then ran "
        f"{solve_report['iterations_run']} ngspice DC operating-point "
        "evaluation(s) of the *whole* topology, each one reading back every "
        "device's real in-circuit gm/Id at its actual coupled Vds and "
        "correcting all three widths simultaneously from that single coupled "
        "measurement."
    )
    if solve_report["converged"]:
        rationale += (
            " Every role's control device landed within the requested gm/Id "
            f"tolerance ({solve_report['gm_id_rel_tol']:.3g} relative)."
        )
    else:
        rationale += (
            " The joint iteration stopped before every role's control device "
            "was inside the requested gm/Id tolerance -- see "
            "joint_solve.iterations for the per-iteration trajectory."
        )
    for role in TOPOLOGY_ROLES:
        if not feasible[role]:
            rationale += (
                f" The '{role}' role's target is out of reach within its "
                f"declared width bounds: {feasibility_notes[role]}."
            )
    return {
        "name": "coupled multi-device joint solve (diff-pair + mirror + tail)",
        "bias": (
            "assembled topology at DC: diode-connected tail-bias replica, "
            "input pair at the declared common-mode voltage, output held at "
            "the common-mode point by a DC-only feedback network"
        ),
        "rationale": rationale,
        "sweep_points": sweep_points,
        "joint_iterations": solve_report["iterations_run"],
        "converged": solve_report["converged"],
        "feasible": all(feasible.values()),
    }


def _topology_error_payload(
    *,
    topology: str,
    corner: dict[str, Any],
    budget: dict[str, Any],
    bias: dict[str, Any],
    targets: dict[str, dict[str, Any]],
    devices: dict[str, dict[str, Any]],
    gm_id_rel_tol: float,
    environment: dict[str, Any],
    provenance: dict[str, Any],
    reason: str,
    joint_solve: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """``status: "error"`` for topology mode -- the evaluator itself could
    not produce a trustworthy result, so there are no device results to
    report. ``method`` is still populated (this module's "no stated method
    is rejected" precedent applies to every status), as are the request
    echoes, so a caller can tell *what* was attempted."""
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "error",
        "topology": topology,
        "corner": _corner_public(corner),
        "budget": {**budget, "measured": None},
        "bias": bias,
        "roles": {
            role: {
                "w_um": None,
                "l_um": devices[role]["l_um"],
                "target_gm_id": targets[role]["gm_id"],
                "control_instance": _ROLE_CONTROL_INSTANCE[role],
                "feasible": None,
            }
            for role in TOPOLOGY_ROLES
        },
        "devices": None,
        "tolerance": {"gm_id_rel": gm_id_rel_tol},
        "joint_solve": joint_solve,
        "method": {
            "name": "coupled multi-device joint solve (diff-pair + mirror + tail)",
            "bias": (
                "assembled topology at DC: diode-connected tail-bias replica, "
                "input pair at the declared common-mode voltage, output held "
                "at the common-mode point by a DC-only feedback network"
            ),
            "rationale": f"Evaluator error: {reason}",
            "sweep_points": None,
            "joint_iterations": (
                joint_solve["iterations_run"] if joint_solve is not None else 0
            ),
            "converged": False,
            "feasible": None,
        },
        "environment": environment,
        "provenance": provenance,
    }


def _write_topology_deck(
    *,
    deck_path: str,
    devices: dict[str, dict[str, Any]],
    widths_um: dict[str, float],
    corner: dict[str, Any],
    budget: dict[str, Any],
    vcm_v: float,
    models_lib: str,
) -> None:
    """Generate one candidate point's ``diff_pair_mirror_tail`` deck: the
    *actual* coupled circuit (never a per-device diode-connected surrogate)
    at the candidate widths, instrumented to read back all six instances'
    operating points plus the output/tail node voltages.

    Topology, mirroring this repo's own hand-sized 5T OTA canary
    (``examples/design-pipeline/ota_5t.spice``) generalized to the request's
    declared models and widths:

    - ``Iref`` injects ``tail_current_a / tail_mirror_ratio`` into the
      diode-connected replica ``Xtailref``, whose gate drives ``Xtail``; the
      ratio is realized as ``Xtail``'s ``mult`` multiplier, so the tail
      device sources ``tail_current_a``.
    - The input pair's sources tie to ``Xtail``'s drain (``tail``), gates to
      the common-mode bias.
    - The PMOS mirror load's diode leg (``Xmirrora``) sits on the ``n1``
      branch and its output leg (``Xmirrorb``) on ``out``.
    - ``Lfb``/``Cfb`` form the canary's own DC-only feedback network: at DC
      the inductor is a short, so the output is held at the common-mode
      operating point instead of floating to a fragile, corner-dependent
      balance point (the exact failure the canary's own
      ``05-sizing.json`` records from its first characterization pass).
      The capacitor opens the loop at AC, keeping the deck reusable for a
      later AC pass.
    """
    tail = devices["tail"]
    input_pair = devices["input_pair"]
    mirror = devices["mirror"]
    vdd = corner["vdd_v"]

    def _x_params(device: dict[str, Any], w_um: float, mult_scale: float = 1.0) -> str:
        return (
            f"l={device['l_um']!r} w={w_um!r} nf={device['nf']!r} "
            f"mult={device['mult'] * mult_scale!r}"
        )

    lines = [
        "* klt size -- generated coupled topology deck "
        "(diff_pair_mirror_tail), do not edit"
    ]
    process_sections = corner.get("process_sections")
    if process_sections:
        for section in process_sections:
            lines.append(f".lib {models_lib} {section}")
    else:
        lines.append(f".lib {models_lib} {corner['process']}")
    lines.append(f"Vdd vdd 0 DC {vdd!r}")
    lines.append(f"Vcm cm 0 DC {vcm_v!r}")
    lines.append("Vin inp cm DC 0")
    lines.append(f"Iref vdd nbias DC {budget['reference_current_a']!r}")
    # DC-only feedback: short at DC (holds out at the common-mode point),
    # open at AC. See this function's docstring.
    lines.append("Lfb out inn 1e9")
    lines.append("Cfb inn cm 1e3")

    lines.append(
        f"Xtailref nbias nbias 0 0 {tail['model']} {_x_params(tail, widths_um['tail'])}"
    )
    lines.append(
        f"Xtail tail nbias 0 0 {tail['model']} "
        f"{_x_params(tail, widths_um['tail'], budget['tail_mirror_ratio'])}"
    )

    input_params = _x_params(input_pair, widths_um["input_pair"])
    lines.append(f"Xinputa n1 inp tail 0 {input_pair['model']} {input_params}")
    lines.append(f"Xinputb out inn tail 0 {input_pair['model']} {input_params}")

    mirror_params = _x_params(mirror, widths_um["mirror"])
    lines.append(f"Xmirrora n1 n1 vdd vdd {mirror['model']} {mirror_params}")
    lines.append(f"Xmirrorb out n1 vdd vdd {mirror['model']} {mirror_params}")

    # A top-level dot-card, like `_write_sweep_deck`'s own `.temp` handling.
    lines.append(f".temp {corner['temperature_c']!r}")

    lines.append(".control")
    lines.append("op")
    for key, instance, role, _is_control in TOPOLOGY_INSTANCES:
        op_element = devices[role]["op_point_element"]
        lines.append(f"echo KLT_SIZE_INSTANCE {key}")
        for param in _OP_PARAMS:
            lines.append(f"print @m.{instance}.{op_element}[{param}]")
    lines.append("echo KLT_SIZE_INSTANCE __nodes__")
    for node in ("out", "tail", "n1", "nbias"):
        lines.append(f"print v({node})")
    lines.append("quit")
    lines.append(".endc")
    lines.append(".end")

    with open(deck_path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def _parse_topology_log(
    log_text: str,
) -> tuple[dict[str, Any], dict[str, float]]:
    """Parse one joint-topology deck's log into ``(points, nodes)``:
    per-instance op-point dicts keyed by :data:`TOPOLOGY_INSTANCES` key,
    plus the node voltages printed after the ``__nodes__`` marker. Mirrors
    :func:`_parse_sweep_log`'s marker-then-values grouping (there keyed by
    sweep-point index instead of instance key).
    """
    points: dict[str, Any] = {}
    nodes: dict[str, float] = {}
    current_key: str | None = None
    current_values: dict[str, float] = {}

    def _flush() -> None:
        if current_key is None or current_key == "__nodes__":
            return
        entry: dict[str, Any] = {}
        for param in _OP_PARAMS:
            entry[_OP_PARAM_KEYS[param]] = current_values.get(param)
        points[current_key] = entry

    for raw_line in log_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        marker = _INSTANCE_MARKER_RE.match(line)
        if marker:
            _flush()
            current_key = marker.group(1)
            current_values = {}
            continue
        if current_key is None:
            continue
        node_match = _NODE_VALUE_RE.match(line)
        if node_match:
            try:
                nodes[node_match.group(1)] = float(node_match.group(2))
            except ValueError:
                pass
            continue
        value_match = _OP_VALUE_RE.match(line)
        if value_match:
            key, raw_value = value_match.groups()
            try:
                current_values[key] = float(raw_value)
            except ValueError:
                continue
    _flush()

    for key, _instance, _role, _control in TOPOLOGY_INSTANCES:
        points.setdefault(key, None)
    return points, nodes
