import unittest

from kilix_transcript_clean.screen import (CONTINUATION, Screen, infer_rows, looks_rotated,
                                           parse_size_record, trim_partial_head)


def replay(data: bytes, rows: int = 5, cols: int = 1000, **kwargs):
    events = {"frames": [], "scrolled": [], "titles": []}
    screen = Screen(rows, cols,
                    on_frame=lambda s, why: events["frames"].append((why, s.lines())),
                    on_scroll=lambda s, line: events["scrolled"].append(line),
                    on_title=events["titles"].append, **kwargs)
    screen.feed(data)
    screen.finish()
    return screen, events


class TextAndCursor(unittest.TestCase):
    def test_lines_and_carriage_return(self):
        screen, _ = replay(b"hello\r\nworld\rW")
        self.assertEqual(screen.lines()[:2], ["hello", "World"])

    def test_absolute_positioning_and_erase(self):
        screen, _ = replay(b"\x1b[2;3Habcdef\x1b[2;5H\x1b[K\x1b[1;1Hx")
        self.assertEqual(screen.lines()[:2], ["x", "  ab"])

    def test_erase_display_below(self):
        screen, _ = replay(b"one\r\ntwo\r\nthree\x1b[2;1H\x1b[J")
        self.assertEqual(screen.lines()[:3], ["one", "", ""])

    def test_long_line_stays_whole_on_wide_virtual_screen(self):
        screen, _ = replay(b"x" * 300)
        self.assertEqual(screen.lines()[0], "x" * 300)

    def test_autowrap_at_real_width_marks_the_continuation(self):
        screen, _ = replay(b"abcdef", cols=4)
        self.assertEqual(screen.lines()[:2], ["abcd", CONTINUATION + "ef"])

    def test_a_row_that_was_not_full_is_not_a_continuation(self):
        # Moving to a row is not wrapping into it, whatever the row held.
        screen, _ = replay(b"abcdef\x1b[1;1H\x1b[2Kab\x1b[2;1Hxy", cols=4)
        self.assertEqual(screen.lines()[:2], ["ab", "xy"])

    def test_wide_and_combining_characters(self):
        screen, _ = replay("界x\r\né!".encode())
        self.assertEqual(screen.lines()[:2], ["界x", "é!"])

    def test_c1_controls_and_bidi_marks_are_not_text(self):
        screen, _ = replay("a\u009bb\u202ec\u2066d\u200de".encode())
        self.assertEqual(screen.lines()[0], "abcd\u200de")

    def test_insert_and_delete_characters(self):
        screen, _ = replay(b"abcd\x1b[1;2H\x1b[2P\x1b[1;1H\x1b[1@")
        self.assertEqual(screen.lines()[0], " ad")

    def test_cursor_down_stops_at_scroll_region_bottom(self):
        # Claude Code moves with CUD inside a region; a real terminal stops at
        # the bottom margin, and so must the replay, or rows land too low.
        screen, _ = replay(b"\x1b[1;3r\x1b[1;1H\x1b[9Bx", rows=6)
        self.assertEqual(screen.lines()[2], "x")

    def test_cursor_up_stops_at_scroll_region_top(self):
        screen, _ = replay(b"\x1b[2;4r\x1b[3;1H\x1b[9Ax", rows=6)
        self.assertEqual(screen.lines()[1], "x")


class Scrolling(unittest.TestCase):
    def test_line_feed_at_bottom_reports_the_line_that_left(self):
        _, events = replay(b"a\r\nb\r\nc\r\nd", rows=2)
        self.assertEqual(events["scrolled"], ["a", "b"])

    def test_region_scroll_reports_from_region_top(self):
        screen, events = replay(b"head\x1b[2;3r\x1b[2;1Hx\x1b[3;1Hy\x1b[1S", rows=4)
        self.assertEqual(events["scrolled"], ["x"])
        self.assertEqual(screen.lines()[:3], ["head", "y", ""])

    def test_insert_and_delete_lines(self):
        screen, _ = replay(b"1\r\n2\r\n3\x1b[2;1H\x1b[L", rows=3)
        self.assertEqual(screen.lines(), ["1", "", "2"])
        screen, _ = replay(b"1\r\n2\r\n3\x1b[1;1H\x1b[M", rows=3)
        self.assertEqual(screen.lines(), ["2", "3", ""])


