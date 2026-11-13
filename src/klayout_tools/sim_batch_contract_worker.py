"""Batch contract: worker capture and runner-support assessment.

Split from :mod:`klayout_tools.sim_batch_contract` (issue #4065), moved verbatim.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

from .remote_launcher import (
    RemoteLaunchError,
    published_runner_identities,
)
from .sim_batch_contract import (
    _BATCH_PREFLIGHT_REJECT_EXIT,
    _DECKS_BASE_RELEASE,
    _DECKS_FIRST_DEV,
    _PAR_BACKEND_FIRST_DEV,
    BATCH_REPORT_FILENAME,
    BATCH_RUNNER_LACKS_DECKS_CODE,
    BATCH_RUNNER_LACKS_PLACE_AND_ROUTE_BACKEND_CODE,
)
from .sim_batch_contract_spec import (
    _release_segment,
)

if TYPE_CHECKING:
    from .sim_batch import BatchConfig


BATCH_WORKER_ENVIRONMENT_FILENAME = "worker_environment.json"

#: Bounds shared by the worker-side capture and the client-side re-validation.
WORKER_CAPTURE_BANNER_MAX = 2000
WORKER_CAPTURE_STR_MAX = 300
WORKER_CAPTURE_FILE_MAX = 65536
WORKER_CAPTURE_PROBE_TIMEOUT_S = 10
#: Most staged model files the worker capture digests / the client re-reads.
WORKER_CAPTURE_MODEL_INPUTS_MAX = 64

#: The worker-side capture program. It is passed to ``python3 -I -c`` as one
#: shell-quoted argument (never interpolated with untrusted text); request
#: values arrive as ``sys.argv`` and everything it observes is serialised with
#: :mod:`json`. Stdlib only, every probe individually guarded and bounded, and
#: it always exits 0 -- a failed capture is a ``diagnostics`` entry, never a
#: job failure. Only allowlisted environment variables are read.
_WORKER_CAPTURE_PROGRAM = r"""
import hashlib, json, os, shutil, sys
import subprocess as sp
BAN, STR, T = @BAN@, @STR@, @TIMEOUT@
out, req_bin, variant, staged = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
model_names = sys.argv[5:5 + @MODELS@]
diag = []
def clip(v, n=STR):
    if not isinstance(v, str):
        return None
    v = "".join(c if (c >= " " or c in "\n\t") else "?" for c in v)
    return v[:n]
def guard(label, fn, default=None):
    try:
        return fn()
    except BaseException as exc:
        diag.append(label + ": " + clip(type(exc).__name__ + " " + str(exc), 160))
        return default
def read(path, n):
    with open(path, "rb") as h:
        return h.read(n + 1)[:n].decode("utf-8", "replace")
doc = {"schema": 1}
def sim():
    sel = req_bin or os.environ.get("KLT_NGSPICE_BINARY") or "ngspice"
    src = "request" if req_bin else ("KLT_NGSPICE_BINARY" if os.environ.get("KLT_NGSPICE_BINARY") else "PATH")
    path = shutil.which(sel)
    s = {"requested_binary": clip(sel), "binary_source": src,
         "selected_path": clip(path) if path else None, "version_banner": None,
         "version_probe": "unavailable"}
    if path:
        try:
            r = sp.run([path, "--version"], stdin=sp.DEVNULL,
                               stdout=sp.PIPE, stderr=sp.STDOUT,
                               timeout=T)
            s["version_banner"] = clip(r.stdout[:BAN * 4].decode("utf-8", "replace"), BAN)
            s["version_probe"] = "ok" if r.returncode == 0 else "exit_%d" % r.returncode
        except sp.TimeoutExpired:
            s["version_probe"] = "timeout"
        except BaseException as exc:
            s["version_probe"] = "error"
            diag.append("simulator probe: " + clip(type(exc).__name__, 80))
    else:
        diag.append("simulator binary not found on worker")
    return s
doc["simulator"] = guard("simulator", sim, {"selected_path": None, "version_banner": None})
def store_version(vdir):
    try:
        d = json.loads(read(os.path.join(os.path.dirname(os.path.realpath(vdir)), ".pdk-store.json"), 65536))
    except (OSError, ValueError):
        return None
    v = d.get("version") if isinstance(d, dict) else None
    return v.strip() if isinstance(v, str) and v.strip() else None
