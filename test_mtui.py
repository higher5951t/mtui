#!/usr/bin/env python3
"""Unit tests for mtui — diagnose and pin down behavior.

Run standalone:      python3 test_mtui.py
Run with pytest:     pytest test_mtui.py -v

The suite is hermetic: HOME, PAPERFLOW_DIR and PAPERTRADE_DIR are redirected
to a throwaway sandbox and tiny SQLite fixtures are built there, so nothing
touches the real ~/paperflow databases and no network is involved. It pins:

  * the bug fixes (palette typing, export clipboard, market Enter ticker,
    default-ticker removal, watchlist missing-file handling, NaN rendering)
  * the loaders and dispatch/key logic against known fixture data
  * every screen / overlay / split-pane rendering without exceptions
  * a real PTY boot of the interactive loop (integration)
"""

import atexit
import fcntl
import importlib.machinery
import importlib.util
import io
import json
import os
import pty
import select
import shutil
import signal
import sqlite3
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────────────
# Hermetic environment — must happen BEFORE importing mtui (it resolves all
# paths at import time).
# ─────────────────────────────────────────────────────────────────────────────
_SANDBOX = tempfile.mkdtemp(prefix="mtui-test-")
atexit.register(shutil.rmtree, _SANDBOX, True)
os.environ["HOME"] = _SANDBOX
os.environ["PAPERFLOW_DIR"] = os.path.join(_SANDBOX, "paperflow")
os.environ["PAPERTRADE_DIR"] = os.path.join(_SANDBOX, "papertrade")

_MTUI_PATH = Path(__file__).resolve().parent / "mtui"
_loader = importlib.machinery.SourceFileLoader("mtui", str(_MTUI_PATH))
_spec = importlib.util.spec_from_loader("mtui", _loader)
mtui = importlib.util.module_from_spec(_spec)
sys.modules["mtui"] = mtui          # dataclass machinery needs the module visible
_loader.exec_module(mtui)


