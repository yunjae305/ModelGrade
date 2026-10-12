"""Conservative structural item groups shared by question and declaration counts."""

from __future__ import annotations

import re
from typing import Any


_LEADING = re.compile(r"^(?P<n>\d{1,3})[.)]\s+\S")
_LABELED = re.compile(r"^(?P<label>[가-힣A-Za-z][가-힣A-Za-z ]{0,30}?)\s+"
                      r"(?P<n>\d{1,3}|[A-Z])(?:\s*[:.)-].*|\s*)$")


def _ordinal(title: str) -> tuple[str, int] | None:
    title = title.strip()
    match = _LEADING.match(title)
    if match:
        return "leading", int(match.group("n"))
    match = _LABELED.match(title)
    if not match:
        return None
    raw = match.group("n")
    value = int(raw) if raw.isdigit() else ord(raw) - ord("A") + 1
    return match.group("label").strip().casefold(), value


def heading_series_from(headings: list[Any], first: Any) -> dict[str, Any] | None:
    """Return a contiguous same-level ordinal series, stopping at a different heading."""
    initial = _ordinal(first.title)
    if initial is None:
        return None
    same_level = [h for h in headings if h.level == first.level and h.start >= first.start]
    selected = [first]
    stopped = None
    for heading in same_level[1:]:
        ordinal = _ordinal(heading.title)
        if ordinal != (initial[0], _ordinal(selected[-1].title)[1] + 1):
            stopped = heading
            break
        selected.append(heading)
    remaining = same_level[len(selected):]
    ambiguous_boundary = any((_ordinal(h.title) or (None, None))[0] == initial[0] for h in remaining)
    return {"kind": "numbered_headings", "count": len(selected),
            "start": selected[0].start, "end": selected[-1].end,
            "items": selected, "excluded_next_heading": stopped.title if stopped else None,
            "ambiguous_boundary": ambiguous_boundary}


def heading_series(headings: list[Any]) -> list[dict[str, Any]]:
    runs = []
    consumed: set[int] = set()
    for heading in headings:
        if heading.start in consumed or _ordinal(heading.title) is None:
            continue
        run = heading_series_from(headings, heading)
        if run:
            runs.append(run)
            consumed.update(h.start for h in run["items"])
    return runs