def pdk():
    root = os.environ.get("PDK_ROOT")
    p = {"variant": clip(variant) or None, "root": clip(root), "stamp_scheme": None,
         "stamp_sha256": None, "stamp_text": None, "models_staged": staged == "1"}
    if not (root and variant):
        if staged != "1":
            diag.append("pdk root or variant unknown on worker")
        return p
    stamp = os.path.join(root, variant, "SOURCES")
    try:
        text = read(stamp, 16384)
    except OSError as exc:
        diag.append("pdk stamp unreadable: " + clip(type(exc).__name__, 80))
        sv = store_version(os.path.join(root, variant))
        if sv:
            p["stamp_scheme"] = "store-version-sha256"
            p["stamp_sha256"] = hashlib.sha256(sv.encode("utf-8")).hexdigest()
            p["stamp_text"] = clip("store:" + sv, 1000)
        return p
    norm = "\n".join(l.rstrip() for l in text.replace("\r\n", "\n").strip().split("\n"))
    p["stamp_scheme"] = "sources-sha256"
    p["stamp_sha256"] = hashlib.sha256(norm.encode("utf-8")).hexdigest()
    p["stamp_text"] = clip(norm, 1000)
    return p
doc["pdk"] = guard("pdk", pdk, {"variant": clip(variant) or None})
def cpu():
    for line in read("/proc/cpuinfo", 1 << 18).split("\n"):
        if line.lower().startswith("model name") and ":" in line:
            return clip(line.split(":", 1)[1].strip())
    diag.append("cpu model not reported")
doc["cpu_model"] = guard("cpu", cpu)
doc["omp_num_threads"] = clip(os.environ.get("OMP_NUM_THREADS"), 40)
def manifest():
    path = os.environ.get("KLT_IMAGE_MANIFEST")
    for cand in ([path] if path else []) + ["/etc/klt-image-manifest.json", "/opt/klt/image-manifest.json"]:
        if not os.path.isfile(cand):
            continue
        data = json.loads(read(cand, 65536))
        if not isinstance(data, dict):
            raise ValueError("manifest is not an object")
        keep = ("pdk", "pdk_snapshot", "pdk_ref", "pdk_pinned", "ngspice_version",
                "klt_ref", "built_at", "image_id")
        m = {k: (data[k] if isinstance(data[k], (bool, int)) else clip(data[k]))
             for k in keep if k in data and isinstance(data[k], (str, bool, int))}
        m["path"] = clip(cand)
        return m
    return None
