import unittest

from kilix_transcript_clean import clean_bytes

from helpers import ESC, body, frame, tui_session


def doc(n: int, prefix: str = "line") -> list[str]:
    return [f"{prefix} {i:03d} {'word ' * (i % 4)}".rstrip() for i in range(n)]


def growth(document: list[str], step: int = 1) -> list[list[str]]:
    return [document[:i] for i in range(step, len(document) + 1, step)] + [document]


class ShellOutput(unittest.TestCase):
    def test_output_longer_than_the_screen_is_kept_in_order(self):
        lines = doc(60)
        data = ("\r\n".join(lines) + "\r\n").encode()
        self.assertEqual(body(clean_bytes(data, rows=10)), lines)

    def test_screen_cleared_without_a_frame_is_kept(self):
        # A shell `clear`: no synchronized update, no quiet stretch, then gone.
        data = b"$ make\r\nbuilt ok\r\n$ clear\r\n\x1b[H\x1b[2J$ ls\r\nREADME\r\n"
        self.assertEqual(body(clean_bytes(data, rows=10)),
                         ["$ make", "built ok", "$ clear", "$ ls", "README"])

    def test_plain_text_without_carriage_returns(self):
        lines = ["first", "  indented", "last"]
        self.assertEqual(body(clean_bytes("\n".join(lines).encode() + b"\n", rows=2)), lines)

    def test_line_feed_alone_only_moves_down_in_terminal_output(self):
        # With CRs present the recording is terminal output, where LF keeps
        # the column: raw-mode programs rely on it.
        result = clean_bytes(b"ab\r\ncd\nef\r\n", rows=5)
        self.assertEqual(body(result), ["ab", "cd", "  ef"])

    def test_progress_redrawn_with_carriage_return_keeps_final_state(self):
        data = b"start\r\n" + b"".join(b"\r%3d%%" % i for i in range(0, 101, 10)) + b"\r\ndone\r\n"
        self.assertEqual(body(clean_bytes(data, rows=5)), ["start", "100%", "done"])


class FullScreenPrograms(unittest.TestCase):
    def test_conversation_scrolling_one_line_at_a_time(self):
        lines = doc(80)
        result = clean_bytes(tui_session(growth(lines), height=12))
        self.assertEqual(body(result), lines)

    def test_conversation_jumping_many_lines_per_frame(self):
        lines = doc(120)
        result = clean_bytes(tui_session(growth(lines, step=9), height=15))
        self.assertEqual(body(result), lines)

    def test_fixed_rows_above_the_conversation_are_not_mistaken_for_it(self):
        # Codex keeps its top row for a pinned prompt; the line just below it
        # used to be lost on every scroll.
        lines = doc(70)
        data = tui_session(growth(lines, step=4), height=12, header=["", ""])
        self.assertEqual(body(clean_bytes(data)), lines)

    def test_text_streamed_in_place_keeps_only_its_final_form(self):
        words = "the quick brown fox jumps over the lazy dog".split()
        states = growth(doc(30))
        for n in range(1, len(words) + 1):
            states.append(doc(30) + [" ".join(words[:n])])
        done = doc(30) + [" ".join(words)]
        states += [done + doc(20, "after")[:i] for i in range(1, 21)]
        result = body(clean_bytes(tui_session(states, height=10)))
        self.assertEqual(result, doc(30) + [" ".join(words)] + doc(20, "after"))

    def test_block_that_collapses_after_scrolling_out_keeps_the_new_form(self):
        # A running tool shows its output, scrolls partly out of view, then
        # collapses to one summary line: the summary replaces the output.
        before = doc(20)
        running = [f"tool output {i}" for i in range(8)]
        after = doc(12, "reply")
        states = growth(before) + [before + ["Running tool"] + running[:4],
                  before + ["Running tool"] + running,
                  before + ["Running tool"] + running + after[:3],
                  before + ["Ran tool"] + after[:3],
                  before + ["Ran tool"] + after]
        result = body(clean_bytes(tui_session(states, height=9)))
        self.assertEqual(result, before + ["Ran tool"] + after)

    def test_repeated_text_far_back_does_not_move_the_window(self):
        # A table shown twice used to anchor the second copy on the first one.
        table = ["┌───┬───┐", "│ a │ 1 │", "├───┼───┤", "│ b │ 2 │", "└───┴───┘"]
        first = doc(10) + table + doc(12, "middle")
        states = growth(first) + [first + ["summary"] + table + doc(3, "end")]
        result = body(clean_bytes(tui_session(states, height=14)))
        self.assertEqual(result, first + ["summary"] + table + doc(3, "end"))

    def test_resize_reflow_is_not_a_second_copy(self):
        long = ["alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu"] * 1
        narrow = ["alpha beta gamma delta", "epsilon zeta eta theta", "iota kappa lambda mu"]
        wide = ["alpha beta gamma delta epsilon zeta", "eta theta iota kappa lambda mu"]
        intro = ["intro one", "intro two"]
        data = (f"{ESC}[?1049h" + frame(intro + narrow, 8) + frame(intro + wide, 8)
                + f"{ESC}[?1049l").encode()
        result = body(clean_bytes(data))
        self.assertEqual(result, intro + wide)
        self.assertTrue(long)

    def test_frames_while_browsing_history_are_ignored(self):
        lines = doc(40)
        footer = ["› Ask Codex to do anything", "  gpt · ~/project"]
        live = tui_session(growth(lines, 5), height=12, footer=footer)
        browsing = frame(doc(8) + ["", "", "  ↓ Back to bottom · esc"], 12)
        data = live[:-len(b"\x1b[?1049l")] + browsing.encode() + live[-len(b"\x1b[?1049l"):]
        self.assertEqual(body(clean_bytes(data, provider="codex")), lines)

    def test_region_scroll_with_repaint_above_is_not_doubled(self):
        # Claude Code scrolls rows 2..N with a region, then repaints row 1
        # with the line that moved up: that line has not left the screen.
        rows = ["r0", "r1", "r2", "r3"]
        out = f"{ESC}[?1049h" + frame(rows, 4)
        for n in range(4, 9):
            out += (f"{ESC}[?2026h{ESC}[2;4r{ESC}[1S{ESC}[r"
                    f"{ESC}[1;1H{rows[1]}{ESC}[K{ESC}[4;1Hr{n}{ESC}[K{ESC}[?2026l")
            rows = rows[1:] + [f"r{n}"]
        out += f"{ESC}[?1049l"
        self.assertEqual(body(clean_bytes(out.encode())), [f"r{i}" for i in range(9)])


