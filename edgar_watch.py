"""
edgar_watch.py — watches your universe for the PUBLIC, LEGAL signals that a
company may be in play.

This is the MKTX post-mortem turned into a daily job. It never touches private
information; everything here is a filing the SEC publishes to the whole world
the moment it lands. Two detectors:

  1. DEAL FORMS (per company, via the SEC submissions API)
     SC 13D / 13D/A  - somebody crossed 5% WITH INTENT TO INFLUENCE CONTROL.
                       Item 4 of the filing states their purpose; activists
                       routinely say outright that they want a sale.
     SC 14D9, SC TO-T/TO-I, SC 13E3  - a tender offer is live.
     PREM14A / DEFM14A / DFAN14A / 425  - merger proxy paperwork. By the time
                       these appear the deal is announced; they are included so
                       the log tells the whole story, not to trade on.

  2. "STRATEGIC ALTERNATIVES" (one EDGAR full-text search across all 8-Ks)
     Corporate code for "we hired a bank and we are for sale." It is a public
     press release and most retail investors do not know the phrase.

WHAT THIS IS NOT: a prediction. Most strategic-alternatives reviews end with no
sale, most activists lose, and only a low single-digit percentage of public
companies get acquired in a year. This widens the odds slightly and tells you
where to READ. It gives you no timing edge, and there is no legal way to get
one -- if news of a deal reaches you before it reaches the tape, somebody with
a duty broke it and trading on it is securities fraud.

READ-ONLY. Places no orders, touches no ledger. Fail-soft: any network or parse
error is printed and skipped, never raised, so it can never break a morning run.

Run:
  python3 edgar_watch.py --list     # show the watchlist + resolved CIKs, no network scan
  python3 edgar_watch.py            # scan, write snapshot, push new hits
  python3 edgar_watch.py --days 30  # widen the lookback window
  python3 edgar_watch.py --all      # re-show hits already reported (ignores dedupe)
  python3 edgar_watch.py --quiet    # no phone push
"""

import argparse
import glob
import gzip
import json
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
SNAP = os.path.join(HERE, "snapshots")
CACHE = os.path.join(HERE, "cache")
ET = ZoneInfo("America/New_York")

SEEN_PATH = os.path.join(SNAP, "edgar_seen.json")
TICKER_MAP_PATH = os.path.join(CACHE, "sec_company_tickers.json")
TICKER_MAP_TTL_H = 24

# SEC asks for <=10 requests/sec and a User-Agent that identifies you.
SEC_PAUSE_S = 0.13
TIMEOUT_S = 25

# Form types worth a look, mapped to how loudly they should read.
# "signal" = somebody is making a move that is not yet a done deal.
# "deal"   = a transaction is already public; logged for the record.
DEAL_FORMS = {
    "SC 13D":   ("signal", "5%+ stake WITH CONTROL INTENT - read Item 4 for their stated purpose"),
    "SC 13D/A": ("signal", "amended control-intent stake - something material changed"),
    "SC 14D9":  ("deal",   "board's response to a tender offer - a bid is on the table"),
    "SC TO-T":  ("deal",   "third-party tender offer - somebody is bidding"),
    "SC TO-I":  ("deal",   "issuer tender offer - company buying its own shares"),
    "SC 13E3":  ("deal",   "going-private transaction"),
    "PREM14A":  ("deal",   "preliminary merger proxy"),
    "DEFM14A":  ("deal",   "definitive merger proxy - deal going to a shareholder vote"),
    "DFAN14A":  ("deal",   "merger solicitation material"),
    "425":      ("deal",   "business-combination communication"),
    # The end of the story. A Form 15 / Form 25 means the company deregistered
    # or delisted — usually because a deal CLOSED. Surfaced so a dead ticker
    # gets pulled out of your watchlist instead of quietly being screened
    # forever. (This is how ANSS, HOLX and MASI were caught.)
    "15-12G":   ("gone",   "deregistered its stock - the company is no longer public"),
    "15-12B":   ("gone",   "deregistered its stock - the company is no longer public"),
    "25-NSE":   ("gone",   "delisted from its exchange"),

    # --- INSIDER (added 2026-08-05) -------------------------------------
    # Form 4 is the one category on this page with real academic support:
    # CLUSTERED, open-market BUYING by officers/directors (especially CEO/CFO
    # at smaller caps) has shown modest predictive value. One insider buy is
    # noise. Note Form 4 also covers SELLING and automatic 10b5-1 plan sales,
    # which carry near-zero information — the parser flags direction so the
    # log doesn't treat a scheduled sale as a signal.
    "4":        ("insider", "insider transaction - read direction; CLUSTER buying is the signal, one buy is noise"),

    # --- DILUTION (added 2026-08-05) ------------------------------------
    # The other side of the ledger, and directly useful: this is the data the
    # window-2 "SEC offering / dilution skip" candidate needs (V2_CHANGES.md
    # #4). A small-cap that just spiked and then files to sell stock into that
    # strength is the classic overnight reversal. BEARISH, logged as such.
    "424B5":    ("dilution", "prospectus supplement - SELLING STOCK, usually into strength"),
    "424B4":    ("dilution", "prospectus - offering priced"),
    "S-1":      ("dilution", "registration of new shares"),
    "S-1/A":    ("dilution", "amended share registration"),
    "S-3":      ("dilution", "shelf registration - permission to sell later"),
    "S-3ASR":   ("dilution", "automatic shelf - can sell immediately"),

    # --- OTHER STAKES ---------------------------------------------------
    "SC 13G":   ("stake",   "5%+ PASSIVE stake - index/institution, weaker than a 13D"),
    "SC 13G/A": ("stake",   "amended passive stake"),
}

