"""Describe differences between the parsed headers of two Liberty files."""

#: Most header deltas listed in one refusal before the rest are summarised.
_MAX_REPORTED_DELTAS = 10


def header_mismatch(
    first_path: str, first: dict[str, str], path: str, other: dict[str, str]
) -> str | None:
    """Format one refusal for the differing fields, in source order."""
    deltas = _header_deltas(first_path, first, path, other)
    if not deltas:
        return None
    if len(deltas) == 1:
        return f"cannot merge: {deltas[0]}"
    lines = [
        f"cannot merge: {len(deltas)} header fields differ between "
        f"'{first_path}' and '{path}':"
    ]
    lines += [f"  - {d}" for d in deltas[:_MAX_REPORTED_DELTAS]]
    if len(deltas) > _MAX_REPORTED_DELTAS:
        lines.append(f"  ... and {len(deltas) - _MAX_REPORTED_DELTAS} more")
    return "\n".join(lines)


def _header_deltas(
    first_path: str, first: dict[str, str], path: str, other: dict[str, str]
) -> list[str]:
    renamed = _pair_renamed_groups(first, other)
    renamed_other = set(renamed.values())
    deltas: list[str] = []
    for key, value in first.items():
        if key in renamed:
            new_key = renamed[key]
            entry = (
                f"'{key}' in '{first_path}' is '{new_key}' in '{path}' "
                "(same group, different name"
            )
            if other[new_key] != value:
                shown_a, shown_b = _clip_around_diff(value, other[new_key])
                entry += f"; body also differs: {shown_a} vs {shown_b}"
            deltas.append(entry + ")")
        elif key not in other:
            deltas.append(f"'{key}' present in '{first_path}', missing in '{path}'")
        elif other[key] != value:
            shown_a, shown_b = _clip_around_diff(value, other[key])
            deltas.append(
                f"'{key}' differs: {shown_a} in '{first_path}', {shown_b} in '{path}'"
            )
    for key in other:
        if key not in first and key not in renamed_other:
            deltas.append(f"'{key}' missing in '{first_path}', present in '{path}'")
    return deltas


def _pair_renamed_groups(
    first: dict[str, str], other: dict[str, str]
) -> dict[str, str]:
    """Map a ``name(A)`` group key only in ``first`` to the ``name(B)`` key
    only in ``other`` when each is the sole unmatched group of that base
    name on its side. Complex attributes (``name (args) ;``, whose stored
    value is their args) are not groups and are never paired."""

    def unmatched(
        fields: dict[str, str], against: dict[str, str]
    ) -> dict[str, list[str]]:
        by_base: dict[str, list[str]] = {}
        for key, value in fields.items():
            if key in against or "(" not in key or not key.endswith(")"):
                continue
            base, args = key[:-1].split("(", 1)
            if value == args:
                continue  # complex attribute, not a group
            by_base.setdefault(base, []).append(key)
        return by_base

    mine, theirs = unmatched(first, other), unmatched(other, first)
    return {
        keys[0]: theirs[base][0]
        for base, keys in mine.items()
        if len(keys) == 1 and len(theirs.get(base, ())) == 1
    }


def _clip_around_diff(a: str, b: str, limit: int = 80) -> tuple[str, str]:
    """Both values clipped to windows around their first differing
    character, so two long values that share a prefix print visibly
    different. Values within ``limit`` print unchanged."""
    if len(a) <= limit and len(b) <= limit:
        return a, b
    common = 0
    for ca, cb in zip(a, b, strict=False):
        if ca != cb:
            break
        common += 1
    start = max(0, common - 20)

    def window(value: str) -> str:
        if len(value) <= limit and start == 0:
            return value
        head = "..." if start else ""
        width = limit - len(head)
        seg = value[start : start + width]
        if start + width < len(value):
            seg = seg[:-3] + "..."
        return head + seg

    return window(a), window(b)
