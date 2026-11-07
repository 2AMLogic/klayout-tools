#!/usr/bin/env python3
"""Tiny Tapeout corpus index walker and pinned-SHA fetch cache (Epic #520
Phase 1.A, issue #5081).

Walks a Tiny Tapeout shuttle index (``https://index.tinytapeout.com/<shuttle>.json``),
filters by PDK, and fetches each project's source archive (and GDS, when the
index or a URL template names one) at its *pinned commit* into a local
content-addressed cache, writing one provenance manifest per design.

Guardrails (Epic #520):

* every artifact is fetched at most once -- a second run makes zero network
  requests for cached items (the URL -> sha256 map lives in the cache);
* an honest, identifying ``User-Agent`` and a configurable pacing floor
  between requests;
* a design whose license cannot be read from its own repo is flagged
  ``excluded`` in its manifest and never silently included;
* the index carries no ``pdk`` field (verified against the live tt06 and
  ttgf0p2 indexes), so the PDK is derived from the shuttle id -- see
  :func:`shuttle_pdk`; a ``--pdk`` filter that matches nothing is an error,
  never a silent empty success;
* all network access goes through one injected ``transport`` callable, so the
  tests (``tests/test_tt_corpus_fetch.py``) run offline against a captured
  slice of the real index.

Cache layout (``--cache-dir``)::

    blobs/<sha256>                      content-addressed artifact bytes
    urls.json                           {url: sha256} -- the "already fetched" map
    manifests/<shuttle>/<macro>.json    per-design provenance manifest

Usage::

    scripts/tt_corpus_fetch.py --shuttle tt06 --pdk sky130A --cache-dir .cache/tt-corpus
    scripts/tt_corpus_fetch.py --index-file captured.json --shuttle tt06 --limit 4

Output is one JSON document on stdout (summary + per-design status).
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys
import tarfile
import time
import urllib.request
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

USER_AGENT = "klayout-tools-tt-corpus/1 (+https://github.com/2AMLogic/klayout-tools)"
INDEX_URL = "https://index.tinytapeout.com/{shuttle}.json"
ARCHIVE_URL = "https://github.com/{owner}/{repo}/archive/{commit}.tar.gz"
DEFAULT_MIN_INTERVAL_S = 1.0
SCHEMA_VERSION = "1"

# transport(url) -> bytes; raises on failure.
Transport = Callable[[str], bytes]

# Order matters: the more specific markers are tested first.
_LICENSE_MARKERS: tuple[tuple[str, str], ...] = (
    ("Apache-2.0", r"apache license[\s,]*version 2\.0"),
    ("AGPL-3.0", r"gnu affero general public license"),
    ("LGPL-3.0", r"gnu lesser general public license"),
    ("GPL-3.0", r"gnu general public license"),
    ("MPL-2.0", r"mozilla public license[\s,]*(version |v)?2\.0"),
    ("MIT", r"permission is hereby granted, free of charge"),
    ("BSD-3-Clause", r"neither the name of .{0,200}nor the names of its contributors"),
    ("BSD-2-Clause", r"redistributions of source code must retain"),
    ("ISC", r"permission to use, copy, modify, and/or distribute this software"),
    (
        "Unlicense",
        r"this is free and unencumbered software released into the public domain",
    ),
    ("CC0-1.0", r"cc0 1\.0 universal"),
)
_LICENSE_FILE = re.compile(
    r"^(license|licence|copying|unlicense)([._-].*)?$", re.IGNORECASE
)


class PdkFilterError(ValueError):
    """``--pdk`` filtered out every project (likely a schema/PDK mismatch)."""


class FetchError(RuntimeError):
    """A transport failure for one URL (recorded per design, not fatal)."""


# --------------------------------------------------------------------------
# Transport with pacing
# --------------------------------------------------------------------------


class PacedHttpTransport:
    """``urllib`` transport with an honest User-Agent and a pacing floor."""

    def __init__(
        self,
        min_interval_s: float = DEFAULT_MIN_INTERVAL_S,
        user_agent: str = USER_AGENT,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        timeout_s: float = 60.0,
    ) -> None:
        self.min_interval_s = max(0.0, float(min_interval_s))
        self.user_agent = user_agent
        self._clock = clock
        self._sleep = sleep
        self._timeout_s = timeout_s
        self._last: float | None = None

    def __call__(self, url: str) -> bytes:
        if self._last is not None:
            wait = self.min_interval_s - (self._clock() - self._last)
            if wait > 0:
                self._sleep(wait)
        self._last = self._clock()
        if not url.startswith("https://"):
            raise FetchError(f"{url}: only https URLs are fetched")
        request = urllib.request.Request(  # noqa: S310
            url, headers={"User-Agent": self.user_agent}
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_s) as response:  # noqa: S310
                return response.read()
        except OSError as exc:  # URLError/HTTPError/timeouts are OSError subclasses
            raise FetchError(f"{url}: {exc}") from exc


# --------------------------------------------------------------------------
# Content-addressed cache
# --------------------------------------------------------------------------


class BlobCache:
    """sha256-addressed blob store plus a URL -> digest map."""

    def __init__(self, root: Path, transport: Transport) -> None:
        self.root = Path(root)
        self._transport = transport
        self._urls_path = self.root / "urls.json"
        self._urls: dict[str, str] = {}
        if self._urls_path.is_file():
            self._urls = json.loads(self._urls_path.read_text())
        self.network_requests = 0

    def blob_path(self, digest: str) -> Path:
        return self.root / "blobs" / digest

    def fetch(self, url: str, *, refresh: bool = False) -> tuple[bytes, str, bool]:
        """Return ``(data, sha256, from_cache)``; network only on a cache miss.

        ``refresh=True`` skips the URL map lookup (for mutable URLs such as a
        shuttle index) and re-points the map at the new digest.
        """
        digest = None if refresh else self._urls.get(url)
        if digest is not None and self.blob_path(digest).is_file():
            return self.blob_path(digest).read_bytes(), digest, True
        data = self._transport(url)
        self.network_requests += 1
        digest = hashlib.sha256(data).hexdigest()
        path = self.blob_path(digest)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.is_file():
            path.write_bytes(data)
        self._urls[url] = digest
        self._urls_path.parent.mkdir(parents=True, exist_ok=True)
        self._urls_path.write_text(
            json.dumps(self._urls, indent=2, sort_keys=True) + "\n"
        )
        return data, digest, False


# --------------------------------------------------------------------------
# Index walking
# --------------------------------------------------------------------------

# The live index has no per-project or top-level ``pdk`` field; the PDK is a
# property of the shuttle. Tiny Tapeout names shuttles by process family:
# ``ttgf*`` = GlobalFoundries gf180mcuD, ``ttihp*`` = IHP sg13g2, and the
# ``tt<NN>`` / ``ttsky*`` series = sky130A.
_SHUTTLE_PDKS: tuple[tuple[str, str], ...] = (
    (r"^ttgf", "gf180mcuD"),
    (r"^ttihp", "ihp-sg13g2"),
    (r"^ttsky", "sky130A"),
    (r"^tt\d+", "sky130A"),
)


def shuttle_pdk(shuttle: str) -> str | None:
    """PDK for a shuttle id by naming convention; ``None`` if unrecognised."""
    for pattern, pdk in _SHUTTLE_PDKS:
        if re.match(pattern, shuttle.lower()):
            return pdk
    return None


def safe_macro(macro: str) -> str | None:
    """``macro`` if usable as a single file name, else ``None``."""
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", macro) and ".." not in macro:
        return macro
    return None


def _owner_repo(repo_url: str) -> tuple[str, str] | None:
    match = re.match(
        r"^https?://github\.com/([^/\s]+)/([^/\s]+?)(?:\.git)?/?$", repo_url.strip()
    )
    return (match.group(1), match.group(2)) if match else None


def walk_index(
    index: dict[str, Any], *, shuttle: str, pdk: str | None = None
) -> list[dict[str, Any]]:
    """Normalise an index document into one record per project.

    A project's PDK is its own ``pdk`` field if present, else the index's
    top-level ``pdk``, else the PDK implied by the shuttle id (the real index
    has neither field); ``pdk=None`` disables filtering. Projects with no pinned commit
    or a non-GitHub repo are kept but marked ``unfetchable`` so the manifest
    says why they are absent, rather than dropping them silently.
    """
    default_pdk = index.get("pdk") or shuttle_pdk(index.get("id") or shuttle)
    out: list[dict[str, Any]] = []
    for project in index.get("projects", []):
        project_pdk = project.get("pdk") or default_pdk
        if pdk is not None and project_pdk != pdk:
            continue
        repo = project.get("repo", "")
        commit = project.get("commit", "")
        parsed = _owner_repo(repo) if repo else None
        problem = None
        if not commit:
            problem = "index entry has no pinned commit"
        elif parsed is None:
            problem = f"repo is not a github.com URL: {repo!r}"
        out.append(
            {
                "macro": project.get("macro") or project.get("address") or "",
                "title": project.get("title", ""),
                "repo": repo,
                "commit": commit,
                "shuttle": shuttle,
                "pdk": project_pdk,
                "owner_repo": parsed,
                "gds_url": project.get("gds_url") or project.get("gds"),
                "unfetchable": problem,
            }
        )
    return out


# --------------------------------------------------------------------------
# License detection
# --------------------------------------------------------------------------


def detect_license(archive: bytes) -> dict[str, Any]:
    """Read the license from a source tarball's top-level license file.

    Returns ``{"spdx": <id or None>, "file": <name or None>}``. ``spdx`` is
    ``None`` when there is no license file or its text matches no known
    license -- both mean the design is excluded.
    """
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:*") as tar:
            for member in tar.getmembers():
                parts = member.name.split("/")
                # GitHub archives wrap everything in one top-level directory.
                if not member.isfile() or len(parts) != 2:
                    continue
                if not _LICENSE_FILE.match(parts[1]):
                    continue
                handle = tar.extractfile(member)
                if handle is None:
                    continue
                text = re.sub(
                    r"\s+", " ", handle.read(1 << 20).decode("utf-8", "replace")
                ).lower()
                for spdx, pattern in _LICENSE_MARKERS:
                    if re.search(pattern, text):
                        return {"spdx": spdx, "file": parts[1]}
                return {"spdx": None, "file": parts[1]}
    except (tarfile.TarError, EOFError, OSError):
        return {"spdx": None, "file": None}
    return {"spdx": None, "file": None}


# --------------------------------------------------------------------------
# Per-design ingest
# --------------------------------------------------------------------------


def ingest_design(
    record: dict[str, Any],
    cache: BlobCache,
    *,
    gds_url_template: str | None = None,
    shuttle_vars: dict[str, str] | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> dict[str, Any]:
    """Fetch one design's artifacts and return its manifest."""
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "macro": record["macro"],
        "title": record["title"],
        "repo": record["repo"],
        "commit": record["commit"],
        "shuttle": record["shuttle"],
        "pdk": record["pdk"],
        "license": {"spdx": None, "file": None},
        "artifacts": {},
        "excluded": False,
        "exclusion_reason": None,
        "fetched_at": now().isoformat(),
    }

    def exclude(reason: str) -> dict[str, Any]:
        manifest["excluded"] = True
        manifest["exclusion_reason"] = reason
        return manifest

    if record["unfetchable"]:
        return exclude(record["unfetchable"])
    owner, repo = record["owner_repo"]
    try:
        archive, digest, cached = cache.fetch(
            ARCHIVE_URL.format(owner=owner, repo=repo, commit=record["commit"])
        )
    except FetchError as exc:
        return exclude(f"source fetch failed: {exc}")
    manifest["artifacts"]["sources"] = {
        "sha256": digest,
        "bytes": len(archive),
        "from_cache": cached,
    }

    manifest["license"] = detect_license(archive)
    if manifest["license"]["spdx"] is None:
        return exclude("no readable license in the repo at the pinned commit")

    gds_url = record["gds_url"]
    if not gds_url and gds_url_template:
        shuttle = shuttle_vars or {}
        if not all(
            shuttle.get(k) for k in re.findall(r"{(shuttle_\w+)}", gds_url_template)
        ):
            return exclude("template needs shuttle repo/commit; index names none")
        try:
            gds_url = gds_url_template.format(
                owner=owner,
                repo=repo,
                commit=record["commit"],
                macro=record["macro"],
                **shuttle,
            )
        except (KeyError, IndexError) as exc:
            return exclude(f"bad --gds-url-template placeholder: {exc}")
    if gds_url:
        try:
            gds, gds_digest, gds_cached = cache.fetch(gds_url)
            manifest["artifacts"]["gds"] = {
                "url": gds_url,
                "sha256": gds_digest,
                "bytes": len(gds),
                "from_cache": gds_cached,
            }
        except FetchError as exc:
            manifest["artifacts"]["gds"] = {"url": gds_url, "error": str(exc)}
    else:
        manifest["artifacts"]["gds"] = None  # none named by the index or a template
    return manifest


