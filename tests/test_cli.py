import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from kilix_transcript_clean.__main__ import main
from kilix_transcript_clean.sources import SourceError, find_session, read_meta

SESSION = "0123456789abcdef"
RECORDING = b"$ echo hi\r\nhi\r\n$ \x1b]0;demo\x07"


def run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


class Command(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        (self.dir / f"{SESSION}.log").write_bytes(RECORDING)
        (self.dir / f"{SESSION}.meta").write_text("cwd=/home/pleb/project\ncmd=/bin/bash --posix\n")
        env = mock.patch.dict(os.environ, {"KITTY_PTY_BROKER_TRANSCRIPT_DIR": str(self.dir)})
        env.start()
        self.addCleanup(env.stop)

    def test_session_id_text(self):
        code, out, _ = run(SESSION)
        self.assertEqual(code, 0)
        self.assertIn(f"# session: {SESSION}", out)
        self.assertIn("# cwd: /home/pleb/project", out)
        self.assertIn("# title: demo", out)
        self.assertIn("$ echo hi\nhi\n", out)

    def test_file_path_json(self):
        code, out, _ = run(str(self.dir / f"{SESSION}.log"), "--format", "json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["schema"], "kilix.transcript.clean/v1")
        self.assertEqual(data["source"]["cmd"], "/bin/bash --posix")
        self.assertEqual(data["sections"][0]["lines"][:2], ["$ echo hi", "hi"])

    def test_no_header(self):
        _, out, _ = run(SESSION, "--no-header")
        self.assertTrue(out.startswith("$ echo hi\n"))

    def test_stdin(self):
        stdin = io.TextIOWrapper(io.BytesIO(RECORDING))
        with mock.patch.object(sys, "stdin", stdin):
            code, out, _ = run("-", "--no-header")
        self.assertEqual((code, out.splitlines()[0]), (0, "$ echo hi"))

    def test_output_file(self):
        target = self.dir / "clean.txt"
        code, out, _ = run(SESSION, "-o", str(target))
        self.assertEqual((code, out), (0, ""))
        self.assertIn("hi", target.read_text())

    def test_missing_session(self):
        code, _, err = run("fedcba9876543210")
        self.assertEqual(code, 1)
        self.assertIn("no transcript for session", err)

    def test_bad_arguments(self):
        self.assertEqual(run(SESSION, "--rows", "1")[0], 2)
        self.assertEqual(run(SESSION, "--pane", "3")[0], 2)
        self.assertEqual(run("not-a-session")[0], 1)

    def test_archived_session(self):
        if not shutil.which("zstd"):
            self.skipTest("zstd is not installed")
        live = self.dir / f"{SESSION}.log"
        (self.dir / "recent").mkdir()
        subprocess.run(["zstd", "-q", str(live), "-o", str(self.dir / "recent" / f"{SESSION}.log.zst")],
                       check=True)
        live.unlink()
        self.assertEqual(find_session(SESSION).parent.name, "recent")
        code, out, _ = run(SESSION, "--no-header")
        self.assertEqual((code, out.splitlines()[0]), (0, "$ echo hi"))
        self.assertEqual(read_meta(find_session(SESSION))["cwd"], "/home/pleb/project")

    def test_pane_lookup(self):
        bin_dir = self.dir / "bin"
        bin_dir.mkdir()
        fake = bin_dir / "kilix"
        listing = {"panes": [{"pane_id": 29, "broker": {"session_id": SESSION}},
                             {"pane_id": 30, "broker": {}}]}
        fake.write_text("#!/bin/sh\ncat <<'EOF'\n" + json.dumps(listing) + "\nEOF\n")
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        with mock.patch.dict(os.environ, {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}):
            code, out, _ = run("--pane", "29")
            self.assertEqual(code, 0)
            self.assertIn("# pane: 29", out)
            self.assertEqual(run("--pane", "30")[0], 1)
            self.assertEqual(run("--pane", "31")[0], 1)

    def test_find_session_rejects_paths(self):
        with self.assertRaises(SourceError):
            find_session("../etc/passwd")


if __name__ == "__main__":
    unittest.main()
