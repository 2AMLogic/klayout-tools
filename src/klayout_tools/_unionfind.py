"""Shared disjoint-set (union-find) helper.

Several modules (``gen_compose_routing``, ``ir_solver``, ``lvs_mismatch``,
``parasitic_integrity``, ``power``) each grouped items by connectivity with
their own hand-rolled copy of this algorithm. :class:`UnionFind` is the single
place it lives now.
"""

from __future__ import annotations

from collections.abc import Hashable, Iterable
from typing import Any


class UnionFind:
    """Dict-backed disjoint set with path compression.

    Nodes are added on first sight (by :meth:`find` or :meth:`union`), or up
    front via ``nodes``. With ``min_root=True`` the smaller of two roots always
    becomes the merged root (nodes must then be orderable) -- a deterministic
    representative for callers whose output order depends on it. Otherwise
    the second root is attached under the first.
    """

    def __init__(
        self, nodes: Iterable[Hashable] = (), *, min_root: bool = False
    ) -> None:
        self._parent: dict[Any, Any] = {node: node for node in nodes}
        self._min_root = min_root

    def find(self, node: Any) -> Any:
        parent = self._parent
        if node not in parent:
            parent[node] = node
            return node
        root = node
        while parent[root] != root:
            root = parent[root]
        while parent[node] != root:
            parent[node], node = root, parent[node]
        return root

    def union(self, a: Any, b: Any) -> bool:
        """Merge the sets of ``a`` and ``b``; ``True`` if they were distinct."""
        root_a, root_b = self.find(a), self.find(b)
        if root_a == root_b:
            return False
        if self._min_root and root_b < root_a:
            root_a, root_b = root_b, root_a
        self._parent[root_b] = root_a
        return True