# Full-text 8-K searches. "strategic alternatives" was the original and stays
# first because it is the highest-value phrase — corporate code for "we hired a
# bank and we are for sale," public, and most retail never reads it.
#
# HONEST FRAMING FOR EVERYTHING BELOW (added 2026-08-05): these are press
# releases. They are public the instant they land and the tape reprices in
# seconds. Adding them makes this a better READING LIST — it tells you which of
# your names had something real happen — it does NOT create a trading edge, and
# the volume-spike backtest (~/daytrade-lab/volume_spike.py, 918 events) found
# no tradeable multi-day drift after exactly this kind of event.
FTS_PHRASES = [
    # --- deal / control ---------------------------------------------------
    ("strategic alternatives",    "signal",  "hired a bank / exploring a sale"),
    ("definitive agreement",      "deal",    "a transaction has been signed"),
    ("letter of intent",          "signal",  "early-stage deal talk"),
    ("unsolicited proposal",      "signal",  "somebody made an uninvited bid"),
    ("completes acquisition",     "deal",    "a purchase closed - company just got bigger"),

    # --- capital returned to shareholders ---------------------------------
    ("share repurchase program",  "capital", "buyback authorized - company thinks it's cheap"),
    ("special dividend",          "capital", "one-off cash return to holders"),
    ("increases quarterly dividend", "capital", "dividend raised - a real cash-flow signal, hard to fake"),
    ("stock split",               "capital", "split announced - usually follows a long run up"),

    # --- CLINICAL (added 2026-08-05) --------------------------------------
    # Trial results are the single largest one-day repricer in healthcare: a
    # Phase 3 readout can double or halve a clinical-stage name overnight.
    # "met the primary endpoint" is the highest-signal phrase in the whole
    # file — it is the sentence that says the trial WORKED. NOTE the failure
    # phrase is deliberately here too: "did not meet" is the same event with
    # the sign flipped, and a monitor that only shows you good news is a
    # monitor that lies to you.
    ("met the primary endpoint",  "clinical", "TRIAL SUCCEEDED - the drug did what it was tested to do"),
    ("did not meet the primary endpoint", "clinical", "TRIAL FAILED - bearish, often severely"),
    ("positive topline",          "clinical", "headline trial data came back good"),
    ("topline results",           "clinical", "trial data released - read for direction"),
    ("orphan drug designation",   "clinical", "FDA status - exclusivity + faster path for a rare disease"),
    ("fast track designation",    "clinical", "FDA status - expedited development"),
    ("priority review",           "clinical", "FDA will decide in ~6 months instead of ~10"),
    ("510(k) clearance",          "clinical", "medical DEVICE cleared to market"),
    ("CE mark",                   "clinical", "approved to market in Europe"),

    # --- operating --------------------------------------------------------
    ("raises full-year guidance", "operating", "management raised its own forecast"),
    ("record quarterly revenue",  "operating", "best revenue quarter on record"),
    ("record backlog",            "operating", "future revenue already contracted"),
    ("FDA approval",              "operating", "drug/device approved - large single-day repricer"),
    ("breakthrough therapy",      "operating", "FDA designation - accelerates a drug program"),
    ("awarded a contract",        "operating", "new revenue booked"),
    ("multi-year agreement",      "operating", "revenue locked in beyond this year"),
    ("strategic partnership",     "operating", "new distribution/tech/commercial tie-up"),
]

FTS_PHRASE = FTS_PHRASES[0][0]        # kept for backwards compatibility
FTS_URL = "https://efts.sec.gov/LATEST/search-index"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
# Two ticker sources on purpose: company_tickers.json (~10.4k) is the one
# everybody uses and it is INCOMPLETE — it was missing ANSS, MASI and HOLX,
# all live companies. ticker.txt carries ~12.1k and has them. We merge.
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
TICKERS_TXT_URL = "https://www.sec.gov/include/ticker.txt"


# --------------------------------------------------------------------------
# plumbing
# --------------------------------------------------------------------------

def contact():
    """SEC requires a real contact string in the User-Agent. Env var wins, then
    config.json, then a sane default."""
    env = os.environ.get("SEC_CONTACT", "").strip()
    if env:
        return env
    try:
        with open(os.path.join(HERE, "config.json")) as f:
            c = json.load(f).get("edgar_watch", {}).get("contact", "").strip()
        if c:
            return c
    except (OSError, json.JSONDecodeError):
        pass
    # SEC fair-access policy requires a real name + email in the User-Agent.
    # Set EDGAR_CONTACT or edgar_watch.contact in config.json before running.
    raise RuntimeError(
        "No EDGAR contact configured. SEC requires 'Name email@example.com' "
        "in the User-Agent. Set the EDGAR_CONTACT env var or "
        "edgar_watch.contact in config.json."
    )