# ─────────────────────────────────────────────────────────────────────────────
# Fixture databases + fake paperflow CLI
# ─────────────────────────────────────────────────────────────────────────────
def _build_fixtures() -> None:
    wh = Path(os.environ["PAPERFLOW_DIR"]) / "data" / "warehouse"
    wh.mkdir(parents=True, exist_ok=True)

    gf = sqlite3.connect(wh / "gfcr.db")
    gf.executescript("""
        CREATE TABLE countries (iso3 TEXT PRIMARY KEY, name TEXT);
        CREATE TABLE country_ratings_gfcr (
            iso3 TEXT, year INTEGER, gfcr_score REAL, gfcr_tier TEXT, s_eff REAL,
            fiscal_stress REAL, external_vulnerability REAL,
            financial_vulnerability REAL, dynamical_stress REAL,
            strategic_risk REAL, demographic_stress REAL,
            climate_transition_risk REAL, technology_risk REAL,
            supply_chain_risk REAL, resilience_buffer REAL, interaction_term REAL,
            gfcr_score_p05 REAL, gfcr_score_p50 REAL, gfcr_score_p95 REAL,
            johnson_su_gamma REAL, johnson_su_delta REAL, peer_group TEXT);
    """)
    # rows for the current round plus two older years (trend sparkline)
    rows = [
        # iso3, year, score, tier, s_eff, fisc, ext, fin, dyn, strat, demo,
        # clim, tech, sup, res, inter, p05, p50, p95, g, d, peer
        ("USA", 2024, 76.0, "A  — strong", 0.60, 0.30, 0.20, 0.10, 0.40, 0.10, 0.10, 0.10, 0.10, 0.10, 0.10, 0.00, 44.0, 76.0, 95.0, -0.4, 1.2, "Advanced"),
        ("USA", 2025, 77.0, "AA — strong", 0.61, 0.30, 0.20, 0.10, 0.40, 0.10, 0.10, 0.10, 0.10, 0.10, 0.10, 0.00, 44.5, 77.0, 95.5, -0.4, 1.2, "Advanced"),
        ("USA", 2026, 78.4, "AA — strong", 0.62, 0.30, 0.20, 0.10, 0.40, 0.10, 0.10, 0.10, 0.10, 0.10, 0.10, 0.00, 45.5, 78.4, 90.9, -0.4, 1.2, "Advanced"),
        ("DEU", 2026, 74.1, "AA — strong", 0.64, 0.30, 0.20, 0.10, 0.40, 0.10, 0.10, 0.10, 0.10, 0.10, 0.10, 0.00, 42.0, 74.1, 88.0, -0.3, 1.1, "Advanced"),
        ("JPN", 2026, 54.2, "BBB — moderate", 0.51, 0.50, 0.40, 0.30, 0.50, 0.20, 0.20, 0.20, 0.20, 0.20, 0.20, 0.00, 30.0, 54.2, 70.0, -0.2, 1.0, "Advanced"),
        ("ZMB", 2026, 21.8, "CCC — stressed", 0.29, 1.09, 0.80, 0.60, 0.90, 0.60, 0.60, 0.60, 0.60, 0.60, 0.60, 0.00, 8.0, 21.8, None, -0.1, 0.9, "Frontier"),
    ]
    gf.executemany(
        "INSERT INTO country_ratings_gfcr VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows)
    gf.execute("INSERT INTO countries VALUES ('USA','United States'),('DEU','Germany'),"
               "('JPN','Japan'),('ZMB','Zambia')")
    gf.commit()
    gf.close()

    pf = sqlite3.connect(wh / "paperflow.db")
    pf.executescript("""
        CREATE TABLE countries (iso3 TEXT PRIMARY KEY, name TEXT);
        CREATE TABLE macro_indicators (
            iso3 TEXT, indicator_code TEXT, indicator_name TEXT, year INTEGER, value REAL);
        CREATE TABLE policy_events (
            id INTEGER PRIMARY KEY, iso3 TEXT, event_date TEXT, event_type TEXT,
            description TEXT, source_url TEXT);
        CREATE TABLE data_audit (
            iso3 TEXT, year INTEGER, indicator TEXT, source TEXT,
            fallback_source TEXT, imputation_flag TEXT, raw_value REAL, final_value REAL);
        CREATE TABLE country_ratings (iso3 TEXT, year INTEGER, narrative_summary TEXT);
    """)
    # GGXWDG debt series for USA 2015-2026: before 2020 avg 98, after 2020 avg 93.
    # A NaN row for 2026 must be dropped by macro_series.
    macro = []
    for y in range(2015, 2025):
        macro.append(("USA", "GGXWDG", "General government debt", y, float(103 - (y - 2015))))
    macro.append(("USA", "GGXWDG", "General government debt", 2025, 92.0))
    macro.append(("USA", "GGXWDG", "General government debt", 2026, float("nan")))
    macro.append(("USA", "PDB_LV", "GDP per hour worked", 2025, 55.0))
    macro.append(("USA", "PDB_LV", "GDP per hour worked", 2026, 56.5))
    pf.executemany("INSERT INTO macro_indicators VALUES (?,?,?,?,?)", macro)
    pf.execute(
        "INSERT INTO policy_events VALUES (1,'USA','2020-06-01','TAX_REFORM',"
        "'Corporate tax reform package','https://example.org/usa2020')")
    pf.execute(
        "INSERT INTO data_audit VALUES "
        "('USA',2026,'GGXWDG','IMF WEO','WB','actual',90.5,90.5),"
        "('ZMB',2026,'GGXWDG','IMF WEO','synthetic','estimated',NULL,180.0)")
    pf.execute("INSERT INTO country_ratings VALUES ('USA',2026,'Strong external position.')")
    pf.execute("INSERT INTO countries VALUES ('USA','United States'),('DEU','Germany'),"
               "('JPN','Japan'),('ZMB','Zambia')")
    pf.commit()
    pf.close()

    pt_dir = Path(os.environ["PAPERTRADE_DIR"]) / "data"
    pt_dir.mkdir(parents=True, exist_ok=True)
    pt = sqlite3.connect(pt_dir / "papertrade.db")
    pt.executescript("""
        CREATE TABLE paper_backtests (
            id INTEGER PRIMARY KEY, run_date TEXT, signal_type TEXT,
            start_year INTEGER, end_year INTEGER, capital REAL, total_return REAL,
            sharpe REAL, sortino REAL, max_drawdown REAL, hit_rate REAL,
            alpha_vs_benchmark REAL, beta_vs_benchmark REAL, n_positions INTEGER,
            payload TEXT);
        CREATE TABLE paper_nav (backtest_id INTEGER, date TEXT, nav REAL, benchmark REAL);
        CREATE TABLE paper_positions (
            backtest_id INTEGER, iso3 TEXT, ticker TEXT, direction TEXT,
            shares INTEGER, entry_price REAL, entry_date TEXT);
    """)
    pt.execute(
        "INSERT INTO paper_backtests VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (1, "2026-08-03", "GFCR_MOM", 2020, 2025, 100000.0, 0.24, 1.35, 1.1,
         -0.12, 0.62, 0.05, 0.9, 12,
         json.dumps({"attribution": [{"module": "fiscal_stress", "spearman": 0.42,
                                      "p_value": 0.01}]})))
    pt.executemany("INSERT INTO paper_nav VALUES (1,?,?,?)",
                   [("2026-01-01", 1.0, 1.0), ("2026-02-01", 1.05, 1.02), ("2026-03-01", 1.12, 1.04)])
    pt.execute("INSERT INTO paper_positions VALUES (1,'USA','SPY','long',100,500.0,'2026-01-01')")
    pt.commit()
    pt.close()

    # fake paperflow CLI so Job tests never touch a real installation
    bin_dir = Path(os.environ["PAPERFLOW_DIR"]) / "venv" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    fake = bin_dir / "paperflow"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, time\n"
        "print('fake paperflow: ' + ' '.join(sys.argv[1:]), flush=True)\n"
        "if '--sleep' in sys.argv:\n"
        "    time.sleep(60)\n"
        "for i in range(3):\n"
        "    print(f'progress {i}', flush=True)\n"
        "    time.sleep(0.05)\n"
        "print('done', flush=True)\n")
    fake.chmod(0o755)


