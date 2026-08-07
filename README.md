# Investment Strategy with Alpaca and Google Cloud Functions

This project contains a set of Python Cloud Functions for managing a multi-strategy portfolio using Alpaca's trading API. The portfolio is composed of **seven complementary strategies**: **HFEA**, **SPXL SMA**, **9-Sig (Jason Kelly Methodology)**, **Dual Momentum (best-of-3 leveraged rotation)**, **Regime SSO (US regime detector)**, **7-Asset Rotator (AAA-family multi-asset rotation)**, and **World 40/30/30 (international diversifier)**.

*Earlier compositions also included: RSSB/WTIP (Structural Alpha, discontinued 2026-05-11 — poor performance); Regime World (WLDU/USFR detector, discontinued 2026-05-12 — replaced by the 7-Asset Rotator + World 40/30/30 pair that delivers better risk-adjusted return and broader diversification).*

## Portfolio Allocation

Current production weights (sum to 100%):

| Strategy | Weight | Role |
|---|---:|---|
| HFEA | 15% | Aggressive 3× leveraged buy-and-hold (UPRO/TMF/KMLM) |
| SPXL SMA | 15% | 3× S&P trend-following with 200-SMA gate |
| 9-Sig | 5% | Systematic TQQQ/AGG with crash protection (tail-risk sleeve) |
| Dual Momentum (best-of-3) | 20% | SPUU/QLD/EFO rotation + DD-stop + vol-target |
| Regime SSO | 12% | 7-signal US regime detector — SSO ↔ USFR rotation |
| 7-Asset Rotator (AAA family) | 15% | Monthly top-3 momentum rotation over NTSD/SAA/EET/UBT/UST/UGL/DBC, inverse-vol weighted with DD30 + vol25 risk controls |
| **World 40/30/30** (new 2026-05-12) | **18%** | **Static 40% WLDU + 30% GOLY + 30% TLT — international diversifier with most tax-efficient profile (quarterly rebal, 3 fixed assets)** |

### Why the 2026-05-12 update

After full Wave 7/8 backtests + Monte Carlo + tax-aware analysis, the portfolio was restructured around four principles:

1. **Cap single-strategy concentration** — no sleeve > 20%. Previously DM 2× was 26%, creating concentration risk.
2. **Lift the highest-Sharpe sleeves** — 7-Asset Rotator (Sharpe 0.74) was underweighted at 9.68%; bumped to 15%.
3. **Tax-aware caps** — high-turnover sleeves (7-Asset Rotator monthly 7-asset rotation, Regime SSO SSO↔USFR rotation) capped to limit short-term cap-gains exposure in this taxable account.
4. **Add international diversification via World 40/30/30** — zero deployed-ticker overlap; the most tax-efficient sleeve (quarterly rebal of 3 fixed assets, no rotation).

| Allocation rule | Cap | Reason |
|---|---:|---|
| HFEA / SPXL SMA | ≤ 15% | US-equity concentration limit |
| Regime SSO | ≤ 12% | Medium turnover (SSO↔USFR) |
| 7-Asset Rotator | ≤ 15% | High turnover (monthly 7-asset rotation, top-3 selection) |
| DM 2× best-of-3 | ≤ 20% | Medium turnover (monthly winner selection from 3 candidates) |
| 9-Sig | ~ 5% | Highest MaxDD (-98%); deliberately small |
| World 40/30/30 | 18% | Lowest turnover (quarterly, 3 fixed assets) — gets the largest single weight |

## Overview of the Strategies

The project is based on seven distinct active investment strategies, each designed to maximize returns by leveraging specific market behaviors and signals. (The numbered sections below cover these seven active strategies plus the discontinued RSSB/WTIP sleeve in section 3, preserved as a historical record.)

### 1. Hedgefundie's Excellent Adventure (HFEA) Strategy

#### **Strategy Overview:**
The HFEA strategy is an aggressive investment approach that involves leveraging a portfolio composed of three leveraged ETFs: 
- **UPRO** (3x leveraged S&P 500) - 45% allocation
- **TMF** (3x leveraged long-term U.S. Treasury bonds) - 25% allocation  
- **KMLM** (KFA Mount Lucas Managed Futures Index Strategy ETF) - 30% allocation

This three-asset approach was selected based on research from the r/LETFs community's 2024 best portfolio competition. The strategy capitalizes on the diversification benefits of combining equities, bonds, and managed futures. KMLM provides additional diversification through exposure to commodity trends and can perform well in different market conditions than traditional stocks and bonds.

#### **Approach in the Script:**
- **Monthly Buys**: The script uses a sophisticated underweight-based allocation system. Instead of fixed percentages, it calculates which assets are underweight relative to their target allocations (45% UPRO, 25% TMF, 30% KMLM) and allocates the monthly investment proportionally to bring the portfolio back towards target. This approach automatically rebalances during monthly contributions.
  
- **Quarterly Rebalancing**: The script includes a quarterly rebalancing function that ensures the portfolio remains aligned with the 45/25/30 target allocation. Rebalancing involves selling portions of over-performing ETFs and buying under-performing ones through a series of paired trades, ensuring the portfolio stays on track with the strategy's risk and return profile.