def dials():
    """Tunables from config.json (per CLAUDE.md nothing is hardcoded), with
    defaults so the script runs fine before the config block exists."""
    d = {"lookback_days": 14, "universe_file": "quality_universe.txt",
         "max_fts_hits": 200, "notify_priority": 4}
    try:
        with open(os.path.join(HERE, "config.json")) as f:
            d.update(json.load(f).get("edgar_watch", {}) or {})
    except (OSError, json.JSONDecodeError):
        pass
    return d


def _get(url, params=None):
    """GET returning parsed JSON. Handles gzip; raises on failure (callers
    catch and fail soft)."""
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "User-Agent": contact(),
        "Accept-Encoding": "gzip, deflate",
        "Accept": "application/json",
    })
    raw = urllib.request.urlopen(req, timeout=TIMEOUT_S).read()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return json.loads(raw)


def ticker_to_cik():
    """{TICKER: cik_int}. Cached 24h in cache/ so a normal run is one request
    at most."""
    fresh = (os.path.exists(TICKER_MAP_PATH) and
             time.time() - os.path.getmtime(TICKER_MAP_PATH) < TICKER_MAP_TTL_H * 3600)
    if fresh:
        try:
            with open(TICKER_MAP_PATH) as f:
                return {k: int(v) for k, v in json.load(f).items()}
        except (OSError, json.JSONDecodeError, ValueError):
            pass
    out = {}
    try:
        for row in _get(TICKERS_URL).values():
            t = str(row.get("ticker", "")).strip().upper()
            if t:
                out[t] = int(row["cik_str"])
    except Exception as e:                                # noqa: BLE001
        print(f"  (company_tickers.json unavailable: {e})")
    time.sleep(SEC_PAUSE_S)
    # Overlay the longer list for anything the first file missed.
    try:
        req = urllib.request.Request(TICKERS_TXT_URL,
                                     headers={"User-Agent": contact()})
        for line in urllib.request.urlopen(req, timeout=TIMEOUT_S).read().decode().splitlines():
            parts = line.split("\t")
            if len(parts) == 2 and parts[0].strip():
                out.setdefault(parts[0].strip().upper(), int(parts[1]))
    except Exception as e:                                # noqa: BLE001
        print(f"  (ticker.txt unavailable: {e})")
    if not out:
        raise RuntimeError("no SEC ticker source reachable")
    os.makedirs(CACHE, exist_ok=True)
    with open(TICKER_MAP_PATH, "w") as f:
        json.dump(out, f)
    return out


def load_watchlist(universe_file):
    """One ticker per line, '#' comments ignored — same format as
    quality_universe.txt. A dedicated edgar_watchlist.txt wins if you make one."""
    for name in ("edgar_watchlist.txt", universe_file):
        path = os.path.join(HERE, name)
        if not os.path.exists(path):
            continue
        out = []
        with open(path) as f:
            for line in f:
                line = line.split("#")[0].strip().upper()
                if line:
                    out.append(line)
        # dedupe, keep order
        seen, uniq = set(), []
        for t in out:
            if t not in seen:
                seen.add(t)
                uniq.append(t)
        return uniq, name
    return [], None


def load_movers():
    """Today's movers board (the newest snapshots/top10_*.json) as a ticker
    list. These are names nobody was watching yesterday — that is the point."""
    files = sorted(glob.glob(os.path.join(SNAP, "top10_*.json")))
    if not files:
        return [], None
    newest = files[-1]
    try:
        picks = json.load(open(newest))
    except (json.JSONDecodeError, OSError):
        return [], None
    out, seen = [], set()
    for p in picks:
        t = (p.get("ticker") or "").strip().upper()
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out, os.path.basename(newest)


def seen_path(movers=False):
    """Separate dedupe ledgers per mode. Sharing one would let a movers hit
    silently suppress the same filing on the standing watchlist (and the
    movers list churns daily, so its ledger is noisier by nature)."""
    return os.path.join(SNAP, "edgar_movers_seen.json" if movers else "edgar_seen.json")


def load_seen(movers=False):
    try:
        with open(seen_path(movers)) as f:
            return set(json.load(f))
    except (OSError, json.JSONDecodeError):
        return set()


def save_seen(seen, movers=False):
    os.makedirs(SNAP, exist_ok=True)
    with open(seen_path(movers), "w") as f:
        json.dump(sorted(seen), f)


def filing_url(cik, accession, primary_doc):
    acc = accession.replace("-", "")
    return (f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/"
            f"{primary_doc or ''}")


# --------------------------------------------------------------------------
# detector 1 — deal forms, per company
# --------------------------------------------------------------------------

