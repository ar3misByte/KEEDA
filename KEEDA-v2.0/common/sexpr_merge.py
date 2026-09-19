"""Three-way merge of KiCad S-expression files (.kicad_sch / .kicad_pcb).

Two people editing the same sheet at the same time used to mean "one of you
loses". But KiCad files are lists of independent objects, each with its own
UUID, so two people who touched DIFFERENT objects can be merged automatically.

    merge(base, mine, theirs)

  base    the last version both sides agreed on
  mine    what is on this computer now
  theirs  what the team's server holds now

Rules, per top-level object (keyed by its uuid):
  * changed on one side only        -> take that side
  * changed identically on both     -> take it
  * deleted on one side, untouched on the other -> deleted
  * changed differently on both     -> CONFLICT (nothing is merged)

Objects without a uuid (title block, paper size, lib_symbols ...) are keyed by
their name; `lib_symbols` and other containers are merged one level deeper so
that two people adding different parts to the design do not conflict.

The merge works on the ORIGINAL TEXT of each object, so formatting is kept
exactly and KiCad sees an ordinary file.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field


@dataclass
class MergeResult:
    text: str | None = None                 # merged file, None when conflicted
    conflicts: list[str] = field(default_factory=list)
    taken_theirs: int = 0                   # objects adopted from the other side
    kept_mine: int = 0

    @property
    def clean(self) -> bool:
        return self.text is not None


def _scan_lists(text: str, start: int = 0):
    """Yield (begin, end) of each list nested exactly one level below `text`'s
    outermost list. `start` is the index of that outer '('."""
    depth = 0
    i = start
    n = len(text)
    begin = None
    while i < n:
        ch = text[i]
        if ch == '"':
            i += 1
            while i < n and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
        elif ch == "(":
            depth += 1
            if depth == 2:
                begin = i
        elif ch == ")":
            if depth == 2 and begin is not None:
                yield begin, i + 1
                begin = None
            depth -= 1
            if depth == 0:
                return
        i += 1


def _split(text: str):
    """(head, [(chunk_with_leading_whitespace, chunk_text)], tail) for one list."""
    outer = text.find("(")
    if outer < 0:
        raise ValueError("not an S-expression")
    spans = list(_scan_lists(text, outer))
    if not spans:
        return text, [], ""
    head_end = spans[0][0]
    while head_end > outer + 1 and text[head_end - 1].isspace():
        head_end -= 1
    chunks = []
    cursor = head_end
    for begin, end in spans:
        chunks.append((text[cursor:end], text[begin:end]))
        cursor = end
    return text[:head_end], chunks, text[cursor:]


_HEAD_RE = re.compile(r'\(\s*([^\s()"]+)')
_STR_RE = re.compile(r'"((?:[^"\\]|\\.)*)"')


def _direct_uuid(body: str) -> str | None:
    """The uuid/tstamp that belongs to THIS object, not to a nested pin."""
    for begin, end in _scan_lists(body, body.find("(")):
        piece = body[begin:end]
        m = re.match(r'\(\s*(uuid|tstamp)\s+"?([^\s")]+)', piece)
        if m:
            return m.group(2)
    return None


def _key(body: str, taken: dict) -> str:
    head = _HEAD_RE.match(body)
    name = head.group(1) if head else "?"
    uid = _direct_uuid(body)
    if uid:
        key = f"{name}:{uid}"
    else:
        first = _STR_RE.search(body[: 200])
        key = f"{name}:{first.group(1)}" if first and name in ("symbol", "property") else name
    if key in taken:                       # repeated uuid-less item: disambiguate by content
        key = f"{key}#{hashlib.md5(_norm(body).encode()).hexdigest()[:8]}"
    return key


def _norm(text: str | None) -> str | None:
    return None if text is None else " ".join(text.split())


def _keyed(chunks) -> dict[str, tuple[str, str]]:
    out: dict[str, tuple[str, str]] = {}
    for full, body in chunks:
        out[_key(body, out)] = (full, body)
    return out


def _merge_list(base: str, mine: str, theirs: str, result: MergeResult,
                path: str, depth: int) -> str | None:
    head_m, chunks_m, tail_m = _split(mine)
    _, chunks_b, _ = _split(base)
    _, chunks_t, _ = _split(theirs)
    b, m, t = _keyed(chunks_b), _keyed(chunks_m), _keyed(chunks_t)

    order = list(m.keys()) + [k for k in t if k not in m]
    pieces: list[str] = []
    for key in order:
        bm, mm, tm = b.get(key), m.get(key), t.get(key)
        nb = _norm(bm[1]) if bm else None
        nm = _norm(mm[1]) if mm else None
        nt = _norm(tm[1]) if tm else None
        if nm == nt:
            chosen = mm
        elif nb == nm:                                   # only they changed it
            chosen = tm
            result.taken_theirs += 1
        elif nb == nt:                                   # only I changed it
            chosen = mm
            result.kept_mine += 1
        elif depth == 0 and mm and tm and bm and "(" in mm[1][1:]:
            inner = _merge_list(bm[1], mm[1], tm[1], result, f"{path}/{key}", depth + 1)
            if inner is None:
                result.conflicts.append(f"{path}/{key}")
                continue
            chosen = (mm[0][: len(mm[0]) - len(mm[1])] + inner, inner)
        else:
            result.conflicts.append(f"{path}/{key}")
            continue
        if chosen is not None:
            pieces.append(chosen[0])
    if result.conflicts:
        return None
    return head_m + "".join(pieces) + tail_m


def merge(base: str, mine: str, theirs: str) -> MergeResult:
    """Three-way merge. `result.text` is None when any object conflicts."""
    result = MergeResult()
    try:
        merged = _merge_list(base, mine, theirs, result, "", 0)
    except ValueError as exc:
        result.conflicts.append(f"unparseable: {exc}")
        return result
    if merged is not None and not _balanced(merged):
        result.conflicts.append("merge produced unbalanced parentheses")
        merged = None
    result.text = merged
    return result


def _balanced(text: str) -> bool:
    depth = 0
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            i += 1
            while i < n and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                return False
        i += 1
    return depth == 0
