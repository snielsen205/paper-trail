"""
dashboard.py — Milestone 4: the dashboard, wired to real data.

A tiny web server (Python stdlib only, per project rules) that serves the
Movers dashboard in your browser. It makes ZERO Alpha Vantage calls — it
only reads what previous runs already saved:

  snapshots/scan_*.json         -> gainers/losers panel
  snapshots/top10_*.json        -> Today's Top 10 setups
  snapshots/track_record.jsonl  -> Track Record tab
  cache/*EARNINGS_CALENDAR*.csv -> Calendar tab (upcoming earnings)

So the flow stays: run scan.py + score.py to get fresh data, then hit
Refresh in the dashboard to re-read the files.

Run it:  python3 dashboard.py       then open  http://localhost:5052
"""

import csv
import glob
import io
import json
import os
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from zoneinfo import ZoneInfo

from av_client import load_config

HERE = os.path.dirname(os.path.abspath(__file__))
SNAP_DIR = os.path.join(HERE, "snapshots")


def bot_payload():
    """Main-bot snapshot enriched with LIVE position P&L (one Alpaca
    positions call, shared conceptually with momentum_payload) plus
    realized P&L summed from the ledger's exit rows. Fail-soft to the
    plain snapshot if the live fetch fails."""
    snap = load_json(os.path.join(SNAP_DIR, "bot_account.json"))
    if snap is None:
        return None

    # Realized P&L: every graded exit in the append-only ledger.
    realized, closed = 0.0, 0
    log_path = os.path.join(SNAP_DIR, "bot_log.jsonl")
    if os.path.exists(log_path):
        with open(log_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if r.get("event") == "exit" and r.get("exit_price") is not None:
                    sign = 1 if r.get("direction") == "LONG" else -1
                    realized += sign * (r["exit_price"] - r["fill_price"]) * r["qty"]
                    closed += 1
    snap["realized_pl"] = round(realized, 2)
    snap["closed_trades"] = closed

    # Live open P&L, direction-aware straight from Alpaca.
    try:
        from alpaca_client import Alpaca
        live = {p["symbol"]: p for p in Alpaca(load_config()).positions()}
        open_pl = open_cost = 0.0
        for pos in snap.get("positions", []):
            lp = live.get(pos.get("ticker"))
            if not lp:
                continue
            pos["now"] = float(lp["current_price"])
            pos["unrealized_pl"] = round(float(lp["unrealized_pl"]), 2)
            pos["unrealized_plpc"] = round(float(lp["unrealized_plpc"]) * 100, 2)
            open_pl += float(lp["unrealized_pl"])
            open_cost += abs(float(lp["cost_basis"]))
        snap["open_pl"] = round(open_pl, 2)
        snap["open_plpc"] = round(open_pl / open_cost * 100, 2) if open_cost else 0.0
        snap["live"] = True
    except Exception:
        snap["live"] = False
    return snap


def analyst_payload():
    """The shadow analyst's verdicts + grading, read from its snapshot
    (no API calls — analyst.py writes analyst_account.json)."""
    return load_json(os.path.join(SNAP_DIR, "analyst_account.json"))


def edgar_payload():
    """The morning SEC filing read (edgar_watch.py writes edgar_latest.json).
    Snapshot only — no network call from the dashboard."""
    return load_json(os.path.join(SNAP_DIR, "edgar_latest.json"))


def edgar_movers_payload():
    """The 10:15 scan of TODAY'S MOVERS — why the day's big names moved.
    Separate file from the watchlist read; different question, different list."""
    return load_json(os.path.join(SNAP_DIR, "edgar_movers_latest.json"))


def edgar_market_payload():
    """The MARKET-WIDE on-demand scan (`edgar_watch.py --market`) — the one that
    finds CLRO-type deal news on ANY filer, not just the watchlist.

    Wired in 2026-08-17. It had been writing edgar_market_latest.json since it
    was built and NOTHING served it, so every market scan lived only in terminal
    scrollback — including the run that caught ARX +43% on a signed merger.
    Unlike the other two this has NO cron: it only refreshes when the owner runs
    it, so the tab shows its own timestamp and the UI must not imply it's live."""
    return load_json(os.path.join(SNAP_DIR, "edgar_market_latest.json"))


def fund_book_payload():
    """The analyst's SIMULATED book (analyst_book.py writes fund_account.json).
    Pure snapshot read — no API calls and, by construction, no broker: the book
    places no orders and holds nothing in the real paper account."""
    return load_json(os.path.join(SNAP_DIR, "fund_account.json"))


def review_payload():
    """The learning review, computed live from the ledgers on each load
    (read-only, no API calls). Fail-soft to None so a review error can never
    take the dashboard down."""
    try:
        from review import build_review
        return build_review()
    except Exception:
        return None


def momentum_payload():
    """Momentum snapshot, enriched with LIVE positions + P&L via one Alpaca
    call so the Momentum tab shows real-time gains, not the last rebalance's
    frozen pre-open state. Fail-soft: if the live fetch fails (offline, keys),
    fall back to whatever the on-disk snapshot holds (kept fresh by the daily
    `momentum_sleeve.py --refresh`). This is the ONLY live API call the
    dashboard makes; everything else is still pure snapshot reads."""
    snap = load_json(os.path.join(SNAP_DIR, "momentum_account.json"))
    if snap is None:
        return None
    try:
        from alpaca_client import Alpaca
        from momentum_sleeve import current_holdings, sleeve_totals
        holdings = current_holdings(Alpaca(load_config()))
        if holdings:
            snap["positions"] = holdings
            snap.update(sleeve_totals(holdings))
            snap["live"] = True
    except Exception:
        snap["live"] = False
    return snap


def macro_payload():
    """Macro-sleeve snapshot, enriched with LIVE ETF positions + P&L via one
    Alpaca call (same pattern as momentum_payload). Fail-soft: if the live fetch
    fails, fall back to the on-disk snapshot (kept fresh by the daily
    `macro_sleeve.py --refresh`)."""
    snap = load_json(os.path.join(SNAP_DIR, "macro_account.json"))
    if snap is None:
        return None
    try:
        from alpaca_client import Alpaca
        from macro_sleeve import current_holdings, sleeve_totals
        holdings = current_holdings(Alpaca(load_config()))
        if holdings:
            snap["positions"] = holdings
            snap.update(sleeve_totals(holdings))
            snap["live"] = True
    except Exception:
        snap["live"] = False
    return snap
CACHE_DIR = os.path.join(HERE, "cache")
LEDGER = os.path.join(SNAP_DIR, "track_record.jsonl")
DEFAULT_PORT = 5052


# ---------------- data assembly (all reads, no API calls) ----------------

def latest(pattern):
    files = sorted(glob.glob(os.path.join(SNAP_DIR, pattern)))
    return files[-1] if files else None


def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def load_ledger():
    if not os.path.exists(LEDGER):
        return []
    rows = []
    with open(LEDGER) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return rows


def load_earnings_week(pool_tickers, days_ahead=7):
    """Upcoming earnings (next N days) for tickers in today's pool,
    read from the cached calendar CSV. No pool/cache -> empty list."""
    files = glob.glob(os.path.join(CACHE_DIR, "*EARNINGS_CALENDAR*.csv"))
    if not files or not pool_tickers:
        return []
    today = datetime.now().date()
    horizon = today + timedelta(days=days_ahead)
    by_date = {}
    with open(max(files, key=os.path.getmtime)) as f:
        for row in csv.DictReader(f):
            sym = (row.get("symbol") or "").strip()
            raw = (row.get("reportDate") or "").strip()
            if sym not in pool_tickers or not raw:
                continue
            try:
                d = datetime.strptime(raw, "%Y-%m-%d").date()
            except ValueError:
                continue
            if today <= d <= horizon:
                by_date.setdefault(str(d), []).append({
                    "ticker": sym,
                    "name": (row.get("name") or "").strip(),
                    "estimate": (row.get("estimate") or "").strip(),
                })
    return [{"date": d, "items": items}
            for d, items in sorted(by_date.items())]


def spark_from_cache(ticker, points=8):
    """Last N daily adjusted closes IF a cached series exists (it will for
    SPY and graded tickers). Missing -> None; the page just hides the line."""
    pattern = os.path.join(CACHE_DIR, f"*{ticker}_TIME_SERIES_DAILY_ADJUSTED*.json")
    files = glob.glob(pattern)
    if not files:
        return None
    data = load_json(max(files, key=os.path.getmtime))
    if not data:
        return None
    series = data.get("Time Series (Daily)", {})
    closes = []
    for d in sorted(series.keys())[-points:]:
        try:
            closes.append(float(series[d]["5. adjusted close"]))
        except (KeyError, ValueError):
            return None
    return closes if len(closes) >= 3 else None


def market_open_now():
    """Rough open/closed pill: Mon-Fri 9:30-16:00 ET (ignores holidays)."""
    now = datetime.now(ZoneInfo("America/New_York"))
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return 9 * 60 + 30 <= minutes < 16 * 60


def build_payload():
    config = load_config()

    scan_path = latest("scan_*.json")
    top10_path = latest("top10_*.json")
    scan = load_json(scan_path) if scan_path else None
    top10 = load_json(top10_path) if top10_path else None

    survivors = (scan or {}).get("survivors", [])
    gainers = sorted([s for s in survivors if s["change_pct"] >= 0],
                     key=lambda s: -s["change_pct"])[:8]
    losers = sorted([s for s in survivors if s["change_pct"] < 0],
                    key=lambda s: s["change_pct"])[:8]

    picks = []
    for p in (top10 or []):
        picks.append({**p, "spark": spark_from_cache(p["ticker"])})

    pool_tickers = {s["ticker"] for s in survivors}
    pool_tickers.update(p["ticker"] for p in picks)

    dials = dict(config.get("bot_dials", {}))       # safe: no key in here
    scoring = dict(config.get("scoring", {}))

    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "market_open": market_open_now(),
        "feed_last_updated": (scan or {}).get("feed_last_updated"),
        "scan_file": os.path.basename(scan_path) if scan_path else None,
        "top10_file": os.path.basename(top10_path) if top10_path else None,
        "top10": picks,
        "gainers": gainers,
        "losers": losers,
        "track_record": load_ledger(),
        "calendar": load_earnings_week(pool_tickers),
        "bot_dials": dials,
        "bot": bot_payload(),
        "momentum": momentum_payload(),
        "macro": macro_payload(),
        "review": review_payload(),
        "analyst": analyst_payload(),
        "fund_book": fund_book_payload(),
        "edgar": edgar_payload(),
        "edgar_movers": edgar_movers_payload(),
        "edgar_market": edgar_market_payload(),
        "scoring": scoring,
        "score_floor": dials.get("score_floor", 80),
    }