def run(
    index: dict[str, Any],
    cache: BlobCache,
    *,
    shuttle: str,
    pdk: str | None = None,
    limit: int | None = None,
    gds_url_template: str | None = None,
) -> dict[str, Any]:
    repo_path = "/".join(p) if (p := _owner_repo(index.get("repo") or "")) else ""
    commit = index.get("commit") or ""
    shuttle_vars = {"shuttle_repo": repo_path, "shuttle_commit": commit}
    records = walk_index(index, shuttle=shuttle, pdk=pdk)
    if pdk is not None and not records and index.get("projects"):
        raise PdkFilterError(
            f"--pdk {pdk!r} matched none of {len(index['projects'])} projects in "
            f"shuttle {shuttle!r} (derived shuttle PDK: {shuttle_pdk(shuttle)!r})"
        )
    if limit is not None:
        records = records[:limit]
    designs = []
    for record in records:
        macro = safe_macro(record["macro"])
        if macro is None:
            # Remote data must never choose a path outside the cache.
            manifest = ingest_design(
                {**record, "unfetchable": f"unsafe macro name: {record['macro']!r}"},
                cache,
            )
            macro = hashlib.sha256(record["macro"].encode()).hexdigest()[:16]
        else:
            manifest = ingest_design(
                record,
                cache,
                gds_url_template=gds_url_template,
                shuttle_vars=shuttle_vars,
            )
        path = cache.root / "manifests" / shuttle / f"{macro}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        designs.append(manifest)
    return {
        "schema_version": SCHEMA_VERSION,
        "shuttle": shuttle,
        "pdk": pdk,
        "total": len(designs),
        "included": sum(1 for d in designs if not d["excluded"]),
        "excluded": sum(1 for d in designs if d["excluded"]),
        "network_requests": cache.network_requests,
        "designs": designs,
    }


