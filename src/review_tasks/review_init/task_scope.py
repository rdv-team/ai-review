"""Shared provenance replay for AI review material and full-file comparison."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Callable, Generic, TypeVar


T = TypeVar("T", str, bytes)


@dataclass
class PromptLine(Generic[T]):
    sign: str
    text: T
    target_line: int | None = None


@dataclass(frozen=True)
class BlameLine:
    sha: str
    source_line: int
    target_line: int
    text: str


BLAME_HEADER_RE = re.compile(r"^([0-9a-f]{40,64}) (\d+) (\d+)(?: (\d+))?$")


def parse_final_blame(raw: bytes, task_shas: set[str]) -> list[BlameLine]:
    """Select final TARGET lines using the same Git attribution for both views."""
    pending: tuple[str, int, int] | None = None
    lines: list[BlameLine] = []
    for text in raw.decode("utf-8", errors="replace").splitlines():
        match = BLAME_HEADER_RE.match(text)
        if match:
            pending = (match.group(1).lower(), int(match.group(2)), int(match.group(3)))
            continue
        if pending is None or not text.startswith("\t"):
            continue
        sha, source_line, target_line = pending
        pending = None
        if sha in task_shas:
            lines.append(BlameLine(sha, source_line, target_line, text[1:]))
    return lines


@dataclass(frozen=True)
class DiffLine(Generic[T]):
    sign: str
    text: T
    old_line: int | None = None
    new_line: int | None = None


@dataclass
class DiffHunk(Generic[T]):
    header: str
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    context: str
    lines: list[DiffLine[T]]


@dataclass(frozen=True)
class NetOldLineState(Generic[T]):
    text: T
    from_task: bool = False
    old_entry_ids: tuple[int, ...] = ()
    ambiguous: bool = False


@dataclass(frozen=True)
class NetOldEntry(Generic[T]):
    index: int
    lines: list[PromptLine[T]]


@dataclass(frozen=True)
class DeletedSpan:
    position: int
    old_entry_ids: tuple[int, ...]


@dataclass(frozen=True)
class ScopeAmbiguity:
    """Zero-based half-open TARGET span, or a gap when start equals end."""
    start: int
    end: int
    reason: str


class TaskScopeError(ValueError):
    """The available history cannot unambiguously isolate the selected changes."""


class TaskScopeIntegrityError(ValueError):
    """History does not reproduce the source bytes; partial recovery is unsafe."""


def quarantine_origins(
    states: list[NetOldLineState[T]],
    deletions: list[DeletedSpan],
    origin_ids: set[int],
    ambiguities: list[ScopeAmbiguity],
    *,
    positions: set[int] | None = None,
) -> list[NetOldLineState[T]]:
    """Keep a whole connected origin group as TARGET context, never old pieces.

    Return copied states and remove affected tombstones in place. Shared origin
    sets form indivisible replacement groups, including nonadjacent descendants.
    """
    positions = positions or set()
    affected = set(origin_ids)
    groups = [state.old_entry_ids for state in states] + [gap.old_entry_ids for gap in deletions]
    while True:
        expanded = affected | {entry for group in groups if affected.intersection(group) for entry in group}
        if expanded == affected:
            break
        affected = expanded
    current = list(states)
    for index, state in enumerate(states):
        if index in positions or affected.intersection(state.old_entry_ids):
            current[index] = replace(state, from_task=False, old_entry_ids=(), ambiguous=True)
            ambiguities.append(ScopeAmbiguity(index, index + 1, "ambiguous_region"))
    for gap in deletions:
        if affected.intersection(gap.old_entry_ids):
            ambiguities.append(ScopeAmbiguity(gap.position, gap.position, "ambiguous_region"))
    deletions[:] = [gap for gap in deletions if not affected.intersection(gap.old_entry_ids)]
    return current


def _move_ambiguities(ambiguities: list[ScopeAmbiguity], start: int, end: int, count: int) -> None:
    """Map markers to a conservative TARGET envelope, including shared edges."""
    delta = count - (end - start)
    moved: list[ScopeAmbiguity] = []
    for span in ambiguities:
        if span.end < start:
            moved.append(span)
        elif span.start > end:
            moved.append(ScopeAmbiguity(span.start + delta, span.end + delta, span.reason))
        else:
            left = span.start if span.start < start else start
            right = span.end + delta if span.end > end else start + count
            moved.append(ScopeAmbiguity(left, right, span.reason))
    ambiguities[:] = moved


def hunk_old_index(hunk: DiffHunk[T]) -> int:
    return hunk.old_start if hunk.old_count == 0 else hunk.old_start - 1


def build_net_old_hunk_segment(
    states: list[NetOldLineState[T]],
    hunk: DiffHunk[T],
    *,
    start: int,
    is_task_commit: bool,
    entries: list[NetOldEntry[T]],
    projected: dict[int, T] | None = None,
    deletion_ids: dict[int, list[int]] | None = None,
) -> tuple[list[NetOldLineState[T]], tuple[int, ...]]:
    """Return replacement states and origins, appending new entries in order.

    Input states and existing entries are unchanged. New entries are allocated
    append-only so their indices remain stable throughout the caller's replay.
    A foreign replacement starts a new provenance chain.
    """
    carried: list[int] = []
    external: list[PromptLine[T]] = []

    def flush_external() -> None:
        if external:
            entries.append(NetOldEntry(index=len(entries), lines=list(external)))
            carried.append(entries[-1].index)
            external.clear()

    index = start
    if is_task_commit:
        for line in hunk.lines:
            if line.sign not in {" ", "-"}:
                continue
            if deletion_ids is not None and index in deletion_ids:
                flush_external()
                carried.extend(deletion_ids[index])
            state = states[index] if index < len(states) else NetOldLineState(text=line.text)
            if line.sign == "-":
                if state.from_task:
                    flush_external()
                    carried.extend(state.old_entry_ids)
                elif projected is None or index + 1 in projected:
                    external.append(PromptLine(
                        sign="-", text=state.text if projected is None else projected[index + 1],
                    ))
            index += 1
        if deletion_ids is not None and index in deletion_ids:
            flush_external()
            carried.extend(deletion_ids[index])
        flush_external()
    task_entry_ids = tuple(dict.fromkeys(carried)) if is_task_commit else ()

    index = start
    segment: list[NetOldLineState[T]] = []
    for line in hunk.lines:
        if line.sign == " ":
            segment.append(states[index] if index < len(states) else NetOldLineState(text=line.text))
            index += 1
        elif line.sign == "-":
            index += 1
        elif line.sign == "+":
            segment.append(NetOldLineState(
                text=line.text, from_task=is_task_commit, old_entry_ids=task_entry_ids,
            ))
    return segment, task_entry_ids


def apply_net_old_hunks(
    states: list[NetOldLineState[T]],
    hunks: list[DiffHunk[T]],
    *,
    is_task_commit: bool,
    entries: list[NetOldEntry[T]],
    project: Callable[[list[NetOldLineState[T]]], dict[int, T]] | None = None,
    deletions: list[DeletedSpan] | None = None,
    strict: bool = False,
    ambiguities: list[ScopeAmbiguity] | None = None,
) -> list[NetOldLineState[T]]:
    """Replay provenance once for both consumers; optional gaps retain deletions.

    Return copied states; append entries and replace the supplied deletion list
    in place. AI uses projected text with tolerant hunk bounds. Full-file
    comparison explicitly requests strict source/provenance checks. Collecting
    deletion gaps is independent from that validation policy.
    An ambiguity collector opts into origin-local recovery; None preserves the
    strict caller's previous fail-closed behavior. Markers follow exact edits
    to final TARGET coordinates. Source-byte mismatches are never recoverable.
    """
    current = list(states)
    for hunk in sorted(hunks, key=hunk_old_index, reverse=True):
        start = max(0, min(len(current), hunk_old_index(hunk)))
        end = max(start, min(len(current), start + hunk.old_count))
        touched = [] if deletions is None else [
            gap for gap in deletions if start <= gap.position <= end
        ]
        if strict:
            removed = [line.text for line in hunk.lines if line.sign in {" ", "-"}]
            if start != hunk_old_index(hunk) or removed != [s.text for s in current[start:end]]:
                raise TaskScopeIntegrityError("История строк не совпадает с исходным содержимым.")
            if not is_task_commit:
                removed_ids = {entry for state in current[start:end] for entry in state.old_entry_ids}
                surviving_ids = {entry for state in current[:start] + current[end:] for entry in state.old_entry_ids}
                if touched or removed_ids & surviving_ids:
                    if ambiguities is None:
                        raise TaskScopeError("Чужая правка неоднозначно пересекает вклад задачи.")
                    affected = removed_ids & surviving_ids
                    affected.update(entry for gap in touched for entry in gap.old_entry_ids)
                    current = quarantine_origins(current, deletions if deletions is not None else [], affected, ambiguities)
                    ambiguities.append(ScopeAmbiguity(start, end, "ambiguous_region"))
                    touched = [] if deletions is None else [gap for gap in deletions if start <= gap.position <= end]
        projected = project(current) if project is not None else None
        deletion_ids: dict[int, list[int]] = {}
        for gap in touched:
            deletion_ids.setdefault(gap.position, []).extend(gap.old_entry_ids)
        segment, replacement_ids = build_net_old_hunk_segment(
            current, hunk, start=start, is_task_commit=is_task_commit,
            entries=entries, projected=projected,
            deletion_ids=deletion_ids,
        )
        if deletions is not None:
            delta = len(segment) - (end - start)
            deletions[:] = [
                DeletedSpan(gap.position + (delta if gap.position > end else 0), gap.old_entry_ids)
                for gap in deletions if gap not in touched
            ]
            if is_task_commit and not segment:
                if replacement_ids:
                    deletions.append(DeletedSpan(start, replacement_ids))
        if ambiguities is not None:
            _move_ambiguities(ambiguities, start, end, len(segment))
        current[start:end] = segment
    return current


def materialize_before(
    states: list[NetOldLineState[bytes]],
    entries: list[NetOldEntry[bytes]],
    deletions: list[DeletedSpan],
    *,
    ambiguities: list[ScopeAmbiguity] | None = None,
) -> bytes:
    """Restore net-old payloads while keeping foreign TARGET context intact."""
    states = list(states)
    deletions = list(deletions)
    while True:
        seen: set[int] = set()
        previous: set[int] = set()
        disconnected: set[int] = set()
        gaps_by_position: dict[int, list[int]] = {}
        for gap in deletions:
            gaps_by_position.setdefault(gap.position, []).extend(gap.old_entry_ids)
        for position in range(len(states) + 1):
            state = states[position] if position < len(states) else None
            ids = set(gaps_by_position.get(position, [])) | (
                set(state.old_entry_ids) if state is not None and state.from_task else set()
            )
            disconnected.update((ids & seen) - previous)
            seen.update(ids)
            if state is not None and not state.from_task:
                previous = set()
            elif ids:
                previous = ids
        if not disconnected:
            break
        if ambiguities is None:
            raise TaskScopeError("Прежние строки неоднозначно разделены чужим контекстом.")
        states = quarantine_origins(states, deletions, disconnected, ambiguities)
    gaps: dict[int, list[int]] = {}
    for gap in deletions:
        gaps.setdefault(gap.position, []).extend(gap.old_entry_ids)
    seen: set[int] = set()
    result: list[bytes] = []
    previous_ids: set[int] = set()
    for position in range(len(states) + 1):
        state = states[position] if position < len(states) else None
        ids = gaps.get(position, []) + (
            list(state.old_entry_ids) if state is not None and state.from_task else []
        )
        current_ids = set(ids)
        for entry_id in dict.fromkeys(ids):
            if entry_id in seen:
                if entry_id not in previous_ids:
                    raise TaskScopeError("Прежние строки неоднозначно разделены чужим контекстом.")
                continue
            result.extend(line.text for line in entries[entry_id].lines)
            seen.add(entry_id)
        if state is not None and not state.from_task:
            result.append(state.text)
            previous_ids = set()
        elif current_ids:
            previous_ids = current_ids
        # An own insertion with no old payload emits nothing into before.
        # Keep the previous origins continuous across that transparent state.
    return b"".join(result)


HUNK_HEADER_RE = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@(?P<context>.*)$"
)


def parse_unified_diff_hunks(diff_text: str) -> list[DiffHunk[str]]:
    lines = diff_text.splitlines()
    hunks: list[DiffHunk[str]] = []
    i = 0
    while i < len(lines):
        header = lines[i]
        match = HUNK_HEADER_RE.match(header)
        if not match:
            i += 1
            continue
        old_start = int(match.group("old_start"))
        new_start = int(match.group("new_start"))
        old_count = int(match.group("old_count") or "1")
        new_count = int(match.group("new_count") or "1")
        old_line = old_start
        new_line = new_start
        diff_lines: list[DiffLine[str]] = []
        i += 1
        while i < len(lines) and not HUNK_HEADER_RE.match(lines[i]):
            raw = lines[i]
            i += 1
            if not raw or raw.startswith("\\ No newline"):
                continue
            if raw.startswith(("diff --git", "index ", "--- ", "+++ ")):
                continue
            sign = raw[0]
            text = raw[1:]
            if sign == "+":
                diff_lines.append(DiffLine(sign=sign, text=text, new_line=new_line))
                new_line += 1
            elif sign == "-":
                diff_lines.append(DiffLine(sign=sign, text=text, old_line=old_line))
                old_line += 1
            elif sign == " ":
                diff_lines.append(DiffLine(sign=sign, text=text, old_line=old_line, new_line=new_line))
                old_line += 1
                new_line += 1
        hunks.append(DiffHunk(
            header=header,
            old_start=old_start,
            old_count=old_count,
            new_start=new_start,
            new_count=new_count,
            context=match.group("context") or "",
            lines=diff_lines,
        ))
    return hunks


