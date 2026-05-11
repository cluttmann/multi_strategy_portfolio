# Investment Strategy with Alpaca and Google Cloud Functions

This project contains a set of Python Cloud Functions for managing a multi-strategy portfolio using Alpaca's trading API. The portfolio is composed of seven complementary strategies: **HFEA**, **SPXL SMA**, **RSSB/WTIP (Structural Alpha)**, **9-Sig (Jason Kelly Methodology)**, **Dual Momentum (best-of-3 leveraged rotation)**, **Regime SSO (US regime detector)**, and **Regime World (global regime detector)**.

## Portfolio Allocation

Current production weights (sum to 100%):

| Strategy | Weight | Role |
|---|---:|---|
| HFEA | 15% | Aggressive 3× leveraged buy-and-hold (UPRO/TMF/KMLM) |
| SPXL SMA | 15% | 3× S&P trend-following with 200-SMA gate |
| RSSB/WTIP | 10% | Structural alpha — global stocks + managed futures (macro hedge) |
| 9-Sig | 5% | Systematic TQQQ/AGG with crash protection (tail-risk sleeve) |
| Dual Momentum (best-of-3) | 20% | SPUU/QLD/EFO rotation + DD-stop + vol-target |
| Regime SSO | 15% | 7-signal US regime detector — SSO ↔ USFR rotation |
| Regime World | 20% | 7-signal global regime detector — WLDU ↔ USFR rotation |

Two regime-detection sleeves (US + global) make up 35% of the portfolio. The combination delivers a 24-year backtested Sharpe of **0.75** with a max drawdown of **-29%** and a CAGR of **15.78%** — see *Backtest Results & Robustness* below.

## Overview of the Strategies

The project is based on seven distinct investment strategies, each designed to maximize returns by leveraging specific market behaviors and signals.

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
- Sharpe: **0.65** (best risk/return among ≤2× sleeves)
- Max DD: **-33.93%**
- Worst year: -26.48%
- Total return: 4,240%

#### **Why Best-of-3 + Vol-Target Beat the Original:**
- Adding QLD captures Nasdaq momentum during tech-led bull runs (2010s, 2020-2024) that pure SPY signal misses
- Vol-targeting prevents the strategy from holding the full 2× exposure during high-vol regimes — meaningfully reduces drawdown without much CAGR cost
- Trailing-peak DD-stop catches multi-month bear markets the monthly momentum signal would otherwise lag

### 3. RSSB/WTIP Strategy (Structural Alpha)

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
The 9-Sig strategy is based on Jason Kelly's methodology from his book "The 3% Signal". It's a systematic approach to managing a TQQQ (3x leveraged NASDAQ-100) and AGG (iShares Core U.S. Aggregate Bond ETF) portfolio with built-in crash protection. The strategy aims for 9% quarterly growth while maintaining an 80/20 allocation between TQQQ and AGG.

#### **Key Components:**

**Target Allocation:**
- **80% TQQQ**: 3x leveraged NASDAQ-100 ETF for growth
- **20% AGG**: Bond ETF for stability and crash protection

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
- **BUY Signal**: When Current TQQQ < Signal Line → Sell AGG, Buy TQQQ
- **SELL Signal**: When Current TQQQ > Signal Line → Sell TQQQ, Buy AGG  
- **HOLD Signal**: When within $25 tolerance of signal line → No action
- **First Quarter**: Signal line set to 80% of total portfolio value

**Crash Protection - "30 Down, Stick Around" Rule:**
- When SPY drops >30% from all-time high, the strategy ignores the first 4 SELL signals
- This prevents selling during major market crashes
- After 4 ignored signals, normal operation resumes

#### **Example Scenarios:**

