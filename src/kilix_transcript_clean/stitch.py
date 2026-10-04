"""Turn a sequence of settled screens into one linear transcript.

The committed transcript plus the screen currently visible are treated as one
document, with the screen as a window onto its end. Each new frame is anchored
where its lines best match that document:

* the window moved down (content scrolled up): the lines that left the top are
  committed;
* the window moved up (an earlier block collapsed or the view scrolled back):
  lines committed too early return to the window, so the redrawn version wins;
* nothing anchors: the screen was replaced. If the new screen holds the same
  words reflowed (a resize), nothing is committed; otherwise the old screen is
  committed whole, minus the program's own chrome.

Lines leave through a scroll (line feed or scroll region) are committed as they
go, so output faster than the frame rate is not lost.
"""

from __future__ import annotations

import difflib
from collections import Counter
from dataclasses import dataclass, field

from .providers import Provider, detect_scores, provider_named
from .screen import Screen


@dataclass
class Segment:
    """A stretch of the transcript drawn on one screen buffer."""

    start: int
    screen: str  # "main" or "alt"
    hits: Counter = field(default_factory=Counter)
    frames: int = 0

    @property
    def provider(self) -> Provider:
        if self.hits:
            name, score = self.hits.most_common(1)[0]
            if score >= 2:
                return provider_named(name)
        return provider_named("shell" if self.screen == "main" else "tui")


@dataclass
class _Pending:
    lines: list[str]   # the content region of the screen
    held: int = 0      # leading rows already committed (a restored main screen)
    top: int = 0       # screen rows above the content region
    header: tuple[str, ...] = ()


def _nonblank(lines: list[str]) -> int:
    return sum(1 for line in lines if line.strip())


def _words(lines: list[str]) -> list[str]:
    return " ".join(lines).split()