_build_fixtures()


# ─────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────────────────────────────────────────
def _new_app():
    return mtui.App()


def _render_all_screens(app, cols=120, lines=40):
    """Render every screen + overlay + split-pane layout; raises on failure."""
    s = app.s
    s.offline = True                       # deterministic: no network
    out = io.StringIO()
    console = mtui.Console(width=cols, height=lines, force_terminal=True, file=out)
    for i, scr in enumerate(mtui.SCREENS):
        s.screen, s.mode = i, "normal"
        console.print(mtui.build_screen(s, app, cols, lines))
    for mode, q in (("palette", "usa"), ("search", "debt"), ("watchlist", ""), ("help", "")):
        s.mode, s.query = mode, q
        console.print(mtui.build_screen(s, app, cols, lines))
    s.mode = "normal"
    s.screen, s.matrix_heatmap = 1, True
    console.print(mtui.build_screen(s, app, cols, lines))
    s.matrix_heatmap = False
    s.layout = "hsplit"
    console.print(mtui.build_screen(s, app, cols, lines))
    s.focus = "pane2"
    console.print(mtui.build_screen(s, app, cols, lines))
    s.layout, s.focus = "single", "nav"


# ─────────────────────────────────────────────────────────────────────────────
# Formatting helpers
# ─────────────────────────────────────────────────────────────────────────────
class TestFormatting(unittest.TestCase):
    def test_pct_handles_none_and_nan(self):
        self.assertEqual(mtui.pct(None), "—")
        self.assertEqual(mtui.pct(float("nan")), "—")
        self.assertEqual(mtui.pct(0.5), "50%")

    def test_num_or_str_nan(self):
        self.assertEqual(mtui.num_or_str(float("nan")), "—")
        self.assertEqual(mtui.num_or_str(None), "—")

    def test_score_and_risk_cells_nan(self):
        self.assertEqual(mtui.score_cell(float("nan")).plain, "—")
        self.assertEqual(mtui.risk_cell(float("nan")).plain, "—")
        # risk values above 1.0 (real data does this) render, not crash
        self.assertEqual(mtui.risk_cell(1.09).plain, "109%")

    def test_ci_bar_nan_does_not_crash(self):
        # regression: int(NaN) used to raise ValueError
        self.assertEqual(mtui.ci_bar(float("nan"), 50, 90).plain, "—")
        self.assertEqual(mtui.ci_bar(None, 50, 90).plain, "—")
        bar = mtui.ci_bar(0.0, 50.0, 100.0).plain
        self.assertIn("●", bar)
        self.assertIn("┃", bar)

    def test_risk_bar(self):
        self.assertEqual(len(mtui.risk_bar(0.9, 10).plain), 10)
        self.assertEqual(len(mtui.risk_bar(float("nan"), 10).plain), 10)  # clamps to 0

    def test_sparkline8(self):
        self.assertEqual(mtui.sparkline8([]).plain, "·" * 7)
        self.assertEqual(mtui.sparkline8([1.0]).plain, "·" * 7)          # single round
        self.assertEqual(mtui.sparkline8([1.0, 1.0, 1.0]).plain, "▄" * 7)  # flat
        self.assertEqual(len(mtui.sparkline8([0, 1, 2, 3, 4, 5, 6, 7, 8]).plain), 7)
        # NaN samples are dropped; only the two real points remain
        self.assertEqual(len(mtui.sparkline8([float("nan"), 1, 2]).plain), 2)

    def test_tier_helpers(self):
        self.assertEqual(mtui.tier_short("AA — strong"), "AA")
        self.assertEqual(mtui.tier_sort("AAA — top"), 1)
        self.assertEqual(mtui.tier_sort("CCC"), 7)

    def test_col_window(self):
        cols = [("A", 4, None), ("B", 4, None), ("C", 4, None)]
        self.assertEqual(mtui.col_window(cols, 0, 12), (0, 2))   # 2×(4+2)=12 fits
        self.assertEqual(mtui.col_window(cols, 5, 12), (1, 3))   # scrolled

    def test_pad_pair(self):
        t = mtui.pad_pair(mtui.Text("L"), mtui.Text("R"), 20)
        self.assertEqual(len(t.plain), 20)