#### **Expected Returns (CAGR):**
- The HFEA strategy with this three-asset allocation has been optimized for improved risk-adjusted returns compared to traditional two-asset HFEA portfolios. 
- **Historical Performance**: Based on [backtesting from 1994 to present](https://testfol.io/?d=eJyNT9tKw0AQ%2FZUyzxGStBUaEEGkL1otog8iJYzJJF072a2TtbWE%2FLsTQy8igss%2B7M45cy4NlOxekecoWNWQNFB7FJ%2Fm6AkSiCaT0VkY6YUAyOb7eRzGx3m%2FsUGGJAr1BID5W2psweiNs5AUyDUFkGG9LNhtIQmPn7QQelfFZ0LhnaqJYza2TLfG5h33PGwDWDvxhWPjNOJLAxarLsUV2WxZoax0zdgN1f7abEyuOZXm5UM9hbQc2oymvc2ds6Rsb7IVSS%2FWvxWr1zsvCq5JMrL%2Bu027CCAXLDVzGxyMn%2BYP94Ob2e1s8Dib%2Ft%2F80PFv%2B0u%2BGJ5GGI072wNnVXH1eYoPwx%2B4Z%2F9bIx6ftli0X39%2BpPY%3D), this portfolio achieved approximately **15% CAGR (pre-tax)** or roughly **13% CAGR (post-tax)**.
- The addition of KMLM provides trend-following and crisis alpha characteristics that can enhance returns during certain market conditions while reducing overall portfolio volatility compared to traditional UPRO/TMF-only portfolios.

#### **Research Sources:**
This implementation is based on extensive backtesting and research from:
- [r/LETFs 2024 Best Portfolio Competition Results](https://www.reddit.com/r/LETFs/comments/1dyl49a/2024_rletfs_best_portfolio_competition_results/)

### 2. Dual Momentum — best-of-3 with DD-stop + vol-target

#### **Strategy Overview:**
This sleeve replaces the original 2-asset Antonacci dual momentum (SPUU vs EFO) with a **best-of-3 multi-asset rotation** that picks the strongest momentum candidate each month from three 2× leveraged equity ETFs. The change pushed the sleeve's 24-year backtested CAGR from ~10.5% to **17.21%** and Sharpe from 0.31 to **0.65**, while keeping effective leverage ≤ 2×.

#### **Universe:**
- **SPUU** — 2× S&P 500 (signal: SPY)
- **QLD** — 2× Nasdaq-100 (signal: QQQ) — the biggest contributor to the upgrade
- **EFO** — 2× MSCI EAFE (signal: EFA)
- **BND** — Vanguard Total Bond Market ETF (defensive + vol-target overflow)

#### **Signal:**
Blended 6-month + 12-month return on the underlying signal symbol, weighted 50/50, with **skip-most-recent-month** (Jegadeesh-Titman) — uses prices from `today − 21 calendar days` as the "now" reference, suppressing short-term reversal noise. Winner is the candidate with highest blended score; if no candidate exceeds +1%, the strategy goes defensive.

#### **Risk Management:**
1. **DD-stop (30%)** — trailing-peak-NAV drawdown stop. If the strategy is ≥ 30% below its peak, force defensive (BND) and reset the peak. Prevents the leveraged ETFs from melting through prolonged bear markets.
2. **Vol-target (25% annualized)** — scale the winner position by `min(1, 0.25 / 60d-realized-vol)`. Excess parks in BND. When SPUU is doing 35% vol, the strategy holds ≈ 71% SPUU + 29% BND.

#### **Monthly Mechanics:**
1. Compute current NAV from all positions (SPUU + QLD + EFO + BND)
2. Update peak NAV; if drawdown > 30% → force defensive, reset peak
3. Otherwise, compute blended momentum scores for SPY, QQQ, EFA
4. Pick winner (highest score, must exceed +1%)
5. Compute 60-day realized vol of the winner ETF
6. Compute target dollar split: `scale × $total` to winner, `(1-scale) × $total` to BND
7. Rebalance current positions to converge to targets

#### **24-Year Backtested Performance (this sleeve standalone):**
- CAGR: **17.21%**
- Sharpe: **0.65** (strong risk/return among ≤2× sleeves; the 7-Asset Rotator now leads all deployed sleeves at 0.74)
- Max DD: **-33.93%**
- Worst year: -26.48%
- Total return: 4,240%

#### **Why Best-of-3 + Vol-Target Beat the Original:**
- Adding QLD captures Nasdaq momentum during tech-led bull runs (2010s, 2020-2024) that pure SPY signal misses
- Vol-targeting prevents the strategy from holding the full 2× exposure during high-vol regimes — meaningfully reduces drawdown without much CAGR cost
- Trailing-peak DD-stop catches multi-month bear markets the monthly momentum signal would otherwise lag

### 3. ~~RSSB/WTIP Strategy~~ — DISCONTINUED 2026-05-11

> **Note:** This strategy was discontinued on **2026-05-11** after the structural-alpha
> thesis failed to materialize in the 2024–2026 live period and the long-window
> backtest revealed poor risk-adjusted performance (CAGR 7.92% / Sharpe 0.39 /
> MaxDD -46.4%). The 10% allocation was redistributed proportionally across the
> remaining six deployed strategies (×100/90 ≈ 1.111). RSSB and WTIP positions
> were liquidated; the BIL holding-fund balance flowed back to cash.
>
> The original strategy documentation below is preserved as a historical record.

#### **Strategy Overview:**
The RSSB/WTIP strategy moves from **Active/Tactical Management** (scripts, signals, rebalancing) to **Structural/Strategic Management** (asset allocation and leverage). Instead of trying to *time* the market or pick the best sectors, you are *stacking* diversified return streams to win in all economic environments.

**Allocation:** 80% **RSSB** / 20% **WTIP**

This strategy provides complete economic coverage through a combination of:
- **RSSB** (Return Stacked U.S. Stocks & Bonds): Provides exposure to global equities and U.S. Treasuries through futures-based leverage
- **WTIP** (WisdomTree International Efficient Core Fund): Provides exposure to TIPS (inflation bonds), managed futures (trend), and hard assets (gold/BTC)

#### **What You Actually Own (The Look-Through):**

For every $10,000 invested, your effective exposure is roughly **$19,700 (1.97x Leverage)**, broken down as follows:

| Asset Class | Effective Exposure | Role |
| :--- | :--- | :--- |
| **Global Equities** | **80%** | The Growth Engine (Bull Markets) |
| **US Treasuries** | **80%** | The Deflation Hedge (Recessions) |
| **TIPS (Inflation Bonds)** | **~17%** | The Cost-of-Living Shield |
| **Managed Futures (Trend)** | **~16%** | The Crisis/Volatility Hedge |
| **Hard Assets (Gold/BTC)** | **~4%** | The Debasement Hedge |

#### **Why This Strategy? (The Investment Thesis)**

**1. Complete Economic Coverage**

Your previous portfolio relied heavily on **Growth** (HFEA, 9-Sig, SPXL) and **Momentum** (Dual). It was vulnerable to a "Choppy Stagflation" environment where trends fail to materialize and stocks/bonds fall together (like 2022).

* **RSSB** covers **High Growth** (Stocks) and **Deflation** (Bonds).
* **WTIP** covers **Inflation** (TIPS) and **Stagflation** (Trend/Gold).

You no longer need a script to "switch" assets; you own the assets that win in every scenario simultaneously.

**2. Institutional "Return Stacking"**

You are utilizing **Capital Efficiency**. By using futures (inside the ETFs), you obtain nearly 200% exposure without the risks of "Volatility Decay" inherent in daily reset 3x ETFs (like UPRO/TQQQ in your HFEA/9-Sig strategies). You are getting $2 of assets working for every $1 you put in, but with cleaner institutional execution.

**3. Operational "Set and Forget"**

You are eliminating "Execution Risk." Your previous setup relied on:
* Cloud Functions not timing out.
* Alpaca/FRED APIs being online.
* Complex logic (SMA crosses, momentum calcs) firing correctly.
* **You** not interfering emotionally during a drawdown.

The 80/20 strategy requires zero code, zero API keys, and zero maintenance other than occasional rebalancing.

#### **Approach in the Script:**
- **Monthly Buys**: The script uses the same sophisticated underweight-based allocation system as HFEA. It calculates which assets are underweight relative to their target allocations (70% RSSB, 30% WTIP) and allocates the monthly investment proportionally to bring the portfolio back towards target. This approach automatically rebalances during monthly contributions.

- **Quarterly Rebalancing**: The script includes a quarterly rebalancing function that ensures the portfolio remains aligned with the 70/30 target allocation. Rebalancing involves selling portions of over-performing ETFs and buying under-performing ones through a series of paired trades, ensuring the portfolio stays on track with the strategy's risk and return profile.

#### **Comparison: 80/20 vs. Your "Cloud Function" Portfolio**

Here is how the new strategy specifically replaces or improves upon your existing six sub-strategies.

**1. vs. HFEA (17.5% of old portfolio)**

* **Old Way:** Leveraged 3x ETFs ($UPRO/$TMF). High volatility decay. If the market moves sideways with high volatility, you lose money.
* **New Way:** **RSSB**. It provides similar Stock/Bond stacking but uses **Futures** rather than daily leveraged ETFs.
* **Benefit:** Lower cost of leverage, less drag from volatility, and tax efficiency (no monthly rebalancing trades triggering tax events).

**2. vs. SPXL 200-SMA Strategy (35% of old portfolio)**

* **Old Way:** Binary Market Timing. If SPY < 200SMA, you go to cash.
* **Risk:** "Whipsaw Risk." If the market dips to 199SMA and bounces to 205SMA, your script sells low and buys high. You miss the initial rebound.
* **New Way:** **WTIP (Trend Component)**. Instead of *you* timing the S&P 500, the Managed Futures inside WTIP automatically go long/short on hundreds of markets (commodities, currencies, rates). It captures the trend without you risking your entire equity position on a single SMA line.

**3. vs. 9-Sig & Dual Momentum (15% of old portfolio)**

* **Old Way:** Aggressive tactical shifts based on relative strength or quarterly signals to chase the "hot hand."
* **New Way:** **Diversification**. Instead of chasing the winner, you hold the 80% Global Stock allocation (RSSB) which naturally captures winners (like Nvidia or Apple) as they grow in the index, while the Trend component (WTIP) captures momentum in non-equity markets (like Oil or the Dollar).

#### **Market Conditions Analysis**

| Market Environment | **Old "Python/Alpaca" Portfolio** Performance | **New "80/20 RSSB/WTIP"** Performance |
| :--- | :--- | :--- |
| **Raging Bull Market** (e.g., 2021, 2023) | **Winner.** 3x Leverage (TQQQ/UPRO) allows you to outperform everything. | **Good, but lower.** You "only" have ~80% equity exposure compared to 100-300% in the old portfolio. |
| **Flash Crash / Correction** (e.g., COVID 2020) | **High Risk.** SMA triggers might lag; HFEA draws down 60%+. | **Resilient.** Treasuries (RSSB) usually spike in value to offset stock losses. |
| **Inflationary Bear** (e.g., 2022) | **Catastrophic.** Stocks and Bonds fall together. HFEA gets crushed. SMA strategy goes to cash (saving some money, but losing to inflation). | **Winner.** This is where WTIP shines. TIPS hold value, and Trend strategies short the falling market, offsetting RSSB losses. |
| **Sideways / Choppy** (e.g., 2015) | **Poor.** Whipsaws in SMA strategy and Volatility Decay in HFEA eat up capital. | **Steady.** Futures leverage doesn't suffer daily decay. Dividends and yield carry the portfolio. |

#### **Pros & Cons Summary**

**✅ Pros of the New Strategy**

1. **Robustness:** No "single point of failure" (like a bug in `main.py` or a broken API connection).
2. **Psychology:** Easier to stick with. You aren't watching "Margin Gates" or "Signal Lines" every month.
3. **Efficiency:** Better tax treatment and lower transaction costs (no bid/ask spread slippage from monthly trading).
4. **Macro-Aware:** Explicitly hedges Inflation and Debasement which your old portfolio did not touch directly.

**❌ Cons (What you are giving up)**

1. **The "Jackpot" Potential:** In a insane bull run (like the late 90s), 3x Leverage (HFEA/9-Sig) is unbeatable. The 80/20 strategy is more conservative (approx 2x leverage).
2. **Control:** You can no longer "tweak" the algorithm. You are relying on the fund managers (Return Stacked / WisdomTree) to execute their mandate.
3. **The Fun Factor:** If you enjoyed coding the bot and watching the Telegram alerts (`🚀 URTH Alert`), you might find this boring. (Though "boring" is usually profitable in investing).

#### **Expected Returns:**
- The RSSB/WTIP strategy aims to provide strong risk-adjusted returns through structural diversification across all economic environments.
- **Historical Performance**: The strategy's "set and forget" approach with futures-based leverage has shown strong risk-adjusted returns with reduced correlation to traditional equity strategies.
- **Risk Management**: The combination of equities, bonds, TIPS, managed futures, and hard assets provides natural hedging across market cycles.

#### **Final Verdict**

Your previous portfolio was a brilliant engineering feat of **Tactical Alpha**—trying to outsmart the market using speed, leverage, and rules.

The **80/20 RSSB/WTIP** portfolio is a feat of **Structural Alpha**—accepting that we cannot predict the future, so we build a vessel that can float on any ocean. It is less work, lower stress, and historically offers a higher Sharpe Ratio (risk-adjusted return).

### 4. S&P 500 with 200-SMA Strategy

#### **Strategy Overview:**
The S&P 500 with 200-SMA strategy is a trend-following investment approach that uses the 200-day Simple Moving Average (SMA) as a signal for entering or exiting the market. The 200-SMA is a widely-used technical indicator that smooths out daily price fluctuations and highlights the underlying trend of the market.

The basic premise of this strategy is that when the S&P 500 index is above its 200-SMA, the market is in an uptrend, and it is generally safer to be invested in equities. Conversely, when the S&P 500 is below its 200-SMA, the market is likely in a downtrend, and it may be prudent to reduce equity exposure or exit the market altogether.

#### **Approach in the Script:**
- **Buying SPXL**: The script monitors the S&P 500's position relative to its 200-SMA with a 1% margin band. If the S&P 500 is more than 1% above the 200-SMA, indicating a confirmed bullish trend, the script will use allocated cash to buy SPXL, a 3x leveraged ETF that tracks the S&P 500. This leverage allows for higher returns during uptrends.
  
- **Selling SPXL**: If the S&P 500 falls more than 1% below its 200-SMA, the script will sell all holdings in SPXL. The 1% margin band helps avoid whipsaws—situations where the market briefly crosses the SMA only to quickly reverse—reducing unnecessary trading and transaction costs.

- **Monthly Contributions**: On the first trading day of each month, if the market is above the 200-SMA (plus margin), the monthly allocation is invested in SPXL. If the market is below the 200-SMA, the cash is held and tracked in Firestore for future deployment when conditions improve.

#### **Expected Returns:**
- The S&P 500 with 200-SMA strategy aims to enhance returns through trend-following and risk management. By avoiding major market drawdowns through strategic exits during downtrends, the strategy seeks to capture the majority of market upside while protecting capital during bear markets. The use of 3x leverage (SPXL) amplifies returns during bullish periods while the 200-SMA timing mechanism provides downside protection. Historical backtests of similar strategies have shown improved risk-adjusted returns compared to buy-and-hold approaches.

### 5. 9-Sig Strategy (Jason Kelly Methodology)

#### **Strategy Overview:**
The 9-Sig strategy is based on Jason Kelly's methodology from his book "The 3% Signal". It's a systematic approach to managing a TQQQ (3x leveraged NASDAQ-100) and AGG (iShares Core U.S. Aggregate Bond ETF) portfolio with built-in crash protection. The strategy aims for 9% quarterly growth while maintaining the canonical **60/40** allocation between TQQQ and AGG.

#### **Key Components:**

**Target Allocation:**
- **60% TQQQ**: 3x leveraged NASDAQ-100 ETF for growth
- **40% AGG**: Bond ETF for stability and crash protection

**Monthly Contributions (First Trading Day of Month):**
- **ALL** monthly contributions go to AGG bonds only
- Amount: $10.25 per month (5% of total $205 monthly investment)
- **Rationale**: This follows the core 3Sig rule - monthly contributions always go to the safer asset

**Quarterly Rebalancing (First Trading Day of Quarter):**
The strategy uses a sophisticated signal line calculation to determine when to rebalance:

```
Signal Line = Previous TQQQ Balance × 1.09 + (Half of Quarterly Contributions)
```

**Rebalancing Logic:**
- **BUY Signal**: When Current TQQQ < Signal Line → Sell AGG, Buy TQQQ. Clamped by two safety valves: the buy may spend at most **90% of the bond holdings** (buying-power throttle) and **bonds never fall below 10% of NAV** (bond floor).
- **SELL Signal**: When Current TQQQ > Signal Line → Sell TQQQ, Buy AGG
- **HOLD Signal**: When within a **2.5%-of-NAV** tolerance band of the signal line → No action
- **First Quarter**: Signal line set to 60% of total portfolio value

**Crash Protection - "30 Down, Stick Around" Rule:**
- When **TQQQ** (the stock ETF) closes ≥30% below its **rolling 8-quarter (~2-year) high**, the strategy ignores the first **2** SELL signals
- This prevents selling during major market crashes
- After 2 ignored signals (while still in the 30-down), it performs a **base reset** — snap back to 60/40 and re-baseline the signal line

**Spike Reset:**
- If TQQQ gains **≥100% in a quarter** while held above 60% of NAV (and not in a 30-down), the position is reset to 60% of NAV — caps a runaway signal line after a parabolic move. (Historically rare; never triggered in backtest.)

#### **Example Scenarios:**

**First Quarter:**
```
Starting: $0 TQQQ, $30.75 AGG (from 3 months of contributions)
Signal Line: $18.45 (60% of total portfolio)
Action: BUY $18.45 worth of TQQQ
Result: $18.45 TQQQ, $12.30 AGG (60/40 allocation)
```

**Normal BUY Signal:**
```
Signal Line: $1,105
Current TQQQ: $1,000 (need $105 more)
Action: Sell $105 worth of AGG → Buy $105 worth of TQQQ
Result: Rebalanced to signal line
```

**Crash Protection Example:**
```
Normal SELL Signal: Current TQQQ > Signal Line
BUT: TQQQ down 35% from its rolling 8-quarter high
Action: SELL_IGNORED (signal ignored due to crash protection)
Result: Hold TQQQ position during market crash
```

#### **Expected Returns:**
- **Target**: 9% quarterly growth (approximately 36% annually compounded)
- **Historical Performance**: Based on Jason Kelly's methodology, this strategy has shown strong risk-adjusted returns with built-in crash protection
- **Risk Management**: The monthly contributions to bonds and crash protection rule help mitigate downside risk

#### **Data Management:**
- All quarterly data is stored in Firestore (`nine-sig-quarters` collection)
- Tracks: balances, signal lines, actions taken, and performance metrics
- Enables accurate calculation of subsequent quarters' signal lines

### 6. Regime SSO (US Regime Detector)

#### **Strategy Overview:**
A 7-signal composite regime detector based on Reddit r/LETFs methodology (u/Neat_Bug1775). Rotates between **SSO** (2× S&P 500) in risk-on conditions and **USFR** (WisdomTree Floating Rate Treasury, cash-like) in risk-off conditions. Designed to fire ≈ 1.4 rotations per year — intentionally slow and noise-resistant.

#### **The Seven Signals (each contributes -1 / 0 / +1):**
1. **Price trend** — SPY vs 200-SMA with 3-day hysteresis (filters whipsaws)
2. **Market breadth** — % of S&P 500 stocks above their 50-SMA
3. **Volatility regime** — VIX level AND trajectory (5-day change)
4. **Trend strength** — 14-day ADX on SPY (must exceed 25 to count)
5. **Credit spread** — HYG/LQD ratio vs its 50-SMA (junk vs investment-grade)
6. **News sentiment** — FinBERT-scored Alpaca news over rolling 24h
7. **Canary universe** — HYG / EEM / IWM vs their 50-SMA (liquidity proxy)

Composite range: roughly -7 to +7.

#### **Plus: Fed Hike Filter:**
Blocks re-entries during aggressive Fed hiking cycles (>50bp in 90 days). Credited with avoiding the 2022 bear-rally trap.

#### **Exit / Re-entry Logic:**
- **EXIT_FAST**: composite ≤ -3 for 3 consecutive days → SSO → USFR
- **EXIT_SLOW**: composite ≤ 0 for 15 consecutive days → SSO → USFR
- **REENTER_CREDIT_VIX** (Path A): 4 weeks of improving credit + declining VIX + positive composite
- **REENTER_NLP** (Path B): composite ≥ +3 for 7 days AND FinBERT confidence ≥ 0.80 over 2 weeks
- **REENTER_STD** (Path C): composite ≥ +3 for 15 consecutive days (always-on fallback)

#### **36-Year Backtested Performance (1990-2026):**
- CAGR: 13.49% • Sharpe: **0.68** • Max DD: **-23.72%** • Worst year: -7.05%
- Lowest drawdown of any leveraged sleeve in the portfolio.

### 7. 7-Asset Rotator (AAA family) — Adaptive Asset Allocation with capital-efficient + ≤2× sleeves

> **Promoted to production 2026-05-12** (was previously in the candidate tier as "AAA Free 2× + NTSD"). Replaces the discontinued Regime World sleeve, paired with the new World 40/30/30.

#### **Strategy Overview:**
Adaptive Asset Allocation (Butler-Philbrick-Gordillo 2012) applied to a 7-asset universe of capital-efficient and 2× leveraged ETFs. Each month it ranks the universe by 6-month price momentum on the unleveraged signal symbols, picks the top-3 positive-momentum candidates, weights them inverse-vol, then applies a portfolio-level vol-target scale. Excess capacity sits in SHV (T-bills).

#### **Universe (signal symbol → held position, all ≤2× per ticker):**
| Signal | Held position | Role |
|---|---|---|
| SPY | **NTSD** (WisdomTree US Plus Intl) | 90% US + 60% intl-futures stack (150% notional) |
| IWM | **SAA** (ProShares Ultra Russell 2000) | 2× US small-cap |
| EEM | **EET** (ProShares Ultra MSCI EM) | 2× emerging markets |
| TLT | **UBT** (ProShares Ultra 20+yr Treasury) | 2× long Treasuries |
| IEF | **UST** (ProShares Ultra 7-10yr Treasury) | 2× intermediate Treasuries |
| GLD | **UGL** (ProShares Ultra Gold) | 2× gold |
| DBC | **DBC** | 1× commodities (no clean 2× equivalent) |

Defensive cash: **SHV** (iShares Short Treasury Bond ETF).

> **Holdings vs. universe (attribution note):** the table above is the *candidate universe* of seven signal→position pairs. Because the strategy holds only the momentum **top-3** at any time (`aaa_config["top_n"] = 3`), a live snapshot of this sleeve normally shows just **3 of the 7 tickers** (plus SHV when exposure is scaled down or the DD-30 stop has fired). Tickers rotate in and out month to month, so the *held* set is a moving subset of the universe and realized P/L from closed rotations is part of the sleeve's return. Attribution by ticker is reliable because no other sleeve owns any of these eight tickers (see `STRATEGY_SYMBOLS["aaa"]`); the canonical per-strategy position record is Firestore `strategy-balances-{env}/aaa`, not a broker/Parqet holdings snapshot (Parqet does not tag transactions by strategy).

#### **Monthly mechanics:**
1. Compute 6-month (126 trading-day) trailing total return on the seven signal symbols.
2. **DD-30 stop check** — if AAA's trailing-peak NAV drawdown breaches -30%, liquidate all positions to SHV and reset peak.
3. Rank positive-momentum signals; pick the top-3.
4. Inverse-vol weight the top-3 using 60-day trailing realized vol of the held positions.
5. Apply **25% annualized vol-target** — scale exposures by `min(1, 0.25 / weighted_portfolio_vol)`. Remainder parks in SHV.
6. Compute target dollar amounts, sells-first then buys.
7. Persist state to Firestore (positions, peak NAV, last scores/picks/weights).

#### **20-Year Backtested Performance (2006-2026, this sleeve standalone):**
- CAGR: **15.97%** • Sharpe: **0.74** (highest of any deployed sleeve) • Max DD: **-28.65%**
- Worst year: -15.04% • Worst rolling 3-year: **+2.22%** (the strategy has never had a losing 3-year period in 20 years)

#### **Why it earned promotion:**
- Highest Sharpe of any deployed sleeve, and the best risk-of-loss profile (never lost over any rolling 3-year window).
- Replaces Regime World's geographic-diversification role with broader asset-class diversification (equity + EM + bonds + gold + commodities).
- Internal momentum-rotation makes it adaptive — outperforms the previous static regime detector in macro regime changes.

#### **Tax-awareness caveat:**
The 7-asset universe with monthly top-3 rotation generates the highest turnover of any sleeve in the portfolio. **Capped at 15% in production to limit short-term capital-gains tax drag in this taxable account.**

### 8. World 40/30/30 — international diversifier (new 2026-05-12)

> **New production sleeve, promoted from Wave 8 research.** Static 40/30/30 blend of WLDU + GOLY + TLT, quarterly rebalance. Zero deployed-ticker overlap. Most tax-efficient sleeve in the portfolio (3 fixed assets, no rotation logic).

#### **Strategy Overview:**
A simple static blend designed to (1) add genuine international equity exposure that the otherwise US-heavy portfolio lacks, and (2) bundle three uncorrelated diversifiers (gold, managed futures, corporate-bond carry) into a single ticker via GOLY's triple-stack structure.

#### **Holdings:**
| Ticker | Weight | Composition / role |
|---|---:|---|
| **WLDU** | 40% | Leverage Shares 2× MSCI World ETP — leveraged international equity (60% US + 40% intl-developed). The portfolio's primary intl-equity anchor; no other deployed sleeve provides clean intl exposure at scale. |
| **GOLY** | 30% | Quantify "Stacked Gold + MF + Corp Bonds" ETF — 50% gold + 50% managed futures + 100% corporate bonds = 200% notional in one ticker. Triple-stacked diversifier. |
| **TLT** | 30% | iShares 20+ Year Treasury — unleveraged duration. Clean macro hedge with no daily-reset decay (unlike HFEA's TMF or AAA's UBT). |

Effective portfolio notional: 0.40×2 + 0.30×2 + 0.30×1 = **1.70**.

#### **Monthly + quarterly mechanics:**
- **Monthly buys** (`make_monthly_buys_f4`): contributions tilt toward the most underweight leg relative to the 40/30/30 target — drift-correcting without forcing a full rebalance.
- **Quarterly rebalance** (`quarterly_rebalance_f4`): on the first trading day of each calendar quarter, sells over-weight legs and buys under-weight legs to restore exact 40/30/30.

#### **24-Year Backtested Performance (this sleeve standalone):**
- CAGR: **12.22%** • Sharpe: **0.68** • Max DD: -43.37%
- Worst year: -27.91% • Lowest volatility of any deployed sleeve (15.12% annualized)

#### **Why it was selected over alternatives:**
After testing 35+ WLDU-based candidate designs across Waves 5/6/7/8:
- Strict rules applied: per-ticker leverage ≤ 2×, zero deployed-ticker overlap.
- **F4 (this design)** delivered the best Sharpe (0.68) of any candidate that fully cleared both rules over the full 24-year backtest window.
- Beats simpler 3-asset variants on Sharpe through GOLY's internal diversification (gold + MF + credit in one position).

#### **Production overlap status:**
- **WLDU, GOLY, TLT — none deployed elsewhere.** This is the cleanest no-overlap sleeve in the portfolio.
- GOLY internally holds managed futures (same asset class as HFEA's KMLM) but via a different ticker — the user explicitly chose the literal-ticker interpretation of the no-overlap rule.

#### **Implementation caveats:**
- **WLDU**: launched 2026-03-12 (Leverage Shares 2× World ETP). 24-year backtest uses synthetic (2× URTHSIM minus financing minus expense ratio) for pre-2026 periods. Live tracking-error to the synthetic is the largest unhedged uncertainty.
- **GOLY**: launched 2025-04. Pre-inception synthetic uses the same component formula (50% GLDSIM + 50% DBMFSIM + 100% LQD) — but only 7 months of live data.
- **DBMF inside GOLY synth**: extended back to 2000-01 via the Testfolio DBMFSIM monthly series (daily-aligned), so the GOLY-synthesised history is honest from 2000+ rather than DBMF's 2019 inception.

## Backtest Results & Robustness

The full portfolio is backtested over **up to 56 years** of spliced daily returns (1970-01-02 → 2026-05-08), using a tiered data layer:
- **Real ETFs** from Alpaca / EODHD post-inception
- **Testfolio SIM data** for the long-history backbone: SPYSIM (1885+), EFASIM (1970+), URTHSIM (1970+), NTSDSIM (1970+), TLTSIM (1962+), IEFSIM (1962+), QQQSIM (1986+), BNDSIM (1986+), GLDSIM (1968+), SLVSIM (1968+), KMLMSIM (1988+), **DBMFSIM (2000+ — newly integrated 2026-05-12)**
- **Synthetic leveraged ETFs** (UPRO/TMF/TQQQ/SPUU/QLD/EFO/SAA/EET/UBT/UST/UGL/TYD/EDC/etc.) using Testfolio L=N formula with daily BIL financing + 40bp spread + expense ratios
- **Synthetic capital-efficient stacks** (NTSX/GDE/RSSB/WTIP/NTSI/GDT/RSIT/GOLY) reconstructed from underlying-component returns

Backtest engine: `research/mega_backtest.py`. Promotion-decision analyses (correlation, portfolio what-if, regime splits, rolling Sharpe, distribution stats) added 2026-05-12 for the final shortlist evaluation.

### Deterministic Backtest (single historical path)

Per-strategy native-window metrics from the 2026-05-12 unified backtest. Each strategy is evaluated on its longest available data window. **The 9-Sig row (and the dependent aggregate) were refreshed 2026-06-04** after the textbook 60/40 correction — 9-Sig CAGR fell 26.0%→18.7% and Sharpe 0.54→0.35 vs the old (implicitly aggressive) 80/20 implementation; see the 9-Sig section above.

| Strategy | Weight | Native window | CAGR | Vol | Sharpe | Max DD | Worst Yr |
|---|---:|---|---:|---:|---:|---:|---:|
| 7-Asset Rotator (AAA family) | 15% | 2006-2026 (20y) | **15.97%** | 18.88% | **0.74** | -28.65% | -15.04% |
| Regime SSO | 12% | 1990-2026 (36y) | 13.49% | 17.00% | 0.68 | **-23.72%** | **-7.05%** |
| **World 40/30/30** (new) | **18%** | 2002-2026 (24y) | 12.22% | **15.12%** | **0.68** | -43.37% | -27.91% |
| DM 2× best-of-3 | 20% | 1987-2026 (39y) | **17.49%** | 23.65% | 0.66 | -39.20% | -26.34% |
| HFEA | 15% | 1988-2026 (38y) | 18.53% | 28.93% | 0.57 | -65.50% | -41.74% |
| 9-Sig | 5% | 1987-2026 (39y) | 18.65% | 48.05% | 0.35 | -98.42% | -80.19% |
| SPXL SMA | 15% | 1970-2026 (56y) | 17.77% | 34.86% | 0.45 | -56.19% | -41.01% |
| **AGGREGATE (deployed, partial-coverage)** | **100%** | 1970-2026 (56y) | **17.83%** | 25.24% | **0.63** | -48.66% | -40.35% |
| 100% SPY (benchmark) | — | 1970-2026 (56y) | 11.02% | 17.26% | 0.52 | -55.19% | -36.79% |
| 100% URTH MSCI World (benchmark) | — | 1970-2026 (56y) | 9.88% | 15.59% | 0.51 | -57.82% | -40.72% |

The aggregate is computed with **partial coverage**: at any date, only deployed strategies with available history contribute (weights renormalize). Earliest aggregate data point is 1970-01-02 using SPXL SMA only (the sole strategy with history back to 1970); DM 2× joins from 1987 and Regime SSO from 1990, and the full 7-sleeve aggregate is available from ~2006 onward.

**Key observations:**
- **Aggregate Sharpe 0.63 over 56 years** is strong despite the long window catching the 1973-74 oil shock, 1980 Volcker recession, 2000-02 dot-com bear, 2008 GFC, 2022 inflation, etc.
- vs SPY: **+6.8pp CAGR, +0.11 Sharpe**, comparable tail risk
- vs MSCI World: **+7.9pp CAGR, +0.12 Sharpe**, comparable tail risk
- $1 → $10,500+ (deployed aggregate) vs $370 (SPY) vs $205 (MSCI World) over 56 years

### Distribution & tail-risk statistics (2026-05-12 additions)

Added with the final shortlist evaluation: skew, excess kurtosis (Fisher), monthly VaR(5%), monthly CVaR(5%), worst rolling 1y/3y/5y compound returns, max days underwater.

| Strategy | Skew (mo) | Excess Kurt | VaR 5% | CVaR 5% | Worst 1Y | Worst 3Y | Worst 5Y | Max Days Underwater |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 7-Asset Rotator | +0.37 | 1.51 | -5.9% | -8.4% | -28.3% | **+2.2%** ⭐ | **+20.0%** ⭐ | 639 (2.5y) |
| Regime SSO | +0.60 | 4.69 | -5.9% | -9.4% | **-17.8%** | -13.0% | -8.5% | 530 (2.1y) |
| World 40/30/30 | -0.51 | 1.40 | -6.6% | -9.4% | -39.8% | -23.8% | -5.5% | 714 (2.8y) |
| DM 2× best-of-3 | -0.17 | 0.75 | -8.9% | -12.3% | -29.8% | -37.4% | -11.5% | 1072 (4.3y) |
| HFEA | -0.19 | 1.54 | -11.4% | -15.9% | -57.9% | -57.0% | -49.0% | 1209 (4.8y) |
| 9-Sig | +0.03 | 1.55 | -18.4% | -28.1% | -87.5% | -96.7% | -92.6% | 4973 (19.7y) |
| SPXL SMA | +0.16 | 1.21 | -14.1% | -19.6% | -48.6% | -55.5% | -41.2% | 1710 (6.8y) |
| AGGREGATE (deployed) | +0.46 | 3.90 | -9.7% | -14.6% | -43.3% | -38.7% | -30.3% | 950 (3.8y) |

⭐ = best-in-class. **The 7-Asset Rotator has never had a losing 3-year period and has never had a losing 5-year period in 20 years.**

### Monte Carlo Robustness (Stationary Block Bootstrap)

Per-strategy bootstrap on each strategy's native window — Politis-Romano stationary block (n=2000 simulations, mean block = 63 trading days ≈ one quarter). Each strategy is sampled independently to avoid zero-padding shorter-window strategies into the joint resample. Beat-benchmark probabilities use matched bootstrap paths.

**Beat-benchmark probabilities (sample of headline numbers):**

| Strategy | vs SPY (CAGR) | vs SPY (Sharpe) | vs SPY (MaxDD) | vs URTH (CAGR) | vs URTH (Sharpe) |
|---|---:|---:|---:|---:|---:|
| **AGGREGATE (deployed)** | **99.7%** | **76.9%** | 17.6% | **99.9%** | **78.3%** |
| 7-Asset Rotator | 84.8% | 82.2% | **87.9%** | 94.2% | 91.8% |
| Regime SSO | 77.1% | 79.3% | **87.0%** | 92.6% | 89.5% |
| HFEA | 99.1% | 65.5% | 2.3% | 100.0% | 91.7% |
| DM 2× best-of-3 | 98.7% | 88.4% | 58.3% | 99.9% | 96.7% |
| 9-Sig | 85.8% | 9.2% | 0.0% | 91.3% | 32.6% |
| SPXL SMA | 95.2% | 22.5% | 1.6% | 96.4% | 27.3% |
| World 40/30/30 | 30.9% | 28.5% | 56.2% | 80.0% | 72.0% |

**What the Monte Carlo tells us:**
- Aggregate beats SPY on CAGR in **99.7% of simulated paths** and on Sharpe in **76.9%** (matching the AGGREGATE row of the table above).
- 7-Asset Rotator and Regime SSO are the standout risk-adjusted sleeves — both beat SPY on MaxDD in **>85% of paths**.
- World 40/30/30's CAGR-beat probability vs SPY (30.9%) is modest because of the strict ≤2× rule on a 56-year window where US has consistently outperformed; its **vs URTH probability (80%)** is the more relevant intl-diversification comparison.
- HFEA / 9-Sig / SPXL SMA fail the MaxDD-beat test as expected — they're return-amplifiers, not risk-controllers.

### Portfolio what-if for World 40/30/30 (final-step promotion check)

Tested injecting World 40/30/30 at 7% allocation into the deployed aggregate (deployed weights renormalized):

| Metric | Pre | Post | Δ |
|---|---:|---:|---:|
| Sharpe | 0.673 | 0.675 | +0.002 |
| CAGR | 18.65% | 18.09% | -0.56pp |
| MaxDD | -48.66% | -48.18% | -0.48pp (slightly tighter) |

The marginal impact is small at 7% but real and directionally favorable on all three metrics. At the final **18% allocation**, the diversification effect is materially larger and dominates the slight CAGR drag.

### Honest Caveats

- **WLDU is brand new** (live since 2026-03-12). The 24-year World 40/30/30 backtest is ~99% synthetic for the WLDU leg (2×URTHSIM minus financing minus expense ratio). Live tracking-error to the synthetic is the largest unhedged uncertainty for the new sleeve.
- **GOLY launched 2025-04** with only ~7 months of live data. The pre-inception synthetic uses the same component formula as the prospectus, but production execution may diverge slightly.
- **DBMFSIM (extended back to 2000-01)** is a Testfolio simulation of the iMGP DBi DBMF Index — it differs from live DBMF by ~2-3pp annualized in our spot-checks.
- Bootstrap can't simulate regimes that don't appear in the 1970-2026 sample (e.g., a 1930s-style depression).
- Whole-share / fractional constraints aren't modeled.
- Margin behavior in the backtest is simplified vs. production's gated margin logic.

## Detailed Analysis of All Strategies

### **Risk and Volatility:**
- **HFEA Strategy**: The HFEA strategy's use of leveraged ETFs means that both gains and losses are magnified. The three-asset allocation (UPRO/TMF/KMLM at 45/25/30) provides better diversification than traditional two-asset HFEA portfolios. KMLM's managed futures component can provide uncorrelated returns during certain market conditions, potentially reducing overall portfolio volatility. However, this strategy still requires a strong risk tolerance and is generally suitable for investors with a long-term horizon who can withstand short-term losses.
  
- **S&P 500 with 200-SMA Strategy**: The 200-SMA strategy, while still involving a leveraged ETF (SPXL), mitigates risk by using a market-timing mechanism. By exiting the market during downtrends, the strategy avoids significant drawdowns, making it less volatile than the HFEA strategy. However, it still carries the risks associated with leveraged ETFs, including the potential for loss during sharp market reversals.

- **9-Sig Strategy**: The 9-Sig strategy balances growth and risk management through systematic rebalancing and crash protection. While it uses leveraged ETFs (TQQQ), the monthly contributions to bonds and the "30 Down, Stick Around" rule provide significant downside protection. The strategy's systematic approach removes emotional decision-making and provides built-in risk management during market crashes.

- **Dual Momentum (best-of-3)**: Uses 2× leveraged ETFs (SPUU/QLD/EFO) and rotates monthly to whichever underlying has the strongest blended 6m/12m momentum (skip-1m). Two layered risk controls — a 30% trailing-peak drawdown stop and a 25% annualized vol target — keep effective leverage ≤ 2× and prevent leveraged-ETF decay during bear markets. The 24-year backtested -34% max DD is roughly half what unhedged HFEA produced.

- **Regime SSO**: 7-signal composite (price trend, breadth, VIX, ADX, credit, news sentiment, canary universe) gates entry to SSO (2× S&P) vs USFR (cash). The composite scoring suppresses single-signal whipsaw — meaningful exits and re-entries fire ~1.4 times per year. Lowest max drawdown (-24%) of any leveraged sleeve.

- **7-Asset Rotator (AAA)**: Adaptive Asset Allocation over seven capital-efficient / 2× sleeves (NTSD/SAA/EET/UBT/UST/UGL/DBC), holding the momentum top-3 with inverse-vol weighting, a 25% annualized vol target, and a 30% trailing-peak drawdown stop to SHV cash. Highest Sharpe (0.74) of any deployed sleeve; never had a losing 3-year period in 20 years.

- **World 40/30/30 (F4)**: Static 40% WLDU (2× MSCI World) + 30% GOLY (gold + managed-futures + corporate-bond triple stack) + 30% TLT, quarterly rebalance. Lowest volatility (15.12% annualized) and lowest turnover of any deployed sleeve — the portfolio's clean international-equity diversifier.

### **Investment Horizon:**
- **HFEA Strategy**: Best suited for long-term investors who can afford to leave their investments untouched for several years, allowing the compounding effect to play out.
  
- **S&P 500 with 200-SMA Strategy**: This strategy can also be used for long-term growth, but with a focus on preserving capital during market downturns. It's more suitable for investors who are cautious about market cycles and prefer to reduce exposure during bear markets.

- **9-Sig Strategy**: Designed for long-term systematic growth with quarterly rebalancing. The strategy's systematic approach and crash protection make it suitable for investors who want exposure to leveraged growth but with built-in risk management. The monthly contributions to bonds provide a steady foundation while the quarterly rebalancing optimizes growth.

- **Dual Momentum (best-of-3)**: Ideal for long-term investors who want a tactical sleeve that adapts to which asset class is leading (US large-cap vs Nasdaq vs international developed). Monthly rebalancing strikes a balance between responsiveness and transaction costs.

- **Regime SSO**: Designed as a slow, defensive-tilted sleeve. The 7-signal composite is intentionally noise-resistant — long flat or sideways markets won't trigger rotations. For investors who want signal-driven downside protection rather than buy-and-hold leverage.

- **7-Asset Rotator / World 40/30/30**: Long-term sleeves rebalanced on a monthly (AAA rotation) and quarterly (F4 static blend) cadence respectively. AAA adapts to whichever asset classes are trending; F4 is a set-and-forget diversifier — together they span the tactical and strategic ends of the horizon spectrum.

### **Key Assumptions:**
- **HFEA Strategy**: Assumes that the diversification benefits of combining equities, bonds, and managed futures will persist, and that over time, the leveraged returns will outweigh the increased volatility. The strategy also assumes that KMLM's trend-following approach will provide crisis alpha and reduce drawdowns during major market dislocations.
  
- **S&P 500 with 200-SMA Strategy**: Assumes that the 200-SMA is a reliable indicator of market trends and that the market's behavior will continue to follow historical patterns where it tends to trend above or below the 200-SMA for extended periods.

- **9-Sig Strategy**: Assumes that the systematic rebalancing approach will capture market growth while the crash protection rule will prevent significant losses during major market downturns. The strategy assumes that the 9% quarterly growth target is achievable over long-term market cycles and that the monthly contributions to bonds provide sufficient stability for the leveraged growth component.

- **Dual Momentum (best-of-3)**: Assumes that momentum persists for 6-12 months due to behavioral biases, that adding QLD (Nasdaq) widens the rotation pool to capture tech-led regimes, and that the layered DD-stop + vol-target combination reduces leverage decay during sustained bears. The skip-1m construction guards against short-term reversal.

- **Regime SSO**: Assumes that composite multi-signal regime detection is more robust than any single indicator (200-SMA, VIX, etc.) and that combining slow (15-day score persistence) with fast (3-day extreme score) exit logic balances false-alarm avoidance with crash protection. The Fed hike filter assumes the monetary-policy environment is a meaningful regime modifier.

- **7-Asset Rotator / World 40/30/30**: Assume that cross-asset momentum persists over ~6-month horizons (AAA) and that stacking uncorrelated return streams — international equity, gold, managed futures, and duration — improves risk-adjusted return without explicit market timing (F4).

## Conclusion

All seven deployed strategies offer unique ways to enhance returns, with complementary risks. HFEA pursues maximum growth through balanced leverage. SPXL SMA captures market gains while avoiding sustained downturns via the 200-SMA. 9-Sig systematizes TQQQ/AGG growth with built-in crash protection. Dual Momentum rotates among three 2× sleeves with DD-stop + vol-target. Regime SSO uses a 7-signal composite to gate entry to 2× S&P. The **7-Asset Rotator** brings adaptive multi-asset rotation (the highest-Sharpe sleeve, never with a losing 3-year period). The **World 40/30/30** provides clean international diversification with the lowest turnover and lowest volatility of any deployed sleeve.

Together, the seven strategies provide a comprehensive blend of aggressive US growth, trend-following, systematic rebalancing, multi-asset momentum, signal-driven risk management, adaptive rotation, and international diversification:

- **HFEA (15%)**: Three-asset leveraged portfolio (UPRO 45% / TMF 25% / KMLM 30%) — aggressive US-equity workhorse
- **SPXL SMA (15%)**: 3× S&P trend-follower with 200-day SMA gate
- **9-Sig (5%)**: Systematic TQQQ/AGG growth with crash protection — tail-risk sleeve, kept small
- **Dual Momentum (20%)**: Best-of-3 rotation (SPUU/QLD/EFO) + DD-stop + vol-target — strong risk/return at ≤2× leverage
- **Regime SSO (12%)**: 7-signal US regime detector — SSO ↔ USFR
- **7-Asset Rotator (15%)**: Adaptive monthly top-3 momentum rotation over NTSD/SAA/EET/UBT/UST/UGL/DBC, inverse-vol weighted, DD30 + vol25 — highest Sharpe of any deployed sleeve
- **World 40/30/30 (18%)**: Static 40% WLDU + 30% GOLY + 30% TLT, quarterly rebalance — international diversifier, most tax-efficient sleeve, zero deployed-ticker overlap

> *Historical context: RSSB/WTIP (10%) was discontinued 2026-05-11 after poor risk-adjusted performance. Regime World (22.22%) was discontinued 2026-05-12 in favor of the 7-Asset Rotator (broader asset-class diversification with monthly adaptation) + World 40/30/30 (clean intl + tax-efficient static blend) pair.*

Each strategy has been selected based on historical backtests, robustness testing (Monte Carlo stationary block bootstrap, 2000 simulated paths per strategy on its native window), promotion-decision analyses (correlation, portfolio what-if, regime splits, rolling Sharpe, tail-risk stats), and current market research. The diversification across seven different approaches produces an aggregate Sharpe of **0.63 over 56 years** with $1 → $10,500+ vs SPY's $370. **The aggregate portfolio beats SPY on CAGR in 99.7% of simulated paths and beats MSCI World on Sharpe in 78.3%** — the strongest portfolio-construction evidence the data supports.

## Index Alert System

The project includes a unified index alert system that monitors multiple indices and provides automated notifications via Telegram when specific conditions are met.

### **Alert Types:**

#### **1. All-Time High (ATH) Drop Alerts**
- **S&P 500**: Monitors for 30% drop from all-time high
- **MSCI World (URTH)**: Monitors for 30% drop from all-time high
- **Schedule**: Every hour during trading hours (9:30 AM - 3:30 PM)
- **Purpose**: Alert when major indices have significant drawdowns for potential investment opportunities

#### **2. SMA Crossing Alerts**
- **URTH 255-day SMA**: Monitors iShares MSCI World ETF crossing above/below 255-day SMA
- **SPY 200-day SMA**: Monitors SPY (S&P 500 ETF) crossing above/below 200-day SMA
- **Schedule**: Every hour during trading hours (9:15 AM - 3:15 PM)
- **Purpose**: Track trend changes and potential market direction shifts

### **Alert Configuration:**
- **Noise Threshold**: 1% minimum deviation to avoid excessive notifications
- **Emoji Indicators**: 🚀 for above SMA, 📉 for below SMA
- **Telegram Integration**: All alerts sent to configured Telegram chat
- **Unified System**: Single Cloud Function handles all alert types with different parameters

### **Example Alert Messages:**
```
🚀 URTH Alert: iShares MSCI World ETF crossed ABOVE its 255-day SMA! 
Current: $180.50 (SMA: $178.20, +1.29%)

📉 SPY Alert: Crossed BELOW its 200-day SMA! 
Current: $432.15 (SMA: $438.50, -1.38%)

Alert: S&P 500 has dropped 32.15% from its ATH! 
Consider a loan with a duration of 6 to 8 years (50k to 100k) at around 4.5% interest max
```

## Project Structure

- `main.py`: The main Python script containing all strategy logic:
  - **HFEA strategy**: Three-asset portfolio (UPRO/TMF/KMLM at 45/25/30) with monthly underweight-based buys and quarterly rebalancing
  - **SPXL SMA strategy**: Trend-following with 200-day SMA (monthly buys and daily trading)
  - **9-Sig strategy**: Jason Kelly methodology with monthly AGG contributions and quarterly TQQQ/AGG signals with crash protection
  - **Dual Momentum strategy**: Best-of-3 rotation across SPUU/QLD/EFO with DD-stop and vol-target
  - **Regime SSO**: 7-signal composite regime detector — SSO ↔ USFR rotation
  - **7-Asset Rotator (AAA family)** *(new 2026-05-12)*: Monthly top-3 momentum rotation over NTSD/SAA/EET/UBT/UST/UGL/DBC with inverse-vol weighting, DD30 stop, vol25 target, and SHV defensive cash
  - **World 40/30/30** *(new 2026-05-12)*: Static 40% WLDU + 30% GOLY + 30% TLT with monthly drift-correcting buys + quarterly full rebalance
  - **Unified index alert system**: Monitors multiple indices for ATH drops and SMA crossings
  - **Firestore integration**: Persistent storage for strategy balances, 9-Sig quarterly data, Dual Momentum + 7-Asset Rotator position tracking, regime score history, World 40/30/30 quarterly rebal idempotency markers, and unified market data cache
  - **Alpaca integration**: All market data fetched from Alpaca IEX feed (no yfinance dependency)
- `research/mega_backtest.py`: Unified research backtest engine — covers all 7 deployed strategies + extensive historic strategy universe; includes Monte Carlo robustness, promotion-decision analyses (correlation / what-if / regime splits / rolling Sharpe / tail risk), and HTML report generation
- `research/extended_data.py`: Tiered data layer — splices Testfolio SIM data (SPYSIM/EFASIM/URTHSIM/NTSDSIM/TLTSIM/IEFSIM/QQQSIM/BNDSIM/GLDSIM/SLVSIM/KMLMSIM/DBMFSIM) with real Alpaca + EODHD feeds for backtests
- `requirements.txt`: Python dependencies including pandas, Google Cloud libraries, and Flask.
- `cloudbuild.yaml`: Google Cloud Build configuration for deploying Cloud Functions and Cloud Scheduler jobs.
- `README.md`: Comprehensive documentation of all strategies and setup instructions.

### **Cloud Functions Deployed:**
- `monthly_invest_all`: **Orchestrator function (RECOMMENDED)** — runs all seven monthly strategies with coordinated budget calculations
- `monthly_buy_hfea`: HFEA monthly investment function (individual execution)
- `rebalance_hfea`: HFEA quarterly rebalancing function
- `monthly_buy_spxl`: SPXL SMA monthly investment function (individual execution)
- `daily_trade_spxl_200sma`: SPXL SMA daily trading function
- `monthly_nine_sig_contributions`: 9-Sig monthly contributions function (individual execution)
- `quarterly_nine_sig_signal`: 9-Sig quarterly signal function
- `monthly_dual_momentum`: Dual Momentum strategy function (individual execution)
- `monthly_buy_regime_sso`: Regime SSO monthly buy
- `daily_regime_check`: Regime SSO daily score check
- **`monthly_buy_aaa`** *(new 2026-05-12)*: 7-Asset Rotator monthly execution (momentum scoring → top-3 inverse-vol → vol-target scale → DD30 check)
- **`monthly_buy_f4`** *(new 2026-05-12)*: World 40/30/30 monthly drift-correcting buys toward 40/30/30 target
- **`quarterly_rebalance_f4`** *(new 2026-05-12)*: World 40/30/30 quarterly rebalance to exact 40/30/30 with Firestore idempotency marker
- `index_alert`: Unified index alert system
- `backfill_regime_scores`: One-shot manual seeder for Regime SSO composite-score history
- `audit_monthly_run`: Day-8 watchdog that verifies the monthly orchestrator actually ran

*(16 Cloud Functions total.)*

### **Cloud Scheduler Jobs:**

*(11 scheduled jobs total.)*

- **Monthly orchestrator**: First trading day of each month at 12:00 PM ET (`monthly_invest_all` — runs all seven monthly strategies with coordinated budgets)
- **Quarterly functions**: First trading day of each quarter (`rebalance_hfea` at 2:00 PM ET, `quarterly_nine_sig_signal` at 1:00 PM ET, **`quarterly_rebalance_f4`** at 3:00 PM ET)
- **Monthly-run watchdog**: 2:00 PM ET on the 8th of each month (`audit_monthly_run`) — alerts via Telegram if the orchestrator failed to run in the day-1-7 window
- **Index alerts**: Hourly during trading hours (9:15 AM - 3:15 PM for SMA alerts, 9:30 AM - 3:30 PM for ATH drop alerts) — four jobs: `sp500_drop`, `msci_drop`, `urth_255sma`, `spy_200sma`
- **Daily SMA functions**: 3:56 PM ET on weekdays (`daily_trade_spxl_200sma`)
- **Daily regime checks**: After-close on weekdays (`daily_regime_check` at 16:30 ET)

**Note**: Individual monthly functions are deployed but not scheduled. They remain available for manual execution and debugging purposes. The `monthly_invest_all` orchestrator is used for production to ensure coordinated budget allocation and prevent over-spending. The AAA and F4 monthly buys are driven **in-process** by the orchestrator (no per-strategy scheduler by design); only `quarterly_rebalance_f4` — which the orchestrator does not cover — has its own dedicated Cloud Scheduler (3:00 PM ET, first trading days of each calendar quarter), with a Firestore idempotency marker (`quarterly-runs-{env}/f4-{quarter}`) so repeat fires are safe.

## Monthly Investment Orchestrator

The `monthly_invest_all` orchestrator is a coordinated execution system that manages all **seven** monthly investment strategies (HFEA, SPXL SMA, 9-Sig, Dual Momentum, Regime SSO, 7-Asset Rotator, and World 40/30/30) in a single unified process.

### **Why Use an Orchestrator?**

Without the orchestrator, each strategy would independently:
1. Check margin conditions
2. Calculate available cash and margin
3. Determine its investment amount
4. Execute trades

This approach creates a critical problem: **each function would try to use the full available buying power**, leading to over-spending and failed trades.

### **How the Orchestrator Solves This**

The orchestrator (`monthly_invest_all_strategies()` function):

1. **Calculates budgets once**: Checks margin conditions and calculates total available buying power a single time
2. **Distributes precisely**: Splits the total amount according to strategy allocations:
   - HFEA: 15%
   - SPXL SMA: 15%
   - 9-Sig: 5%
   - Dual Momentum: 20%
   - Regime SSO: 12%
   - 7-Asset Rotator: 15%
   - World 40/30/30: 18%
3. **Passes pre-calculated amounts**: Each strategy receives its exact budget and margin conditions as parameters
4. **Prevents over-spending**: Since budgets are pre-calculated, there's no risk of multiple strategies competing for the same funds

### **Key Features**

- **Coordinated execution**: All strategies run in sequence with shared context
- **Exact splits**: Portfolio allocation percentages are maintained precisely
- **Single margin check**: Margin conditions evaluated once and shared across all strategies
- **Unified reporting**: Consolidated Telegram notifications show the complete picture
- **Fail-safe design**: If one strategy fails, others can still execute

### **Production Recommendation**

For production deployments, **always use the orchestrator** (`monthly_invest_all`) instead of scheduling individual monthly functions. This ensures:
- Consistent portfolio allocation
- No race conditions between functions
- Accurate budget management
- Simplified monitoring and debugging

The individual functions remain deployed for manual testing and debugging but should not be scheduled in production environments.

## Margin-Aware Investment Logic

The system includes intelligent margin control applied via the monthly orchestrator, which runs a **single** margin check and shares the result across all seven monthly investment strategies (HFEA, SPXL SMA, 9-Sig, Dual Momentum, Regime SSO, 7-Asset Rotator, and World 40/30/30). This feature enables controlled use of leverage (up to +10%) only when market conditions are favorable and borrowing costs are reasonable.

### **Core Principles**

1. **Conservative Leverage**: Maximum +10% exposure (1.10× leverage) - enhances returns without excessive risk
2. **Rule-Based Activation**: Margin only enabled when ALL safety gates pass
3. **Automatic Deactivation**: Switches to cash-only mode when conditions deteriorate
4. **Full Transparency**: Every monthly cycle generates a consolidated Telegram report with decision rationale

### **Four Safety Gates**

Margin is enabled ONLY when all four conditions are met:

#### Gate 1: Market Trend
- **Requirement**: SPY > 200-day SMA
- **Rationale**: Only use leverage in confirmed bull markets
- **Note**: Uses SPY (S&P 500 ETF) as S&P 500 Index proxy

#### Gate 2: Margin Rate
- **Requirement**: Borrowing cost ≤ 8.0%
- **Calculation**: FRED Federal Funds Rate + spread
  - Accounts < $35k: FRED rate + 2.5%
  - Accounts ≥ $35k: FRED rate + 1.0%
- **Data Source**: Federal Reserve Economic Data (FRED) API - DFEDTARU series
- **Rationale**: Avoid expensive borrowing that erodes returns

#### Gate 3: Buffer
- **Requirement**: Buffer ≥ 5%
- **Formula**: `(Equity / Portfolio Value) - (Maintenance Margin / Portfolio Value)`
- **Rationale**: Maintain safety cushion above maintenance margin

#### Gate 4: Leverage
- **Requirement**: Current leverage < 1.14×
- **Formula**: `Portfolio Value / Equity`
- **Rationale**: Prevent over-leveraging

### **Investment Behavior**

#### When Margin is Enabled (All gates pass)
- **Buying Power**: `Cash + (Equity × 10%)`
- **Approach**: All-or-Nothing - invest full monthly amount or skip entirely
- **Firestore**: Not applicable (actively investing)
- **Reporting**: Shows green decision with all gate details

#### When Margin is Disabled (Any gate fails)
- **Buying Power**: Cash only (no margin borrowing)
- **If Still Leveraged** (Leverage > 1.0×):
  - Skip all investments to prioritize deleveraging
  - No Firestore additions (money stays in account)
- **If Equity-Only** (Leverage ≤ 1.0×):
  - Use available cash for investments if sufficient
  - **SPXL SMA Only**: Add skipped amount to Firestore when SMA trend is bearish
  - **HFEA/9-Sig**: Skip without Firestore addition
- **Reporting**: Shows red decision with failed gate(s) highlighted

### **Firestore Logic**

The system tracks skipped investments differently based on strategy and reason:

- **Add to Firestore**: Only for SPXL SMA strategy when:
  1. Index is below 200-SMA (bearish trend), AND
  2. Account is fully equity-only (leverage ≤ 1.0×)
  
- **Skip Firestore**: In all other cases:
  - Margin gates fail (not SMA-related)
  - Account is still leveraged (deleveraging priority)
  - HFEA or 9-Sig strategies (no Firestore tracking)

### **Telegram Reporting**

Each monthly investment cycle generates ONE consolidated message per strategy:

```
📊 [Strategy Name] Monthly Update

Market Trend: ✅ SPY $585.00 (200-SMA: $550.00)
Margin Rate: ✅ 6.5% (FRED 4.0% + 2.5%)
Buffer: ✅ 8.2%
Leverage: ✅ 1.05x

Decision: 🟢 Margin ENABLED (+10%) / 🔴 Cash-Only Mode

Account: Equity $15,000.00 | Portfolio $15,750.00 | Cash $500.00

Action: [Invested $97.50 / Skipped - reason]
```

### **Configuration**

All margin control parameters are defined in `margin_control_config`:

```python
margin_control_config = {
    "target_margin_pct": 0.10,      # Maximum +10% leverage
    "max_margin_rate": 0.08,        # 8% rate threshold
    "min_buffer_pct": 0.05,         # 5% minimum buffer
    "max_leverage": 1.14,           # Maximum 1.14x leverage
    "spread_below_35k": 0.025,      # +2.5% for accounts <$35k
    "spread_above_35k": 0.01,       # +1.0% for accounts ≥$35k
    "portfolio_threshold": 35000,   # Threshold for spread calculation
}
```

### **Fail-Safe Mechanisms**

- **Data Unavailable**: If the FRED API or Alpaca fails → default to cash-only mode
- **API Errors**: All errors logged and reported via Telegram
- **Deleveraging Priority**: When gates fail while leveraged, skip all investments to reduce exposure

## Technical Configuration

### **Key Parameters:**

**Dynamic Monthly Investment:**
- Investment amounts are calculated dynamically each month based on available cash and margin conditions
- Total available = Account cash − Reserved amounts (for bearish strategies) + Approved margin (up to +10% of equity)
- **Split across 7 strategies:** HFEA 15%, SPXL SMA 15%, 9-Sig 5%, Dual Momentum 20%, Regime SSO 12%, 7-Asset Rotator 15%, World 40/30/30 18%
- All-or-Nothing approach: Invest full calculated amount or skip entirely

**HFEA Strategy:**
- Portfolio allocation: **15%** of total monthly investment
- Asset allocation: UPRO 45%, TMF 25%, KMLM 30%
- Rebalancing: Quarterly with 0.5% fee margin
- Investment approach: Underweight-based proportional allocation

**SPXL SMA Strategy:**
- Portfolio allocation: **15%** of total monthly investment
- SMA period: 200 days
- Margin band: 1% (to avoid whipsaws)
- Tracked index: S&P 500 (SPY ETF as proxy)

**9-Sig Strategy:**
- Portfolio allocation: **5%** of total monthly investment
- Target allocation: TQQQ 60%, AGG 40% (Kelly canonical)
- Quarterly growth target: 9%
- Monthly contributions: 100% to AGG (bonds)
- Signal tolerance: 2.5% of sleeve NAV (micro-trade guard)
- Crash protection: 30-down on TQQQ vs rolling 8-quarter high, ignore 2 sells then base-reset; 90% buy throttle + 10% bond floor; spike reset on TQQQ +100%/quarter
- Crash protection: "30 Down, Stick Around" rule (ignores first 2 sell signals when TQQQ ≥30% below its rolling 8-quarter high, then base-resets to 60/40)
- Bond rebalancing threshold: 30% (triggers rebalancing when AGG exceeds this)

**Dual Momentum Strategy (best-of-3):**
- Portfolio allocation: **20%** of total monthly investment
- Asset universe: SPUU (2× S&P 500), QLD (2× Nasdaq), EFO (2× MSCI EAFE), BND (defensive)
- Momentum signal: blended 6m+12m skip-1m on SPY/QQQ/EFA
- DD-stop: 30% trailing-peak NAV
- Vol-target: 25% annualized (60d realized vol)
- Rebalancing frequency: Monthly (first trading day)

**Regime SSO Strategy:**
- Portfolio allocation: **12%** of total monthly investment
- 7-signal composite (price trend / breadth / VIX / ADX / credit / news / canary) + Fed-hike filter
- Holds SSO (2× S&P) when risk-on, USFR (floating-rate Treasury) when risk-off
- Designed to fire ~1.4 rotations per year — slow and noise-resistant

**7-Asset Rotator (AAA family) — new 2026-05-12:**
- Portfolio allocation: **15%** of total monthly investment (capped — highest-turnover sleeve)
- Universe: 7 capital-efficient / 2× ETFs — NTSD (signal SPY), SAA (IWM), EET (EEM), UBT (TLT), UST (IEF), UGL (GLD), DBC (DBC)
- Defensive cash: SHV
- Selection: monthly top-3 by 6m momentum on signal symbols
- Weighting: inverse-vol on the top-3 (60-day realized vol of held positions)
- Vol-target: 25% annualized portfolio vol scale
- DD-30 stop: trailing-peak NAV breach → all to SHV, reset peak
- Tolerance: $5 minimum trade size

**World 40/30/30 — new 2026-05-12:**
- Portfolio allocation: **18%** of total monthly investment (largest single sleeve)
- Fixed targets: WLDU 40% / GOLY 30% / TLT 30%
- Monthly buys: drift-correcting (tilt new contribution toward underweight legs)
- Quarterly rebal: bring positions back to exact 40/30/30 on first trading day of each calendar quarter
- Tolerance: $5 minimum trade size, 5pp drift threshold for early rebal
- Tax-efficiency: 3 fixed tickers, no rotation — lowest turnover sleeve in portfolio

**Alert System:**
- ATH drop threshold: 30% for S&P 500 and MSCI World
- SMA noise threshold: 1% (minimum deviation to trigger alert)
- URTH SMA period: 255 days
- SPY SMA period: 200 days

### **Data Storage:**
- **Firestore Collections:**
  - `strategy-balances-live` / `strategy-balances-paper`: Tracks invested amounts and position details for each strategy (HFEA, SPXL SMA, 9-Sig, Dual Momentum, Regime SSO, 7-Asset Rotator, World 40/30/30)
  - `nine-sig-quarters`: Historical quarterly data for 9-Sig signal calculations
  - `nine-sig-monthly-contributions`: Tracks actual monthly 9-Sig contributions for accurate quarterly signal calculation
  - `regime-scores`: Daily Regime SSO composite scores and signal history (`regime-world-scores` is a historical collection only — Regime World retired 2026-05-12)
  - `quarterly-runs-live` / `quarterly-runs-paper`: Idempotency markers for quarterly functions (`hfea-{quarter}`, `nine_sig-{quarter}`, **`f4-{quarter}`** *new*)
  - `monthly-runs-live` / `monthly-runs-paper`: Idempotency markers for the monthly orchestrator
  - `market-data`: Unified collection caching market prices, SMA values (200-day, 255-day), crossing states, and alert timestamps (5-minute cache expiry) — single source of truth for all market data

**Dual Momentum Tracking** (`strategy-balances-live/dual_momentum`):
  - `total_invested`, `primary_position`, `primary_shares`, `primary_target_pct`, `defensive_shares`, `peak_nav`, `last_momentum_check` (scores, winner, dd_triggered, vol_scale)

**7-Asset Rotator Tracking** (`strategy-balances-live/aaa`) *— new*:
  - `total_invested`, `peak_nav`, `current_positions` (shares per ticker), `current_values` (dollar per ticker), `last_momentum_check` (scores per signal, top-3 picks, inverse-vol weights, vol-target scale, DD-triggered flag, drawdown reading)

**World 40/30/30 Tracking** (`strategy-balances-live/f4`) *— new*:
  - `total_invested`, `peak_nav`, `current_positions` (shares per WLDU/GOLY/TLT), `current_values` (dollar per leg), `last_buy_date`, `last_rebal_date`

### **Trading Platform:**
- **Alpaca API**: Live and paper trading environments supported
- **Order execution**: Market orders with fill-wait logic (5-minute polling, 300-second timeout)
- **Market Data**: Uses SPY (S&P 500 ETF) as proxy for S&P 500 Index - tracks with <0.1% difference
- **Data Source**: Alpaca IEX feed (included with Basic subscription) - no rate limiting, 5 years of historical data
- **Caching**: 5-minute Firestore cache for all price and SMA data to minimize API calls

## Setup

### Prerequisites

- [Google Cloud SDK](https://cloud.google.com/sdk/docs/install)
- [Python 3.10+](https://www.python.org/downloads/)
- Alpaca Trading Account (live or paper)
- Google Cloud Project with Firestore enabled
- Telegram Bot (for notifications)

### Installing Dependencies

First, clone the repository and navigate into the project directory:

```bash
git clone https://github.com/cluttmann/multi_strategy_portfolio.git
cd multi_strategy_portfolio
pip install -r requirements.txt
```

### Local Development and Testing

The script supports local execution for testing strategies before deploying to Google Cloud:

```bash
# RECOMMENDED - Monthly Orchestrator (runs all seven monthly strategies with coordinated budgets)
python3 main.py --action monthly_invest_all --env paper --force

# Individual Strategy Testing (for debugging specific strategies)
# HFEA Strategy
python3 main.py --action monthly_buy_hfea --env paper --force
python3 main.py --action rebalance_hfea --env paper

# SPXL SMA Strategy
python3 main.py --action monthly_buy_spxl --env paper --force
python3 main.py --action sell_spxl_below_200sma --env paper
python3 main.py --action buy_spxl_above_200sma --env paper

# 9-Sig Strategy (with force execution for testing outside trading days)
python3 main.py --action monthly_nine_sig_contributions --env paper --force
python3 main.py --action quarterly_nine_sig_signal --env paper --force

# Dual Momentum Strategy (with force execution for testing outside trading days)
python3 main.py --action monthly_dual_momentum --env paper --force

# Regime SSO Strategy
python3 main.py --action monthly_buy_regime_sso --env paper --force
python3 main.py --action daily_regime_check --env paper

# 7-Asset Rotator (AAA family) — new 2026-05-12
python3 main.py --action monthly_buy_aaa --env paper --force

# World 40/30/30 — new 2026-05-12
python3 main.py --action monthly_buy_f4 --env paper --force
python3 main.py --action quarterly_rebalance_f4 --env paper --force
```

**Why use the orchestrator (`monthly_invest_all`)?**
- Calculates budgets once and distributes them to all strategies
- Ensures exact percentage splits (15% HFEA, 15% SPXL SMA, 5% 9-Sig, 20% Dual Momentum, 12% Regime SSO, 15% 7-Asset Rotator, 18% World 40/30/30)
- Prevents over-spending by coordinating margin and cash allocation
- Recommended for production use to maintain portfolio balance

**Environment Variables:**
Create a `.env` file in the project root with the following variables:
```
ALPACA_API_KEY_LIVE=your_live_key
ALPACA_SECRET_KEY_LIVE=your_live_secret
ALPACA_API_KEY_PAPER=your_paper_key
ALPACA_SECRET_KEY_PAPER=your_paper_secret
TELEGRAM_KEY=your_telegram_bot_token
TELEGRAM_CHAT_ID=your_telegram_chat_id       # ETF-Bot (diese Strategien)
TELEGRAM_CHAT_ID_QNT=your_quant_chat_id      # Quant-Desk; fehlt er, fällt quant/ auf TELEGRAM_CHAT_ID zurück
FREDKEY=your_fred_api_key
GOOGLE_CLOUD_PROJECT_ID=your_project_id
```

**Note**: Get a free FRED API key from https://fred.stlouisfed.org/docs/api/api_key.html

### Deployment to Google Cloud

The project uses Google Cloud Build for automated deployment:

Deployment is automatic: a **push to `main`** on GitHub fires the Cloud Build trigger, which runs `cloudbuild.yaml` (deploying all Cloud Functions and re-creating the Cloud Scheduler jobs).

```bash
# Primary path — just push; the Cloud Build trigger deploys everything
git push origin main

# Manual FALLBACK only (use if the trigger fails):
gcloud auth login
gcloud config set project YOUR_PROJECT_ID
gcloud builds submit --config cloudbuild.yaml
```

**Required Google Cloud Setup:**
1. Enable Cloud Functions API
2. Enable Cloud Scheduler API
3. Enable Firestore API
4. Enable Secret Manager API
5. Store API keys in Secret Manager:
   - `ALPACA_API_KEY_LIVE`
   - `ALPACA_SECRET_KEY_LIVE`
   - `ALPACA_API_KEY_PAPER`
   - `ALPACA_SECRET_KEY_PAPER`
   - `TELEGRAM_KEY`
   - `TELEGRAM_CHAT_ID`
   - `FREDKEY` (for margin rate calculations)

The `cloudbuild.yaml` file defines all Cloud Functions and their corresponding Cloud Scheduler jobs. Deploy steps are grouped into sequential waves (A → B → C1 → C2, then a scheduler wave D) that cap peak concurrent inner container-builds at ≤6 to stay under the project's Cloud Build concurrency quota and avoid EXPIRED build failures. Green-path build time is ~25-30 min.

## Additional Features

### **Trading Day Detection**

The system uses `pandas_market_calendars` to accurately detect:
- Regular trading days
- First trading day of the month
- First trading day of the quarter

This ensures all functions execute only on appropriate market days, avoiding failed trades on holidays and weekends.

### **Telegram Notifications**

All trading actions, rebalancing operations, and alerts are sent via Telegram for real-time monitoring. This includes:
- Trade confirmations with quantities and prices
- Portfolio allocation updates
- Alert notifications (ATH drops, SMA crossings)
- Error messages and timeouts

### **Force Execution Mode**

The 9-Sig strategy functions support a `--force` flag for testing purposes, allowing execution outside of scheduled trading days. This is useful for:
- Testing strategy logic without waiting for month/quarter start
- Debugging signal calculations
- Validating Firestore data storage

**Note:** Force execution should only be used in paper trading environment.

## 2026-05-12 Production Update — Deployment Checklist

The portfolio was restructured on **2026-05-12**. This section is preserved as a point-in-time record of that migration — **all steps below are complete**: the update has been live since 2026-05-12 and the Firestore/GCP cleanup finished 2026-05-17.

### Code changes already in place ✅
- [x] `main.py`: `strategy_allocations` updated (new 15/15/5/20/12/15/18 split)
- [x] `main.py`: `STRATEGY_SYMBOLS` updated (added `aaa` and `f4` keys; removed `regime_world`)
- [x] `main.py`: `aaa_config` and `f4_config` added
- [x] `main.py`: `make_monthly_buys_aaa()`, `make_monthly_buys_f4()`, `quarterly_rebalance_f4()` implemented with full risk-control logic
- [x] `main.py`: Helper functions `get_aaa_position_value()`, `get_f4_position_value()`, `_aaa_six_month_momentum()`, `_aaa_realized_vol()`
- [x] `main.py`: Orchestrator (`monthly_invest_all_strategies`) wires AAA + F4, removes Regime World
- [x] `main.py`: HTTP routes (`/monthly_buy_aaa`, `/monthly_buy_f4`, `/quarterly_rebalance_f4`) added; Regime World routes removed
- [x] `main.py`: CLI argparse + `run_local()` action handlers updated
- [x] `main.py`: `audit_monthly_run` updated with new strategy expected-symbols
- [x] `main.py`: `get_all_strategy_values()`, `calculate_rebalanced_allocations()`, `print_allocation_dashboard()` reflect the new strategy keys
- [x] `research/mega_backtest.py`: `AGGREGATE_WEIGHTS` and `DEPLOYED_STRATEGIES` updated with new allocation; `CANDIDATE_STRATEGIES` cleared (F4 promoted)
- [x] `research/extended_data.py`: DBMFSIM monthly returns integrated and daily-aligned, splicing with real DBMF at 2019-05-08

### Pre-deployment steps (completed ✅)
- [x] **Liquidated legacy WLDU/USFR positions** from the retired Regime World sleeve before the first `monthly_buy_f4` run (the new code does not inherit pre-existing positions from the Regime World era). USFR remains in use by Regime SSO.
- [x] **Firestore cleanup (2026-05-17)** — the Regime World Cloud Functions and `daily_regime_world_check` scheduler were deleted from GCP. The orphaned `strategy-balances-live/regime_world` document (and `regime-world-scores`) were left in place as a historical record.
- [x] **Paper-tested (2026-05-12)** — `monthly_buy_aaa` and `monthly_buy_f4` verified end-to-end via `python3 main.py --action monthly_buy_aaa --env paper --force` (surfaced the `NON_FRACTIONABLE_TICKERS` fix for WLDU/GOLY/NTSD).
- [x] **Cloud Scheduler** — `quarterly_rebalance_f4` given its own scheduler (3:00 PM ET, first trading days of each calendar quarter). `monthly_buy_aaa` / `monthly_buy_f4` are orchestrator-driven in-process, so they intentionally have **no** per-strategy scheduler.
- [x] **Deployed** — via the Cloud Build trigger on push to `main` (not a manual `gcloud builds submit`).
- [x] **First orchestrator run monitored** — all 7 strategies fired successfully via the consolidated Telegram summary.

## Contributing

This is a personal trading bot implementation. Feel free to fork and adapt for your own use, but please note:
- This is not financial advice
- Leveraged ETFs carry significant risk
- Past performance does not guarantee future results
- Always test thoroughly in paper trading before using live funds

## License

This project is for educational and personal use only.