# ---------------- the server ----------------

class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            with open(os.path.join(HERE, "dashboard.html"), "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
        # NOTE: the /polybot route was REMOVED 2026-08-12 with the Poly Bot tab
        # (owner's call). polybot/ still exists on disk but nothing serves it.
        elif self.path == "/api/data":
            try:
                body = json.dumps(build_payload()).encode()
                self._send(200, body, "application/json")
            except Exception as e:
                err = json.dumps({"error": str(e)}).encode()
                self._send(500, err, "application/json")
        else:
            self._send(404, b"not found", "text/plain")

    def log_message(self, fmt, *args):
        pass                                        # keep the terminal quiet


def lan_ip():
    """This Mac's address on the home network, for phone access.
    Best-effort — returns None if it can't be determined."""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))          # no data sent; just picks a route
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return None


def main():
    config = load_config()
    dash = config.get("dashboard", {})
    port = dash.get("port", DEFAULT_PORT)
    # "lan": true (default) also serves phones/tablets on your own Wi-Fi.
    # The dashboard is read-only and never sends any API key.
    host = "0.0.0.0" if dash.get("lan", True) else "127.0.0.1"
    server = HTTPServer((host, port), Handler)
    print(f"\nMovers dashboard running:  http://localhost:{port}")
    if host == "0.0.0.0":
        ip = lan_ip()
        if ip:
            print(f"On your phone (same Wi-Fi):  http://{ip}:{port}")
    print("It reads snapshots only — run scan.py / score.py for fresh data.")
    print("Stop it with Ctrl+C.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")


if __name__ == "__main__":
    main()
