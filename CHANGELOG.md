# Changelog

## 0.1.0 — unreleased

- First version: replay a kitty-pty-broker transcript on a virtual screen,
  stitch its settled frames into one transcript, and drop the chrome of Codex,
  Claude Code, Grok and omp.
- `kilix-transcript-clean` command: reads a file, a broker session id (live,
  `recent/` or `archive/`, `.zst` included) or a kilix pane id, and writes text
  or `kilix.transcript.clean/v1` JSON.
- Reads kitty-pty-broker's size records (`ESC _ kilix-transcript;rows=R;cols=C`)
  to replay at the real pane size and through resizes. Lines the terminal
  wrapped are joined back together. Older recordings without the records are
  sized by inference.
- Handles recordings cut by the broker's size limit, broker graphics notes,
  scroll regions, alternate screens, pinned header rows, collapsing tool
  blocks, history browsing and resize reflow.
