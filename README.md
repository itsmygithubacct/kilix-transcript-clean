# kilix-transcript-clean

`kilix-transcript-clean` turns a recorded kilix pane transcript into the text
a person saw in that pane. The output has no control codes and none of the
program's own chrome, so people and agents can both read it.

kitty-pty-broker records every byte a pane's program writes. Full-screen
programs like Claude Code, Codex, Grok and omp redraw the screen in place
thousands of times. Stripping the escape codes from such a recording leaves
spinner frames, half-drawn rows and text in no particular order. This module
replays the recording on a small virtual screen instead, and stitches the
screens into one transcript in reading order.

## Command

```sh
kilix-transcript-clean --pane 29                  # a live or recently closed pane
kilix-transcript-clean 9fe0949333510cc7           # a broker session id
kilix-transcript-clean path/to/session.log.zst    # a file, compressed or not
kilix-transcript-clean --format json 9fe0949333510cc7
```

Text output starts with a short header: the session, its working directory
and command, the last window title, and which programs appeared. Each
program's part of the transcript is headed by a `── Codex ──` style line:

```
# session: 9fe0949333510cc7
# cwd: /home/pleb/project
# title: Check game status | pleb-tower
# programs: shell, Codex

── shell ──
pleb@plebian:~/project$ codex --yolo

── Codex ──
› run the release checks

• I'll run the checks and report what fails.
...
```

| Option | Meaning |
| --- | --- |
| `--pane ID` | Find the pane's session with `kilix panes list --json` |
| `--format json` | `kilix.transcript.clean/v1` JSON: source, titles, and sections of lines |
| `--provider NAME` | Skip detection and treat the screen as `codex`, `claude`, `grok`, `omp`, `shell` or `tui` |
| `--rows N` | Terminal height, when the recording never positions the cursor |
| `--no-header` | Text without the header lines |
| `-o FILE` | Write to a file |

A session id is looked up in the transcript directory. That directory is
`$KITTY_PTY_BROKER_TRANSCRIPT_DIR`, or `$KILIX_STATE_DIRECTORY/transcripts`,
or `~/.local/gpu_terminal/kilix/state/transcripts`. The lookup checks the live
`<id>.log` first, then `recent/<id>.log.zst`, then `archive/<id>.log.zst`.
Compressed files need the `zstd` command.

## Library

```python
from kilix_transcript_clean import clean_bytes

transcript = clean_bytes(open("session.log", "rb").read())
print(transcript.text())        # or transcript.json(), transcript.lines
for section in transcript.sections:
    print(section.label, len(section.lines))
```

There are no dependencies beyond the Python standard library (3.10 or newer).

## How it works

1. **Replay.** `screen.py` is a small terminal. It models cursor movement,
   erasing, scroll regions, line insert/delete, the alternate screen and
   window titles, and ignores colour.
   - kitty-pty-broker records the pane size as a private APC that terminals
     ignore (`ESC _ kilix-transcript;rows=R;cols=C ESC \`): when the
     transcript opens, on every resize, and after a rotation, flagged
     `rotated=1`. With it, the replay uses the real size, so it wraps where the
     terminal wrapped. Rows that only continue a line past the right margin
     are joined back into that line.
   - Older recordings carry no size. Then the height is the largest row the
     program addresses, and the width is a wide virtual 1000 columns, so long
     lines stay whole.
   - The broker's `[kitty-pty-broker: N bytes of graphics elided]` notes are
     removed before replay, because they never reached the screen.
2. **Settled frames.** A frame is taken at the end of each synchronized update
   (`CSI ? 2026 l`), before a clear, at screen switches, and after quiet stretches
   of output from programs that do not synchronize.
3. **Stitching.** `stitch.py` treats the committed transcript plus the visible
   screen as one document, with the screen as a window onto its end. Each frame
   is anchored by its topmost run of lines that match that document:
   - When the window moves down, the lines that left the top are committed.
   - When it moves up (a running tool block collapses to a one-line summary),
     the early lines come back, so the final rendering wins.
   - A frame with the same words reflowed (a resize) adds nothing.
   - A frame that matches nothing commits the old screen whole.
   - Line feeds and scroll regions commit lines as they leave, so fast output
     between frames is kept.
4. **Programs.** `providers.py` recognises Codex, Claude Code, Grok and omp from
   their screens. It knows their fixed rows: Codex's pinned prompt, Grok's path
   header, prompt boxes, status lines and key hints. It also knows their status
   lines (spinners with timers). Only those rows are dropped; any line that might
   be conversation is kept. A shell, or any other program, is kept as it was drawn.

## Accuracy

Measured against the programs' own session logs, on transcripts from this
fleet:

| Program | Sample | Assistant messages present in full |
| --- | --- | --- |
| Codex | 3 sessions, 0.9–6.9 MB | all; the only differences are Codex's own rewording of links and tables |
| Claude Code | 7.5 MB, about 5 hours | 127 of 136; most of the rest are tables, which Claude Code draws as boxes |
| Grok | 6.4 MB | all within the recording |
| omp | 4.2 MB | all |

Replay reuses unchanged screen rows, batches ordinary text writes and caches
repeated character widths and control sequences. These caches are bounded;
each recording is still replayed in full, including its redraws and resizes.

## Limits

- **Rotated recordings.** When a recording reaches the broker's size limit
  (8 MiB by default), the oldest quarter is dropped. Such a file begins in the
  middle of the session. The header says so, and nothing is taken from the
  screen until it has been mostly redrawn. A recording with the broker's size
  record says for certain whether it was rotated; for an older one it is
  inferred from its size and its first bytes.
- **Unshown text.** Text that a program never draws is not in the transcript.
  That includes tool output collapsed behind "ctrl+t to expand", earlier
  history while the person scrolled back, and anything that scrolled past
  between two frames inside a full-screen program.
- **Programs' own logs.** For Claude Code and Codex, their session files have
  more detail. The pane transcript is what was on screen, for any program,
  including the shell around it.

## Development

```sh
make test     # unit tests
make check    # tests, then byte-compile
```