class _AppCase(unittest.TestCase):
    """Creates a fresh App per test and always closes its DB pool."""
    def setUp(self):
        self.app = _new_app()

    def tearDown(self):
        if getattr(self, "app", None) is not None:
            self.app.pool.close()


# ─────────────────────────────────────────────────────────────────────────────
# App loaders against the fixture DBs
# ─────────────────────────────────────────────────────────────────────────────
class TestAppLoaders(_AppCase):

    def test_rating_year_and_alerts(self):
        self.assertEqual(self.app.rating_year, 2026)
        # only ZMB (21.8) is below the 40 warning band in the current round
        self.assertEqual(self.app.alert_count(), 1)

    def test_countries(self):
        isos = {c["iso"] for c in self.app.countries()}
        self.assertIn("USA", isos)
        self.assertIn("ZMB", isos)

    def test_matrix_data(self):
        data = self.app.matrix_data()
        self.assertEqual(len(data), 4)
        usa = next(d for d in data if d["iso"] == "USA")
        self.assertEqual(usa["gfcr_score"], 78.4)
        self.assertEqual(usa["name"], "United States")
        self.assertEqual(len(usa["trend"]), 3)          # 2024/2025/2026 rounds
        # score present for every row
        self.assertTrue(all(d["gfcr_score"] is not None for d in data))

    def test_heatmap_data(self):
        data = self.app.heatmap_data()
        self.assertEqual(len(data), 4)
        self.assertIn("fiscal_stress", data[0])
        self.assertIn("interaction_term", data[0])

    def test_macro_series_drops_nan(self):
        series = self.app.macro_series("USA")
        years = [y for y, _ in series["GGXWDG"]]
        self.assertNotIn(2026, years)                    # NaN row removed
        self.assertEqual(max(years), 2025)
        # 2020 debt = 103 - 5 = 98, 2024 = 103 - 9 = 94
        self.assertEqual(series["GGXWDG"][0], (2015, 103.0))

    def test_policy_impact(self):
        impact = dict((name, (b, a, d)) for name, b, a, d in self.app.policy_impact("USA", 2020))
        b, a, d = impact["Debt % GDP"]
        self.assertAlmostEqual(b, 101.0)                 # avg 2015-2019
        self.assertAlmostEqual(a, 96.0)                  # avg 2020-2024
        self.assertAlmostEqual(d, -5.0)                  # debt falling = improvement

    def test_audit_rows_filter(self):
        all_rows = self.app.audit_rows(False)
        self.assertEqual(len(all_rows), 2)
        self.assertIn("actual", {r["imputation_flag"] for r in all_rows})
        est = self.app.audit_rows(True)
        self.assertEqual(len(est), 1)
        self.assertEqual(est[0]["imputation_flag"], "estimated")

    def test_narrative(self):
        self.assertIn("Strong", self.app.narrative("USA"))

    def test_papertrade(self):
        runs = self.app.papertrade_runs()
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["payload"]["attribution"][0]["module"], "fiscal_stress")
        self.assertEqual(len(self.app.papertrade_nav(1)), 3)
        self.assertEqual(len(self.app.papertrade_positions(1)), 1)


