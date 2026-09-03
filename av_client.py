"""
av_client.py — the data layer.
Talks to Alpha Vantage with three protections built in:
  1. Rate limiter: never exceeds the requests/minute in config.json
  2. Cache: anything static (earnings dates, overviews) isn't re-fetched all day
  3. Retries: one transient network hiccup doesn't kill the scan
Both the screener and the bot import this file. Nothing else in the
project talks to the API directly.
"""

import json
import time
import os
import urllib.request
import urllib.parse

BASE_URL = "https://www.alphavantage.co/query"
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")


def load_config():
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "config.json")
    if not os.path.exists(path):
        raise SystemExit(
            "\nNo config.json found.\n"
            "Fix: copy config.example.json to config.json and paste your "
            "Alpha Vantage premium key into it.\n"
        )
    with open(path) as f:
        cfg = json.load(f)
    if "PASTE_YOUR" in cfg.get("alpha_vantage_api_key", ""):
        raise SystemExit(
            "\nconfig.json still has the placeholder key.\n"
            "Fix: open config.json and replace PASTE_YOUR_PREMIUM_KEY_HERE "
            "with your real Alpha Vantage key.\n"
        )
    return cfg


class RateLimiter:
    """Sliding-window limiter: blocks until a call slot is free."""

    def __init__(self, per_minute):
        self.per_minute = per_minute
        self.calls = []

    def wait_for_slot(self):
        now = time.time()
        self.calls = [t for t in self.calls if now - t < 60]
        if len(self.calls) >= self.per_minute:
            sleep_for = 60 - (now - self.calls[0]) + 0.1
            print(f"  (rate limit: waiting {sleep_for:.0f}s...)")
            time.sleep(sleep_for)
        self.calls.append(time.time())


class AlphaVantage:
    def __init__(self, config):
        self.key = config["alpha_vantage_api_key"]
        self.entitlement = config.get("entitlement", "realtime")
        self.limiter = RateLimiter(config.get("requests_per_minute", 150))
        os.makedirs(CACHE_DIR, exist_ok=True)

    # ---------- core request machinery ----------

    def _get(self, params, cache_minutes=0, raw=False):
        """One API call. cache_minutes>0 = reuse a recent saved copy.
        raw=True: endpoint returns CSV text, not JSON (e.g. the earnings
        calendar). We still cache and rate-limit it the same way; we just
        don't parse it, and we watch for AV sneaking a JSON error back
        where CSV was expected."""
        params = dict(params)
        params["apikey"] = self.key
        if self.entitlement:
            params["entitlement"] = self.entitlement

        cache_name = "_".join(
            str(v) for k, v in sorted(params.items()) if k != "apikey"
        )
        ext = ".csv" if raw else ".json"
        cache_path = os.path.join(CACHE_DIR, cache_name[:150] + ext)

        if cache_minutes > 0 and os.path.exists(cache_path):
            age = time.time() - os.path.getmtime(cache_path)
            if age < cache_minutes * 60:
                with open(cache_path) as f:
                    return f.read() if raw else json.load(f)

        url = BASE_URL + "?" + urllib.parse.urlencode(params)
        last_err = None
        for attempt in range(3):
            self.limiter.wait_for_slot()
            try:
                with urllib.request.urlopen(url, timeout=30) as resp:
                    body = resp.read().decode()

                if raw:
                    # A CSV endpoint that errors hands back a small JSON blob.
                    stripped = body.lstrip()
                    if stripped.startswith("{"):
                        data = json.loads(stripped)
                        for bad_key in ("Note", "Information", "Error Message"):
                            if isinstance(data, dict) and bad_key in data:
                                raise RuntimeError(f"API said: {data[bad_key]}")
                        raise RuntimeError("Expected CSV, got JSON.")
                    with open(cache_path, "w") as f:
                        f.write(body)
                    return body

                data = json.loads(body)
                # Alpha Vantage signals problems inside a 200 response:
                for bad_key in ("Note", "Information", "Error Message"):
                    if isinstance(data, dict) and bad_key in data:
                        raise RuntimeError(f"API said: {data[bad_key]}")
                with open(cache_path, "w") as f:
                    json.dump(data, f)
                return data
            except Exception as e:
                last_err = e
                if "Invalid API call" in str(e):
                    break            # permanent error: retrying won't help
                if attempt < 2:
                    time.sleep(2 * (attempt + 1))
        raise RuntimeError(
            f"API call failed.\n"
            f"Function: {params.get('function')}\n"
            f"Error: {last_err}\n"
            f"(Paste this whole message to Claude to debug.)"
        )

    # ---------- endpoints the app uses ----------

    def top_gainers_losers(self):
        """The market-wide movers feed. Never cached — it's the live pulse."""
        return self._get({"function": "TOP_GAINERS_LOSERS"})

    def bulk_quotes(self, tickers):
        """Realtime quotes for up to 100 symbols in ONE call."""
        return self._get({
            "function": "REALTIME_BULK_QUOTES",
            "symbol": ",".join(tickers[:100]),
        })

    def atr(self, ticker):
        """Average True Range, daily, 14-period. Cached 4h — moves slowly."""
        return self._get({
            "function": "ATR", "symbol": ticker,
            "interval": "daily", "time_period": 14,
        }, cache_minutes=240)

    def news_sentiment(self, ticker):
        """News + sentiment for one ticker. Cached 30 min."""
        return self._get({
            "function": "NEWS_SENTIMENT", "tickers": ticker, "limit": 20,
        }, cache_minutes=30)

    def put_call_ratio(self, ticker):
        """Realtime options put/call ratio. Not cached — it's a live signal."""
        return self._get({
            "function": "REALTIME_PUT_CALL_RATIO", "symbol": ticker,
        })

    def earnings_calendar(self, horizon="3month"):
        """Upcoming earnings dates for the whole market, as CSV text.
        Columns: symbol,name,reportDate,fiscalDateEnding,estimate,currency.
        Cached 12h — a company's next report date barely moves intraday.
        Returns raw CSV; parse with the csv module at the call site."""
        return self._get(
            {"function": "EARNINGS_CALENDAR", "horizon": horizon},
            cache_minutes=720, raw=True,
        )

    def company_overview(self, ticker):
        """Company fundamentals incl. Sector. Cached 24h — sectors don't
        move. Used by the bot's max-2-per-sector rule (rulebook §10)."""
        return self._get({
            "function": "OVERVIEW", "symbol": ticker,
        }, cache_minutes=1440)

    def daily_adjusted(self, ticker):
        """Daily bars with split/dividend-adjusted closes. Cached 6h.
        Used by the Track Record to grade a pick close-to-close over its
        holding window, and to measure SPY over the same window."""
        return self._get({
            "function": "TIME_SERIES_DAILY_ADJUSTED",
            "symbol": ticker, "outputsize": "compact",
        }, cache_minutes=360)
