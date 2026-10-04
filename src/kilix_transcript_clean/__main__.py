"""kilix-transcript-clean: print a pane transcript as readable text."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import PROVIDERS, __version__, clean_bytes
from .sources import SourceError, resolve


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kilix-transcript-clean",
        description="Replay a kilix pane transcript and print the text a person saw, "
                    "without control codes or the program's own chrome.")
    parser.add_argument("source", nargs="?",
                        help="transcript file (.log or .log.zst), broker session id, or - for stdin")
    parser.add_argument("--pane", help="read the transcript of this kilix pane id")
    parser.add_argument("--format", choices=("text", "json"), default="text",
                        help="text for people (default) or json for programs")
    parser.add_argument("--provider", choices=sorted(PROVIDERS),
                        help="treat every screen as this program instead of detecting it")
    parser.add_argument("--rows", type=int,
                        help="terminal height, when the recording cannot show it")
    parser.add_argument("--no-header", action="store_true",
                        help="omit the session, cwd and title lines from text output")
    parser.add_argument("-o", "--output", type=Path, help="write here instead of stdout")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.rows is not None and not 2 <= args.rows <= 1000:
        print("kilix-transcript-clean: --rows must be between 2 and 1000", file=sys.stderr)
        return 2
    if args.source and args.pane:
        print("kilix-transcript-clean: give a source or --pane, not both", file=sys.stderr)
        return 2
    try:
        if args.source == "-":
            data, info = sys.stdin.buffer.read(), {"path": "-"}
        else:
            data, info = resolve(args.source, args.pane)
    except SourceError as error:
        print(f"kilix-transcript-clean: {error}", file=sys.stderr)
        return 1
    transcript = clean_bytes(data, rows=args.rows, provider=args.provider, source=info)
    text = transcript.json() if args.format == "json" else transcript.text(header=not args.no_header)
    if args.output:
        try:
            args.output.write_text(text, encoding="utf-8")
        except OSError as error:
            print(f"kilix-transcript-clean: cannot write {args.output}: {error.strerror}",
                  file=sys.stderr)
            return 1
        return 0
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
    except BrokenPipeError:
        sys.stderr.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