# ─────────────────────────────────────────────────────────────────────────────
# Key dispatch / state machine (includes the fixed typing bug)
# ─────────────────────────────────────────────────────────────────────────────
class TestDispatch(_AppCase):
    def setUp(self):
        super().setUp()
        self.s = self.app.s

    def test_palette_typing_not_eaten_by_vim_aliases(self):
        # regression: j/k/g/G/J/K used to be remapped to navigation even while
        # typing, so "japan" came out as "apan"
        mtui.handle_key(self.s, self.app, "/")
        for ch in "japan":
            mtui.handle_key(self.s, self.app, ch)
        self.assertEqual(self.s.query, "japan")

    def test_vim_aliases_still_work_in_normal_mode(self):
        start = self.s.screen
        mtui.handle_key(self.s, self.app, "k")           # k == UP
        self.assertEqual(self.s.screen, (start - 1) % len(mtui.SCREENS))
        mtui.handle_key(self.s, self.app, "j")           # j == DOWN
        self.assertEqual(self.s.screen, start)

    def test_palette_flow_jumps_to_country(self):
        mtui.handle_key(self.s, self.app, "/")
        for ch in "usa":
            mtui.handle_key(self.s, self.app, ch)
        mtui.render_palette(self.s, self.app, 30)   # main loop repaints before Enter
        mtui.handle_key(self.s, self.app, "ENTER")
        self.assertEqual(self.s.screen, 2)               # country deep-dive
        self.assertEqual(self.s.sel_iso, "USA")

    def test_search_flow_opens_deep_dive(self):
        mtui.handle_key(self.s, self.app, "CTRL_F")
        for ch in "debt":
            mtui.handle_key(self.s, self.app, ch)
        mtui.render_search(self.s, self.app, 30)    # populate state.results
        self.assertTrue(self.s.results)
        mtui.handle_key(self.s, self.app, "ENTER")
        self.assertEqual(self.s.screen, 2)
        self.assertEqual(self.s.sel_iso, "USA")

    def test_screen_hotkeys_and_quit(self):
        mtui.handle_key(self.s, self.app, "8")
        self.assertEqual(self.s.screen, 7)               # papertrade
        mtui.handle_key(self.s, self.app, "1")
        self.assertEqual(self.s.screen, 0)               # dashboard
        mtui.handle_key(self.s, self.app, "q")
        self.assertTrue(self.s.quit)

    def test_heatmap_toggle_only_on_matrix(self):
        mtui.handle_key(self.s, self.app, "2")           # matrix
        mtui.handle_key(self.s, self.app, "H")
        self.assertTrue(self.s.matrix_heatmap)
        mtui.handle_key(self.s, self.app, "H")
        self.assertFalse(self.s.matrix_heatmap)

    def test_sort_cycle(self):
        mtui.handle_key(self.s, self.app, "2")
        mtui.handle_key(self.s, self.app, "s")           # score -> country
        self.assertEqual(self.s.sort_key, "country")
        mtui.handle_key(self.s, self.app, "S")
        self.assertTrue(self.s.sort_asc)

    def test_omni_iso_and_function_and_macro(self):
        # ISO jump
        mtui.handle_key(self.s, self.app, " ")
        for ch in "DEU":
            mtui.handle_key(self.s, self.app, ch)
        mtui.handle_key(self.s, self.app, "ENTER")
        self.assertEqual(self.s.screen, 2)
        self.assertEqual(self.s.sel_iso, "DEU")
        # function code
        mtui.handle_key(self.s, self.app, " ")
        for ch in "MAT":
            mtui.handle_key(self.s, self.app, ch)
        mtui.handle_key(self.s, self.app, "ENTER")
        self.assertEqual(self.s.screen, 1)
        # macro code -> global search
        mtui.handle_key(self.s, self.app, " ")
        for ch in "GGXWDG":
            mtui.handle_key(self.s, self.app, ch)
        mtui.handle_key(self.s, self.app, "ENTER")
        self.assertEqual(self.s.mode, "search")
        self.assertEqual(self.s.query, "GGXWDG")

    def test_omni_add_ticker(self):
        mtui.handle_key(self.s, self.app, "a")           # h_market_add
        self.assertEqual(self.s.mode, "omni")
        for ch in "TSLA":
            mtui.handle_key(self.s, self.app, ch)
        mtui.handle_key(self.s, self.app, "ENTER")
        self.assertEqual([t[0] for t in self.s.market_custom], ["TSLA"])


