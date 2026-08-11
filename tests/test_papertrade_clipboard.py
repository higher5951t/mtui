"""Tests for papertrade's clipboard helpers (stubbed subprocess).

These pin the fix that made the helpers tolerate subprocess timeouts instead
of crashing: `except (subprocess.SubprocessError, OSError)` catches both
CalledProcessError and TimeoutExpired (the old code used the builtin
`TimeoutError`, which never matches `subprocess.TimeoutExpired`).
"""

import runpy
import subprocess as real_subprocess
import unittest
from pathlib import Path

PAPERTRADE = Path(__file__).resolve().parent.parent / "papertrade"


def load_papertrade():
    if not PAPERTRADE.exists():
        raise unittest.SkipTest("papertrade not installed in this environment")
    ns = runpy.run_path(str(PAPERTRADE))
    return ns, ns["copy_to_clipboard"].__globals__


def run_raising(exc):
    def boom(*a, **k):
        raise exc
    return boom


class PapertradeClipboardTests(unittest.TestCase):

    def _stub(self, g, run):
        # The module's except clauses evaluate subprocess.SubprocessError at
        # exception time, so the stub must expose it.
        g["subprocess"] = type(
            "SP", (),
            {"SubprocessError": real_subprocess.SubprocessError,
             "TimeoutExpired": real_subprocess.TimeoutExpired,
             "CalledProcessError": real_subprocess.CalledProcessError,
             "run": staticmethod(run)},
        )()

    def test_copy_timeout_returns_false(self):
        ns, g = load_papertrade()
        self._stub(g, run_raising(
            real_subprocess.TimeoutExpired(["wl-copy"], 30)))
        self.assertIs(ns["copy_to_clipboard"]("x"), False)

    def test_readback_timeout_returns_none(self):
        ns, g = load_papertrade()
        self._stub(g, run_raising(
            real_subprocess.TimeoutExpired(["wl-paste"], 30)))
        g["shutil"] = type("SH", (), {"which": staticmethod(lambda _: "/usr/bin/wl-paste")})()
        self.assertIsNone(ns["readback_clipboard"]())

    def test_copy_calledprocesserror_returns_false(self):
        ns, g = load_papertrade()
        self._stub(g, run_raising(
            real_subprocess.CalledProcessError(1, ["wl-copy"])))
        self.assertIs(ns["copy_to_clipboard"]("x"), False)


if __name__ == "__main__":
    unittest.main()
