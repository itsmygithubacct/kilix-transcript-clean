"""A small terminal screen that replays recorded PTY output.

Only what affects the text a person saw is modelled: cursor movement, erasing,
scroll regions, insert/delete, the alternate screen and window titles.
Colours and other attributes are ignored.

The recorder does not store the terminal size, so the screen is a fixed number
of rows and a very wide virtual width. Text written past the real right margin
stays on one row instead of wrapping, which keeps long lines whole.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Callable

# One token: a CSI, OSC, string sequence (DCS/APC/PM/SOS), other escape,
# single control byte, or a run of printable bytes.
TOKEN = re.compile(
    rb"\x1b\[[\x30-\x3f]*[\x20-\x2f]*[\x40-\x7e]"
    rb"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"
    rb"|\x1b[P_^X][^\x1b]*(?:\x1b\\)?"
    rb"|\x1b[\x20-\x2f]*[\x30-\x7e]"
    rb"|\x1b"
    rb"|[\x00-\x1a\x1c-\x1f\x7f]"
    rb"|[^\x00-\x1f\x7f]+"
)

# kitty-pty-broker replaces kitty graphics with this note, framed by CRLF.
# It never reached the screen, so replaying it would move the cursor.
BROKER_NOTE = re.compile(rb"\r?\n?\[kitty-pty-broker: [^\]\r\n]{0,120}\]\r?\n?")

# A rotated transcript can begin inside an escape sequence.
_PARTIAL_HEAD = re.compile(rb"[\x30-\x3f]{0,64}[\x20-\x2f]{0,4}[\x40-\x7e]")

FrameReason = str  # "sync", "clear", "alt-on", "alt-off", "resize", "idle", "eof"

# kitty-pty-broker records the pane size as a private APC that terminals
# ignore: ESC _ kilix-transcript;rows=R;cols=C[;rotated=1] ESC \
SIZE_RECORD = re.compile(rb"\x1b_kilix-transcript;([^\x1b]{0,200})\x1b\\")

# Marks a row that continues the one above because text ran past the right
# margin. Rendering joins such rows back into the line the program wrote.
CONTINUATION = "\x00"


class Row(list):
    """One screen row of cells, remembering whether it was reached by autowrap."""

    __slots__ = ("wrapped",)

    def __init__(self, cells=()):
        super().__init__(cells)
        self.wrapped = False


def parse_size_record(body: bytes) -> tuple[int, int, bool] | None:
    """(rows, cols, rotated) from a size record's body, if it is well formed."""
    fields = {}
    for part in body.decode("ascii", "replace").split(";"):
        key, sep, value = part.partition("=")
        if sep:
            fields[key] = value
    try:
        rows, cols = int(fields["rows"]), int(fields["cols"])
    except (KeyError, ValueError):
        return None
    if not (1 <= rows <= 1000 and 1 <= cols <= 4000):
        return None
    return rows, cols, fields.get("rotated") == "1"


# Characters a terminal acts on or that reorder text, never shown as text:
# C1 controls and the bidirectional embedding, override and isolate marks.
# Keeping them would let a transcript display differently from what it says.
_INVISIBLE = frozenset(chr(c) for c in range(0x80, 0xA0)) | frozenset(
    "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069\u200e\u200f\u061c")


def cell_width(ch: str) -> int:
    if unicodedata.combining(ch) or unicodedata.category(ch) in ("Mn", "Me", "Cf"):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def looks_rotated(data: bytes, limit: int = 8 << 20) -> bool:
    """Whether a recording probably lost its start to the size cap.

    kitty-pty-broker keeps the newest three quarters when a transcript reaches
    its limit (8 MiB by default), so a rotated file is at least that large, and
    usually starts inside an escape sequence or a character.
    """
    return len(data) >= (limit * 3) // 4 - 4096 or trim_partial_head(data) != data


def trim_partial_head(data: bytes) -> bytes:
    """Drop the tail of an escape sequence or UTF-8 character at the start."""
    i = 0
    while i < len(data) and i < 4 and 0x80 <= data[i] < 0xC0:
        i += 1
    data = data[i:]
    if data[:1] != b"\x1b":
        m = _PARTIAL_HEAD.match(data)
        if m and m.end() > 1 and re.search(rb"[;\d]", data[: m.end()]):
            data = data[m.end():]
    return data


