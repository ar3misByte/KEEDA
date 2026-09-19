"""S-expression reader for KiCad files.

KiCad stores `.kicad_sch` and `.kicad_pcb` as S-expressions. The PCB side of
KiCad Live never needs this - it reads the live board over the IPC API - but
eeschema does not implement that API in KiCad 10.0.6, so the schematic side
reads the file instead. See docs/SCHEMATIC.md.

This is a reader only. KiCad Live never writes a schematic file; doing so
under a running eeschema would be silently overwritten the next time the user
pressed Ctrl+S, and could lose their work.

Measured: 229 KB schematic parsed in ~17 ms, so re-parsing on save is cheap.
"""
from __future__ import annotations

import io
import re
from typing import Any

# One token: a paren, a quoted string (with escapes), or a bare atom.
_TOKEN = re.compile(r'''\(|\)|"(?:[^"\\]|\\.)*"|[^\s()]+''')


class SExprError(ValueError):
    """The file was not well-formed S-expression."""


def parse(text: str) -> list:
    """Parse S-expression text into nested lists.

    ``(symbol (at 1 2))`` becomes ``['symbol', ['at', '1', '2']]``.
    Atoms stay strings; callers coerce, because KiCad's grammar is
    position-dependent and only the caller knows what a field means.
    """
    stack: list[list] = []
    current: list = []

    for match in _TOKEN.finditer(text):
        token = match.group()
        if token == "(":
            stack.append(current)
            current = []
        elif token == ")":
            if not stack:
                raise SExprError("unbalanced closing parenthesis")
            finished = current
            current = stack.pop()
            current.append(finished)
        elif token.startswith('"'):
            current.append(_unquote(token))
        else:
            current.append(token)

    if stack:
        raise SExprError("unbalanced opening parenthesis")
    return current


def _unquote(token: str) -> str:
    body = token[1:-1]
    if "\\" not in body:
        return body
    out = []
    escaped = False
    for char in body:
        if escaped:
            out.append({"n": "\n", "t": "\t"}.get(char, char))
            escaped = False
        elif char == "\\":
            escaped = True
        else:
            out.append(char)
    return "".join(out)


def parse_file(path: str) -> list:
    """Parse a KiCad file and return its single root node."""
    with io.open(path, "r", encoding="utf-8") as handle:
        text = handle.read()
    tree = parse(text)
    if not tree or not isinstance(tree[0], list):
        raise SExprError(f"{path}: no root S-expression found")
    return tree[0]


# --------------------------------------------------------------------------
# Small accessors. KiCad nodes are `[keyword, *values, *children]`, so these
# three cover almost every lookup the parsers need.
# --------------------------------------------------------------------------

def children(node: list, keyword: str) -> list[list]:
    """Every direct child list whose head is `keyword`."""
    return [c for c in node
            if isinstance(c, list) and c and c[0] == keyword]


def child(node: list, keyword: str) -> list | None:
    """The first direct child list whose head is `keyword`, or None."""
    for candidate in node:
        if isinstance(candidate, list) and candidate and candidate[0] == keyword:
            return candidate
    return None


def values(node: list, keyword: str) -> list[Any] | None:
    """The values of the first `keyword` child, without the keyword itself.

    ``values(sym, 'at')`` on ``(at 31.75 91.44 180)`` gives
    ``['31.75', '91.44', '180']``.
    """
    found = child(node, keyword)
    return found[1:] if found is not None else None


def value(node: list, keyword: str, index: int = 0, default=None):
    """One value of a `keyword` child, or `default` when absent."""
    found = values(node, keyword)
    if found is None or index >= len(found):
        return default
    item = found[index]
    return item if not isinstance(item, list) else default


def properties(node: list) -> dict[str, str]:
    """`(property "Reference" "R1" ...)` children as a plain dict."""
    out: dict[str, str] = {}
    for prop in children(node, "property"):
        if len(prop) >= 3 and isinstance(prop[1], str) and isinstance(prop[2], str):
            out[prop[1]] = prop[2]
    return out


def as_float(raw, default: float = 0.0) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default
