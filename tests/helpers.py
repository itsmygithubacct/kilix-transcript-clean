"""Byte streams shaped like the programs kilix records."""

from __future__ import annotations

ESC = "\x1b"


def frame(rows: list[str], height: int, top: int = 0) -> str:
    """One synchronized full repaint: every row positioned absolutely."""
    out = [f"{ESC}[?2026h"]
    for i in range(height):
        text = rows[i] if i < len(rows) else ""
        out.append(f"{ESC}[{top + i + 1};1H{text}{ESC}[K")
    out.append(f"{ESC}[?2026l")
    return "".join(out)


def window(document: list[str], height: int) -> list[str]:
    """The last ``height`` lines of a document, as a bottom-pinned TUI shows it."""
    return document[-height:] if len(document) > height else list(document)


def tui_session(states: list[list[str]], height: int, footer: list[str] = (),
                header: list[str] = ()) -> bytes:
    """A full-screen program that repaints a growing conversation.

    Each state is the whole conversation at one moment; the screen shows its
    end, between optional fixed header and footer rows.
    """
    body = height - len(header) - len(footer)
    out = [f"{ESC}[?1049h"]
    for document in states:
        rows = list(header) + window(document, body)
        rows += [""] * (height - len(footer) - len(rows)) + list(footer)
        out.append(frame(rows, height))
    out.append(f"{ESC}[?1049l")
    return "".join(out).encode()


def body(transcript) -> list[str]:
    """Transcript lines without blank lines, for exact comparisons."""
    return [line for line in transcript.lines if line.strip()]