class RecordedSize(unittest.TestCase):
    RECORD = "\x1b_kilix-transcript;rows={};cols={}{}\x1b\\"

    def record(self, rows, cols, rotated=False):
        return self.RECORD.format(rows, cols, ";rotated=1" if rotated else "").encode()

    def test_long_shell_lines_wrap_on_screen_but_read_whole(self):
        long = "x" * 50 + " end"
        data = self.record(5, 20) + f"$ echo\r\n{long}\r\n$ \r\n".encode()
        self.assertEqual(body(clean_bytes(data)), ["$ echo", long, "$"])

    def test_wrapped_line_scrolling_off_the_top_is_rejoined(self):
        lines = [f"{i:02d}" + "y" * 30 for i in range(12)]
        data = self.record(4, 16) + "\r\n".join(lines).encode() + b"\r\n"
        self.assertEqual(body(clean_bytes(data)), lines)

    def test_record_settles_rotation_both_ways(self):
        plain = self.record(10, 80) + b"hello\r\n"
        self.assertFalse(clean_bytes(plain + b" " * (6 << 20)).source["rotated"])
        cut = self.record(10, 80, rotated=True) + b"38;2;1mhello\r\n"
        result = clean_bytes(cut)
        self.assertTrue(result.source["rotated"])
        self.assertTrue(result.source["size_recorded"])
        self.assertNotIn("38;2;1m", "".join(result.lines))

    def test_program_addressing_rows_below_the_inferred_height(self):
        # Without the record the height would be inferred from row 2 only,
        # and the line feeds below would scroll the conversation away.
        data = (self.record(6, 40) + b"\x1b[?1049h" + frame(["a", "b"], 2).encode()
                + b"\x1b[6;1Hbottom" + b"\x1b[?1049l")
        self.assertEqual(body(clean_bytes(data)), ["a", "b", "bottom"])


class ScreensAndSegments(unittest.TestCase):
    def test_shell_then_program_then_shell(self):
        lines = doc(30, "tui")
        data = (b"$ run\r\n" + tui_session(growth(lines, 3), height=8)
                + b"$ after\r\n")
        result = clean_bytes(data)
        self.assertEqual(body(result), ["$ run"] + lines + ["$ after"])
        self.assertEqual([s.screen for s in result.sections], ["main", "alt", "main"])

    def test_program_repainting_the_same_conversation_is_not_doubled(self):
        lines = doc(12)
        once = tui_session([lines], height=20)
        result = clean_bytes(once + once)
        self.assertEqual(body(result), lines)

    def test_partial_screen_at_the_start_of_a_cut_recording_is_dropped(self):
        # Rotation leaves the first screen half drawn: those fragments are not
        # conversation. The size check marks this recording as rotated.
        partial = b"5;3H" + b"".join(f"{ESC}[?2026h{ESC}[3;1H{'W' + 'o' * i}{ESC}[?2026l".encode()
                                     for i in range(4))
        lines = doc(30)
        rest = b"".join(frame(w, 10).encode() for w in [lines[i:i + 10] for i in range(0, 21, 3)])
        result = clean_bytes(partial + rest + b" " * (6 << 20), rows=10)
        self.assertNotIn("W", body(result))
        self.assertTrue(result.source["rotated"])

    def test_titles_drop_spinners_and_attention_markers(self):
        data = b"".join(f"{ESC}]0;{t}\x07".encode() for t in
                        ["⠋ Task | repo", "⠙ Task | repo", "[ ! ] Action Required | Task",
                         "[ . ] Action Required | Task", "✳ Other"])
        self.assertEqual(clean_bytes(data).titles, ["Task | repo", "Action Required | Task", "Other"])


if __name__ == "__main__":
    unittest.main()
