"""Merged, hierarchically transformed conductor geometry and its GDS labels."""

from __future__ import annotations

from dataclasses import dataclass

import klayout.db as kdb

from ._layout import region, texts


@dataclass
class ConductorLayer:
    shapes: kdb.Region
    labels: kdb.Texts

    @classmethod
    def read(
        cls,
        layout: kdb.Layout,
        cell: kdb.Cell,
        drawing: tuple[int, int],
        label: tuple[int, int] | None,
    ) -> ConductorLayer:
        return cls(region(layout, cell, drawing).merged(), texts(layout, cell, label))

    def names_on(self, shapes: kdb.Region) -> list[str]:
        return [text.string for text in self.labels.interacting(shapes).each()]