def scan_deal_forms(tickers, cikmap, cutoff):
    """Walk each watchlist name's recent filings for the forms in DEAL_FORMS."""
    hits, unresolved, errors = [], [], []
    for i, tk in enumerate(tickers, 1):
        cik = cikmap.get(tk)
        if not cik:
            unresolved.append(tk)
            continue
        try:
            data = _get(SUBMISSIONS_URL.format(cik=cik))
        except Exception as e:                            # noqa: BLE001
            errors.append(f"{tk}: {e}")
            continue
        finally:
            time.sleep(SEC_PAUSE_S)

        rec = (data.get("filings") or {}).get("recent") or {}
        forms = rec.get("form", [])
        dates = rec.get("filingDate", [])
        accs = rec.get("accessionNumber", [])
        docs = rec.get("primaryDocument", [])
        name = data.get("name", tk)

        # A delisting form is NOT proof the company is gone. Form 25-NSE also
        # delists a single security — a note or a preferred series — while the
        # common stock keeps trading. (Real false positive 2026-07-31: RJF,
        # Raymond James, filed a 25-NSE on 2026-01-02 and is still filing
        # 10-Qs.) So corroborate: the latest periodic report wins. If a 10-Q or
        # 10-K postdates the delisting form, the company is alive and we say so
        # instead of telling you to delete a live name from your watchlist.
        last_periodic = max((d for f, d in zip(forms, dates)
                             if f in ("10-Q", "10-K")), default="")

        for form, fdate, acc, doc in zip(forms, dates, accs, docs):
            if form not in DEAL_FORMS or fdate < cutoff:
                continue
            kind, meaning = DEAL_FORMS[form]
            if kind == "gone" and last_periodic > fdate:
                kind = "check"
                meaning = (f"{form} filed, but a periodic report followed on "
                           f"{last_periodic} - the COMMON STOCK is still alive. "
                           "This most likely delisted a note or preferred "
                           "series. Do not remove the ticker.")
            hits.append({
                "ticker": tk, "company": name, "cik": cik, "form": form,
                "kind": kind, "filed": fdate, "accession": acc,
                "meaning": meaning, "source": "submissions",
                "url": filing_url(cik, acc, doc),
            })
        if i % 15 == 0:
            print(f"  ...{i}/{len(tickers)} checked")
    return hits, unresolved, errors


# --------------------------------------------------------------------------
# detector 2 — "strategic alternatives", one full-text search
# --------------------------------------------------------------------------

def _fts_one(phrase, kind, blurb, watch_ciks, cutoff, today, max_hits):
    """One EDGAR full-text search for a single phrase across 8-Ks in the window,
    keeping only CIKs on our watchlist. Split out so the whole phrase list can
    be scanned by the same paging/dedupe logic."""
    hits, errors = [], []
    page, PAGE = 0, 100
    while page < max_hits:
        params = {"q": f'"{phrase}"', "forms": "8-K",
                  "startdt": cutoff, "enddt": today, "from": page}
        try:
            data = _get(FTS_URL, params)
        except Exception as e:                            # noqa: BLE001
            errors.append(f'full-text search "{phrase}": {e}')
            break
        finally:
            time.sleep(SEC_PAUSE_S)

        rows = ((data.get("hits") or {}).get("hits")) or []
        if not rows:
            break
        for r in rows:
            src = r.get("_source") or {}
            for c in src.get("ciks", []):
                try:
                    cik_i = int(c)
                except (TypeError, ValueError):
                    continue
                # watch_ciks None = MARKET-WIDE: keep every filer. EDGAR's
                # full-text index already covers the whole market; the CIK
                # filter is the only thing that was hiding names like CLRO,
                # which nobody has on a watchlist the day before it merges.
                if watch_ciks is not None and cik_i not in watch_ciks:
                    continue
                acc = src.get("adsh", "")
                # display_names look like "CLEARONE INC  (CLRO)  (CIK 0000840715)"
                disp = (src.get("display_names") or [""])[0]
                tick = ""
                m = re.search(r"\(([A-Z][A-Z0-9.\-]{0,6})\)", disp)
                if m:
                    tick = m.group(1)
                hits.append({
                    "ticker": (watch_ciks.get(cik_i) if watch_ciks else tick) or tick or str(cik_i),
                    "company": (src.get("display_names") or [""])[0],
                    "cik": cik_i, "form": src.get("form", "8-K"),
                    "kind": kind, "filed": src.get("file_date", ""),
                    "accession": acc,
                    "phrase": phrase,
                    "meaning": f'8-K contains "{phrase}" - {blurb}',
                    "source": "full-text",
                    "url": filing_url(cik_i, acc, (r.get("_id", "").split(":") or [""])[-1]),
                })
        if len(rows) < PAGE:
            break
        page += PAGE
    return hits, errors