**First Quarter:**
```
Starting: $0 TQQQ, $30.75 AGG (from 3 months of contributions)
Signal Line: $24.60 (80% of total portfolio)
Action: BUY $24.60 worth of TQQQ
Result: $24.60 TQQQ, $6.15 AGG (80/20 allocation)
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
BUT: SPY down 35% from ATH
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

#### **24-Year Backtested Performance:**
- CAGR: 12.16% • Sharpe: **0.62** • Max DD: **-23.59%** • Worst year: -13.40%
- Lowest drawdown of any leveraged sleeve in the portfolio.

### 7. Regime World (Global Regime Detector)

#### **Strategy Overview:**
Sister strategy to Regime SSO, applied to global equities. Same 7-signal apparatus + Fed filter, but with world-specific signal sources. Rotates between **WLDU** (Leverage Shares 2× MSCI World ETF) and **USFR** (cash). Provides geographic diversification from the otherwise US-heavy portfolio.

#### **World-Specific Signals:**
| Signal | Regime SSO source | Regime World source |
|---|---|---|
| 1. Trend | SPY 200-SMA | **URTH 255-SMA** (longer window for global indices) |
| 2. Breadth | S&P 500 constituents | **Ex-US country ETF basket** (EFA/EEM/VWO + 12 country ETFs) |
| 3. Volatility | VIX | VIX (universal) |
| 4. ADX | SPY 14-day ADX | **URTH 14-day ADX** |
| 5. Credit | HYG/LQD | HYG/LQD (universal — global credit) |
| 6. News | US-centric Alpaca news firehose | **Filtered to global ETF tickers** (URTH, EFA, EEM, VWO, VEA, ACWI, IEFA) |
| 7. Canary | HYG/EEM/IWM | HYG/EEM/IWM (universal liquidity) |
| Fed filter | DFEDTARU | DFEDTARU (Fed matters globally) |

#### **24-Year Backtested Performance:**
- CAGR: 9.89% • Sharpe: 0.43 • Max DD: -30.94% • Worst year: **-10.49%**
- Lower raw return than Regime SSO because URTH lagged SPY ~4 pp/year over 2002-2026. Earns its keep through **geographic decorrelation** and the **best worst-year of any leveraged sleeve**.

#### **Implementation Caveat — Synthetic Backtest:**
WLDU launched **2026-03-12**. The 24-year backtest is ~99% synthetic (2× URTH minus financing minus expense ratio). Live WLDU's tracking error vs. the synthetic model is the largest unhedged uncertainty in this sleeve.

#### **Shared Engine:**
Both regime sleeves run on a parameterized engine — same code, two config dicts (`regime_sso_config`, `regime_world_config`). Daily score checks run separately at 16:30 (SSO) and 16:35 (World) on weekdays to avoid concurrent FinBERT model loads.

## Backtest Results & Robustness

The full portfolio is backtested over **24 years** (2002-07 → 2026-05) using EODHD-sourced spliced daily returns, with synthetic reconstruction for assets pre-inception (UPRO/TMF/TQQQ/SPUU/QLD/EFO/SOXL/EDC, WLDU, KMLM/DBMF, RSSB, WTIP, USFR). Backtest engine: `/tmp/mega_backtest/mega_backtest.py`.

### Deterministic Backtest (single historical path)

| Strategy | Weight | CAGR | Vol | Sharpe | Max DD | Worst Yr | Best Yr | Total Ret |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Dual Momentum (best-of-3) | 20% | 17.21% | 23.31% | **0.65** | -33.93% | -26.48% | +65.04% | 4,240% |
| Regime SSO | 15% | 12.16% | 16.52% | 0.62 | **-23.59%** | -13.40% | +70.47% | 1,427% |
| HFEA | 15% | 18.91% | 27.57% | 0.61 | -67.24% | -41.74% | +71.90% | 6,020% |
| Regime World | 20% | 9.89% | 18.23% | 0.43 | -30.94% | **-10.49%** | +46.30% | 838% |
| 9-Sig | 5% | **22.43%** | 48.06% | 0.42 | -93.06% | -84.84% | **+164.68%** | **12,119%** |
| SPXL SMA | 15% | 14.96% | 30.57% | 0.42 | -54.86% | -41.32% | +118.37% | 2,642% |
| RSSB/WTIP | 10% | 8.26% | 15.18% | 0.41 | -46.02% | -25.19% | +30.50% | 558% |
| **AGGREGATE (production)** | **100%** | **15.78%** | **18.31%** | **0.75** | **-28.89%** | **-23.43%** | **+56.59%** | **3,146%** |
| 100% SPY (benchmark) | — | 11.52% | 18.87% | 0.50 | -55.19% | -36.79% | +32.31% | 1,232% |
| 100% URTH MSCI World (benchmark) | — | 7.20% | 13.20% | 0.39 | -34.01% | -17.97% | +28.15% | 421% |

Key observations:
- **Aggregate Sharpe (0.75) exceeds every individual sleeve** — the diversification benefit is real and large.
- **Aggregate volatility (18.31%) is *lower* than SPY (18.87%)** despite delivering 1.5× the return.
- vs SPY: +4.26 pp CAGR, +0.25 Sharpe, **26 pp tighter drawdown**.
- vs MSCI World: +8.58 pp CAGR, +0.36 Sharpe, 5 pp tighter drawdown.
- $1 → $32.46 (production) vs $13.32 (SPY) vs $5.21 (MSCI World) over 24 years.

### Monte Carlo Robustness (Stationary Block Bootstrap)

To test whether the deterministic result depends on the specific historical path, the daily strategy returns are jointly resampled via Politis-Romano stationary bootstrap (n = 2000 simulations, mean block = 63 trading days ≈ one quarter). Joint sampling preserves cross-strategy correlation (e.g., 2008-style crisis comovement).

**Aggregate distribution (n = 2000 simulated 24-year paths):**

| Metric | Deterministic | p5 (worst) | p50 (median) | p95 (best) |
|---|---:|---:|---:|---:|
| CAGR | +15.78% | +9.82% | +15.72% | +22.20% |
| Sharpe | 0.75 | 0.43 | 0.75 | 1.11 |
| Max DD | -28.89% | -45.26% | -31.90% | -24.30% |
| Worst Yr | -23.43% | -27.83% | -15.23% | -4.09% |

**Beat-benchmark probabilities (matched bootstrap paths):**

| Strategy | vs SPY (CAGR) | vs SPY (Sharpe) | vs SPY (MaxDD) | vs MSCI World (CAGR) | vs MSCI World (Sharpe) |
|---|---:|---:|---:|---:|---:|
| **AGGREGATE (production)** | **98.0%** | **96.1%** | **85.7%** | **100.0%** | **99.4%** |
| HFEA | 97.2% | 74.1% | 2.1% | 98.4% | 84.3% |
| Dual Momentum (best-of-3) | 95.0% | 75.5% | 57.0% | 99.7% | 92.7% |
| 9-Sig | 89.8% | 27.9% | 0.0% | 91.3% | 58.1% |
| SPXL SMA | 74.2% | 32.5% | 8.8% | 92.0% | 56.9% |
| Regime SSO | 57.0% | 66.7% | 81.2% | 95.5% | 85.9% |
| Regime World | 32.7% | 34.8% | 77.5% | 83.5% | 57.5% |
| RSSB/WTIP | 9.3% | 28.0% | 56.4% | 66.4% | 56.1% |

**What the Monte Carlo tells us:**
- The deterministic result (15.78% / 0.75 / -29%) sits **almost exactly at the p50** of every metric — the historical 24 years was *not* a lucky path. No survivorship bias.
- Aggregate beats SPY on CAGR in **98% of simulated paths** and on Sharpe in **96%**. Beats MSCI World on Sharpe **99% of the time**.
- Tail risks are bounded: **0% chance of finishing negative** over 24 years; **13.7% chance of breaching -40% drawdown**; **1.7% chance of breaching -50%**.
- Sleeves that look weak in isolation (RSSB/WTIP only beats SPY on CAGR 9% of the time) pay for themselves through diversification — they're macro hedges, not return chasers.
- The aggregate's deterministic MaxDD of -29% sits between p75 and p95 of the distribution — slightly *better than typical*. **Future drawdowns may run a bit worse than the historical experience.**

### Honest Caveats

- Regime World's 24-year backtest is ~99% synthetic. Live WLDU has only ~2 months of data.
- RSSB pre-2023-12 and WTIP pre-2025-06 are synthetic component reconstructions.
- Synthetic-WTIP correlation with live WTIP is ~0.57 — absolute returns may differ from a live-only backtest.
- Bootstrap can't simulate regimes that don't appear in the 2002-2026 sample (e.g., 1970s stagflation).
- Whole-share / fractional constraints aren't modeled.
- Margin behavior in the backtest is simplified vs. production's gated margin logic.

## Detailed Analysis of All Strategies

### **Risk and Volatility:**
- **HFEA Strategy**: The HFEA strategy's use of leveraged ETFs means that both gains and losses are magnified. The three-asset allocation (UPRO/TMF/KMLM at 45/25/30) provides better diversification than traditional two-asset HFEA portfolios. KMLM's managed futures component can provide uncorrelated returns during certain market conditions, potentially reducing overall portfolio volatility. However, this strategy still requires a strong risk tolerance and is generally suitable for investors with a long-term horizon who can withstand short-term losses.
  
- **S&P 500 with 200-SMA Strategy**: The 200-SMA strategy, while still involving a leveraged ETF (SPXL), mitigates risk by using a market-timing mechanism. By exiting the market during downtrends, the strategy avoids significant drawdowns, making it less volatile than the HFEA strategy. However, it still carries the risks associated with leveraged ETFs, including the potential for loss during sharp market reversals.

- **9-Sig Strategy**: The 9-Sig strategy balances growth and risk management through systematic rebalancing and crash protection. While it uses leveraged ETFs (TQQQ), the monthly contributions to bonds and the "30 Down, Stick Around" rule provide significant downside protection. The strategy's systematic approach removes emotional decision-making and provides built-in risk management during market crashes.

- **Dual Momentum (best-of-3)**: Uses 2× leveraged ETFs (SPUU/QLD/EFO) and rotates monthly to whichever underlying has the strongest blended 6m/12m momentum (skip-1m). Two layered risk controls — a 30% trailing-peak drawdown stop and a 25% annualized vol target — keep effective leverage ≤ 2× and prevent leveraged-ETF decay during bear markets. The 24-year backtested -34% max DD is roughly half what unhedged HFEA produced.

- **Regime SSO**: 7-signal composite (price trend, breadth, VIX, ADX, credit, news sentiment, canary universe) gates entry to SSO (2× S&P) vs USFR (cash). The composite scoring suppresses single-signal whipsaw — meaningful exits and re-entries fire ~1.4 times per year. Lowest max drawdown (-24%) of any leveraged sleeve.

- **Regime World**: Same 7-signal apparatus applied to global equities (URTH 255-SMA, ex-US country basket breadth, global ETF news filter). Holds WLDU (2× MSCI World) vs USFR. Lower CAGR than Regime SSO because URTH lagged SPY ~4 pp/year over the backtest window, but offers genuine geographic diversification. Best worst-year (-10%) of any leveraged sleeve.

### **Investment Horizon:**
- **HFEA Strategy**: Best suited for long-term investors who can afford to leave their investments untouched for several years, allowing the compounding effect to play out.
  
- **S&P 500 with 200-SMA Strategy**: This strategy can also be used for long-term growth, but with a focus on preserving capital during market downturns. It's more suitable for investors who are cautious about market cycles and prefer to reduce exposure during bear markets.

- **9-Sig Strategy**: Designed for long-term systematic growth with quarterly rebalancing. The strategy's systematic approach and crash protection make it suitable for investors who want exposure to leveraged growth but with built-in risk management. The monthly contributions to bonds provide a steady foundation while the quarterly rebalancing optimizes growth.

- **Dual Momentum (best-of-3)**: Ideal for long-term investors who want a tactical sleeve that adapts to which asset class is leading (US large-cap vs Nasdaq vs international developed). Monthly rebalancing strikes a balance between responsiveness and transaction costs.

- **Regime SSO / Regime World**: Both designed as slow, defensive-tilted sleeves. The 7-signal composite is intentionally noise-resistant — long flat or sideways markets won't trigger rotations. Investors who want signal-driven downside protection rather than buy-and-hold leverage.

### **Key Assumptions:**
- **HFEA Strategy**: Assumes that the diversification benefits of combining equities, bonds, and managed futures will persist, and that over time, the leveraged returns will outweigh the increased volatility. The strategy also assumes that KMLM's trend-following approach will provide crisis alpha and reduce drawdowns during major market dislocations.
  
- **S&P 500 with 200-SMA Strategy**: Assumes that the 200-SMA is a reliable indicator of market trends and that the market's behavior will continue to follow historical patterns where it tends to trend above or below the 200-SMA for extended periods.

- **9-Sig Strategy**: Assumes that the systematic rebalancing approach will capture market growth while the crash protection rule will prevent significant losses during major market downturns. The strategy assumes that the 9% quarterly growth target is achievable over long-term market cycles and that the monthly contributions to bonds provide sufficient stability for the leveraged growth component.

- **Dual Momentum (best-of-3)**: Assumes that momentum persists for 6-12 months due to behavioral biases, that adding QLD (Nasdaq) widens the rotation pool to capture tech-led regimes, and that the layered DD-stop + vol-target combination reduces leverage decay during sustained bears. The skip-1m construction guards against short-term reversal.

- **Regime SSO / Regime World**: Assume that composite multi-signal regime detection is more robust than any single indicator (200-SMA, VIX, etc.) and that combining slow (15-day score persistence) with fast (3-day extreme score) exit logic balances false-alarm avoidance with crash protection. The Fed hike filter assumes monetary policy environment is a meaningful regime modifier.

## Conclusion

All seven strategies offer unique ways to potentially enhance returns, but they come with their own sets of risks and assumptions. HFEA pursues maximum growth through balanced leverage. SPXL SMA captures market gains while avoiding sustained downturns via the 200-SMA. 9-Sig systematizes TQQQ/AGG growth with built-in crash protection. Dual Momentum rotates among three 2× sleeves with DD-stop + vol-target. The two regime detectors (SSO/World) use 7-signal composites to gate entry to leveraged risk assets — preserving capital during compositely-flagged risk-off regimes.

Together, these strategies provide a comprehensive blend of aggressive growth, trend-following, structural alpha, and signal-driven risk management:

- **HFEA (15%)**: Three-asset leveraged portfolio (UPRO 45%, TMF 25%, KMLM 30%)
- **SPXL SMA (15%)**: Trend-following with 200-day SMA gate on SPXL (3× S&P)
- **RSSB/WTIP (10%)**: Structural alpha — 70% RSSB / 30% WTIP — macro hedge across economic environments
- **9-Sig (5%)**: Systematic TQQQ/AGG growth with crash protection — tail-risk sleeve, kept small
- **Dual Momentum (20%)**: Best-of-3 rotation (SPUU/QLD/EFO/BND) + DD-stop + vol-target — best risk/return at ≤2× leverage
- **Regime SSO (15%)**: 7-signal US regime detector — SSO ↔ USFR
- **Regime World (20%)**: 7-signal global regime detector — WLDU ↔ USFR

Each strategy has been selected based on historical backtests, robustness testing (Monte Carlo stationary block bootstrap, 2000 simulated paths), and current market research. The diversification across seven different approaches — buy-and-hold leverage, US trend-following, structural alpha, systematic rebalancing, multi-asset momentum rotation, and dual regime detection — produces an aggregate Sharpe (0.75) that exceeds every individual sleeve. **The aggregate portfolio beats SPY on Sharpe in 96% of simulated paths and beats MSCI World on Sharpe in 99% of simulated paths** — the strongest portfolio-construction evidence the data supports.

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
  - **RSSB/WTIP strategy**: Two-asset portfolio (RSSB/WTIP at 70/30) with monthly underweight-based buys and quarterly rebalancing
  - **SPXL SMA strategy**: Trend-following with 200-day SMA (monthly buys and daily trading)
  - **9-Sig strategy**: Jason Kelly methodology with monthly AGG contributions and quarterly TQQQ/AGG signals with crash protection
  - **Dual Momentum strategy**: Tactical allocation between SPUU/EFO/BND using 12-month relative and absolute momentum
  - **Unified index alert system**: Monitors multiple indices for ATH drops and SMA crossings
  - **Firestore integration**: Persistent storage for strategy balances, 9-Sig quarterly data, Dual Momentum position tracking, and unified market data cache
  - **Alpaca integration**: All market data fetched from Alpaca IEX feed (no yfinance dependency)
- `requirements.txt`: Python dependencies including pandas, Google Cloud libraries, and Flask.
- `cloudbuild.yaml`: Google Cloud Build configuration for deploying Cloud Functions and Cloud Scheduler jobs.
- `README.md`: Comprehensive documentation of all strategies and setup instructions.

### **Cloud Functions Deployed:**
- `monthly_invest_all`: **Orchestrator function (RECOMMENDED)** - Runs all six monthly strategies with coordinated budget calculations
- `monthly_buy_hfea`: HFEA monthly investment function (individual execution)
- `rebalance_hfea`: HFEA quarterly rebalancing function
- `monthly_buy_rssb_wtip`: RSSB/WTIP monthly investment function (individual execution)
- `rebalance_rssb_wtip`: RSSB/WTIP quarterly rebalancing function
- `monthly_buy_spxl`: SPXL SMA monthly investment function (individual execution)
- `daily_trade_spxl_200sma`: SPXL SMA daily trading function
- `monthly_nine_sig_contributions`: 9-Sig monthly contributions function (individual execution)
- `quarterly_nine_sig_signal`: 9-Sig quarterly signal function
- `monthly_dual_momentum`: Dual Momentum strategy function (individual execution)
- `index_alert`: Unified index alert system

### **Cloud Scheduler Jobs:**
- **Monthly orchestrator**: First trading day of each month at 12:00 PM ET (`monthly_invest_all` - runs all six monthly strategies with coordinated budgets)
- **Quarterly functions**: First trading day of each quarter at specified times (`rebalance_hfea` at 2:00 PM ET, `rebalance_rssb_wtip` at 2:00 PM ET, `quarterly_nine_sig_signal` at 1:00 PM ET)
- **Index alerts**: Hourly during trading hours (9:15 AM - 3:15 PM for SMA alerts, 9:30 AM - 3:30 PM for ATH drop alerts)
- **Daily SMA functions**: 3:56 PM ET on weekdays (`daily_trade_spxl_200sma`)

**Note**: Individual monthly functions (`monthly_buy_hfea`, `monthly_buy_rssb_wtip`, `monthly_buy_spxl`, `monthly_nine_sig_contributions`, `monthly_dual_momentum`) are deployed but not scheduled. They remain available for manual execution and debugging purposes. The `monthly_invest_all` orchestrator is used for production to ensure coordinated budget allocation and prevent over-spending.

## Monthly Investment Orchestrator

The `monthly_invest_all` orchestrator is a coordinated execution system that manages all six monthly investment strategies (HFEA, RSSB/WTIP, SPXL SMA, 9-Sig, Dual Momentum, and Regime SSO) in a single unified process.

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
   - RSSB/WTIP: 10%
   - 9-Sig: 5%
   - Dual Momentum: 20%
   - Regime SSO: 15%
   - Regime World: 20%
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

The system includes intelligent margin control for all monthly investment functions (HFEA, SPXL SMA, and 9-Sig). This feature enables controlled use of leverage (up to +10%) only when market conditions are favorable and borrowing costs are reasonable.

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

- **Data Unavailable**: If FRED API, yfinance, or Alpaca fails → default to cash-only mode
- **API Errors**: All errors logged and reported via Telegram
- **Deleveraging Priority**: When gates fail while leveraged, skip all investments to reduce exposure

## Technical Configuration

### **Key Parameters:**

**Dynamic Monthly Investment:**
- Investment amounts are calculated dynamically each month based on available cash and margin conditions
- Total available = Account cash - Reserved amounts (for bearish strategies) + Approved margin (up to +10% of equity)
- Split across strategies: HFEA 42.5%, SPXL SMA 42.5%, 9-Sig 5%, Dual Momentum 10%
- All-or-Nothing approach: Invest full calculated amount or skip entirely

**HFEA Strategy:**
- Portfolio allocation: 47.5% of total monthly investment
- Asset allocation: UPRO 45%, TMF 25%, KMLM 30%
- Rebalancing: Quarterly with 0.5% fee margin
- Investment approach: Underweight-based proportional allocation

**SPXL SMA Strategy:**
- Portfolio allocation: 47.5% of total monthly investment
- SMA period: 200 days
- Margin band: 1% (to avoid whipsaws)
- Tracked index: S&P 500 (SPY ETF as proxy)

**9-Sig Strategy:**
- Portfolio allocation: 5% of total monthly investment
- Target allocation: TQQQ 80%, AGG 20%
- Quarterly growth target: 9%
- Monthly contributions: 100% to AGG (bonds)
- Signal tolerance: $25 (minimum trade amount)
- Crash protection: "30 Down, Stick Around" rule (ignores first 4 sell signals when SPY down >30% from ATH)
- Bond rebalancing threshold: 30% (triggers rebalancing when AGG exceeds this)

**Dual Momentum Strategy:**
- Portfolio allocation: 10% of total monthly investment
- Asset universe: SPUU (2x S&P 500), EFO (2x MSCI EAFE), BND (bonds)
- Momentum lookback: 12 months (252 trading days)
- Rebalancing frequency: Monthly (first trading day)
- Decision logic: Invest in relative momentum winner if positive, otherwise BND
- Position management: 100% allocation to single position, full switch on signal change

**Alert System:**
- ATH drop threshold: 30% for S&P 500 and MSCI World
- SMA noise threshold: 1% (minimum deviation to trigger alert)
- URTH SMA period: 255 days
- SPY SMA period: 200 days

### **Data Storage:**
- **Firestore Collections:**
  - `strategy-balances-live` / `strategy-balances-paper`: Tracks invested amounts and position details for each strategy (including Dual Momentum and RSSB/WTIP position tracking)
  - `nine-sig-quarters`: Historical quarterly data for 9-Sig signal calculations
  - `nine-sig-monthly-contributions`: Tracks actual monthly 9-Sig contributions for accurate quarterly signal calculation
  - `market-data`: Unified collection caching market prices, SMA values (200-day, 255-day), crossing states, and alert timestamps (5-minute cache expiry) - single source of truth for all market data

**Dual Momentum Tracking (in strategy-balances-live/dual_momentum):**
  - `total_invested`: Cumulative cash contributions to strategy
  - `current_position`: Current holding (SPUU, EFO, or BND)
  - `shares_held`: Number of shares in current position
  - `last_trade_date`: Date of last position change
  - `last_momentum_check`: Detailed momentum calculations (SPUU/EFO returns, winner, signal)

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
# RECOMMENDED - Monthly Orchestrator (runs all six monthly strategies with coordinated budgets)
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
```