class Screen:
    """Replays terminal output and reports frames, scrolled lines and titles."""

    def __init__(
        self,
        rows: int,
        cols: int = 1000,
        on_frame: Callable[["Screen", FrameReason], None] | None = None,
        on_scroll: Callable[["Screen", str], None] | None = None,
        on_title: Callable[[str], None] | None = None,
        idle_bytes: int = 4096,
        warm: bool = True,
        newline_returns: bool = False,
    ) -> None:
        self.rows = max(2, rows)
        self.cols = max(2, cols)
        self.on_frame = on_frame or (lambda screen, why: None)
        self.on_scroll = on_scroll or (lambda screen, line: None)
        self.on_title = on_title or (lambda title: None)
        self.idle_bytes = idle_bytes
        self.main: list[list[str]] = self._blank()
        self.grid = self.main
        self.in_alt = False
        self.y = self.x = 0
        self.top, self.bottom = 0, self.rows - 1
        self.saved = (0, 0)
        self.saved_main = (0, 0)
        self.wrap_pending = False
        self.autowrap = True
        self.dirty = False
        self.in_sync = False
        self._since_frame = 0
        # A recording cut at the front starts with a screen whose earlier
        # contents are unknown. It is "warm" once most rows have been drawn
        # or the program cleared it; until then frames are only partial.
        self.warm = warm
        # A line feed only moves down. Text written without a PTY (a plain log)
        # has no carriage returns, and there a line feed starts a new line.
        self.newline_returns = newline_returns
        self._touched: set[int] = set()
        self._scrolled_full = False

    # -- state -------------------------------------------------------------
    def _blank(self) -> list[Row]:
        return [Row() for _ in range(self.rows)]

    def _text(self, row: Row, previous_full: bool) -> str:
        text = "".join(row).rstrip()
        return CONTINUATION + text if row.wrapped and previous_full else text

    def lines(self) -> list[str]:
        # The top row may continue a line that has already scrolled away.
        out, full = [], self._scrolled_full
        for row in self.grid:
            out.append(self._text(row, full))
            full = len(row) >= self.cols
        return out

    def _frame(self, why: FrameReason) -> None:
        if self.dirty or why in ("alt-on", "alt-off", "eof"):
            self.on_frame(self, why)
        self.dirty = False
        self._since_frame = 0

    # -- scrolling ---------------------------------------------------------
    def scroll_up(self, n: int = 1) -> None:
        for _ in range(min(n, self.bottom - self.top + 1)):
            above = self.grid[self.top - 1] if self.top > 0 else None
            row = self.grid.pop(self.top)
            self.grid.insert(self.bottom, Row())
            self.dirty = True
            full = self._scrolled_full if above is None else len(above) >= self.cols
            self._scrolled_full = len(row) >= self.cols
            self.on_scroll(self, self._text(row, full))

    def scroll_down(self, n: int = 1) -> None:
        for _ in range(min(n, self.bottom - self.top + 1)):
            self.grid.pop(self.bottom)
            self.grid.insert(self.top, Row())
            self.dirty = True

    def linefeed(self) -> None:
        self.wrap_pending = False
        if self.y == self.bottom:
            self.scroll_up()
        elif self.y < self.rows - 1:
            self.y += 1

    # -- text --------------------------------------------------------------
    def _known(self) -> None:
        self.warm = True
        self._touched.clear()
        self._scrolled_full = False

    def text(self, s: str) -> None:
        self.dirty = True
        self._since_frame += len(s)
        if not self.warm:
            self._touched.add(self.y)
            if len(self._touched) >= 0.6 * self.rows:
                self._known()
        row = self.grid[self.y]
        for ch in s:
            if ch in _INVISIBLE:
                continue
            w = cell_width(ch)
            if w == 0:
                if self.x > 0 and self.x <= len(row):
                    row[self.x - 1] += ch
                continue
            if self.wrap_pending or self.x + w > self.cols:
                if self.autowrap:
                    self.x = 0
                    self.linefeed()
                    row = self.grid[self.y]
                    row.wrapped = True
                else:
                    self.x = self.cols - w
                self.wrap_pending = False
            if len(row) < self.x + w:
                row.extend(" " * (self.x + w - len(row)))
            row[self.x] = ch
            if w == 2:
                row[self.x + 1] = ""
            self.x += w
            if self.x >= self.cols:
                self.x = self.cols - 1
                self.wrap_pending = True
        # Programs without synchronized output still need settled frames, but a
        # frame taken inside a synchronized update would be half drawn.
        if self._since_frame >= self.idle_bytes and not self.in_sync:
            self._frame("idle")

    # -- erasing -----------------------------------------------------------
    def _erase(self, row: list[str], start: int, end: int | None) -> None:
        end = len(row) if end is None else min(end, len(row))
        if start >= len(row):
            return
        if end >= len(row):
            del row[start:]
        else:
            row[start:end] = " " * (end - start)

    def erase_line(self, mode: int) -> None:
        self.dirty = True
        row = self.grid[self.y]
        if mode == 0:
            self._erase(row, self.x, None)
        elif mode == 1:
            self._erase(row, 0, self.x + 1)
        elif mode == 2:
            row.clear()
            row.wrapped = False

    def erase_display(self, mode: int) -> None:
        if mode in (2, 3):
            self._frame("clear")
            self.grid[:] = self._blank()
            self.dirty = True
            self._known()
            return
        self.erase_line(mode)
        span = range(self.y + 1, self.rows) if mode == 0 else range(0, self.y)
        for r in span:
            self.grid[r] = Row()

    # -- input -------------------------------------------------------------
    def feed(self, data: bytes) -> None:
        data = BROKER_NOTE.sub(b"", data)
        for m in TOKEN.finditer(data):
            token = m.group()
            first = token[0]
            if first >= 0x20 and first != 0x7F:
                self.text(token.decode("utf-8", "replace"))
            elif first == 0x1B:
                self.escape(token)
            else:
                self.control(first)

    def finish(self) -> None:
        self._frame("eof")

    def control(self, c: int) -> None:
        if c in (0x0A, 0x0B, 0x0C):
            self.linefeed()
            if self.newline_returns:
                self.x = 0
        elif c == 0x0D:
            self.x = 0
            self.wrap_pending = False
        elif c == 0x08:
            self.x = max(0, self.x - 1)
            self.wrap_pending = False
        elif c == 0x09:
            self.x = min(self.cols - 1, (self.x // 8 + 1) * 8)

    def escape(self, t: bytes) -> None:
        if len(t) < 2:
            return
        kind = t[1]
        if kind == 0x5B:
            self.csi(t)
        elif kind == 0x5F:
            m = SIZE_RECORD.fullmatch(t)
            size = parse_size_record(m.group(1)) if m else None
            if size:
                self.resize(size[0], size[1])
        elif kind == 0x5D:
            body = t[2:]
            if body.endswith(b"\x1b\\"):
                body = body[:-2]
            body = body.rstrip(b"\x07")
            text = body.decode("utf-8", "replace")
            if text[:2] in ("0;", "2;"):
                self.on_title(text[2:])
        elif t == b"\x1b7":
            self.saved = (self.y, self.x)
        elif t == b"\x1b8":
            self.y, self.x = self.saved
            self.wrap_pending = False
        elif t == b"\x1bM":
            if self.y == self.top:
                self.scroll_down()
            else:
                self.y = max(0, self.y - 1)
        elif t == b"\x1bD":
            self.linefeed()
        elif t == b"\x1bE":
            self.x = 0
            self.linefeed()
        elif t == b"\x1bc":
            self._frame("clear")
            self._known()
            self.grid[:] = self._blank()
            self.y = self.x = 0
            self.top, self.bottom = 0, self.rows - 1

    def csi(self, t: bytes) -> None:
        final = chr(t[-1])
        body = t[2:-1].decode("latin-1")
        marker = body[:1] if body[:1] in "?<>=" else ""
        body = body[len(marker):].rstrip(" !\"#$%&'()*+,-./")
        params = [int(p) if p.isdigit() else 0 for p in re.split(r"[;:]", body)] if body else []

        def arg(i: int, default: int = 1) -> int:
            return params[i] if len(params) > i and params[i] else default

        if marker:
            if marker == "?" and final in "hl":
                on = final == "h"
                for mode in params:
                    if mode in (1049, 1047, 47):
                        self.set_alt(on, mode == 1049)
                    elif mode == 7:
                        self.autowrap = on
                    elif mode == 2026:
                        self.in_sync = on
                        if not on:
                            self._frame("sync")
            return
        if final not in "m":
            self.wrap_pending = False
        if final in "Hf":
            self.y = min(self.rows - 1, arg(0) - 1)
            self.x = min(self.cols - 1, arg(1) - 1)
        elif final == "A":
            floor = self.top if self.y >= self.top else 0
            self.y = max(floor, self.y - arg(0))
        elif final in "Be":
            ceiling = self.bottom if self.y <= self.bottom else self.rows - 1
            self.y = min(ceiling, self.y + arg(0))
        elif final in "Ca":
            self.x = min(self.cols - 1, self.x + arg(0))
        elif final == "D":
            self.x = max(0, self.x - arg(0))
        elif final == "E":
            self.y = min(self.rows - 1, self.y + arg(0))
            self.x = 0
        elif final == "F":
            self.y = max(0, self.y - arg(0))
            self.x = 0
        elif final in "G`":
            self.x = min(self.cols - 1, arg(0) - 1)
        elif final == "d":
            self.y = min(self.rows - 1, arg(0) - 1)
        elif final == "K":
            self.erase_line(arg(0, 0))
        elif final == "J":
            self.erase_display(arg(0, 0))
        elif final == "X":
            self.dirty = True
            self._erase(self.grid[self.y], self.x, self.x + arg(0))
        elif final == "P":
            self.dirty = True
            del self.grid[self.y][self.x:self.x + arg(0)]
        elif final == "@":
            row = self.grid[self.y]
            if self.x < len(row):
                self.dirty = True
                row[self.x:self.x] = " " * arg(0)
        elif final == "L":
            if self.top <= self.y <= self.bottom:
                self.dirty = True
                for _ in range(min(arg(0), self.bottom - self.y + 1)):
                    self.grid.pop(self.bottom)
                    self.grid.insert(self.y, Row())
        elif final == "M":
            if self.top <= self.y <= self.bottom:
                self.dirty = True
                for _ in range(min(arg(0), self.bottom - self.y + 1)):
                    self.grid.pop(self.y)
                    self.grid.insert(self.bottom, Row())
        elif final == "S":
            self.scroll_up(arg(0))
        elif final == "T":
            self.scroll_down(arg(0))
        elif final == "r":
            top, bottom = arg(0) - 1, arg(1, self.rows) - 1
            if 0 <= top < bottom < self.rows:
                self.top, self.bottom = top, bottom
            else:
                self.top, self.bottom = 0, self.rows - 1
            self.y = self.x = 0
        elif final == "s":
            self.saved = (self.y, self.x)
        elif final == "u":
            self.y, self.x = self.saved

    def resize(self, rows: int, cols: int) -> None:
        """Change the screen size, as the recorded pane did.

        A shorter screen drops blank rows below the cursor first, then pushes
        rows off the top, the way kitty moves them into scrollback. Programs
        redraw after a resize, so nothing is reflowed here.
        """
        rows, cols = max(2, rows), max(2, cols)
        if (rows, cols) == (self.rows, self.cols):
            return
        self._frame("resize")
        for grid in {id(self.main): self.main, id(self.grid): self.grid}.values():
            current = grid is self.grid
            while len(grid) > rows:
                if not grid[-1] and (not current or self.y < len(grid) - 1):
                    grid.pop()
                    continue
                row = grid.pop(0)
                if current:
                    self.y = max(0, self.y - 1)
                    if not self.in_alt:
                        self.on_scroll(self, "".join(row).rstrip())
            while len(grid) < rows:
                grid.append(Row())
        self.rows, self.cols = rows, cols
        self.top, self.bottom = 0, rows - 1
        self.y, self.x = min(self.y, rows - 1), min(self.x, cols - 1)
        self.saved = (min(self.saved[0], rows - 1), min(self.saved[1], cols - 1))
        self.saved_main = (min(self.saved_main[0], rows - 1), min(self.saved_main[1], cols - 1))
        self.wrap_pending = False
        self.dirty = True

    def set_alt(self, on: bool, save_cursor: bool) -> None:
        if on == self.in_alt:
            return
        self._frame("alt-on" if on else "alt-off")
        if on:
            if save_cursor:
                self.saved_main = (self.y, self.x)
            self.grid = self._blank()
            self._known()
        else:
            self.grid = self.main
            if save_cursor:
                self.y, self.x = self.saved_main
        self.in_alt = on
        self.top, self.bottom = 0, self.rows - 1
        self.dirty = True


def infer_rows(data: bytes, default: int = 24) -> int:
    """The tallest row any absolute cursor move or scroll region names."""
    rows = [int(m) for m in re.findall(rb"\x1b\[(\d{1,4});\d{1,4}[Hf]", data)]
    rows += [int(m) for m in re.findall(rb"\x1b\[\d{1,4};(\d{1,4})r", data)]
    rows += [int(m) for m in re.findall(rb"\x1b\[(\d{1,4})d", data)]
    rows = [r for r in rows if r <= 1000]
    return max(rows) if rows else default