# ─────────────────────────────────────────────────────────────────────────────
# Live Markets behavior
# ─────────────────────────────────────────────────────────────────────────────
class TestMarket(_AppCase):
    def setUp(self):
        super().setUp()
        self.s = self.app.s
        self.s.offline = False
        self.s.market_live = True
        self.s.market_data = {
            "SPX": {"px": 100.0, "chg": 1.0, "pct": 1.0, "label": "S&P",
                    "symbol": "^GSPC", "category": "EQ-IDX", "history": [1, 2]},
            "VIX": {"px": 200.0, "chg": 4.0, "pct": 2.0, "label": "Vol",
                    "symbol": "^VIX", "category": "VOL", "history": [3, 4]},
        }

    def test_market_enter_resolves_displayed_item(self):
        # regression: used to index the raw market_data dict by market_sel,
        # which reported the wrong ticker once filter/sort reordered the view
        self.s.screen, self.s.focus = 6, "content"
        self.s.market_filter_cat = "VOL"
        self.s.market_sel = 0
        mtui.h_enter(self.s, self.app)
        self.assertEqual(self.s.toast, "detail: Vol")

    def test_market_sort_and_filter(self):
        self.s.market_sort = "pct"
        items = mtui._market_items(self.s)
        self.assertEqual([t for t, _ in items], ["VIX", "SPX"])
        self.s.market_filter_cat = "EQ-IDX"
        items = mtui._market_items(self.s)
        self.assertEqual([t for t, _ in items], ["SPX"])

    def test_default_ticker_cannot_be_removed(self):
        # regression: 'd' used to pop a default ticker's snapshot
        self.s.market_sel = 0
        mtui.h_market_remove(self.s, self.app)
        self.assertIn("SPX", self.s.market_data)
        self.assertIn("default", self.s.toast)

    def test_custom_ticker_can_be_removed(self):
        self.s.market_custom = [("TSLA", "TSLA", "Tesla", "EQ")]
        self.s.market_data["TSLA"] = {"px": 1, "chg": 0, "pct": 0, "label": "Tesla",
                                      "symbol": "TSLA", "category": "EQ", "history": [1]}
        self.s.market_sel = len(mtui._market_items(self.s)) - 1   # last row = TSLA
        mtui.h_market_remove(self.s, self.app)
        self.assertNotIn("TSLA", self.s.market_data)
        self.assertNotIn("TSLA", [t[0] for t in self.s.market_custom])


# ─────────────────────────────────────────────────────────────────────────────
# Export + clipboard
# ─────────────────────────────────────────────────────────────────────────────
class TestExport(_AppCase):
    def setUp(self):
        super().setUp()
        self.s = self.app.s
        for f in mtui.EXPORT_DIR.glob("*.csv"):
            f.unlink()
        self._writes = []
        writes = self._writes
        self._popen = subprocess.Popen
        self._which = mtui.shutil.which

        class FakePopen:
            def __init__(self, tool, stdin=None, stdout=None, stderr=None):
                self.returncode = 0

            def communicate(self, data, timeout=None):
                writes.append(data)
                return (b"", b"")

        subprocess.Popen = FakePopen
        mtui.shutil.which = lambda t: t          # pretend every clipboard tool exists

    def tearDown(self):
        subprocess.Popen = self._popen
        mtui.shutil.which = self._which

    def test_export_writes_file_and_single_clipboard_copy(self):
        # regression: a second copy_clipboard("") probe wiped the clipboard
        self.s.screen = 1                           # macro matrix
        mtui.h_export(self.s, self.app)
        self.assertEqual(len(self._writes), 1)
        data = self._writes[0]
        self.assertTrue(data.startswith(b"iso3,country,score,tier"))
        self.assertNotEqual(data, b"")              # never an empty overwrite
        self.assertIn("exported", self.s.toast)
        files = list(mtui.EXPORT_DIR.glob("matrix_*.csv"))
        self.assertEqual(len(files), 1)
        self.assertIn(b"ZMB", files[0].read_bytes())

    def test_export_market_screen_is_a_noop(self):
        self.s.screen = 6
        mtui.h_export(self.s, self.app)
        self.assertEqual(self.s.toast, "nothing to export here")

    def test_copy_clipboard_no_tools(self):
        mtui.shutil.which = lambda t: None
        # no clipboard tool -> False (nothing copied), never raises
        self.assertFalse(mtui.copy_clipboard("hello"))