def filter_form4_purchases(hits):
    """Form 4 without a direction filter is NOISE, not signal — a raw scan is
    ~90% scheduled 10b5-1 sales (code S), gifts (G) and comp awards (A), none
    of which carry information, and they bury the handful of filings that do.

    Only OPEN-MARKET PURCHASES (transaction code P) are the documented signal,
    and even then it's cluster buying by officers that matters, not one buy.
    So: fetch each Form 4's raw XML (the submissions URL points at the XSL-
    rendered HTML; stripping the /xsl.../ segment gives the machine-readable
    XML), keep only filings containing a P, and annotate with who and how many.

    Fails SAFE toward silence: a Form 4 we can't verify is dropped, because an
    unverifiable insider filing is exactly the noise this filter exists to kill.
    Returns (kept_hits, dropped_count, error_count)."""
    kept, dropped, errors = [], 0, 0
    for h in hits:
        if h.get("form") != "4":
            kept.append(h)
            continue
        raw_url = re.sub(r"/xsl[^/]+/", "/", h.get("url", ""))
        try:
            req = urllib.request.Request(raw_url, headers={"User-Agent": contact()})
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
                body = r.read().decode("utf-8", "ignore")
        except Exception:                                 # noqa: BLE001
            errors += 1
            dropped += 1
            continue
        finally:
            time.sleep(SEC_PAUSE_S)
        codes = re.findall(r"<transactionCode>([A-Z])</transactionCode>", body)
        if "P" not in codes:
            dropped += 1
            continue
        owner = re.search(r"<rptOwnerName>([^<]+)", body)
        title = re.search(r"<officerTitle>([^<]+)", body)
        shares = re.findall(r"<transactionShares>\s*<value>([\d.]+)", body)
        bought = sum(float(s) for s in shares) if shares else None
        h = dict(h)
        h["insider"] = (owner.group(1).strip() if owner else "?")
        h["insider_title"] = (title.group(1).strip() if title else "")
        h["shares"] = bought
        h["meaning"] = (f"OPEN-MARKET BUY by {h['insider']}"
                        + (f" ({h['insider_title']})" if h["insider_title"] else "")
                        + (f" - {bought:,.0f} shares" if bought else "")
                        + " - cluster buying is the signal, one buy is noise")
        kept.append(h)
    return kept, dropped, errors


def scan_strategic_alternatives(cikmap, tickers, cutoff, max_hits, phrases=None):
    """Full-text scan across every configured phrase. Each phrase is one query
    against all 8-Ks in the window (cheaper and kinder to SEC than per-company
    queries). Deduped on (ticker, accession, phrase) so the same 8-K matching
    two phrases is reported once per phrase, not once per page."""
    watch_ciks = {cikmap[t]: t for t in tickers if t in cikmap}
    today = datetime.now(ET).date().isoformat()
    all_hits, all_errors, seen = [], [], set()
    for phrase, kind, blurb in (phrases or FTS_PHRASES):
        hits, errors = _fts_one(phrase, kind, blurb, watch_ciks,
                                cutoff, today, max_hits)
        all_errors.extend(errors)
        for h in hits:
            key = (h["ticker"], h["accession"], h["phrase"])
            if key not in seen:
                seen.add(key)
                all_hits.append(h)
    return all_hits, all_errors
    return hits, errors


# --------------------------------------------------------------------------

# Market-wide only searches phrases where a hit is genuinely notable on ANY
# company. The operating phrases ("record quarterly revenue", "awarded a
# contract") are true of hundreds of filers a week and would bury everything.
MARKET_PHRASES = [
    ("definitive agreement",      "deal",     "a transaction has been SIGNED"),
    ("merger agreement",          "deal",     "merger paperwork signed"),
    ("unsolicited proposal",      "signal",   "somebody made an uninvited bid"),
    ("strategic alternatives",    "signal",   "hired a bank / exploring a sale"),
    ("letter of intent",          "signal",   "early-stage deal talk"),
    ("met the primary endpoint",  "clinical", "TRIAL SUCCEEDED"),
    ("did not meet the primary endpoint", "clinical", "TRIAL FAILED - bearish"),
    ("positive topline",          "clinical", "headline trial data came back good"),
    ("FDA approval",              "clinical", "drug/device approved"),
]


def quote_move(row):
    """(percent move, source) from a bulk-quote row, or (None, None).

    BEFORE THE OPEN Alpha Vantage returns close=0.0 with change_percent=-100.0
    — a fake wipeout, not a real move. Any name filed overnight would show
    -100% and sort to the top, which is exactly backwards for a scan whose
    whole purpose is running at odd hours. So: trust the regular-session number
    only when there IS a regular session price, else use the extended-hours
    quote, else report honestly that we don't know."""
    try:
        close = float(row.get("close") or 0)
    except (TypeError, ValueError):
        close = 0.0
    if close > 0:
        try:
            return float(row.get("change_percent")), "regular"
        except (TypeError, ValueError):
            pass
    for k in ("extended_hours_change_percent",):
        v = row.get(k)
        if v not in (None, "", "None"):
            try:
                return float(v), "extended"
            except (TypeError, ValueError):
                pass
    return None, None