**Why use the orchestrator (`monthly_invest_all`)?**
- Calculates budgets once and distributes them to all strategies
- Ensures exact percentage splits (15% HFEA, 15% SPXL SMA, 10% RSSB/WTIP, 5% 9-Sig, 20% Dual Momentum, 15% Regime SSO, 20% Regime World)
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
TELEGRAM_CHAT_ID=your_telegram_chat_id
FREDKEY=your_fred_api_key
GOOGLE_CLOUD_PROJECT_ID=your_project_id
```

**Note**: Get a free FRED API key from https://fred.stlouisfed.org/docs/api/api_key.html

### Deployment to Google Cloud

The project uses Google Cloud Build for automated deployment:

```bash
# Authenticate with Google Cloud
gcloud auth login

# Set your project
gcloud config set project YOUR_PROJECT_ID

# Deploy all functions and schedulers
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

The `cloudbuild.yaml` file defines all Cloud Functions and their corresponding Cloud Scheduler jobs. Deployment is parallelized for faster updates.

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

## Contributing

This is a personal trading bot implementation. Feel free to fork and adapt for your own use, but please note:
- This is not financial advice
- Leveraged ETFs carry significant risk
- Past performance does not guarantee future results
- Always test thoroughly in paper trading before using live funds

## License

This project is for educational and personal use only.
