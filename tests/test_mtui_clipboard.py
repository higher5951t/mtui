"""Tests for mtui's clipboard helper (read-only, stubbed subprocess)."""

import runpy
import subprocess as real_subprocess
import unittest
from pathlib import Path

MTUI = Path(__file__).resolve().parent.parent / "mtui"


def load_mtui():
    try:
        ns = runpy.run_path(str(MTUI))
    except ImportError as exc:
        # mtui needs `rich` at import time; skip cleanly when it's absent (CI).
        raise unittest.SkipTest(f"mtui dependency unavailable: {exc}")
    return ns, ns["copy_clipboard"].__globals__


def which_only_wl_copy(tool):
    """Only wl-copy exists; xclip/xsel/pbcopy are missing (headless-Wayland box)."""
    return "/usr/bin/wl-copy" if tool == "wl-copy" else None


class MtuiClipboardTests(unittest.TestCase):

    def test_returns_matching_read_back_tool(self):
        ns, g = load_mtui()
        class OkProc:
            def __init__(self, *a, **k):
                self.returncode = 0
            def communicate(self, data=None, timeout=None):
                return (b"", b"")
        class _SP:
            PIPE = real_subprocess.PIPE
            DEVNULL = real_subprocess.DEVNULL
            TimeoutExpired = real_subprocess.TimeoutExpired
            @staticmethod
            def Popen(*a, **k):
                return OkProc()
        g["subprocess"] = _SP
        g["shutil"] = type("SH", (), {"which": staticmethod(which_only_wl_copy)})()
        self.assertEqual(ns["copy_clipboard"]("x"), ["wl-paste", "--no-newline"])

    def test_timeout_kills_child(self):
        ns, g = load_mtui()
        class HungProc:
            def __init__(self, *a, **k):
                self.killed = False
            def communicate(self, data=None, timeout=None):
                raise real_subprocess.TimeoutExpired("wl-copy", 8)
            def kill(self):
                self.killed = True
        hung = HungProc()
        class _SP:
            PIPE = real_subprocess.PIPE
            DEVNULL = real_subprocess.DEVNULL
            TimeoutExpired = real_subprocess.TimeoutExpired
            @staticmethod
            def Popen(*a, **k):
                return hung
        g["subprocess"] = _SP
        g["shutil"] = type("SH", (), {"which": staticmethod(which_only_wl_copy)})()
        self.assertIsNone(ns["copy_clipboard"]("x"))
        self.assertTrue(hung.killed)

    def test_returns_none_when_all_tools_fail(self):
        ns, g = load_mtui()
        class _SP:
            PIPE = real_subprocess.PIPE
            DEVNULL = real_subprocess.DEVNULL
            TimeoutExpired = real_subprocess.TimeoutExpired
            @staticmethod
            def Popen(*a, **k):
                raise OSError("no tool")
        g["subprocess"] = _SP
        g["shutil"] = type("SH", (), {"which": staticmethod(which_only_wl_copy)})()
        self.assertIsNone(ns["copy_clipboard"]("x"))

    def test_empty_copy_is_still_truthy_when_family_works(self):
        # Legacy call site `if copy_clipboard(""):` relies on truthiness.
        ns, g = load_mtui()
        class OkProc:
            def __init__(self, *a, **k):
                self.returncode = 0
            def communicate(self, data=None, timeout=None):
                return (b"", b"")
        class _SP:
            PIPE = real_subprocess.PIPE
            DEVNULL = real_subprocess.DEVNULL
            TimeoutExpired = real_subprocess.TimeoutExpired
            @staticmethod
            def Popen(*a, **k):
                return OkProc()
        g["subprocess"] = _SP
        g["shutil"] = type("SH", (), {"which": staticmethod(which_only_wl_copy)})()
        self.assertTrue(ns["copy_clipboard"](""))


if __name__ == "__main__":
    unittest.main()
