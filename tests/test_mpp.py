"""Unit tests for the mpp merged-copy command.

The suite is read-only and PATH-independent: every subprocess/clipboard
interaction is stubbed inside the loaded module, and the four tools are
resolved inside this repo, so the tests run anywhere (including CI without
paperflow/papertrail/papertrade installed).
"""

import contextlib
import io
import runpy
import subprocess as real_subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MPP = REPO / "mpp"


def load_mpp(stub_paths=True):
    ns = runpy.run_path(str(MPP))
    g = ns["part_paperflow"].__globals__
    if stub_paths:
        install_path_stub(g)
    return ns, g


def install_path_stub(g):
    """Resolve the four tools inside this repo so tests run anywhere (CI)."""
    def which(tool):
        target = {"mtui": "mtui", "paperflow": "paperflow",
                  "papertrail": "papertrail", "papertrade": "papertrade"}.get(tool)
        return str(REPO / target) if target else None
    g["shutil"] = type("SH", (), {"which": staticmethod(which)})()


class FakeResult:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def make_fake_subprocess(calls, papertrail="fail", paperflow="ok"):
    """Fake subprocess module whose run() serves synthetic exports.

    papertrail: 'fail' -> exit 1 (forces the legacy fallback); 'decode' ->
    raises UnicodeDecodeError; 'ok' -> clean dump.
    paperflow: 'ok' -> payload plus the two-BAR footer; 'timeout' ->
    raises TimeoutExpired.
    """
    class _FakeSP:
        TimeoutExpired = real_subprocess.TimeoutExpired
        CalledProcessError = real_subprocess.CalledProcessError
        SubprocessError = real_subprocess.SubprocessError
        DEVNULL = real_subprocess.DEVNULL
        PIPE = real_subprocess.PIPE

        @staticmethod
        def run(cmd, capture_output=False, text=False, timeout=None, env=None, **kw):
            name = Path(cmd[0]).name
            calls.append((name, dict(env or {})))
            if name == "papertrail":
                if papertrail == "decode":
                    raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "simulated")
                if papertrail == "ok":
                    return FakeResult(0, "FILE: papertrail\nCONTENT\n")
                return FakeResult(1, "")          # exit 1 -> fallback
            if name == "papertrade":
                return FakeResult(0, "PAPERTRADE-PAYLOAD\n")
            if name == "paperflow":
                if paperflow == "timeout":
                    raise real_subprocess.TimeoutExpired(["paperflow"], 45)
                bar = "=" * 70
                out = (bar + "\nHEADER\n" + bar + "\n\ncontent\n"
                       + bar + "\nsummary\n" + bar + "\n")
                return FakeResult(0, out)
            raise AssertionError(f"unexpected command: {cmd}")

        @staticmethod
        def Popen(*a, **k):
            raise AssertionError("Popen must not run in these tests")

    return _FakeSP


class MppPartTests(unittest.TestCase):

    def test_build_payload_falls_back_on_papertrail_failure(self):
        ns, g = load_mpp()
        calls = []
        g["subprocess"] = make_fake_subprocess(calls, papertrail="fail")
        _, parts, failures = ns["build_payload"]("TEST")
        self.assertEqual(failures, [])
        self.assertIn("FILE:", parts[2]["text"])          # fallback payload

    def test_paperflow_trailer_is_stripped(self):
        ns, g = load_mpp()
        calls = []
        g["subprocess"] = make_fake_subprocess(calls)
        _, parts, _ = ns["build_payload"]("TEST")
        self.assertIn("content", parts[1]["text"])
        self.assertNotIn("summary", parts[1]["text"])     # footer removed

    def test_paperflow_no_clipboard_env_is_passed(self):
        ns, g = load_mpp()
        calls = []
        g["subprocess"] = make_fake_subprocess(calls)
        ns["build_payload"]("TEST")
        pf = [c for c in calls if c[0] == "paperflow"]
        self.assertEqual(pf[0][1].get("PAPERFLOW_NO_CLIPBOARD"), "1")

    def test_papertrail_plain_env_is_passed(self):
        ns, g = load_mpp()
        calls = []
        g["subprocess"] = make_fake_subprocess(calls)
        ns["build_payload"]("TEST")
        pt = [c for c in calls if c[0] == "papertrail"]
        self.assertEqual(pt[0][1].get("PAPERTRAIL_PLAIN"), "1")

    def test_unicode_decode_error_routes_to_fallback(self):
        ns, g = load_mpp()
        calls = []
        g["subprocess"] = make_fake_subprocess(calls, papertrail="decode")
        _, parts, failures = ns["build_payload"]("TEST")
        self.assertEqual(failures, [])
        self.assertIn("FILE:", parts[2]["text"])

    def test_paperflow_timeout_is_reported_as_failure(self):
        ns, g = load_mpp()
        calls = []
        g["subprocess"] = make_fake_subprocess(calls, paperflow="timeout")
        _, _, failures = ns["build_payload"]("TEST")
        self.assertTrue(any("paperflow" in f for f in failures))