def market_scan(args, lookback):
    """Scan EVERY EDGAR filer for the phrases that matter, with no watchlist.

    This is the answer to "find me the next CLRO." That merger was invisible to
    a watchlist scan because nobody watches ClearOne — but its 8-K said
    "definitive agreement," and EDGAR's full-text index covers the whole market.
    Runs at any hour, needs no morning board.

    Still not a timing edge: a filing is public the second it posts and the tape
    reprices immediately. This finds what happened, fast, across everything.
    """
    today = datetime.now(ET).date().isoformat()
    cutoff = (datetime.now(ET).date() - timedelta(days=lookback)).isoformat()
    print(f"edgar_watch MARKET-WIDE: every filer, {len(MARKET_PHRASES)} phrases, "
          f"since {cutoff}")
    hits, errors, seen_keys = [], [], set()
    for phrase, kind, blurb in MARKET_PHRASES:
        print(f'  "{phrase}"...', flush=True)
        got, errs = _fts_one(phrase, kind, blurb, None, cutoff, today,
                             int(dials()["max_fts_hits"]))
        errors.extend(errs)
        for h in got:
            key = (h["ticker"], h["accession"], h["phrase"])
            if key not in seen_keys:
                seen_keys.add(key)
                hits.append(h)

    # --- the filter that makes this usable -------------------------------
    # 389 filings in 3 days is a firehose: "definitive agreement" is the phrase
    # companies use for EVERY contract, not just mergers. The thing that made
    # CLRO different from the other 388 is that the stock actually MOVED.
    # So: price every hit and keep the ones the market reacted to. A merger
    # that matters reprices the stock; a routine supply contract does not.
    if args.min_move > 0 and hits:
        syms = sorted({h["ticker"] for h in hits
                       if h["ticker"] and h["ticker"].isalpha()})
        print(f"  pricing {len(syms)} names to find which ones the market "
              f"actually reacted to (>= {args.min_move:g}%)...", flush=True)
        moves = {}
        try:
            from av_client import AlphaVantage, load_config as _lc
            av = AlphaVantage(_lc())
            for i in range(0, len(syms), 100):
                for row in av.bulk_quotes(syms[i:i + 100]).get("data", []):
                    moves[row["symbol"]] = quote_move(row)
        except Exception as e:                            # noqa: BLE001
            print(f"  (price check unavailable: {e} — showing all hits)")
            moves = None
        if moves:
            for h in hits:
                h["change_pct"], h["move_src"] = moves.get(h["ticker"], (None, None))
            # A name we could not price is NOT dropped — that would silently
            # hide the 3am scan's entire point. It is shown, flagged, and sorted
            # last, so a missing quote never looks like a missing filing.
            priced = [h for h in hits if h.get("change_pct") is not None
                      and abs(h["change_pct"]) >= args.min_move]
            unpriced = [h for h in hits if h.get("change_pct") is None]
            note = (f"  {len(priced)} of {len(hits)} filings are on names that moved "
                    f">= {args.min_move:g}%")
            if unpriced:
                note += f"; {len(unpriced)} could not be priced (shown, flagged)"
            print(note)
            hits = (sorted(priced, key=lambda h: -abs(h["change_pct"]))
                    + sorted(unpriced, key=lambda h: h.get("filed", ""), reverse=True))

    path_seen = os.path.join(SNAP, "edgar_market_seen.json")
    seen = set()
    try:
        with open(path_seen) as f:
            seen = set(json.load(f))
    except (OSError, json.JSONDecodeError):
        seen = set()
    new = [h for h in hits if f"{h['ticker']}|{h['accession']}" not in seen]
    show = hits if args.all else new

    print()
    if not show:
        print(f"  nothing new. ({len(hits)} filing(s) in the window; "
              "use --all to re-show them.)")
    else:
        for kind, label in (("deal", "DEALS — a transaction is signed"),
                            ("signal", "IN PLAY — talks, bids, sale process"),
                            ("clinical", "CLINICAL — trial results / FDA")):
            group = [h for h in show if h["kind"] == kind]
            if not group:
                continue
            group.sort(key=lambda h: h.get("filed", ""), reverse=True)
            print(f"  {label}  ({len(group)})")
            for h in group:
                mv = h.get("change_pct")
                if mv is None:
                    tag = "  [no quote yet]"
                else:
                    tag = (f"  [{mv:+.1f}%"
                           + (" pre/post]" if h.get("move_src") == "extended" else " today]"))
                print(f"    {h['filed']}  {h['ticker']:8} {h['company'][:40]}{tag}")
                print(f"      {h['meaning']}")
                print(f"      {h['url']}")
            print()

    if errors:
        print(f"  {len(errors)} error(s), skipped:")
        for e in errors[:4]:
            print(f"    {e}")

    os.makedirs(SNAP, exist_ok=True)
    stamp = datetime.now(ET)
    out = {"updated": stamp.isoformat(), "read_only": True, "mode": "market",
           "watchlist_file": "MARKET-WIDE (no watchlist)",
           "tickers_scanned": "all filers", "lookback_days": lookback,
           "since": cutoff, "hits": hits, "new_hits": new,
           "unresolved_tickers": [], "errors": errors}
    with open(os.path.join(SNAP, "edgar_market_latest.json"), "w") as f:
        json.dump(out, f, indent=2)
    if not args.all:
        with open(path_seen, "w") as f:
            json.dump(sorted(seen | {f"{h['ticker']}|{h['accession']}" for h in hits}), f)
    print(f"  wrote snapshots/edgar_market_latest.json  ({len(hits)} hit(s), "
          f"{len(new)} new)")

    if new and not args.quiet:
        lines = [f"{h['ticker']} — {h['meaning'][:60]}" for h in new[:8]]
        try:
            from bot import notify
            notify(f"{len(new)} market-wide filing signal"
                   f"{'s' if len(new) != 1 else ''}",
                   "\n".join(lines) + "\n\nPublic filings. Not a prediction.",
                   priority=int(dials()["notify_priority"]), tags="mag")
        except Exception as e:                            # noqa: BLE001
            print(f"  notify failed (non-fatal): {e}")


