import unittest

from kilix_transcript_clean.providers import detect_scores, provider_named

CODEX = [
    "› run the release checks",
    "",
    "• Ran git status",
    "  └ clean",
    "",
    "• The push is ready.",
    "",
    "• Working (12s • esc to interrupt)",
    "  └ Tip: Use /recap to summarize.",
    "",
    "› Ask Codex to do anything",
    "",
    "  GPT-6 max · ~/project · Check status",
    "  ← for agents · ? for shortcuts",
]

CLAUDE = [
    "● The files are fixed.",
    "",
    "  Ran 2 shell commands",
    "",
    "✻ Wrangling… (2m 38s · ↓ 8.0k tokens)",
    "",
    "─" * 60,
    "❯ ",
    "─" * 60,
    "  ⏵⏵ bypass permissions on (shift+tab to cycle) · esc to interrupt",
]

GROK = [
    "  main ~/gpu_terminal                          [Dashboard]",
    "",
    "     ❯ pause here",
    "     ◆ Thought for 0.1s",
    "     Paused. No new waves.                          10:53 PM",
    "",
    "    ⠹ Waiting for response… 20s            20s ⇣174k [stop]",
    "",
    "  ╭──────────────────────────────╮",
    "  │ ❯                            │",
    "  ╰──── Grok 4.6 (xhigh) ────────╯",
    "  Shift+Tab:mode  │  Ctrl+.:shortcuts",
]

OMP = [
    "╭─── omp v18.3.2 ───╮",
    "│ $ ls",
    "├─── Output ─────────",
    "│ file",
    "│ ⟦Wall: 0.57s | Timeout: 300s⟧",
    "╰───────────────────",
    "",
    " Checking the files.",
    "",
    "  ⎋ Finding update URLs",
    " ⠋ 54s > ◒ Qwen 3.8 Max > 📁 …/needle > $0.06 ▶──6%──",
    "╰─",
]


class Detection(unittest.TestCase):
    def test_each_program_is_recognised(self):
        for name, screen in (("codex", CODEX), ("claude", CLAUDE), ("grok", GROK), ("omp", OMP)):
            with self.subTest(name):
                self.assertEqual(detect_scores(screen).most_common(1)[0][0], name)

    def test_plain_shell_matches_nothing(self):
        self.assertFalse(detect_scores(["$ ls", "README.md  src  tests", "$ "]))

    def test_unknown_provider_name(self):
        with self.assertRaises(ValueError):
            provider_named("vim")


class Regions(unittest.TestCase):
    def test_codex_keeps_conversation_and_drops_composer_and_status(self):
        body, top = provider_named("codex").content(CODEX)
        self.assertEqual(top, 1)  # the pinned prompt row
        self.assertEqual(body[-1], "• The push is ready.")

    def test_codex_blank_top_row_is_reserved(self):
        self.assertEqual(provider_named("codex").header_rows([""] + CODEX[1:]), 1)

    def test_codex_history_browsing(self):
        codex = provider_named("codex")
        self.assertTrue(codex.browsing(["text", "", "    ↓ Back to bottom · esc"]))
        self.assertFalse(codex.browsing(CODEX))

    def test_claude_history_browsing(self):
        claude = provider_named("claude")
        for marker in ("Jump to bottom (ctrl+End) ↓", "1 new message (ctrl+End) ↓", "3 new messages (ctrl+End) ↓"):
            with self.subTest(marker):
                self.assertTrue(claude.browsing(CLAUDE[:3] + ["   " + marker] + CLAUDE[5:]))
        self.assertFalse(claude.browsing(CLAUDE))

    def test_claude_drops_prompt_box_and_spinner(self):
        body, top = provider_named("claude").content(CLAUDE)
        self.assertEqual((top, body), (0, CLAUDE[:3]))

    def test_grok_drops_header_spinner_and_input_box(self):
        body, top = provider_named("grok").content(GROK)
        self.assertEqual(top, 2)
        self.assertEqual(body[-1], "     Paused. No new waves.                          10:53 PM")
        self.assertEqual(provider_named("grok").tidy(body[-1]), "     Paused. No new waves.")

    def test_omp_drops_status_and_working_lines(self):
        body, _ = provider_named("omp").content(OMP)
        self.assertEqual(body[-1], " Checking the files.")


class Chrome(unittest.TestCase):
    def test_status_lines(self):
        cases = {
            "codex": ["• Working (16s • esc to interrupt)", "  └ Tip: Use /recap to summarize."],
            "claude": ["✻ Wrangling… (8m 15s · ↓ 38.4k tokens)", "· Burrowing…",
                       "  ⏵⏵ bypass permissions on (shift+tab to cycle)"],
            "grok": ["    ⠹ Waiting for response… 20s", "  ~/gpu_terminal    266K / 500K │ [Dashboard]"],
            "omp": [" ⠋ 54s > ◒ Qwen 3.8 Max > 📁 …/needle > $0.06 ▶──6%──", "  ⎋ Finding update URLs"],
        }
        for name, lines in cases.items():
            for line in lines:
                with self.subTest(name=name, line=line):
                    self.assertTrue(provider_named(name).is_chrome(line))

    def test_conversation_is_not_chrome(self):
        for name in ("codex", "claude", "grok", "omp", "shell"):
            for line in ("● Working on the parser now.", "• Ran pytest", "Working (as designed)",
                         "  - item one", "Done. 3 files changed."):
                with self.subTest(name=name, line=line):
                    self.assertFalse(provider_named(name).is_chrome(line))

    def test_braille_art_is_decoration(self):
        self.assertTrue(provider_named("shell").is_chrome("   ⣠⣶⣿⣿⣿⣿⣿⣿⣿⣿⣦⣤⣤⣤"))
        self.assertFalse(provider_named("shell").is_chrome("Braille ⠃⠗⠁ in a sentence"))


if __name__ == "__main__":
    unittest.main()
