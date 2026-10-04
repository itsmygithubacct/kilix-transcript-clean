"""Find and read kilix pane transcripts.

kitty-pty-broker writes ``<session>.log`` and a ``<session>.meta`` sidecar
(``cwd=`` and ``cmd=`` lines) into the transcript directory. When a pane
closes, kilix compresses its log into ``recent/`` and later ``archive/`` as
``<session>.log.zst``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

SESSION_ID = re.compile(r"[0-9a-f]{16}")


class SourceError(Exception):
    """A transcript could not be found or read."""


def transcript_dir() -> Path:
    configured = os.environ.get("KITTY_PTY_BROKER_TRANSCRIPT_DIR")
    if configured:
        return Path(configured)
    state = os.environ.get("KILIX_STATE_DIRECTORY")
    if state:
        return Path(state) / "transcripts"
    return Path.home() / ".local/gpu_terminal/kilix/state/transcripts"


def find_session(session: str, root: Path | None = None) -> Path:
    """The newest copy of a broker session's log: live, recent or archived."""
    if not SESSION_ID.fullmatch(session):
        raise SourceError(f"not a transcript session id: {session!r}")
    root = root or transcript_dir()
    for candidate in (root / f"{session}.log",
                      root / "recent" / f"{session}.log.zst",
                      root / "archive" / f"{session}.log.zst"):
        if candidate.is_file():
            return candidate
    raise SourceError(f"no transcript for session {session} under {root}")


def pane_session(pane: str) -> str:
    """The broker session recorded for a kilix pane id, via ``kilix panes``."""
    kilix = shutil.which("kilix")
    if not kilix:
        raise SourceError("the kilix command is needed to look up a pane")
    try:
        result = subprocess.run([kilix, "panes", "list", "--json"], capture_output=True,
                                text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SourceError(f"could not list kilix panes: {error}") from None
    if result.returncode:
        raise SourceError(f"kilix panes list failed (exit {result.returncode})")
    try:
        panes = json.loads(result.stdout).get("panes", [])
    except (ValueError, AttributeError):
        raise SourceError("kilix panes list returned unreadable JSON") from None
    for entry in panes:
        if str(entry.get("pane_id")) == str(pane):
            session = (entry.get("broker") or {}).get("session_id")
            if session and SESSION_ID.fullmatch(session):
                return session
            raise SourceError(f"pane {pane} has no recorded transcript")
    raise SourceError(f"no kilix pane with id {pane}")


def read_bytes(path: Path) -> bytes:
    """A transcript's raw bytes, decompressing ``.zst`` with the zstd tool."""
    if path.suffix != ".zst":
        try:
            return path.read_bytes()
        except OSError as error:
            raise SourceError(f"cannot read {path}: {error.strerror}") from None
    zstd = shutil.which("zstd")
    if not zstd:
        raise SourceError(f"{path} is compressed and the zstd command is not installed")
    result = subprocess.run([zstd, "-dcq", "--", str(path)], capture_output=True, check=False)
    if result.returncode:
        raise SourceError(f"zstd could not decompress {path} (exit {result.returncode})")
    return result.stdout


def read_meta(path: Path) -> dict:
    """The ``cwd`` and ``cmd`` the broker recorded when the pane started."""
    name = path.name.split(".", 1)[0]
    for sidecar in (path.parent / f"{name}.meta", path.parent.parent / f"{name}.meta"):
        if sidecar.is_file():
            info = {}
            try:
                text = sidecar.read_text(errors="replace")
            except OSError:
                return {}
            for line in text.splitlines():
                key, sep, value = line.partition("=")
                if sep and key in ("cwd", "cmd"):
                    info[key] = value
            return info
    return {}


def resolve(source: str | None = None, pane: str | None = None) -> tuple[bytes, dict]:
    """Bytes and description of a transcript named by path, session id or pane."""
    if pane is not None:
        source = pane_session(pane)
    if source is None:
        raise SourceError("name a transcript file, a session id, or --pane")
    path = Path(source)
    if not path.is_file():
        path = find_session(source)
    info = {"session": path.name.split(".", 1)[0], "path": str(path)}
    if pane is not None:
        info["pane"] = str(pane)
    info.update(read_meta(path))
    return read_bytes(path), info