class Screens(unittest.TestCase):
    def test_alternate_screen_keeps_main_contents(self):
        screen, events = replay(b"shell$ \x1b[?1049hTUI\x1b[?1049l")
        self.assertEqual(screen.lines()[0], "shell$")
        reasons = [why for why, _ in events["frames"]]
        self.assertIn("alt-on", reasons)
        self.assertIn("alt-off", reasons)

    def test_frames_at_end_of_synchronized_updates(self):
        _, events = replay(b"\x1b[?2026ha\x1b[?2026l\x1b[?2026hb\x1b[?2026l")
        self.assertEqual([why for why, _ in events["frames"]], ["sync", "sync", "eof"])

    def test_no_idle_frame_inside_synchronized_update(self):
        _, events = replay(b"\x1b[?2026h" + b"x" * 50 + b"\x1b[?2026l", idle_bytes=10)
        self.assertEqual([why for why, _ in events["frames"]], ["sync", "eof"])

    def test_idle_frames_without_synchronized_updates(self):
        _, events = replay(b"x" * 50, idle_bytes=10)
        self.assertIn("idle", [why for why, _ in events["frames"]])

    def test_titles(self):
        _, events = replay(b"\x1b]0;first\x07\x1b]2;second\x1b\\\x1b]7;file://h/p\x07")
        self.assertEqual(events["titles"], ["first", "second"])

    def test_broker_graphics_note_never_reaches_the_screen(self):
        screen, _ = replay(b"\x1b[1;1Hab\r\n[kitty-pty-broker: 20 bytes of graphics elided]\r\ncd")
        self.assertEqual(screen.lines()[:2], ["abcd", ""])

    def test_unknown_sequences_are_ignored(self):
        screen, _ = replay(b"\x1b[>4;2m\x1b[<u\x1b[?1000h\x1bP@kitty-print|aGk=\x1b\\\x1b_Ga=q\x1b\\ok")
        self.assertEqual(screen.lines()[0], "ok")


