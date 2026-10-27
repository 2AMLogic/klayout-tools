"""Unit tests for the shared union-find helper."""

from __future__ import annotations

from klayout_tools._unionfind import UnionFind


def test_union_reports_whether_sets_were_distinct() -> None:
    uf = UnionFind()
    assert uf.union("a", "b") is True
    assert uf.union("b", "a") is False
    assert uf.find("a") == uf.find("b")


def test_unseen_nodes_are_their_own_root() -> None:
    assert UnionFind().find("x") == "x"


def test_min_root_picks_smallest_representative() -> None:
    uf = UnionFind(range(5), min_root=True)
    uf.union(3, 4)
    uf.union(4, 1)
    uf.union(2, 3)
    assert {uf.find(i) for i in (1, 2, 3, 4)} == {1}
    assert uf.find(0) == 0
