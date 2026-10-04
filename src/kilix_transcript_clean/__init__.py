"""Readable text from recorded kilix pane transcripts.

kitty-pty-broker records the raw bytes a pane's program wrote, control codes
included. Full-screen programs such as Claude Code, Codex, Grok and omp redraw
the screen in place, so stripping escape codes leaves a pile of fragments.
This package replays the recording on a small virtual screen, stitches the
settled screens into one transcript, and drops each program's chrome.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .providers import PROVIDERS, provider_named
from .screen import (CONTINUATION, SIZE_RECORD, Screen, infer_rows, looks_rotated,
                     parse_size_record, trim_partial_head)
from .stitch import Stitcher

__all__ = ["Section", "Transcript", "clean_bytes", "PROVIDERS"]
__version__ = "0.1.0"

SCHEMA = "kilix.transcript.clean/v1"

# Leading activity glyphs, and the blinking attention marker kilix adds, that
# animate window titles without changing what they name.
_TITLE_SPINNER = re.compile(r"^(?:[⠀-⣿✳✻✶✢✽·*◐◓◑◒⏳⌛ ]|\[ [!.] \] )+")


@dataclass
class Section:
    """Consecutive transcript lines drawn by one program."""

    provider: str
    label: str
    screen: str
    lines: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"provider": self.provider, "label": self.label,
                "screen": self.screen, "lines": self.lines}


@dataclass
class Transcript:
    sections: list[Section]
    titles: list[str]
    source: dict = field(default_factory=dict)

    @property
    def lines(self) -> list[str]:
        return [line for section in self.sections for line in section.lines]

    def text(self, header: bool = True) -> str:
        out: list[str] = []
        if header:
            for key in ("session", "pane", "cwd", "cmd"):
                if self.source.get(key):
                    out.append(f"# {key}: {self.source[key]}")
            if self.titles:
                out.append(f"# title: {self.titles[-1]}")
            if self.source.get("rotated"):
                out.append("# note: the recording reached its size limit, so it starts mid-session")
            programs = list(dict.fromkeys(s.label for s in self.sections))
            if programs:
                out.append(f"# programs: {', '.join(programs)}")
            out.append("")
        for i, section in enumerate(self.sections):
            if i or header:
                out.append(f"── {section.label} ──")
            out.extend(section.lines)
            out.append("")
        return "\n".join(out).rstrip("\n") + "\n"

    def as_dict(self) -> dict:
        return {"schema": SCHEMA, "source": self.source, "titles": self.titles,
                "sections": [s.as_dict() for s in self.sections]}

    def json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False, indent=1) + "\n"


def _join_wrapped(lines: list[str]) -> list[str]:
    """Rejoin rows that only wrapped because they reached the right margin."""
    out: list[str] = []
    for line in lines:
        if line.startswith(CONTINUATION):
            if out:
                out[-1] += line[1:]
                continue
            line = line[1:]
        out.append(line)
    return out


def _tidy(lines: list[str]) -> list[str]:
    out: list[str] = []
    for line in lines:
        if not line.strip():
            if out and out[-1]:
                out.append("")
            continue
        out.append(line)
    while out and not out[-1]:
        out.pop()
    return out


def _clean_title(title: str) -> str:
    return _TITLE_SPINNER.sub("", title).strip()


def clean_bytes(data: bytes, rows: int | None = None, provider: str | None = None,
                source: dict | None = None) -> Transcript:
    """Replay a recorded transcript and return its readable text."""
    if provider is not None:
        provider_named(provider)
    # A recording that starts with the broker's size record says exactly how
    # big the pane was and whether the size cap cut its beginning off.
    # Older recordings carry neither, so both are inferred.
    head = SIZE_RECORD.match(data)
    size = parse_size_record(head.group(1)) if head else None
    if size:
        rotated = size[2]
        if rotated:
            data = head.group(0) + trim_partial_head(data[head.end():])
        start_rows, cols = size[0], size[1]
    else:
        rotated = looks_rotated(data)
        data = trim_partial_head(data)
        start_rows, cols = infer_rows(data), 1000
    titles: list[str] = []
    seen: set[str] = set()

    def on_title(title: str) -> None:
        title = _clean_title(title)
        if title and title not in seen:
            seen.add(title)
            titles.append(title)

    stitcher = Stitcher(provider)
    # A PTY turns every line feed a program writes into CR LF, so a recording
    # with no carriage return at all was never terminal output: plain text.
    screen = Screen(rows or start_rows, cols, on_frame=stitcher.on_frame,
                    on_scroll=stitcher.on_scroll, on_title=on_title, warm=not rotated,
                    newline_returns=b"\r" not in data)
    screen.feed(data)
    screen.finish()
    stitcher.finish()

    sections: list[Section] = []
    bounds = [seg.start for seg in stitcher.segments[1:]] + [len(stitcher.doc)]
    for seg, end in zip(stitcher.segments, bounds):
        chosen = stitcher.provider_for(seg)
        body = _join_wrapped([text for text, _ in stitcher.doc[seg.start:end]])
        body = _tidy([chosen.tidy(line).rstrip() for line in body if not chosen.is_chrome(line)])
        if not body:
            continue
        if sections and sections[-1].provider == chosen.name and sections[-1].screen == seg.screen:
            sections[-1].lines.extend([""] + body)
            continue
        sections.append(Section(chosen.name, chosen.label, seg.screen, body))
    info = dict(source or {})
    info["rotated"] = rotated
    info["size_recorded"] = size is not None
    return Transcript(sections, titles, info)