class SizeRecords(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(parse_size_record(b"rows=57;cols=212"), (57, 212, False))
        self.assertEqual(parse_size_record(b"rows=5;cols=9;rotated=1"), (5, 9, True))
        self.assertIsNone(parse_size_record(b"rows=0;cols=80"))
        self.assertIsNone(parse_size_record(b"cols=80"))

    def test_record_resizes_and_is_invisible(self):
        screen, events = replay(b"ab\x1b_kilix-transcript;rows=3;cols=6\x1b\\cdefgh", rows=5)
        self.assertEqual((screen.rows, screen.cols), (3, 6))
        self.assertEqual(screen.lines(), ["abcdef", CONTINUATION + "gh", ""])
        self.assertIn("resize", [why for why, _ in events["frames"]])

    def test_shrinking_pushes_rows_off_the_top_like_kitty(self):
        data = b"1\r\n2\r\n3\r\n4" + b"\x1b_kilix-transcript;rows=2;cols=80\x1b\\"
        screen, events = replay(data, rows=4)
        self.assertEqual(screen.lines(), ["3", "4"])
        self.assertEqual(events["scrolled"], ["1", "2"])

    def test_shrinking_drops_blank_rows_below_the_cursor_first(self):
        screen, events = replay(b"1\r\n2\x1b_kilix-transcript;rows=2;cols=80\x1b\\", rows=4)
        self.assertEqual(screen.lines(), ["1", "2"])
        self.assertEqual(events["scrolled"], [])

    def test_malformed_record_is_ignored(self):
        screen, _ = replay(b"\x1b_kilix-transcript;rows=x\x1b\\ok", rows=3)
        self.assertEqual((screen.rows, screen.lines()[0]), (3, "ok"))


class Recording(unittest.TestCase):
    def test_trim_partial_escape_at_start(self):
        self.assertEqual(trim_partial_head(b"38;2;1;2;3mtext"), b"text")
        self.assertEqual(trim_partial_head(b"\x80\x80abc"), b"abc")
        self.assertEqual(trim_partial_head(b"plain text"), b"plain text")
        self.assertEqual(trim_partial_head(b"\x1b[1mx"), b"\x1b[1mx")

    def test_rotation_guess(self):
        self.assertTrue(looks_rotated(b"5;3Hrest"))
        self.assertTrue(looks_rotated(b"x" * (6 << 20)))
        self.assertFalse(looks_rotated(b"\x1b[?1049hhello"))

    def test_infer_rows(self):
        self.assertEqual(infer_rows(b"\x1b[40;1H\x1b[2;51r"), 51)
        self.assertEqual(infer_rows(b"no positioning"), 24)

    def test_warm_after_most_rows_drawn(self):
        screen, _ = replay(b"\x1b[1;1Ha", rows=4, warm=False)
        self.assertFalse(screen.warm)
        screen, _ = replay(b"\x1b[1;1Ha\x1b[2;1Hb\x1b[3;1Hc", rows=4, warm=False)
        self.assertTrue(screen.warm)
        screen, _ = replay(b"\x1b[2J", rows=4, warm=False)
        self.assertTrue(screen.warm)


class ReplayCaching(unittest.TestCase):
    def test_rendered_rows_follow_every_cell_edit(self):
        screen = Screen(3, 20)
        screen.feed(b"abcdef")
        self.assertEqual(screen.lines()[0], "abcdef")
        edits = (
            (b"\x1b[1;2HX", "aXcdef"),
            (b"\x1b[1;3H\x1b[2P", "aXef"),
            (b"\x1b[1;2H\x1b[2@", "a  Xef"),
            (b"\x1b[1;4H\x1b[2X", "a    f"),
            (b"\x1b[1;3H\x1b[K", "a"),
            (b"\x1b[2K", ""),
            (b"\x1b[1;1Hok", "ok"),
            (b"\x1b[2J", ""),
        )
        for sequence, expected in edits:
            with self.subTest(sequence=sequence):
                screen.feed(sequence)
                self.assertEqual(screen.lines()[0], expected)

    def test_cached_row_keeps_space_when_the_next_write_wraps(self):
        screen = Screen(3, 4)
        screen.feed(b"abc ")
        self.assertEqual(screen.lines()[0], "abc")
        screen.feed(b"d")
        self.assertEqual(screen.lines()[:2], ["abc ", CONTINUATION + "d"])

    def test_style_sequences_preserve_pending_wrap(self):
        screen, _ = replay(b"abcd\x1b[38;2;1;2;3m\x1b[4:3m\x1b[>4;2me", cols=4)
        self.assertEqual(screen.lines()[:2], ["abcd", CONTINUATION + "e"])

    def test_ascii_runs_pad_gaps_and_overwrite_without_removing_suffix(self):
        screen, _ = replay(b"abcdef\x1b[1;2HXY\x1b[1;9Hz", cols=12)
        self.assertEqual(screen.lines()[0], "aXYdef  z")

    def test_right_margin_with_autowrap_disabled(self):
        screen, _ = replay(b"\x1b[?7labcdef\rXY", cols=4)
        self.assertEqual(screen.lines()[:2], ["XYcf", ""])

    def test_cached_row_tracks_combining_and_wide_character_edits(self):
        screen = Screen(3, 8)
        screen.feed(b"e")
        self.assertEqual(screen.lines()[0], "e")
        screen.feed("\u0301".encode())
        self.assertEqual(screen.lines()[0], "e\u0301")
        screen.feed("\r界".encode())
        self.assertEqual(screen.lines()[0], "界")
        screen.feed(b"\rZ")
        self.assertEqual(screen.lines()[0], "Z")


if __name__ == "__main__":
    unittest.main()