def main():
    p = argparse.ArgumentParser(description="Watch EDGAR for in-play signals.")
    p.add_argument("--list", action="store_true",
                   help="show the watchlist and resolved CIKs, then exit (no scan)")
    p.add_argument("--days", type=int, default=None,
                   help="lookback window in days (default from config, 14)")
    p.add_argument("--all", action="store_true",
                   help="include hits already reported on a previous run")
    p.add_argument("--quiet", action="store_true", help="no phone push")
    p.add_argument("--movers", action="store_true",
                   help="scan TODAY'S MOVERS (the latest top10 board) instead of "
                        "the standing watchlist — answers WHY a name moved")
    p.add_argument("--tickers", default=None,
                   help="comma-separated tickers to scan instead of the watchlist")
    p.add_argument("--market", action="store_true",
                   help="MARKET-WIDE: every filer, no watchlist. Finds CLRO-type "
                        "deal news anywhere. Run this any hour — it needs no "
                        "morning board and no watchlist.")
    p.add_argument("--min-move", type=float, default=5.0,
                   help="with --market, only show filings on names that moved at "
                        "least this %% today (default 5). Use 0 to show everything.")
    args = p.parse_args()

    d = dials()
    lookback = args.days if args.days is not None else int(d["lookback_days"])

    # Three sources, in priority order. --movers exists because nobody
    # pre-lists the small cap that is about to announce a merger: CLRO went
    # +183% before the 9:31 scan and was on no watchlist anywhere. Scanning the
    # movers board after the fact does NOT get you in early — that move is over
    # — it tells you WHAT THE FILING SAID, so a +200% pop is identifiable as a
    # real deal vs an offering vs nothing, in a minute instead of an hour.
    if args.market:
        # No watchlist at all — the deal-form scan (one request per company) is
        # impossible market-wide, so this is full-text only, over the high-signal
        # phrases. Runs at any hour and depends on nothing else in the app.
        market_scan(args, lookback)
        return
    if args.tickers:
        tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
        src_file = "--tickers"
    elif args.movers:
        tickers, src_file = load_movers()
        if not tickers:
            print("edgar_watch --movers: no top10 snapshot found; nothing to scan.")
            return
    else:
        tickers, src_file = load_watchlist(d["universe_file"])
    if not tickers:
        print("edgar_watch: no watchlist found "
              f"(looked for edgar_watchlist.txt and {d['universe_file']}).")
        return

    try:
        cikmap = ticker_to_cik()
    except Exception as e:                                # noqa: BLE001
        print(f"edgar_watch: could not load the SEC ticker map ({e}) — stopping.")
        return

    resolved = [t for t in tickers if t in cikmap]
    missing = [t for t in tickers if t not in cikmap]

    if args.list:
        print(f"\nwatchlist: {src_file} — {len(tickers)} tickers, "
              f"{len(resolved)} resolved to a CIK\n")
        for t in tickers:
            cik = cikmap.get(t)
            print(f"  {t:6} {('CIK %d' % cik) if cik else '-- not found on EDGAR'}")
        print(f"\nlookback would be {lookback} days. Nothing was scanned.\n")
        return

    cutoff = (datetime.now(ET).date() - timedelta(days=lookback)).isoformat()
    print(f"edgar_watch: {len(resolved)} names from {src_file}, "
          f"filings since {cutoff}")
    print("  scanning deal forms (one request per name)...")
    hits, unresolved, errors = scan_deal_forms(resolved, cikmap, cutoff)
    n_f4 = sum(1 for h in hits if h.get("form") == "4")
    if n_f4:
        print(f"  verifying {n_f4} Form 4(s) — keeping open-market BUYS only...")
        hits, f4_dropped, f4_errors = filter_form4_purchases(hits)
        print(f"    dropped {f4_dropped} (sales / gifts / comp awards"
              + (f", {f4_errors} unreadable" if f4_errors else "") + ")")
    print(f"  scanning 8-Ks for {len(FTS_PHRASES)} phrases "
          f"(\"{FTS_PHRASES[0][0]}\", \"{FTS_PHRASES[1][0]}\", ...)...")
    fts_hits, fts_errors = scan_strategic_alternatives(
        cikmap, resolved, cutoff, int(d["max_fts_hits"]))
    hits += fts_hits
    errors += fts_errors

    # dedupe within this run (a filing can hit both detectors)
    uniq = {}
    for h in hits:
        uniq[(h["ticker"], h["accession"], h["source"])] = h
    hits = sorted(uniq.values(), key=lambda h: (h["filed"], h["ticker"]), reverse=True)

    seen = load_seen(args.movers)
    new = [h for h in hits if f"{h['ticker']}:{h['accession']}" not in seen]
    show = hits if args.all else new

    # ---- report -----------------------------------------------------------
    print()
    if not show:
        print(f"  nothing new. ({len(hits)} known filing(s) in the window; "
              "use --all to re-show them.)")
    else:
        # Every kind gets a section. A hit that is collected but never printed
        # is worse than not collecting it — it looks like nothing happened.
        # Ordered by how much they should change what you go read.
        def of(k):
            return [h for h in show if h["kind"] == k]
        for label, group in (
                ("IN PLAY — not yet a deal", of("signal")),
                ("ANNOUNCED — already public", of("deal")),
                ("CLINICAL — trial results, FDA status, device clearance", of("clinical")),
                ("INSIDER BUYING — open-market purchases only", of("insider")),
                ("CAPITAL RETURN — buybacks / special dividends", of("capital")),
                ("OPERATING NEWS — guidance, approvals, contracts", of("operating")),
                ("DILUTION — company is selling stock (bearish)", of("dilution")),
                ("PASSIVE STAKE — 5%+ institution, weaker than a 13D", of("stake")),
                ("DELISTED — remove from the watchlist", of("gone")),
                ("LOOKS delisted but ISN'T — no action", of("check"))):
            if not group:
                continue
            print(f"  {label}")
            for h in group:
                print(f"    {h['filed']}  {h['ticker']:6} {h['form']:9} {h['company'][:38]}")
                print(f"      {h['meaning']}")
                print(f"      {h['url']}")
            print()

    if missing:
        print(f"  not on EDGAR by ticker ({len(missing)}): {', '.join(missing)}")
    if errors:
        print(f"  {len(errors)} error(s), skipped:")
        for e in errors[:5]:
            print(f"    {e}")

    # ---- snapshot ---------------------------------------------------------
    os.makedirs(SNAP, exist_ok=True)
    stamp = datetime.now(ET)
    out = {
        "updated": stamp.isoformat(),
        "read_only": True,
        "mode": "movers" if args.movers else ("tickers" if args.tickers else "watchlist"),
        "watchlist_file": src_file,
        "tickers_scanned": len(resolved),
        "lookback_days": lookback,
        "since": cutoff,
        "hits": hits,
        "new_hits": new,
        "unresolved_tickers": missing,
        "errors": errors,
    }
    # The movers scan writes to its OWN files. It answers a different question
    # than the standing watchlist ("why did this unknown name move today?" vs
    # "did anything happen to names I follow?"), and clobbering edgar_latest
    # would wipe the watchlist read every morning.
    tag = "edgar_movers" if args.movers else "edgar"
    path = os.path.join(SNAP, f"{tag}_{stamp.date().isoformat()}.json")
    with open(path, "w") as f:
        json.dump(out, f, indent=2)
    with open(os.path.join(SNAP, f"{tag}_latest.json"), "w") as f:
        json.dump(out, f, indent=2)
    print(f"  wrote {os.path.relpath(path, HERE)} (+ {tag}_latest.json)")

    # ---- push -------------------------------------------------------------
    # Only categories that should make you go READ something get a pocket buzz.
    # "signal" = a sale process may be starting; "insider" = a real open-market
    # buy (already filtered to code P); "dilution" = the company is selling
    # stock, which is the one bearish alert worth interrupting your day for.
    # Buybacks, guidance and announced deals stay in the log — they're context,
    # not something to act on, and alert fatigue kills a monitor faster than
    # missing a filing does.
    # "clinical" is included because a trial readout is the largest single-day
    # move in healthcare and it can land any morning — that IS worth a buzz,
    # in either direction (the failure phrase is in FTS_PHRASES on purpose).
    PUSH_KINDS = {"signal", "insider", "dilution", "clinical"}
    pushable = [h for h in new if h["kind"] in PUSH_KINDS]
    if pushable and not args.quiet:
        lines = [f"{h['ticker']} {h['form']} ({h['filed']}) — {h['meaning'][:70]}"
                 for h in pushable[:8]]
        msg = "\n".join(lines) + "\n\nPublic filings only. Not a prediction."
        title = (f"{len(pushable)} EDGAR signal"
                 f"{'s' if len(pushable) != 1 else ''} on your watchlist")
        try:
            from bot import notify
            notify(title, msg, priority=int(d["notify_priority"]), tags="mag")
        except Exception as e:                            # noqa: BLE001
            print(f"  notify failed (non-fatal): {e}")

    if new:
        seen.update(f"{h['ticker']}:{h['accession']}" for h in new)
        save_seen(seen, args.movers)


if __name__ == "__main__":
    main()