class MppFallbackDiscoveryTests(unittest.TestCase):
    """Pin the fallback's file-set rules against papertrail's own project."""

    HFC = Path("/home/ars/hedge-fund-cloner")

    def test_fallback_discovers_papertrail_style_file_set(self):
        if not self.HFC.is_dir():
            self.skipTest("hedge-fund-cloner not installed")
        ns, g = load_mpp()
        files = ns["_paper_files"](self.HFC)
        rels = [r for r, _ in files]
        self.assertIn("papertrail", rels)                # the launcher script
        self.assertIn("requirements.txt", rels)          # requirements
        self.assertTrue(any(r.endswith(".md") for r in rels))
        self.assertTrue(any(r.startswith("config/") and r.endswith((".yaml", ".yml"))
                            for r in rels))
        for r in rels:
            parts = Path(r).parts
            self.assertNotIn("__pycache__", parts)
            self.assertNotIn(".git", parts)
            self.assertNotIn("data", parts)
        # every listed file must actually exist
        for rel, abspath in files:
            self.assertTrue(abspath.is_file(), rel)

    def test_fallback_payload_has_expected_structure(self):
        ns, g = load_mpp()
        text = ns["_paper_dump_payload"](REPO)          # walks the tests dir
        self.assertIn(" FILE: ", text)
        self.assertIn(" PATH: ", text)
        self.assertIn(" LINES: ", text)
        self.assertIn("═" * 70, text)                    # HEAVY separators


class MppClipboardTests(unittest.TestCase):

    def test_copy_clipboard_timeout_kills_child(self):
        ns, g = load_mpp()
        class HungProc:
            def __init__(self, *a, **k):
                self.killed = False
            def communicate(self, data=None, timeout=None):
                raise real_subprocess.TimeoutExpired("tool", 10)
            def kill(self):
                self.killed = True
        hung = HungProc()
        class _SP:
            TimeoutExpired = real_subprocess.TimeoutExpired
            DEVNULL = real_subprocess.DEVNULL
            PIPE = real_subprocess.PIPE
            @staticmethod
            def Popen(*a, **k):
                return hung
        g["subprocess"] = _SP
        g["shutil"] = type("SH", (), {"which": staticmethod(lambda _: "/usr/bin/tool")})()
        result = ns["_copy_clipboard"]("x")
        self.assertIsNone(result)
        self.assertTrue(hung.killed)

    def test_dump_mode_is_pure_and_exits_zero(self):
        ns, g = load_mpp()
        calls = []
        g["subprocess"] = make_fake_subprocess(calls)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = ns["main"](["--code", "dump"])
        self.assertEqual(rc, 0)
        text = out.getvalue()
        for name in ("MTUI", "PAPERFLOW", "PAPERTRAIL", "PAPERTRADE"):
            self.assertIn(name, text.upper())

    def test_copy_mode_verifies_same_family_with_crlf(self):
        ns, g = load_mpp()
        calls = []
        g["subprocess"] = make_fake_subprocess(calls)
        g["_copy_clipboard"] = lambda text: (g.update(_last=text), ["xclip", "-o"])[1]
        g["_readback_clipboard"] = lambda rt: g["_last"].replace("\n", "\r\n")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = ns["main"](["--code", "copy"])
        self.assertEqual(rc, 0)
        self.assertIn("verified", out.getvalue())

    def test_readback_mismatch_returns_one(self):
        ns, g = load_mpp()
        calls = []
        g["subprocess"] = make_fake_subprocess(calls)
        g["_copy_clipboard"] = lambda text: (g.update(_last=text), ["xclip", "-o"])[1]
        g["_readback_clipboard"] = lambda rt: "CORRUPTED"
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = ns["main"](["--code", "copy"])
        self.assertEqual(rc, 1)
        self.assertIn("MISMATCH", out.getvalue())

    def test_broken_pipe_in_dump_mode_is_handled(self):
        ns, g = load_mpp()
        calls = []
        g["subprocess"] = make_fake_subprocess(calls)
        class RaisingWriter:
            def write(self, s):
                raise BrokenPipeError("pipe closed")
            def fileno(self):
                return 999      # not an open fd: dup2 fails harmlessly
            def flush(self):
                pass
        real_stdout = g["sys"].stdout
        g["sys"].stdout = RaisingWriter()
        try:
            rc = ns["main"](["--code", "dump"])   # must not raise
        finally:
            g["sys"].stdout = real_stdout
        self.assertEqual(rc, 0)

    def test_version(self):
        ns, g = load_mpp()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = ns["main"](["--version"])
        self.assertEqual(rc, 0)
        self.assertIn("2.0.1", out.getvalue())

    def test_help(self):
        ns, g = load_mpp()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = ns["main"](["--help"])
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
