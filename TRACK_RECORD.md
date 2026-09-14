# Track Record

Every pick this screener has made, from **2026-07-21** to **2026-09-11** (38 trading days). Nothing is excluded. The losers are here because a track record that drops them is not a track record.

**Two different things are measured here, and they do not mean the same thing:**

1. **Signal quality** (this file's main table) — what every pick did if you bought it and held it blind for the grading window. No stop, no target, no exit rule. This measures the *screener*.
2. **Strategy result** (the bot section) — what the trading rules actually produced on the picks the bot took, with stops, 2:1 targets, 1% position sizing and a 5-day time stop. This measures the *system*.

The gap between them is the value of the risk rules, and it is large. Read one as the other and you will get the wrong answer.

*Generated 2026-09-14 from `snapshots/track_record.jsonl`. Regenerate with `python3 make_track_record.py`.*

## How grading works

- A pick is logged **before** the outcome is known, on the day it is made.
- It is graded at a fixed horizon set in config, measured **close-to-close** on split-adjusted prices, against SPY over the identical window.
- Once graded, the result is **frozen**. The rule cannot be changed after seeing the outcome.
- `SHORT` picks have their sign inverted, so a stock that doubles is a -100% short.
- Picks the bot flagged **WATCH** (no position taken) are graded and shown separately. They are kept because a signal that was right but untradeable is still information about the screener.

## Results

| Segment | Picks | Win rate | Avg return | Median | Beat SPY | SPY avg |
|---|---|---|---|---|---|---|
| **All calls** | 183 | 43.2% | -5.85% | -2.42% | 41.0% | +0.50% |
| LONG | 154 | 42.9% | -5.54% | -2.70% | 39.6% | +0.53% |
| SHORT | 29 | 44.8% | -7.49% | -0.19% | 48.3% | +0.34% |
| WATCH (no position) | 146 | 32.9% | -4.33% | -8.05% | 0.0% | +0.41% |

51 picks are logged but not yet ripe for grading.

## What the bot actually did

The screener's raw picks are one thing; the traded system is another. Across **18 closed trades**, the bot realized **$6,527.92** — **+6.53%** on its $100,000 starting equity — at a 55.6% win rate.

| | |
|---|---|
| Closed trades | 18 |
| Realized P&L | **$6,527.92** (+6.53% of starting equity) |
| Win rate | 55.6% |
| Avg per trade | +6.10% |
| Best / worst | +59.1% / -20.4% |

**How every position was closed:**

| Exit reason | Trades |
|---|---|
| time stop (attributed from log) | 8 |
| target hit | 4 |
| stop hit | 3 |
| closed outside the bot (unattributed) | 2 |
| time stop | 1 |

Exits marked *attributed from log* were recorded before the bot logged its own exit intent; they are matched to the `time_stop_submitted` event the bot wrote at the time it initiated the close. Two early exits match no such event and are left **unattributed** rather than assumed benign — under rulebook 7 the bot is not supposed to be closed by hand, so an exit it cannot account for is a finding, not a footnote. The bot now records its exit intent before closing, so new exits are attributed directly.

That figure is realized P&L from the bot's **own** fills and exits. It is deliberately not read off account equity: the paper account is shared with other strategies, so its equity is not the bot's result.

## The honest read

Held blind, the average pick loses **5.85%** while SPY returns **+0.50%** over the same windows. Traded under the rulebook, the same signal source returned **+6.53%**. **The risk management is doing the work, not the signal.**

The clearest single case is **PLAG**. The ledger grades it **-86.3%** — bought at 5.81, and five days later it traded at 0.79. The bot logged the same pick as **+59.15%, target hit**: it took profit at its 2:1 target and was out long before the collapse. One pick, opposite outcomes, and the difference is entirely the exit rule.

Read honestly, that cuts both ways. A screener whose picks lose money when held is not a good screener, and the median call of -2.42% says the edge in the raw signal is thin at best. What the record supports is a narrower claim: **a mechanical exit discipline can turn a mediocre signal into a positive result** — which is worth knowing, and is not the same as having found alpha.

The sample is small. 18 closed trades over 38 trading days is not enough to distinguish skill from a favorable tape, several winners carry most of the P&L, and it is paper money, where fills are kinder than they would be live. The evaluation window is frozen precisely so this gets more data before anyone concludes anything.

## Every graded call

| Date | Ticker | Dir | Entry | Exit | Return | SPY | Beat SPY |
|---|---|---|---|---|---|---|---|
| 2026-07-21 | DHR | LONG | 179.01 | 199.03 | +11.18% | -1.00% | yes |
| 2026-07-21 | HIHO | LONG | 1.07 | 1.09 | +1.87% | -1.00% | yes |
| 2026-07-21 | NOK | LONG | 10.63 | 8.905 | -16.23% | -1.00% | no |
| 2026-07-21 | SLGB | LONG | 0.9799 | 0.5684 | -41.99% | -1.00% | no |
| 2026-07-21 | SOXS | LONG | 45.13 | 62.93 | +39.44% | -1.00% | yes |
| 2026-07-21 | UTZ | SHORT | 14.06 | 14.08 | -0.14% | -1.00% | yes |
| 2026-07-22 | SMCI | LONG | 30.56 | 25.7 | -15.90% | -2.39% | no |
| 2026-07-22 | SOXS | LONG | 44.52 | 73.11 | +64.22% | -2.39% | yes |
| 2026-07-22 | T | LONG | 23.04 | 23.945 | +3.93% | -2.39% | yes |
| 2026-07-23 | ADVB | LONG | 16.78 | 10.08 | -39.93% | +0.48% | no |
| 2026-07-23 | DOMO | LONG | 3.95 | 3.7 | -6.33% | +0.48% | no |
| 2026-07-23 | NOK | LONG | 9.6806 | 9.075 | -6.26% | +0.48% | no |
| 2026-07-23 | SOXS | LONG | 45.61 | 53.98 | +18.35% | +0.48% | yes |
| 2026-07-23 | SQQQ | LONG | 43.27 | 44.62 | +3.12% | +0.48% | yes |
| 2026-07-23 | TSLL | LONG | 7.76 | 7.21 | -7.09% | +0.48% | no |
| 2026-07-23 | TSLQ | LONG | 25.86 | 27.52 | +6.42% | +0.48% | yes |
| 2026-07-24 | DRAM | LONG | 53.2 | 50.36 | -5.34% | +1.08% | no |
| 2026-07-24 | INTC | LONG | 92.32 | 90.2 | -2.30% | +1.08% | no |
| 2026-07-24 | LVWR | LONG | 1.46 | 1.79 | +22.60% | +1.08% | yes |
| 2026-07-24 | NOK | LONG | 9.0538 | 9.1 | +0.51% | +1.08% | no |
| 2026-07-24 | SKYQ | LONG | 4.7 | 4.65 | -1.06% | +1.08% | no |
| 2026-07-24 | SOXS | LONG | 51.53 | 54.25 | +5.28% | +1.08% | yes |
| 2026-07-24 | THC | LONG | 233.2 | 254.83 | +9.28% | +1.08% | yes |
| 2026-07-24 | TSLL | LONG | 7.42 | 7.31 | -1.48% | +1.08% | no |
| 2026-07-24 | WLDS | LONG | 3.53 | 2.56 | -27.48% | +1.08% | no |
| 2026-07-27 | ENTX | LONG | 3.92 | 2.96 | -24.49% | +2.51% | no |
| 2026-07-27 | NOK | LONG | 9.2329 | 9.35 | +1.27% | +2.51% | no |
| 2026-07-27 | SOXS | LONG | 54.93 | 53.1 | -3.33% | +2.51% | no |
| 2026-07-27 | TSLL | LONG | 7.24 | 7.81 | +7.87% | +2.51% | yes |
| 2026-07-28 | GOSS | LONG | 0.2051 | 0.1815 | -11.51% | +4.10% | no |
| 2026-07-28 | NOK | LONG | 8.93 | 9.93 | +11.20% | +4.10% | yes |
| 2026-07-28 | REPL | SHORT | 5.35 | 11.92 | -122.80% | +4.10% | no |
| 2026-07-28 | SOXS | LONG | 62.87 | 42.41 | -32.54% | +4.10% | no |
| 2026-07-29 | AMIX | SHORT | 4.57 | 12.1 | -164.77% | +5.53% | no |
| 2026-07-29 | CBZ | LONG | 54.9 | 54.8 | -0.18% | +5.53% | no |
| 2026-07-29 | DFNS | LONG | 50.22 | 45.91 | -8.58% | +5.53% | no |
| 2026-07-29 | SKDD | LONG | 21.24 | 13.06 | -38.51% | +5.53% | no |
| 2026-07-29 | SKHY | LONG | 126.79 | 151.03 | +19.12% | +5.53% | yes |
| 2026-07-29 | SKUU | LONG | 15.1 | 20.65 | +36.75% | +5.53% | yes |
| 2026-07-29 | SOFI | LONG | 15.25 | 18.25 | +19.67% | +5.53% | yes |
| 2026-07-30 | INTC | LONG | 91.13 | 99.81 | +9.52% | +3.63% | yes |
| 2026-07-30 | MSFT | LONG | 451.1 | 499.86 | +10.81% | +3.63% | yes |
| 2026-07-30 | NOK | LONG | 9.09 | 9.425 | +3.69% | +3.63% | yes |
| 2026-07-30 | NUWE | LONG | 4.45 | 1.63 | -63.37% | +3.63% | no |
| 2026-07-30 | SIMO | LONG | 255.1 | 267.43 | +4.83% | +3.63% | yes |
| 2026-07-30 | XRX | LONG | 3.49 | 3.23 | -7.45% | +3.63% | no |
| 2026-07-31 | AXTI | LONG | 60.43 | 88.58 | +46.58% | +3.51% | yes |
| 2026-07-31 | DRAM | LONG | 50.37 | 50.54 | +0.34% | +3.51% | no |
| 2026-07-31 | FCUV | LONG | 11.6 | 5.2 | -55.17% | +3.51% | no |
| 2026-07-31 | KUST | LONG | 1.24 | 1.21 | -2.42% | +3.51% | no |
| 2026-07-31 | MGRX | LONG | 0.5571 | 0.4921 | -11.67% | +3.51% | no |
| 2026-07-31 | REPL | SHORT | 11.2 | 12.06 | -7.68% | +3.51% | no |
| 2026-08-03 | ATKR | LONG | 93.55 | 93.75 | +0.21% | +2.03% | no |
| 2026-08-03 | FCUV | SHORT | 11.5 | 6.44 | +44.00% | +2.03% | yes |
| 2026-08-03 | SOXS | LONG | 53.08 | 45.21 | -14.83% | +2.03% | no |
| 2026-08-04 | ADGM | LONG | 0.8 | 0.7191 | -10.11% | -0.10% | no |
| 2026-08-04 | ENSC | LONG | 0.3842 | 0.4205 | +9.45% | -0.10% | yes |
| 2026-08-04 | NOK | LONG | 9.92 | 9.46 | -4.64% | -0.10% | no |
| 2026-08-04 | TNMG | LONG | 0.4757 | 0.4702 | -1.16% | -0.10% | no |
| 2026-08-04 | VTGN | LONG | 0.3 | 0.2691 | -10.30% | -0.10% | no |
| 2026-08-05 | DBGI | LONG | 16.28 | 16.64 | +2.21% | +0.35% | yes |
| 2026-08-05 | EOSE | LONG | 3.82 | 4.24 | +10.99% | +0.35% | yes |
| 2026-08-05 | NVDA | LONG | 219.22 | 224.09 | +2.22% | +0.35% | yes |
| 2026-08-05 | SSPC | SHORT | 21.21 | 10.105 | +52.36% | +0.35% | yes |
| 2026-08-06 | CELZ | LONG | 1.15 | 1.24 | +7.83% | +1.20% | yes |
| 2026-08-06 | CLRO | LONG | 9.8 | 5.67 | -42.14% | +1.20% | no |
| 2026-08-06 | ENSC | LONG | 0.5243 | 0.3485 | -33.53% | +1.20% | no |
| 2026-08-06 | IOVA | LONG | 6.21 | 6.52 | +4.99% | +1.20% | yes |
| 2026-08-06 | SOUN | LONG | 7.08 | 7.48 | +5.65% | +1.20% | yes |
| 2026-08-06 | SPCX | SHORT | 114.92 | 141.29 | -22.95% | +1.20% | no |
| 2026-08-06 | SURG | LONG | 0.325 | 0.2466 | -24.12% | +1.20% | no |
| 2026-08-07 | DOCS | LONG | 27.4 | 24.815 | -9.43% | +0.40% | no |
| 2026-08-07 | SOXL | SHORT | 140.25 | 145.0 | -3.39% | +0.40% | no |
| 2026-08-07 | SPCX | SHORT | 133.11 | 140.0 | -5.18% | +0.40% | no |
| 2026-08-07 | TTD | LONG | 13.8 | 14.14 | +2.46% | +0.40% | yes |
| 2026-08-07 | VATE | LONG | 12.11 | 7.96 | -34.27% | +0.40% | no |
| 2026-08-07 | ZENA | LONG | 1.65 | 2.14 | +29.70% | +0.40% | yes |
| 2026-08-10 | ABCL | LONG | 9.34 | 11.2 | +19.91% | -0.05% | yes |
| 2026-08-10 | ACHR | LONG | 6.26 | 6.395 | +2.16% | -0.05% | yes |
| 2026-08-10 | HUDI | LONG | 0.925 | 0.95 | +2.70% | -0.05% | yes |
| 2026-08-10 | HZO | SHORT | 52.12 | 52.22 | -0.19% | -0.05% | no |
| 2026-08-10 | INTC | LONG | 97.52 | 103.49 | +6.12% | -0.05% | yes |
| 2026-08-10 | SPCX | LONG | 138.74 | 146.23 | +5.40% | -0.05% | yes |
| 2026-08-11 | FRMI | LONG | 7.12 | 5.74 | -19.38% | -0.42% | no |
| 2026-08-11 | MREO | LONG | 0.3055 | 0.313 | +2.45% | -0.42% | yes |
| 2026-08-11 | PLAG | LONG | 5.81 | 0.794 | -86.33% | -0.42% | no |
| 2026-08-11 | PLUG | LONG | 2.22 | 2.16 | -2.70% | -0.42% | no |
| 2026-08-11 | WAFU | LONG | 1.665 | 1.44 | -13.51% | -0.42% | no |
| 2026-08-11 | WXM | SHORT | 7.9 | 4.55 | +42.41% | -0.42% | yes |
| 2026-08-12 | CRWG | LONG | 30.25 | 21.09 | -30.28% | -0.44% | no |
| 2026-08-12 | CRWU | LONG | 5.79 | 4.055 | -29.97% | -0.44% | no |
| 2026-08-12 | SOXL | SHORT | 142.16 | 120.6 | +15.17% | -0.44% | yes |
| 2026-08-13 | ARX | LONG | 19.51 | 19.57 | +0.31% | -1.94% | yes |
| 2026-08-13 | DFSC | LONG | 2.31 | 1.69 | -26.84% | -1.94% | no |
| 2026-08-13 | FGI | LONG | 11.72 | 8.83 | -24.66% | -1.94% | no |
| 2026-08-13 | GXAI | LONG | 1.26 | 0.866 | -31.27% | -1.94% | no |
| 2026-08-13 | IVDA | LONG | 0.5253 | 0.335 | -36.23% | -1.94% | no |
| 2026-08-13 | ONDS | LONG | 8.91 | 8.38 | -5.95% | -1.94% | no |
| 2026-08-13 | SPCX | LONG | 141.29 | 134.0 | -5.16% | -1.94% | no |
| 2026-08-14 | AKAN | LONG | 6.45 | 3.86 | -40.16% | -1.38% | no |
| 2026-08-14 | ONDS | LONG | 9.24 | 8.71 | -5.74% | -1.38% | no |
| 2026-08-14 | SURG | LONG | 0.282 | 0.1705 | -39.54% | -1.38% | no |
| 2026-08-17 | HIVE | LONG | 3.07 | 2.86 | -6.84% | -1.18% | no |
| 2026-08-17 | NOK | LONG | 10.78 | 9.955 | -7.65% | -1.18% | no |
| 2026-08-17 | OABI | LONG | 3.82 | 4.71 | +23.30% | -1.18% | yes |
| 2026-08-17 | SNAP | SHORT | 5.18 | 5.53 | -6.76% | -1.18% | no |
| 2026-08-18 | DRAM | LONG | 55.1 | 56.2 | +2.00% | -0.21% | yes |
| 2026-08-18 | DT | LONG | 49.26 | 50.06 | +1.62% | -0.21% | yes |
| 2026-08-18 | NOK | LONG | 10.39 | 10.34 | -0.48% | -0.21% | no |
| 2026-08-18 | SGLY | LONG | 3.49 | 2.15 | -38.40% | -0.21% | no |
| 2026-08-18 | SLE | LONG | 5.5 | 3.96 | -28.00% | -0.21% | no |
| 2026-08-18 | TRUG | LONG | 1.09 | 0.585 | -46.33% | -0.21% | no |
| 2026-08-18 | XOS | LONG | 4.44 | 3.52 | -20.72% | -0.21% | no |
| 2026-08-19 | FEMY | LONG | 2.71 | 2.92 | +7.75% | -0.40% | yes |
| 2026-08-19 | KORU | LONG | 19.11 | 20.47 | +7.12% | -0.40% | yes |
| 2026-08-19 | MRNY | LONG | 41.7053 | 35.28 | -15.41% | -0.40% | no |
| 2026-08-19 | SKHY | LONG | 156.16 | 158.02 | +1.19% | -0.40% | yes |
| 2026-08-19 | SOXL | SHORT | 120.74 | 116.66 | +3.38% | -0.40% | yes |
| 2026-08-19 | VRAX | LONG | 3.18 | 2.77 | -12.89% | -0.40% | no |
| 2026-08-19 | WYFI | LONG | 21.38 | 20.0 | -6.45% | -0.40% | no |
| 2026-08-20 | HUIZ | LONG | 1.84 | 1.71 | -7.07% | +1.11% | no |
| 2026-08-20 | IBIT | LONG | 41.2 | 45.29 | +9.93% | +1.11% | yes |
| 2026-08-20 | KORU | LONG | 20.33 | 21.44 | +5.46% | +1.11% | yes |
| 2026-08-20 | LOOP | LONG | 0.645 | 0.557 | -13.64% | +1.11% | no |
| 2026-08-20 | MSTU | LONG | 24.0727 | 35.27 | +46.51% | +1.11% | yes |
| 2026-08-21 | BITX | LONG | 17.18 | 17.325 | +0.84% | +0.48% | yes |
| 2026-08-21 | CONL | LONG | 6.25 | 5.65 | -9.60% | +0.48% | no |
| 2026-08-21 | HOWL | LONG | 0.8735 | 0.9433 | +7.99% | +0.48% | yes |
| 2026-08-21 | IBIT | LONG | 43.68 | 43.9 | +0.50% | +0.48% | yes |
| 2026-08-21 | INTC | LONG | 90.07 | 89.47 | -0.67% | +0.48% | no |
| 2026-08-21 | MSTU | LONG | 27.0446 | 30.03 | +11.04% | +0.48% | yes |
| 2026-08-21 | NVDA | LONG | 214.72 | 217.55 | +1.32% | +0.48% | yes |
| 2026-08-21 | RFAI | LONG | 58.0 | 52.05 | -10.26% | +0.48% | no |
| 2026-08-21 | SUGP | LONG | 1.6 | 0.8 | -50.00% | +0.48% | no |
| 2026-08-24 | BTCT | SHORT | 1.74 | 2.095 | -20.40% | +0.46% | no |
| 2026-08-24 | DXST | LONG | 2.95 | 2.59 | -12.20% | +0.46% | no |
| 2026-08-24 | IBIT | LONG | 44.64 | 44.67 | +0.07% | +0.46% | no |
| 2026-08-24 | SOXL | SHORT | 111.16 | 112.83 | -1.50% | +0.46% | no |
| 2026-08-24 | SOXS | LONG | 50.75 | 49.01 | -3.43% | +0.46% | no |
| 2026-08-25 | BTCT | LONG | 2.0 | 1.915 | -4.25% | -0.54% | no |
| 2026-08-25 | DAIC | LONG | 3.88 | 4.25 | +9.54% | -0.54% | yes |
| 2026-08-25 | DKS | LONG | 124.31 | 133.3983 | +7.31% | -0.54% | yes |
| 2026-08-25 | PRZO | LONG | 0.82 | 0.8647 | +5.45% | -0.54% | yes |
| 2026-08-25 | RZLV | LONG | 2.96 | 2.3466 | -20.72% | -0.54% | no |
| 2026-08-25 | SOXL | SHORT | 115.67 | 106.94 | +7.55% | -0.54% | yes |
| 2026-08-27 | MERC | LONG | 0.39 | 0.3646 | -6.51% | +0.27% | no |
| 2026-08-27 | NVD | LONG | 3.73 | 3.65 | -2.14% | +0.27% | no |
| 2026-08-27 | NVDA | LONG | 227.98 | 228.45 | +0.21% | +0.27% | no |
| 2026-08-27 | OKTA | SHORT | 172.91 | 170.42 | +1.44% | +0.27% | yes |
| 2026-08-27 | SOXL | SHORT | 123.05 | 106.81 | +13.20% | +0.27% | yes |
| 2026-08-27 | VNRX | LONG | 0.501 | 0.3411 | -31.92% | +0.27% | no |
| 2026-08-27 | WKSP | LONG | 0.63 | 0.5793 | -8.05% | +0.27% | no |
| 2026-08-28 | AFRM | LONG | 77.76 | 72.35 | -6.96% | +0.11% | no |
| 2026-08-28 | FNGR | LONG | 0.398 | 0.1974 | -50.40% | +0.11% | no |
| 2026-08-28 | IREN | LONG | 35.45 | 44.68 | +26.04% | +0.11% | yes |
| 2026-08-28 | NA | LONG | 2.22 | 2.24 | +0.90% | +0.11% | yes |
| 2026-08-28 | NVDA | LONG | 217.55 | 230.36 | +5.89% | +0.11% | yes |
| 2026-08-28 | PYPL | LONG | 53.5237 | 54.96 | +2.68% | +0.11% | yes |
| 2026-08-28 | SOLS | LONG | 63.53 | 63.73 | +0.31% | +0.11% | yes |
| 2026-08-31 | EIX | SHORT | 53.98 | 59.345 | -9.94% | -0.14% | no |
| 2026-08-31 | IBIT | SHORT | 44.67 | 44.39 | +0.63% | -0.14% | yes |
| 2026-08-31 | SOXS | SHORT | 49.02 | 44.09 | +10.06% | -0.14% | yes |
| 2026-09-01 | FRVO | LONG | 19.75 | 17.24 | -12.71% | +0.09% | no |
| 2026-09-01 | NIO | LONG | 4.06 | 3.685 | -9.24% | +0.09% | no |
| 2026-09-01 | RZLV | LONG | 2.39 | 2.3 | -3.77% | +0.09% | no |
| 2026-09-01 | SOXL | SHORT | 105.91 | 125.88 | -18.86% | +0.09% | no |
| 2026-09-01 | SOXS | LONG | 52.22 | 43.185 | -17.30% | +0.09% | no |
| 2026-09-01 | TQQQ | SHORT | 69.15 | 71.55 | -3.47% | +0.09% | no |
| 2026-09-02 | BIAF | LONG | 9.75 | 9.39 | -3.69% | -0.94% | no |
| 2026-09-02 | CRDO | SHORT | 165.22 | 160.31 | +2.97% | -0.94% | yes |
| 2026-09-02 | IBIT | SHORT | 43.79 | 43.68 | +0.25% | -0.94% | yes |
| 2026-09-02 | MDB | SHORT | 375.4 | 373.87 | +0.41% | -0.94% | yes |
| 2026-09-02 | NIO | LONG | 3.86 | 3.575 | -7.38% | -0.94% | no |
| 2026-09-02 | NVDA | LONG | 224.41 | 218.36 | -2.70% | -0.94% | no |
| 2026-09-02 | PPBT | LONG | 1.98 | 1.74 | -12.12% | -0.94% | no |
| 2026-09-02 | SNXX | LONG | 14.02 | 16.4 | +16.98% | -0.94% | yes |
| 2026-09-02 | SOXL | SHORT | 106.35 | 115.62 | -8.72% | -0.94% | no |
| 2026-09-02 | SOXS | LONG | 51.83 | 46.79 | -9.72% | -0.94% | no |
| 2026-09-03 | GPRO | LONG | 1.39 | 1.38 | -0.72% | -1.16% | yes |
| 2026-09-03 | NVDA | LONG | 228.1887 | 218.29 | -4.34% | -1.16% | no |
| 2026-09-03 | RARE | LONG | 14.85 | 14.3 | -3.70% | -1.16% | no |
| 2026-09-03 | SOXL | SHORT | 106.74 | 121.84 | -14.15% | -1.16% | no |
| 2026-09-03 | TSLL | LONG | 10.38 | 9.71 | -6.45% | -1.16% | no |

