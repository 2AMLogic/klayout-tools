"""Format section tables for Markdown and plain-text reports."""

from __future__ import annotations


def markdown_table(headers: tuple[str, ...], rows: list[tuple[str, ...]]) -> str:
    def escape(cell: str) -> str:
        return cell.replace("|", "\\|").replace("\n", " ")

    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    lines.extend("| " + " | ".join(escape(cell) for cell in row) + " |" for row in rows)
    return "\n".join(lines)


def plain_table(headers: tuple[str, ...], rows: list[tuple[str, ...]]) -> str:
    widths = [
        max(len(headers[col]), max((len(row[col]) for row in rows), default=0))
        for col in range(len(headers))
    ]

    def fmt(row: tuple[str, ...]) -> str:
        return "  ".join(row[col].ljust(widths[col]) for col in range(len(headers)))

    lines = [fmt(headers), "  ".join("-" * width for width in widths)]
    lines.extend(fmt(row) for row in rows)
    return "\n".join(lines)