doc["image_manifest"] = guard("image manifest", manifest)
def model_inputs():
    if staged != "1":
        return None
    base = os.environ.get("EDA_INPUT_DIR") or "."
    found = []
    for name in model_names:
        h = hashlib.sha256()
        with open(os.path.join(base, name), "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        found.append({"name": clip(name), "sha256": h.hexdigest()})
    return found
doc["model_inputs"] = guard("model inputs", model_inputs)
doc["diagnostics"] = diag[:20]
body = json.dumps(doc, ensure_ascii=True)
tmp = out + ".tmp"
with open(tmp, "w") as h:
    h.write(body)
os.replace(tmp, out)
"""  # noqa: E501
_WORKER_CAPTURE_PROGRAM = (
    _WORKER_CAPTURE_PROGRAM.replace("@BAN@", str(WORKER_CAPTURE_BANNER_MAX))
    .replace("@STR@", str(WORKER_CAPTURE_STR_MAX))
    .replace("@TIMEOUT@", str(WORKER_CAPTURE_PROBE_TIMEOUT_S))
    .replace("@MODELS@", str(WORKER_CAPTURE_MODEL_INPUTS_MAX))
)


def _worker_capture_command(
    remote_request: dict[str, Any], model_names: Sequence[str] = ()
) -> str:
    """The best-effort worker capture invocation (issue #2834), run from the
    runner preflight. Bounded by ``timeout`` when present, stderr discarded,
    ``|| true`` so it can never fail or alter the job."""
    options = remote_request.get("options") or {}
    models = remote_request.get("models") or {}
    binary = options.get("ngspice_binary")
    variant = models.get("pdk")
    staged = "1" if options.get("stage_model_inputs") else "0"
    args = [
        shlex.quote(str(binary) if isinstance(binary, str) else ""),
        shlex.quote(str(variant) if isinstance(variant, str) else ""),
        staged,
        *(shlex.quote(name) for name in model_names[:WORKER_CAPTURE_MODEL_INPUTS_MAX]),
    ]
    target = f'"$EDA_OUTPUT_DIR/{BATCH_WORKER_ENVIRONMENT_FILENAME}"'
    return (
        'wt=; command -v timeout >/dev/null 2>&1 && wt="timeout 30"; '
        "{ command -v python3 >/dev/null 2>&1 && "
        f"$wt python3 -I -c {shlex.quote(_WORKER_CAPTURE_PROGRAM)} "
        f"{target} {' '.join(args)} 2>/dev/null; true; }}"
    )


def _runner_supports_decks(version: str) -> bool | None:
    """Whether a runner ``klt --version`` build string knows ``decks[]``
    (issue #3303): ``None`` when it cannot be established.

    Unlike the major.minor gate (:func:`_runner_gate_release`) this reads the
    full release segment and the ``.devN`` distance, because the capability
    arrived mid-``0.7`` line: after ``0.7.1`` is released, or on a
    ``0.7.1.dev<N>`` build with ``N`` at/after :data:`_DECKS_FIRST_DEV`, or in
    any later minor. :func:`_deck_preflight_lines` repeats this in shell.
    """
    return _runner_supports_since(version, _DECKS_FIRST_DEV)


def _runner_supports_par_backend(version: str) -> bool | None:
    """Whether a runner ``klt --version`` build string accepts ``--backend``
    on ``place-and-route`` (issue #4124); same rule as
    :func:`_runner_supports_decks` with :data:`_PAR_BACKEND_FIRST_DEV`."""
    return _runner_supports_since(version, _PAR_BACKEND_FIRST_DEV)


def _runner_supports_since(version: str, first_dev: int) -> bool | None:
    """A capability that arrived at ``0.7.1.dev<first_dev>``: ``None`` when
    ``version`` cannot be read."""
    release = _release_segment(version)
    if release is None:
        return None
    parts = [int(part) for part in release.split(".")]
    if len(parts) < 2:
        return None
    parts += [0] * (3 - len(parts))
    if tuple(parts[:2]) != _DECKS_BASE_RELEASE:
        return tuple(parts[:2]) > _DECKS_BASE_RELEASE
    if parts[2] >= 2:
        return True
    if parts[2] == 0:
        return False
    dev = re.search(r"\.dev(\d+)", version.split("+", 1)[0])
    return dev is None or int(dev.group(1)) >= first_dev


def _deck_preflight_lines() -> str:
    """Shell appended to the runner preflight of a deck-list job (issue
    #3303; needs ``$rv`` from :func:`_runner_preflight_script`). Unlike the
    version gate this is not relaxed by ``runner_version_check: warn``: a
    runner that cannot read ``decks[]`` fails the job for certain, so the
    job exits :data:`_BATCH_PREFLIGHT_REJECT_EXIT` with a klt error envelope
    naming :data:`BATCH_RUNNER_LACKS_DECKS_CODE` before ``klt sim`` runs."""
    return _capability_preflight_lines(
        command="sim",
        code=BATCH_RUNNER_LACKS_DECKS_CODE,
        first_dev=_DECKS_FIRST_DEV,
        lacks="deck-list (decks[]) requests",
        remedy="rebuild the runner image from a klt that includes decks[]",
    )


def par_backend_preflight_lines() -> str:
    """:func:`_deck_preflight_lines` for a ``place-and-route`` batch job
    (issue #4124): a runner whose ``klt place-and-route`` predates
    ``--backend`` exits :data:`_BATCH_PREFLIGHT_REJECT_EXIT` naming
    :data:`BATCH_RUNNER_LACKS_PLACE_AND_ROUTE_BACKEND_CODE`."""
    return _capability_preflight_lines(
        command="place-and-route",
        code=BATCH_RUNNER_LACKS_PLACE_AND_ROUTE_BACKEND_CODE,
        first_dev=_PAR_BACKEND_FIRST_DEV,
        lacks="--backend on place-and-route",
        remedy="rebuild the runner image from a klt that includes "
        "place-and-route --backend",
    )


def _capability_preflight_lines(
    *, command: str, code: str, first_dev: int, lacks: str, remedy: str
) -> str:
    """The shell behind :func:`_deck_preflight_lines` and
    :func:`par_backend_preflight_lines`: reject a runner older than
    ``0.7.1.dev<first_dev>``. ``lacks`` and ``remedy`` are embedded in a
    double-quoted shell string, so they must not hold ``"``, ``$`` or a
    backtick."""
    report_path = f'"$EDA_OUTPUT_DIR/{BATCH_REPORT_FILENAME}"'
    base_major, base_minor = _DECKS_BASE_RELEASE
    return (
        "dk=; rb=${rv%%+*}; dn=; "
        'case "$rb" in *.dev[0-9]*) dn=${rb##*.dev}; dn=${dn%%[!0-9]*}; '
        "rb=${rb%%.dev*} ;; esac; "
        "rb=${rb%%[!0-9.]*}; rb=${rb%.}; "
        'case "$rb" in ""|.*|*..*) ;; *.*) '
        "dma=${rb%%.*}; drest=${rb#*.}; dmi=${drest%%.*}; dpa=0; "
        'case "$drest" in *.*) dpa=${drest#*.}; dpa=${dpa%%.*} ;; esac; '
        f'if [ "$dma" -gt {base_major} ] || '
        f'{{ [ "$dma" -eq {base_major} ] && [ "$dmi" -gt {base_minor} ]; }}; '
        "then dk=yes; "
        f'elif [ "$dma" -eq {base_major} ] && [ "$dmi" -eq {base_minor} ]; then '
        'if [ "$dpa" -ge 2 ]; then dk=yes; '
        'elif [ "$dpa" -eq 1 ] && { [ -z "$dn" ] || '
        f'[ "$dn" -ge {first_dev} ]; }}; then dk=yes; fi; fi ;; esac; '
        'if [ -z "$dk" ]; then '
        'dmsg="the fleet runner runs klt ${rv:-<unknown>}, which does not know '
        f'{lacks} -- the request was not run ({remedy})"; '
        'printf \'{"schema_version": 1, "error": {"command": "' + command + '", '
        f'"code": "{code}", "message": "%s"}}}}\\n\' '
        '"$dmsg" >' + report_path + "; "
        'echo "$dmsg" >&2; '
        "exit " + str(_BATCH_PREFLIGHT_REJECT_EXIT) + "; fi; "
    )


def _assess_deck_support(request: dict[str, Any], config: BatchConfig) -> None:
    """Refuse a deck-list batch submit before any S3 write or launch when
    every image the AMI manifest could boot is known to predate ``decks[]``
    (issue #3303). Same candidate selection as
    :func:`_assess_published_runner`; anything less certain (no manifest,
    a legacy entry without ``klt_version``, a mix) proceeds to the
    runner-side probe in :func:`_deck_preflight_lines`. Raises regardless of
    ``runner_version_check``: the job could not run on any such image."""
    _assess_capability_support(
        request,
        config,
        supports=_runner_supports_decks,
        code=BATCH_RUNNER_LACKS_DECKS_CODE,
        predates="deck-list (decks[]) support",
        remedy="rebuild the runner image from a klt that includes decks[]; "
        "see docs/cli/sim.md",
    )


def assess_par_backend_support(request: dict[str, Any], config: BatchConfig) -> None:
    """:func:`_assess_deck_support` for ``place-and-route --backend batch``
    (issue #4124); ``request`` only needs ``models.pdk``."""
    _assess_capability_support(
        request,
        config,
        supports=_runner_supports_par_backend,
        code=BATCH_RUNNER_LACKS_PLACE_AND_ROUTE_BACKEND_CODE,
        predates="place-and-route --backend (needs klt "
        f"0.7.1.dev{_PAR_BACKEND_FIRST_DEV} or later)",
        remedy="rebuild the runner image from a klt that includes "
        "place-and-route --backend; see docs/cli/place-and-route.md",
    )


def _assess_capability_support(
    request: dict[str, Any],
    config: BatchConfig,
    *,
    supports: Callable[[str], bool | None],
    code: str,
    predates: str,
    remedy: str,
) -> None:
    from .sim import SimError

    pdk = (request.get("models") or {}).get("pdk")
    try:
        images = published_runner_identities(str(pdk) if pdk else None, config.region)
    except (RemoteLaunchError, OSError, TypeError, ValueError, AttributeError):
        return
    if not images:
        return
    support = [
        supports(image["klt_version"]) if image["klt_version"] else None
        for image in images
    ]
    if any(item is not False for item in support):
        return
    published = "; ".join(
        f"{image['ami_id']} (pdk {image['pdk']}, region {image['region']}) "
        f"runs klt {image['klt_version']}"
        + (f" from ref {image['klt_ref']}" if image["klt_ref"] else "")
        for image in images
    )
    raise SimError(
        f"backend 'batch' refused before submission: the AMI manifest "
        f"publishes the fleet runner image as {published}, which predates "
        f"{predates} -- the request was not submitted and no "
        f"instance was launched ({remedy})",
        code=code,
    )