def main(argv: list[str] | None = None, transport: Transport | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--shuttle", required=True, help="shuttle id, e.g. tt06")
    parser.add_argument(
        "--pdk", help="keep only projects on this PDK (e.g. sky130A, gf180mcuD)"
    )
    parser.add_argument("--cache-dir", default=".cache/tt-corpus", type=Path)
    parser.add_argument(
        "--index-file", type=Path, help="read the index from a local file (no network)"
    )
    parser.add_argument(
        "--refresh-index",
        action="store_true",
        help="re-download the shuttle index even if cached (it is mutable)",
    )
    parser.add_argument("--limit", type=int, help="process at most N projects")
    parser.add_argument(
        "--min-interval",
        type=float,
        default=DEFAULT_MIN_INTERVAL_S,
        help="pacing floor in seconds between requests (default %(default)s)",
    )
    parser.add_argument(
        "--gds-url-template",
        help="layout URL template if the index names none. Project placeholders: "
        "{owner},{repo},{commit},{macro}; shuttle-level (from the index document's "
        "own repo/commit): {shuttle_repo} (owner/repo),{shuttle_commit}",
    )
    args = parser.parse_args(argv)

    transport = transport or PacedHttpTransport(args.min_interval)
    cache = BlobCache(args.cache_dir, transport)
    try:
        if args.index_file:
            index = json.loads(args.index_file.read_text())
        else:
            data, _, _ = cache.fetch(
                INDEX_URL.format(shuttle=args.shuttle), refresh=args.refresh_index
            )
            index = json.loads(data)
    except (FetchError, OSError, ValueError) as exc:
        print(
            json.dumps(
                {"schema_version": SCHEMA_VERSION, "error": f"index unavailable: {exc}"}
            )
        )
        return 2
    try:
        summary = run(
            index,
            cache,
            shuttle=args.shuttle,
            pdk=args.pdk,
            limit=args.limit,
            gds_url_template=args.gds_url_template,
        )
    except PdkFilterError as exc:
        print(json.dumps({"schema_version": SCHEMA_VERSION, "error": str(exc)}))
        return 3
    json.dump(summary, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
