"""What each terminal program draws around its conversation.

A provider recognises its program from screen text and says which parts of a
screen are chrome rather than content:

* ``header_rows``: fixed rows at the top that never scroll with the content;
* ``footer_start``: where the bottom chrome begins (prompt box, status line,
  key hints);
* ``is_chrome``: single lines that are status rather than conversation, such as
  spinners with elapsed-time counters. These are dropped from the output only.

Rules are deliberately narrow. A line that might be conversation is kept.
"""

from __future__ import annotations

import re
from collections import Counter

_BRAILLE = "⠀-⣿"


def _is_art(line: str) -> bool:
    """Braille pixel art: splash screens and animated spinners, not text."""
    marks = [c for c in line if not c.isspace()]
    if not marks:
        return False
    braille = sum(1 for c in marks if "\u2800" <= c <= "\u28ff")
    return braille >= 0.6 * len(marks)


def _bottom(lines: list[str], window: int) -> int:
    """Index of the first row in the last ``window`` non-blank rows."""
    end = len(lines)
    while end and not lines[end - 1].strip():
        end -= 1
    seen, i = 0, end
    while i > 0 and seen < window:
        i -= 1
        if lines[i].strip():
            seen += 1
    return i


class Provider:
    name = "shell"
    label = "shell"
    signatures: tuple[tuple[re.Pattern[str], int], ...] = ()
    chrome: tuple[re.Pattern[str], ...] = ()
    # Header rows that repeat conversation text (a pinned prompt) are committed
    # when they change and are not already in the transcript.
    header_is_content = False

    def header_rows(self, lines: list[str]) -> int:
        return 0

    def browsing(self, lines: list[str]) -> bool:
        """True while the person scrolls back through history: not new output."""
        return False

    def footer_start(self, lines: list[str]) -> int:
        return len(lines)

    def is_chrome(self, line: str) -> bool:
        return _is_art(line) or any(p.search(line) for p in self.chrome)

    def tidy(self, line: str) -> str:
        """Remove decoration drawn beside the text on the same row."""
        return line

    def content(self, lines: list[str]) -> tuple[list[str], int]:
        """The scrolling part of a screen, and how many rows were cut above it."""
        top = self.header_rows(lines)
        end = max(top, self.footer_start(lines))
        return lines[top:end], top

    def strip_frame(self, lines: list[str]) -> list[str]:
        return self.content(lines)[0]


class Tui(Provider):
    """An unrecognised full-screen program: keep everything."""

    name = "tui"
    label = "full-screen program"


class Codex(Provider):
    name = "codex"
    label = "Codex"
    signatures = (
        (re.compile(r"OpenAI Codex"), 5),
        (re.compile(r"esc to interrupt\)"), 2),
        (re.compile(r"^\s*• (?:Working|Ran|Explored|Edited|Updated Plan)\b"), 2),
        (re.compile(r"^\s*Worked for \d"), 2),
        (re.compile(r"ctrl\+t to expand"), 2),
        (re.compile(r"^› "), 1),
    )
    chrome = (
        re.compile(r"^\s*• Working(?: \(\d.*\)|\s{2,}\d)"),
        re.compile(r"· /ps to view · /stop to close\s*$"),
        re.compile(r"^\s*└ Tip: "),
        re.compile(r"^\s*(?:← for agents · )?\? for shortcuts"),
        re.compile(r"^\s*\d+% context left"),
    )
    header_is_content = True
    _composer = re.compile(r"^› ")
    _popup = re.compile(r"^(?:› |  )/\w+(?: {2,}\S|\s*$)")
    _status = re.compile(r"^\s*(?:• Working \(|└ Tip: |■ |⚠ )")

    _back = re.compile(r"↓ Back to bottom")

    def browsing(self, lines: list[str]) -> bool:
        return any(self._back.search(line) for line in lines[_bottom(lines, 4):])

    def header_rows(self, lines: list[str]) -> int:
        # Codex keeps the top row for the latest prompt, pinned while its reply
        # scrolls underneath; the row is blank until there is one to pin.
        if not lines:
            return 0
        if not lines[0].strip():
            return 1
        # A prompt on the top row is pinned unless it is the composer itself.
        return 1 if self._composer.match(lines[0]) and self._composer_row(lines) > 0 else 0

    def _composer_row(self, lines: list[str]) -> int:
        for i in range(len(lines) - 1, _bottom(lines, 12) - 1, -1):
            if self._composer.match(lines[i]):
                return i
        return -1

    def footer_start(self, lines: list[str]) -> int:
        cut = self._composer_row(lines)
        if cut < 0:
            return len(lines)
        while cut > 0 and (not lines[cut - 1].strip() or self._popup.match(lines[cut - 1])
                           or self._status.match(lines[cut - 1])):
            cut -= 1
        return cut