class Stitcher:
    TAIL = 200          # committed lines kept per screen for anchoring
    SAMPLE_EVERY = 25   # frames between provider-detection samples

    def __init__(self, provider: str | None = None) -> None:
        self.forced = provider_named(provider) if provider else None
        self.doc: list[tuple[str, int]] = []
        self.segments: list[Segment] = [Segment(0, "main")]
        self.pending: dict[str, _Pending | None] = {"main": None, "alt": None}
        self.tail: dict[str, list[str]] = {"main": [], "alt": []}
        self.frozen_main: list[str] | None = None
        self.floor = 0
        # Lines scrolled out of a region that starts below the content top.
        # Programs often repaint such a line just above the region, so it is
        # only committed if the next frame shows it really left.
        self.limbo: dict[str, list[str]] = {"main": [], "alt": []}
        self.warm = True

    # -- helpers -----------------------------------------------------------
    @property
    def segment(self) -> Segment:
        return self.segments[-1]

    def provider_for(self, segment: Segment) -> Provider:
        return self.forced or segment.provider

    def _commit(self, screen: str, lines: list[str]) -> None:
        index = len(self.segments) - 1
        for line in lines:
            self.doc.append((line, index))
        tail = self.tail[screen]
        tail.extend(lines)
        del tail[: max(0, len(tail) - self.TAIL)]

    def _flush(self, screen: str, boundary: bool = True) -> None:
        """Commit the whole screen. ``boundary`` stops later frames reaching back
        past it, for when the screen's contents are gone for good."""
        pending = self.pending[screen]
        if pending is None:
            return
        lines = pending.lines[pending.held:]
        while lines and not lines[-1].strip():
            lines.pop()
        # Only a full-screen program repaints a conversation it already showed;
        # the main screen's own history is tracked row by row instead.
        self._commit(screen, self._unseen(lines) if screen == "alt" else lines)
        if boundary:
            self.floor = len(self.doc)
        self.pending[screen] = None

    def _unseen(self, block: list[str]) -> list[str]:
        """Drop the part of a whole screen that repeats the transcript's end.

        A program that repaints its conversation (switching screens, a resize
        it could not redraw in place) shows lines that are already committed.
        An exact overlap between the transcript's end and the screen's start
        is trusted. Failing that, a looser match must cover most of the
        screen's distinct lines, so rows that merely look alike (wrapped
        fragments, separators) never count as a repeat.
        """
        window = [text for text, _ in self.doc[-(2 * len(block) + 40):]]
        if _nonblank(block) < 2 or not window:
            return block
        for k in range(min(len(block), len(window)), 1, -1):
            if window[-k:] == block[:k] and len({line for line in block[:k] if line.strip()}) >= 2:
                return block[k:]
        distinct = {line for line in block if line.strip()}
        if len(distinct) < 5:
            return block
        matcher = difflib.SequenceMatcher(None, window, block, autojunk=False)
        blocks = [m for m in matcher.get_matching_blocks() if m.size]
        matched = {line for m in blocks for line in block[m.b:m.b + m.size] if line.strip()}
        if len(matched) < 0.6 * len(distinct):
            return block
        return block[max(m.b + m.size for m in blocks):]

    def _sample(self, lines: list[str]) -> None:
        seg = self.segment
        seg.frames += 1
        if seg.frames <= 40 or seg.frames % self.SAMPLE_EVERY == 0:
            seg.hits.update(detect_scores(lines))

    def _new_segment(self, screen: str) -> None:
        if self.segment.start == len(self.doc) and not self.segment.frames:
            self.segment.screen = screen
            return
        self.segments.append(Segment(len(self.doc), screen))

    # -- screen callbacks --------------------------------------------------
    def on_scroll(self, screen: Screen, line: str) -> None:
        name = "alt" if screen.in_alt else "main"
        pending = self.pending[name]
        if pending is None:
            self._commit(name, [line])
            return
        row = screen.top - pending.top
        if row < 0:
            return  # a fixed header row scrolled, not content
        if screen.top > 0:
            self.limbo[name].append(line)
            return
        if row < len(pending.lines):
            rows = pending.lines
            del rows[row]
            rows.insert(min(screen.bottom - pending.top, len(rows)), "")
            if row < pending.held:
                pending.held -= 1
                return
        self._commit(name, [line])

    def _settle_limbo(self, name: str, old: list[str], new: list[str]) -> None:
        limbo = self.limbo[name]
        if not limbo:
            return
        visible = set(old) | set(new)
        self._commit(name, [line for line in limbo if line not in visible])
        limbo.clear()

    def on_frame(self, screen: Screen, why: str) -> None:
        self.warm = screen.warm or why == "eof"
        name = "alt" if screen.in_alt else "main"
        if why == "alt-on":
            # The main screen is hidden, not lost: commit what it shows now and
            # remember it so the restored screen is not committed twice.
            self._flush_main_for_alt(screen)
            self._new_segment("alt")
            return
        if why == "alt-off":
            # The program's last output may never have been synchronized:
            # take the screen as it stands before it disappears.
            lines = screen.lines()
            if screen.in_alt and not self.provider_for(self.segment).browsing(lines):
                result = self._advance("alt", *self._content(lines))
                if result is not None:
                    self.pending["alt"] = result
            self._flush("alt")
            self._new_segment("main")
            if self.frozen_main is not None:
                rows = list(self.frozen_main)
                self.pending["main"] = _Pending(rows, held=len(rows))
                self.frozen_main = None
            return
        lines = screen.lines()
        if why not in ("clear", "eof") and self.provider_for(self.segment).browsing(lines):
            return
        new = self._content(lines)
        before = self.pending[name]
        old = list(before.lines) if before else []
        result = self._advance(name, *new)
        if why == "clear":
            # Whatever the screen shows is about to be erased: it is final,
            # even if no frame was taken since it was drawn (a shell `clear`).
            self._settle_limbo(name, old, new[0])
            if result is not None:
                self.pending[name] = result
            self._flush(name)
            return
        self._settle_limbo(name, old, new[0])
        if result is not None:
            self.pending[name] = result
        if why == "eof":
            self._flush(name)
            if name == "alt":
                self._flush("main")

    def _content(self, lines: list[str]) -> tuple[list[str], int, tuple[str, ...]]:
        self._sample(lines)
        body, top = self.provider_for(self.segment).content(lines)
        return body, top, tuple(lines[:top])

    def _header_changed(self, name: str, old: tuple[str, ...], new: tuple[str, ...]) -> None:
        if old == new or not self.provider_for(self.segment).header_is_content:
            return
        recent = set(self.tail[name][-80:])
        missing = [line for line in old if line.strip() and line not in recent]
        if missing:
            self._commit(name, missing)

    def _flush_main_for_alt(self, screen: Screen) -> None:
        if not screen.in_alt:
            result = self._advance("main", *self._content(screen.lines()))
            if result is not None:
                self.pending["main"] = result
        pending = self.pending["main"]
        if pending is None:
            self.frozen_main = None
            return
        self.frozen_main = list(pending.lines)
        self._commit("main", pending.lines[pending.held:])
        self.floor = len(self.doc)
        self.pending["main"] = None

    # -- alignment -----------------------------------------------------------
    def _advance(self, name: str, new: list[str], top: int = 0,
                 header: tuple[str, ...] = ()) -> _Pending | None:
        pending = self.pending[name]
        if pending is None:
            return _Pending(list(new), 0, top, header) if _nonblank(new) else None
        self._header_changed(name, pending.header, header)
        result = self._move(name, pending, new, top)
        result.header = header
        return result

    def _move(self, name: str, pending: _Pending, new: list[str], top: int) -> _Pending:
        old = pending.lines
        if old == new:
            pending.top = top
            return pending
        if not _nonblank(old):
            return _Pending(list(new), 0, top)
        if len(old) == len(new):
            # A few rows redrawn in place (streaming text, a spinner) is not a
            # scroll; anchoring them could only invent movement.
            changed = sum(1 for a, b in zip(old, new) if a != b)
            if changed <= max(2, _nonblank(new) // 4):
                return _Pending(list(new), self._still_held(pending, new), top)
        tail = [] if pending.held else self.tail[name][-len(old):]
        # Never reach back past the last flush: those lines are settled.
        tail = tail[len(tail) - min(len(tail), len(self.doc) - self.floor):]
        reference = tail + old
        anchor = self._anchor(reference, new, old)
        if anchor is None:
            return self._replace(name, pending, new, top)
        start, shift = anchor
        offset = shift - len(tail)  # where new[0] sits relative to old[0]
        if offset > 0:
            # Rows above the anchored block that did not change are fixed (a
            # blank margin, a pinned prompt); the lines that left are below them.
            lead = 0
            while lead < start and lead < len(old) and old[lead] == new[lead]:
                lead += 1
            gone = old[lead:lead + offset]
            skip = max(0, min(pending.held, lead + offset) - lead)
            self._commit(name, gone[skip:])
            return _Pending(list(new), max(0, pending.held - offset), top)
        if offset < 0:
            # Lines committed too early are back on screen; the newer render wins.
            back = min(-offset, len(tail))
            del self.doc[len(self.doc) - back:]
            del self.tail[name][len(self.tail[name]) - back:]
        return _Pending(list(new), self._still_held(pending, new), top)

    @staticmethod
    def _still_held(pending: _Pending, new: list[str]) -> int:
        """Leading rows of a restored screen that are still the committed ones."""
        held = 0
        while held < pending.held and held < len(new) and pending.lines[held] == new[held]:
            held += 1
        return held

    @staticmethod
    def _anchor(reference: list[str], new: list[str], old: list[str]) -> tuple[int, int] | None:
        """Where ``new`` sits in ``reference``: (first anchored row, shift).

        The window's top rows decide its position, so the topmost run of
        matching non-blank lines wins. A larger match further down is more
        likely repeated text (a table shown twice) than the window's position.
        """
        need = max(1, min(2, _nonblank(old), _nonblank(new)))
        where: dict[str, list[int]] = {}
        for i, line in enumerate(reference):
            if line.strip():
                where.setdefault(line, []).append(i)
        expected = len(reference) - len(old)
        for b, line in enumerate(new):
            best = None
            for a in where.get(line, ()):
                if a > 0 and b > 0 and reference[a - 1] == new[b - 1] and reference[a - 1].strip():
                    continue  # inside a run that starts higher up
                size = 0
                while (a + size < len(reference) and b + size < len(new)
                       and reference[a + size] == new[b + size]):
                    size += 1
                weight = _nonblank(new[b:b + size])
                if weight < need:
                    continue
                key = (weight, -abs((a - b) - expected))
                if best is None or key > best[0]:
                    best = (key, (b, a - b))
            if best is not None:
                return best[1]
        return None

    def _replace(self, name: str, pending: _Pending, new: list[str], top: int) -> _Pending:
        if not self.warm:
            return _Pending(list(new), 0, top)  # a partial screen, not content
        old_words = _words(pending.lines[pending.held:])
        new_words = _words(new)
        matcher = difflib.SequenceMatcher(None, old_words, new_words, autojunk=False)
        if old_words and matcher.quick_ratio() >= 0.6 and matcher.ratio() >= 0.6:
            return _Pending(list(new), 0, top)  # reflowed by a resize
        # The window jumped further than one screen: what it showed is
        # committed, but a later collapse may still pull it back.
        self._flush(name, boundary=False)
        return _Pending(list(new), 0, top)

    # -- result --------------------------------------------------------------
    def finish(self) -> None:
        for name in ("alt", "main"):
            self._flush(name)
