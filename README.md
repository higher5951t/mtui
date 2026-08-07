# mtui — Bloomberg-Style Sovereign Rating Terminal

> A dense, keyboard-driven financial terminal for [**Paperflow**](https://github.com/higher5951t/paperflow)'s GFCR v6.0 sovereign rating engine — think Bloomberg, but zero-cost, offline, and in your terminal.

![Python](https://img.shields.io/badge/python-3.11+-blue)
![Rich](https://img.shields.io/badge/depends-rich-yellow)
![License](https://img.shields.io/badge/license-MIT-green)
![Lines](https://img.shields.io/badge/single--file-2%2C905%20lines-black)

```text
┌────────────────────────────────────────────────────────────────┐
│ MTUI  PAPERFLOW GFCR v6.0 │ MACRO MATRIX  14:22:31  DB:LIVE   │
├───────────┬────────────────────────────────────────────────────┤
│ ◎DASHBOARD│  MACRO MATRIX — sorted by score ▾                 │
│ ◈MACRO M..│  ISO  COUNTRY      SCORE TIER S_EFF FISC EXT DYN  │
│ ⌖COUNTRY..│  USA  United State  78.4 AA   0.62  62%  45% 55%  │
│ ≡POLICY...│  DEU  Germany       74.1 AA   0.64  ...  ▁▂▃▄▆▇█ │
│ ⚠DATA AUD.│  JPN  Japan         54.2 BBB  0.51  55%  ... ...  │
│ ⚙SYSTEM...│  ZMB  Zambia        21.8 CCC  0.29  80%  ▁▂▃▄▅▆▇ │
├───────────┴────────────────────────────────────────────────────┤
│ » : commands · Ctrl+F search       14:21:03 open DEU · sort    │
│ ↑↓ nav · Enter act · Tab→ · 1..6 screens · / palette · q quit  │
└────────────────────────────────────────────────────────────────┘
```

## ✨ What it does

A single-file, keyboard-driven terminal that reads **Paperflow's SQLite databases** (`data/warehouse/paperflow.db` + `gfcr.db`) and turns the GFCR v6.0 11-module sovereign rating engine into a live, Bloomberg-style workstation:

- **📊 Six screens** — Dashboard, Macro Matrix, Country Deep-Dive, Policy Events, Data Audit, System Control
- **📈 Live market tape** — S&P 500, VIX, US 10Y, DXY, Gold, WTI, BTC polled every 15s (toggleable `--offline`)
- **🔍 Global search** — `Ctrl+F` across every Paperflow table; omnibar palette (`/` or `:`) for country/action/screen jumps like `DEU`, `MAT`, `POL`, `DASH`
- **🗺️ Watchlist** — pin/unpin countries with `W`, deep-dive with `Enter`
- **🥘 rich rendering** — ASCII sparklines (▁▂▃▄▅▆▇█), risk bars, 90% credible intervals, per-module mini grids
- **⚙️ ETL control** — run Paperflow's full pipeline, live fetch, or GFCR rating jobs from inside the terminal with a live progress ticker
- **📤 Export** — current view to CSV / clipboard
- **Dense Bloomberg aesthetics** — amber headers, zero chrome, everything reachable by keyboard

## 🧱 How it works

| Piece | Detail |
|---|---|
| **Engine** | Paperflow GFCR v6.0 — an 11-module sovereign capacity model (Johnson SU score transform, REML peer normalization, Kalman dynamical stress, 90% CI via delta method) |
| **Data** | Reads read-only SQLite `paperflow.db` + `gfcr.db`; polls Yahoo Finance for the tape |
| **Architecture** | `TerminalState` dataclass, dictionary key-dispatch tables, split-pane mirror state, `MarketDaemon` + `Job` threads, self-healing connection pool — no hardcoded paths (`PAPERFLOW_DIR` env override) |
| **Resilience** | All errors → `~/.mtui/error.log` + visible toast; never swallowed |

## 📦 Requirements

- **Python 3.10+**
- A running **Paperflow** install (for the databases + ETL commands) — or `PAPERFLOW_DIR=/path/to/paperflow`

```bash
pip install rich
```

## ♟ Install

```bash
# Either run this repo's copy directly…
./mtui

# …or install to PATH (requires paperflow in ~/paperflow, good enough):
ln -sf $(pwd)/mtui ~/.local/bin/mtui     # then: mtui
```

## ⌨️ Usage

```bash
mtui                    # launch the terminal
mtui --offline          # launch without the live market tape
mtui --version          # show version
mtui --code             # copy mtui's full source to your clipboard
mtui --help             # usage
```

> Requires an interactive terminal.

## 🎹 Keybindings

| Key | Action |
|---|---|
| `↑↓` / `k j` | navigate — screens (nav) or rows (content) |
| `Enter` | open deep-dive / run ETL action |
| `Tab` / `→` | focus content · `Esc` / `←` back to nav |
| `1..6` | jump to screen (Dashboard … System Control) |
| `s` / `S` | cycle matrix sort key / flip direction |
| `<` `>` `,` `.` | horizontal scroll in wide tables (`[` `]` = page) |
| `/` or `:` | command palette — country, action or screen |
| `Ctrl+F` | global search across all Paperflow tables |
| `W` | pin/unpin current country on the watchlist |
| `F2` | open the watchlist pane |
| `F` | Data Audit: estimated/fallback rows only |
| `E` | export current view to CSV (+ clipboard) |
| `X` | kill the running ETL job |
| `r` | refresh — re-read both databases |
| `q` / `Ctrl-C` | quit |

## 🔊 Screens

| # | Screen | Contents |
|---|---|---|
| 0 | **Dashboard** | headline ratings, alert counters, market tape |
| 1 | **Macro Matrix** | 40-country GFCR grid — sortable, sparklined |
| 2 | **Country Deep-Dive** | 11-module breakdown, CI bars, 5-yr macro series, LLM narrative |
| 3 | **Policy Events** | reform detection + 5y-before/5y-after analysis |
| 4 | **Data Audit** | provenance grid, imputed-only filter, live ETL control |
| 5 | **System Control** | run/pipe Paperflow jobs, health/status, Streamlit UI |

## 🧩 Works with

- **Paperflow** — the GFCR v6.0 sovereign rating platform → [github.com/higher5951t/paperflow](https://github.com/higher5951t/paperflow)
- 100% offline-capable (demo seed data bundled in Paperflow)

## 📄 License

MIT — free to use, modify, and run. Full credits to the Paperflow GFCR v6.0 team for the rating engine.