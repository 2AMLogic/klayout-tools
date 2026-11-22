"""Capture the model inputs cited in a simulation report before dispatch."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from . import env_provenance
from ._provenance import sha256_file


def _capture_model_files(paths: tuple[str, ...]) -> tuple[tuple[str, str | None], ...]:
    return tuple((path, sha256_file(path)) for path in paths)


def _model_file_entries(
    files: tuple[tuple[str, str | None], ...], *, repo_root: str | None
) -> list[dict[str, Any]]:
    return [
        {
            "name": os.path.basename(path),
            **env_provenance.repo_relative_path(path, repo_root=repo_root),
            "sha256": digest,
        }
        for path, digest in files
    ]


@dataclass(frozen=True)
class ModelInputDigests:
    """Client-side model hashes captured in load order before dispatch (#6508)."""

    models_lib_sha256: str | None
    osdi_preload: tuple[tuple[str, str | None], ...]
    corner_section_libs: tuple[tuple[str, str | None], ...]

    @classmethod
    def capture(
        cls,
        models_lib: str | None,
        osdi_preload: tuple[str, ...],
        corner_section_libs: tuple[str, ...],
    ) -> ModelInputDigests:
        return cls(
            sha256_file(models_lib),
            _capture_model_files(osdi_preload),
            _capture_model_files(corner_section_libs),
        )

    def environment(self, *, repo_root: str | None) -> dict[str, list[dict[str, Any]]]:
        """Report OSDI files in load order and section libraries in first-use order."""
        return {
            key: _model_file_entries(files, repo_root=repo_root)
            for key, files in (
                ("osdi_preload", self.osdi_preload),
                ("corner_section_libs", self.corner_section_libs),
            )
            if files
        }