# ─────────────────────────────────────────────────────────────────────────────
# Watchlist persistence
# ─────────────────────────────────────────────────────────────────────────────
class TestWatchlist(unittest.TestCase):
    def setUp(self):
        if mtui.WATCHLIST_PATH.exists():
            mtui.WATCHLIST_PATH.unlink()
        self._apps = []

    def tearDown(self):
        for a in self._apps:
            a.pool.close()

    def _mk(self):
        app = _new_app()
        self._apps.append(app)
        return app

    def test_missing_file_is_silent_first_run(self):
        # regression: every launch logged a full traceback when the file was absent
        calls = []
        orig = mtui.log_error
        mtui.log_error = lambda msg: calls.append(msg)
        try:
            app = self._mk()
        finally:
            mtui.log_error = orig
        self.assertEqual(calls, [])
        self.assertEqual(app.s.watch, set())

    def test_save_load_roundtrip(self):
        app = self._mk()
        app.s.watch = {"USA", "zmb"}
        app.save_watchlist()
        app2 = self._mk()
        self.assertEqual(app2.s.watch, {"USA", "ZMB"})

    def test_corrupt_file_logs_once_and_resets(self):
        mtui.WATCHLIST_PATH.write_text("{not json")
        calls = []
        orig = mtui.log_error
        mtui.log_error = lambda msg: calls.append(msg)
        try:
            app = self._mk()
        finally:
            mtui.log_error = orig
        self.assertEqual(len(calls), 1)
        self.assertEqual(app.s.watch, set())

    def test_toggle_watch_from_matrix(self):
        app = self._mk()
        s = app.s
        s.screen, s.focus = 1, "content"
        mtui.render_matrix(s, app, 120, 40)         # populates s.order
        s.sel = 0                                    # top row = USA (highest score)
        mtui.h_toggle_watch(s, app)
        self.assertIn("USA", s.watch)
        mtui.h_toggle_watch(s, app)
        self.assertNotIn("USA", s.watch)


# ─────────────────────────────────────────────────────────────────────────────
# Workspaces
# ─────────────────────────────────────────────────────────────────────────────
class TestWorkspaces(_AppCase):
    def test_save_and_cycle(self):
        s = self.app.s
        s.screen, s.sel_iso = 2, "DEU"
        s.layout = "hsplit"
        mtui.h_workspace_save(s, self.app)
        self.assertTrue(mtui.WORKSPACES_PATH.exists())
        s.layout = "single"
        s.sel_iso = "USA"
        mtui.h_workspace_cycle(s, self.app)
        self.assertEqual(s.layout, "hsplit")
        self.assertEqual(s.sel_iso, "DEU")


# ─────────────────────────────────────────────────────────────────────────────
# DBPool
# ─────────────────────────────────────────────────────────────────────────────
class TestDBPool(unittest.TestCase):
    def tearDown(self):
        if getattr(self, "pool", None) is not None:
            self.pool.close()

    def test_missing_db_returns_none(self):
        pool = mtui.DBPool([mtui.WAREHOUSE / "nope.db"])
        self.pool = pool
        self.assertIsNone(pool.query(mtui.WAREHOUSE / "nope.db", "SELECT 1"))
        self.assertEqual(pool.scalar(mtui.WAREHOUSE / "nope.db", "SELECT 1", default=7), 7)

    def test_self_healing_after_stale_connection(self):
        pool = _new_app().pool
        self.pool = pool
        rows = pool.query(mtui.GFCR_DB, "SELECT COUNT(*) FROM country_ratings_gfcr")
        self.assertTrue(rows)
        con = pool._conns[mtui.GFCR_DB]
        con.close()                                  # simulate a stale/dropped conn
        rows2 = pool.query(mtui.GFCR_DB, "SELECT COUNT(*) FROM country_ratings_gfcr")
        self.assertTrue(rows2)
        self.assertEqual(rows[0][0], rows2[0][0])