class Claude(Provider):
    name = "claude"
    label = "Claude Code"
    signatures = (
        (re.compile(r"Claude Code"), 5),
        (re.compile(r"⏵⏵ "), 3),
        (re.compile(r"shift\+tab to cycle"), 2),
        (re.compile(r"^● "), 1),
        (re.compile(r"^\s+⎿ "), 2),
        (re.compile(r"^[·✢✳✶✻✽*] \S+… \("), 2),
    )
    chrome = (
        re.compile(r"^[·✢✳✶✻✽✺*] [A-Z][\w-]*…(?: \(.*\))?\s*$"),
        re.compile(r"^\s*⏵⏵ "),
        re.compile(r"^\s*\(ctrl\+b to run in background\)\s*$"),
        re.compile(r"^\s*(?:← for agents · )?\? for shortcuts"),
        re.compile(r"^\s*⎿\s+Tip: "),
    )
    _rule = re.compile(r"^─{10,}")
    _prompt = re.compile(r"^❯")
    # Shown above the prompt while the person scrolls back through history.
    _back = re.compile(r"(?:Jump to bottom|new messages?) \(ctrl\+End\) ↓")

    def browsing(self, lines: list[str]) -> bool:
        return any(self._back.search(line) for line in lines[_bottom(lines, 8):])

    _spinner = re.compile(r"^(?:[·✢✳✶✻✽✺*] \S|\s+⎿\s+Tip: )")

    def footer_start(self, lines: list[str]) -> int:
        start = _bottom(lines, 10)
        for i in range(start, len(lines) - 1):
            if self._rule.match(lines[i]) and self._prompt.match(lines[i + 1]):
                cut = i
                while cut > 0 and (not lines[cut - 1].strip() or self._spinner.match(lines[cut - 1])):
                    cut -= 1
                return cut
        return len(lines)


class Grok(Provider):
    name = "grok"
    label = "Grok"
    signatures = (
        (re.compile(r"Grok (?:Build|\d)"), 5),
        (re.compile(r"Shift\+Tab:mode"), 4),
        (re.compile(r"Ctrl\+\.:shortcuts"), 3),
        (re.compile(r"^\s+◆ (?:Thought for|Run |Edit |Read |Creating )"), 2),
        (re.compile(r"always-approve"), 2),
    )
    chrome = (
        re.compile(rf"^\s*[{_BRAILLE}] .*(?:…|\[stop\])"),
        re.compile(r"Shift\+Tab:mode"),
        re.compile(r"\d+(?:\.\d+)?K / \d+(?:\.\d+)?K(?:\s*│\s*\[\w[\w ]*\])?\s*$"),
        re.compile(r"^\s*❯ /\w+ [A-Z]"),
        re.compile(r"^\s*█\s*$"),
    )
    _header = re.compile(
        r"^(?:\s*$|\s+\S+ [~/]\S*(?:\s{2,}\[\w[\w ]*\])?\s*$"
        r"|.*\d+(?:\.\d+)?K / \d+(?:\.\d+)?K(?:\s*│\s*\[\w[\w ]*\])?\s*$)")
    _box = re.compile(r"^\s*(?:╭─|─{10,})")
    # Grok's right gutter: message times, the scrollbar thumb, short counters.
    _gutter = re.compile(r"(?:\s{6,}(?:\d{1,2}:\d{2} [AP]M|[█▐▌])|\s{20,}\d{1,4})\s*$")

    def tidy(self, line: str) -> str:
        return self._gutter.sub("", line)
    _spinner = re.compile(rf"^\s*(?:[{_BRAILLE}] |█\s*$)")

    def header_rows(self, lines: list[str]) -> int:
        n = 0
        while n < min(3, len(lines)) and self._header.match(lines[n]):
            n += 1
        return n

    def footer_start(self, lines: list[str]) -> int:
        start = _bottom(lines, 8)
        for i in range(start, len(lines)):
            if self._box.match(lines[i]):
                cut = i
                while cut > 0 and (not lines[cut - 1].strip() or self._spinner.match(lines[cut - 1])):
                    cut -= 1
                return cut
        return len(lines)


class Omp(Provider):
    name = "omp"
    label = "omp"
    signatures = (
        (re.compile(r"oh-my-pi|\bomp v\d"), 5),
        (re.compile(r"⟦Wall: [\d.]+s"), 3),
        (re.compile(r"^├─── Output ─"), 3),
        (re.compile(rf"^\s*(?:[{_BRAILLE}]|π) .* > .*▶"), 4),
    )
    chrome = (
        re.compile(rf"^\s*(?:[{_BRAILLE}]|π)(?: \d+\w*)? > .*▶"),
        re.compile(r"^\s+⎋ "),
        re.compile(r"^╰─\s*$"),
    )
    _status = re.compile(rf"^\s*(?:[{_BRAILLE}]|π)(?: \d+\w*)? > ")
    _working = re.compile(r"^\s+⎋ ")

    def footer_start(self, lines: list[str]) -> int:
        start = _bottom(lines, 6)
        for i in range(start, len(lines)):
            if self._status.match(lines[i]):
                cut = i
                while cut > 0 and (not lines[cut - 1].strip() or self._working.match(lines[cut - 1])):
                    cut -= 1
                return cut
        return len(lines)


PROVIDERS: dict[str, Provider] = {
    p.name: p for p in (Provider(), Tui(), Codex(), Claude(), Grok(), Omp())
}
DETECTABLE = tuple(PROVIDERS[name] for name in ("codex", "claude", "grok", "omp"))


def provider_named(name: str) -> Provider:
    try:
        return PROVIDERS[name]
    except KeyError:
        raise ValueError(f"unknown provider {name!r}; choose from {', '.join(PROVIDERS)}") from None


def detect_scores(lines: list[str]) -> Counter:
    """Signature weights found on one screen, by provider name."""
    scores: Counter = Counter()
    for provider in DETECTABLE:
        for pattern, weight in provider.signatures:
            if any(pattern.search(line) for line in lines):
                scores[provider.name] += weight
    return scores
