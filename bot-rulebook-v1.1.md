# Bot Rulebook — v1.1
Paper-trading bot spec for the Movers app. Every rule here is mechanical: derived from data, never guessed. Alpaca Paper account only — no real money is ever connected under this spec.

## 1. What it trades
- Only picks from the app's daily Top 10 with **score ≥ 80** and a direction of **LONG or SHORT**.
- WATCH picks are never traded. They are logged as "no call" and excluded from performance stats.
- Liquidity filter: skip any pick whose **average daily dollar volume is under $20M** (dollar volume — price × shares traded — is what predicts fill quality; raw share count is misleading on cheap stocks).

## 2. Entry (the minutes clock)
- At **9:45 AM ET**, for each qualifying pick, pull the live quote.
- Place a **limit order** at the quote (LONG: at or a hair above ask; SHORT: at or a hair below bid). Never a market order.
- **Walk-away rule:** if the limit is not filled by **10:05 AM (20 min)**, cancel it and log the pick as *not tradeable today*. No chasing, ever. Unfilled orders are data, not failures.
- This 20-minute window governs only how long the bot tries to GET IN. It has nothing to do with how long a filled position is held.

## 3. Stop-loss (volatility-based)
- Stop distance = **1.75 × ATR(14)** from the actual fill price.
- LONG: stop below entry. SHORT: stop above entry.
- ATR comes from the Alpha Vantage ATR endpoint at scan time, daily interval, 14-period.
- Rationale: a fixed % stop is too loose for calm names and gets noise-stopped on volatile ones. ATR sizes the stop to each stock's normal daily swing.

## 4. Target (reward-to-risk)
- Target distance = **2 × the stop distance** (2:1 reward:risk), placed from the fill price.
- At 2:1, the strategy is profitable at a ~40% win rate — the honest bar.

## 5. Position size (fixed risk)
- Risk per trade = **1% of paper account equity**.
- Shares = (1% of equity) ÷ (per-share stop distance). Round down.
- Cap: no single position may exceed **10% of account equity** in notional value.
  - Why 10%, not lower: with 1.75×ATR stops on volatile movers (stop ≈ 8-9% from entry), the 1% risk formula naturally sizes positions at ~10-12% notional. A tighter cap would silently cut most trades to a fraction of the intended risk, making the logged stats measure a different strategy than this spec. 10% preserves the risk rule while still preventing concentration blowups.
- Effect: every trade risks the same dollars regardless of price or volatility, keeping stats comparable across picks.

## 6. Order structure
- Everything submits as **one bracket order** to Alpaca: entry limit + attached stop + attached target. The position is never in the market unprotected, even for a second.

## 7. Holding & exit (the days clock)
A filled position exits only by one of three events:
1. **Stop hits** → loss taken, logged.
2. **Target hits** → win taken, logged.
3. **Time stop:** if neither has hit after **5 trading days**, exit at next open and log as "time-stopped." (Catalyst-driven moves are front-loaded; a position drifting sideways for 5 days is a thesis that expired, and the capital belongs in the next setup.)
- No manual intervention in the bot's positions. Its results must stay a clean test of the rules.

## 8. Consistency rules (what makes the test valid)
- The rule-set stays **frozen for a minimum 6-week evaluation window**. No mid-stream tweaks.
- After the window: read the Track Record verdict vs. SPY, then change **at most one dial** (the 1.75, the 2:1, the 1%, the 5-day) and run another fixed window.
- Future variants (e.g., "all calls" vs. "80+ only") run in parallel as separate logged strategies against the same days — never as replacements mid-test.

## 9. Logging (non-negotiable)
Every pick, every day, logs: date, ticker, direction, score, ATR, intended entry, fill status, actual fill price, stop, target, exit price, exit reason, result %, and SPY's move over the same holding period.
- Unfilled and skipped picks are logged too — "signal was right but untradeable" is a finding, not a footnote.

## 10. Portfolio limits (the pile, not the trade)
- Max **5 concurrent open positions**.
- Max **5% total open risk** across all positions (sum of per-trade risk at stops).
- Max **2 positions per sector** at once (movers cluster; correlated positions fail together).
- If a day's qualifying picks exceed available slots, take the **highest scores first**; the rest are logged as "no room."

## 11. Conflicts & repeats
- Pick is a ticker already held → **skip**, log "already in position." Never add to or average a position.
- Pick is the opposite direction of a held position → **skip the new signal**; the existing trade runs its course untouched. The bot never holds both sides and never flips early.

## 12. Shorting reality
- Before any SHORT entry, check Alpaca's shortable/borrowable status. Unavailable → **skip and log "short unavailable."**
- That log matters: if the best short signals are systematically unborrowable, the strategy's short side doesn't exist in practice — the test should reveal that, not hide it.

## 13. Partial fills & halts
- Entry window ends partially filled → cancel the unfilled remainder, **resize the bracket to the filled quantity**, log the partial.
- Stock halted before entry → skip, log "halted."
- Stock halted while held → hold; the bracket resolves at reopen. Log the halt and any gap through the stop.

## 14. Ops, sanity & reconciliation
- **Market calendar aware** (via Alpaca's calendar API): no runs on holidays; half-days shift the time-stop exit to that day's actual open/close. All times **ET**.
- **Data sanity:** missing/stale ATR, a crossed or zero quote, or any malformed response → skip that pick and log "bad data." Never trade on garbage inputs.
- **Daily reconciliation:** after close, the bot's internal log is checked against Alpaca's actual account state. Any mismatch is flagged loudly, never silently auto-corrected.
- **Missed runs:** if the bot fails to execute one morning (machine down, API outage), that day is logged as missed. It never enters late to "catch up" — a 1 PM entry is a different strategy than this spec.
- Errors alert visibly (notification/log flag). A silent failure is the worst failure.

## 15. Circuit breaker & config
- If paper equity draws down **10% from the evaluation window's start**, the bot pauses new entries and flags for review. Existing brackets still resolve. Resuming is a deliberate human decision, logged with a reason.
- All dials live in **one versioned config file**. Any change records the date, old value, new value, and reason — the audit trail that makes "change one dial at a time" enforceable.

## 16. Standing caveats (in the spec on purpose)
- This bot tests a hypothesis with fake money. Nothing here is investment advice, and paper results — even good ones — do not guarantee live results.
- Stops do not guarantee exit price: gaps through a stop fill worse than the stop. Paper trading will surface this; that's part of the test.
- Real money is a separate, future decision, gated on the Track Record — not on optimism.

---
*The dials: 80 (score floor) · $20M (dollar-volume floor) · 20 min (entry window) · 1.75×ATR (stop) · 2:1 (target) · 1% (risk/trade) · 10% (position cap) · 5 (max positions) · 5% (max total open risk) · 2 (max per sector) · 5 days (time stop) · 10% (drawdown pause) · 6 weeks (frozen window). Change one at a time, with data as the reason.*