# ─────────────────────────────────────────────────────────────────────────────
# ETL Job runner (against the fake paperflow CLI)
# ─────────────────────────────────────────────────────────────────────────────
class TestJob(_AppCase):
    def setUp(self):
        super().setUp()
        self.orig_cmd = mtui.PAPERFLOW_CMD
        mtui.PAPERFLOW_CMD = str(mtui.PAPERFLOW_DIR / "venv" / "bin" / "paperflow")

    def tearDown(self):
        mtui.PAPERFLOW_CMD = self.orig_cmd

    def test_job_runs_to_completion(self):
        self.app.run_job("test", ["--x"])
        self.assertTrue(self.app.s.job.running)
        deadline = time.monotonic() + 10
        while not self.app.s.job.done and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(self.app.s.job.rc, 0)
        self.assertIn("done", self.app.s.job.lines)
        self.assertIn("fake paperflow: --x", self.app.s.job.lines)

    def test_job_kill(self):
        self.app.run_job("sleepy", ["--sleep"])
        self.assertTrue(self.app.s.job.running)
        self.app.kill_job()
        deadline = time.monotonic() + 5
        while self.app.s.job.running and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(self.app.s.job.running)
        self.assertIsNotNone(self.app.s.job.rc)


# ─────────────────────────────────────────────────────────────────────────────
# Rendering every screen / overlay / split layout without exceptions
# ─────────────────────────────────────────────────────────────────────────────
class TestRender(_AppCase):

    def test_all_screens_render(self):
        _render_all_screens(self.app)

    def test_every_country_deep_dive_renders(self):
        s = self.app.s
        s.offline = True
        out = io.StringIO()
        console = mtui.Console(width=120, height=40, force_terminal=True, file=out)
        for d in self.app.matrix_data():
            s.screen, s.sel_iso = 2, d["iso"]
            console.print(mtui.build_screen(s, self.app, 120, 40))

    def test_every_policy_event_renders(self):
        s = self.app.s
        s.offline = True
        out = io.StringIO()
        console = mtui.Console(width=120, height=40, force_terminal=True, file=out)
        events = self.app.policy_events()
        for i in range(len(events)):
            s.screen, s.policy_sel = 3, i
            console.print(mtui.build_screen(s, self.app, 120, 40))


# ─────────────────────────────────────────────────────────────────────────────
# Integration: boot the real interactive loop on a PTY
# ─────────────────────────────────────────────────────────────────────────────
class TestIntegration(unittest.TestCase):
    def test_pty_boot_navigate_and_quit(self):
        pid, fd = pty.fork()
        if pid == 0:
            os.execv(sys.executable, [sys.executable, str(_MTUI_PATH), "--offline"])
        try:
            fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
            buf = b""
            start = time.monotonic()
            sent = False
            while time.monotonic() - start < 15:
                r, _, _ = select.select([fd], [], [], 0.5)
                if r:
                    try:
                        data = os.read(fd, 65536)
                    except OSError:
                        break
                    if not data:
                        break
                    buf += data
                if not sent and time.monotonic() - start > 3:
                    os.write(fd, b"\x1b[B")          # Down arrow on the nav rail
                    time.sleep(0.2)
                    os.write(fd, b"2")               # Macro Matrix
                    time.sleep(0.2)
                    os.write(fd, b"\x1b[20~")        # F9 -> split panes
                    time.sleep(0.2)
                    os.write(fd, b"\t")              # Tab -> content
                    time.sleep(0.2)
                    os.write(fd, b"q")               # quit
                    sent = True
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass
        text = buf.decode("utf-8", "replace")
        self.assertIn("MTUI", text)
        self.assertIn("MACRO MATRIX", text)
        self.assertIn("mtui — bye", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
