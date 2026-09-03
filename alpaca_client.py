"""
alpaca_client.py — the ONLY file that talks to Alpaca.

PAPER ACCOUNT ONLY. The trading URL is hardcoded to Alpaca's paper
endpoint and this file refuses to construct a client for anything else.
Real money is out of scope for this project, period (rulebook §16).

Two Alpaca APIs are used:
  paper-api.alpaca.markets  -> account, orders, positions, clock, calendar
  data.alpaca.markets       -> latest quotes (free IEX feed)

Errors raise RuntimeError with a friendly message (callers fail soft and
log, per rulebook §14 — never trade on garbage inputs).
"""

import json
import time
import urllib.request
import urllib.error
import urllib.parse

TRADING_URL = "https://paper-api.alpaca.markets"   # PAPER. Never change.
DATA_URL = "https://data.alpaca.markets"


class Alpaca:
    def __init__(self, config):
        alp = config.get("alpaca", {})
        self.key = alp.get("api_key_id", "")
        self.secret = alp.get("api_secret_key", "")
        if not self.key or "PASTE" in self.key:
            raise SystemExit(
                "\nNo Alpaca paper keys in config.json yet.\n"
                "Fix (5 minutes, free):\n"
                "  1. Sign up at https://alpaca.markets (choose Paper Trading)\n"
                "  2. Dashboard -> Home -> 'API Keys' (make sure the toggle\n"
                "     says PAPER) -> Generate New Keys\n"
                "  3. Paste both values into the \"alpaca\" section of\n"
                "     config.json\n"
                "Until then you can still rehearse with:  python3 bot.py "
                "enter --dry-run\n"
            )
        # Paper keys start with "PK". A live key here would be a violation
        # of the whole spec — refuse loudly before any request is made.
        if not self.key.startswith("PK"):
            raise SystemExit(
                "\nSTOP: the Alpaca key in config.json does not look like a "
                "PAPER key (they start with 'PK').\nThis project never "
                "touches a live account. Generate PAPER keys and retry.\n"
            )

    # ---------- core request machinery ----------

    def _request(self, method, base, path, params=None, body=None):
        url = base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "APCA-API-KEY-ID": self.key,
            "APCA-API-SECRET-KEY": self.secret,
            "Content-Type": "application/json",
        })
        last_err = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    text = resp.read().decode()
                    return json.loads(text) if text.strip() else {}
            except urllib.error.HTTPError as e:
                detail = e.read().decode()[:300]
                # 4xx = our request is wrong; retrying won't change that.
                if 400 <= e.code < 500:
                    raise RuntimeError(
                        f"Alpaca rejected {method} {path} ({e.code}): {detail}"
                    )
                last_err = f"{e.code}: {detail}"
            except Exception as e:
                last_err = str(e)
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
        raise RuntimeError(
            f"Alpaca call failed after retries.\n{method} {path}\n"
            f"Error: {last_err}\n(Paste this whole message to Claude.)"
        )

    def _get(self, path, params=None, base=None):
        return self._request("GET", base or TRADING_URL, path, params=params)

    def _post(self, path, body):
        return self._request("POST", TRADING_URL, path, body=body)

    def _delete(self, path, params=None):
        return self._request("DELETE", TRADING_URL, path, params=params)

    # ---------- account / market ----------

    def account(self):
        return self._get("/v2/account")

    def clock(self):
        """Is the market open right now? Includes next open/close times."""
        return self._get("/v2/clock")

    def calendar(self, start, end):
        """Trading days between two YYYY-MM-DD dates, with real open/close
        times (handles holidays and half-days — rulebook §14)."""
        return self._get("/v2/calendar", {"start": start, "end": end})

    def positions(self):
        return self._get("/v2/positions")

    def asset(self, symbol):
        """Includes 'shortable' and 'easy_to_borrow' (rulebook §12) and
        'tradable' (halted/delisted names show up here)."""
        return self._get(f"/v2/assets/{symbol}")

    # ---------- quotes (data API) ----------

    def latest_quote(self, symbol):
        """Latest bid/ask. Returns dict with 'bp','ap','bs','as' or raises."""
        out = self._get(f"/v2/stocks/{symbol}/quotes/latest",
                        params={"feed": "iex"}, base=DATA_URL)
        return out.get("quote") or {}

    def bars(self, symbol, timeframe, start, end):
        """Historical bars from the data API (paginated; RFC-3339 start/end).
        Added for the momentum sleeve; the movers pipeline still uses Alpha
        Vantage for its data. Free IEX feed."""
        out, token = [], None
        while True:
            params = {"timeframe": timeframe, "start": start, "end": end,
                      "feed": "iex", "adjustment": "raw", "limit": 10000,
                      "sort": "asc"}
            if token:
                params["page_token"] = token
            d = self._get(f"/v2/stocks/{symbol}/bars", params=params, base=DATA_URL)
            out.extend(d.get("bars") or [])
            token = d.get("next_page_token")
            if not token:
                return out
            time.sleep(0.1)

    # ---------- orders ----------

    def submit_bracket(self, symbol, side, qty, limit_price, stop_price,
                       target_price):
        """One bracket order: entry limit + attached stop + attached target.
        The position is never unprotected (rulebook §6).

        time_in_force is GTC, NOT day: a 'day' bracket's stop/target legs
        EXPIRE at the close of the entry day, leaving the position naked every
        day after. GTC keeps the protection standing across days. The unfilled
        ENTRY is still cancelled explicitly by bot.py's 10:05 walk-away rule,
        so GTC doesn't leave a stale entry order lingering."""
        return self._post("/v2/orders", {
            "symbol": symbol,
            "side": side,                      # "buy" or "sell" (sell=short)
            "type": "limit",
            "qty": str(qty),
            "limit_price": str(limit_price),
            "time_in_force": "gtc",
            "order_class": "bracket",
            "take_profit": {"limit_price": str(target_price)},
            "stop_loss": {"stop_price": str(stop_price)},
        })

    def submit_oco_exit(self, symbol, side, qty, target_price, stop_price):
        """Re-protect an EXISTING position with a one-cancels-other exit: a
        take-profit limit and a stop, GTC. `side` is the EXIT side ('sell' to
        close a long, 'buy' to close a short). Filling one leg auto-cancels the
        other, so the position can never be double-closed. Used to restore
        protection after a partial fill cancels the original bracket legs, or
        to re-arm a position whose day-order legs expired."""
        return self._post("/v2/orders", {
            "symbol": symbol,
            "side": side,
            "type": "limit",
            "qty": str(qty),
            "time_in_force": "gtc",
            "order_class": "oco",
            "take_profit": {"limit_price": str(target_price)},
            "stop_loss": {"stop_price": str(stop_price)},
        })

    def submit_market(self, symbol, side, qty=None, notional=None,
                      client_order_id=None):
        """Plain market order (day) — by whole shares (qty) OR by dollar
        amount (notional, for fractional-share allocation). Used by the
        momentum sleeve; the movers bot still uses submit_bracket so its
        positions stay protected per rulebook 6. An optional client_order_id
        makes the submit idempotent — Alpaca rejects a duplicate id, so a
        retry can't place the same order twice."""
        body = {"symbol": symbol, "side": side, "type": "market",
                "time_in_force": "day"}
        if notional is not None:
            body["notional"] = str(round(float(notional), 2))
        else:
            body["qty"] = str(qty)
        if client_order_id:
            body["client_order_id"] = client_order_id
        return self._post("/v2/orders", body)

    def get_order(self, order_id):
        return self._get(f"/v2/orders/{order_id}")

    def list_orders(self, status="all", after=None, limit=200):
        params = {"status": status, "limit": limit, "nested": "true"}
        if after:
            params["after"] = after
        return self._get("/v2/orders", params)

    def cancel_order(self, order_id):
        return self._delete(f"/v2/orders/{order_id}")

    def close_position(self, symbol):
        """Market-close a position, cancelling its bracket legs first.
        Used ONLY for the 5-day time stop (rulebook §7.3)."""
        return self._delete(f"/v2/positions/{symbol}",
                            params={"cancel_orders": "true"})
