# Personal Investment Policy & Strategy Playbook

**Owner:** Carl Johannes  
**Version:** 1.0  
**Stand:** 12. September 2026

---

# Part I — Investment Policy

## 1. Purpose

This document defines the strategic rules of the liquid investment portfolio across Alpaca, Scalable Capital and Trade Republic. It documents the intended structure and decision logic rather than a point-in-time holdings snapshot.

The portfolio combines global equities, embedded ETF leverage, trend and momentum signals, government bonds, gold, commodities, managed futures and tactical defensive allocations.

A core principle is to distinguish clearly between systematic rules, discretionary decisions and financing/leverage rules.

## 2. Portfolio architecture

| Platform | Role | Core approach |
|---|---|---|
| **Alpaca** | Systematic multi-strategy portfolio | Six automated strategies with different signals and risk drivers |
| **Scalable Capital** | Strategic core with additional leverage | 50% Euro-HFEA + 50% 2x ACWI SMA |
| **Trade Republic** | Long-term 2x World trend strategy | Target: 100% 2x MSCI World with 255-day SMA |
| **DBX0AN** | Defensive EUR position | Risk-off instrument for Scalable and Trade Republic |

The portfolio should be understood primarily through the strategies, not merely through the list of ETFs held at a broker.

## 3. Source of truth

### Alpaca

Alpaca is code-driven. The authoritative source for active strategies is the production code on the `main` branch of:

`cluttmann/multi_strategy_portfolio`

Historical README sections, research files, backtests, design documents or legacy code do **not** by themselves define an active strategy.

A strategy is considered active only if it is part of the current production allocation / execution flow.

As of this version there are exactly **six active Alpaca strategies**. `World 40/30/30 / F4` was retired on 2026-09-09 and removed from the live allocation and active ticker ownership registry.

If this document and current Alpaca production code ever conflict, **production code takes precedence for Alpaca**.

### Scalable Capital and Trade Republic

For Scalable and Trade Republic, this document is the strategy source of truth. Parqet and broker holdings are used to verify implementation.

## 4. Systematic vs discretionary decisions

### Systematic

The following are intended to be followed mechanically:

- Alpaca strategy rules and signals
- Alpaca margin gates
- Scalable ACWI 250-day SMA strategy
- Trade Republic World 255-day SMA strategy
- Risk-off rotations defined within those strategies

### Discretionary

The following remain discretionary unless explicitly changed in this policy:

- Timing of the migration of legacy Trade Republic holdings
- Additional contributions
- Future changes to the strategy architecture
- New external financing decisions

The planned Trade Republic migration around a meaningful market correction (roughly -10% or more) is **not** a mechanical trading signal.

## 5. Leverage policy

The portfolio uses three distinct forms of leverage that must be considered separately.

### 5.1 Embedded ETF leverage

Several strategies use leveraged ETFs, including 2x and 3x equity, bond and gold products. This leverage is embedded inside the fund and is distinct from broker margin.

### 5.2 Alpaca margin

Alpaca may use broker margin up to approximately **+10% additional account exposure**.

Margin is enabled only when all four gates pass:

1. US equity market / SPX is above its 200-day SMA.
2. Estimated margin rate is <= 8%.
3. Maintenance-margin buffer is >= 5%.
4. Portfolio value / equity remains below 1.14x.

Margin is an account-level overlay, not a seventh strategy.

### 5.3 Scalable securities-backed credit

Scalable may use a securities-backed credit line subject to:

`Scalable credit / gross Scalable securities value <= 20%`

The denominator is the gross market value of securities **before** subtracting the credit balance.

The current interest rate at the time of this version is approximately **3.5% p.a.**. The interest rate is variable; the 20% maximum credit ratio is the policy rule.

### 5.4 External consumer loan

A separate external loan has already been invested into Scalable:

- Original principal: **EUR 30,000**
- Term: **84 months**
- Interest rate: **5.29% p.a.**
- Use of proceeds: **fully invested into the Scalable portfolio**

This loan is economically assigned to the whole Scalable portfolio rather than to one individual Scalable sleeve.

It must be distinguished from the variable Scalable securities-backed credit.

## 6. Defensive EUR position

The defensive asset for the European SMA strategies is:

**Xtrackers II EUR Overnight Rate Swap UCITS ETF 1C**  
WKN: **DBX0AN**  
ISIN: **LU0290358497**

It is used as the Risk-off / cash-like EUR holding for Scalable and Trade Republic.

## 7. SMA governance for Scalable and Trade Republic

SMA signals are based on the **unleveraged underlying equity market**, not on the leveraged ETF itself.

- **MSCI ACWI (EUR): 250-day SMA**
- **MSCI World (EUR): 255-day SMA**

The GitHub monitoring code currently uses the EUR-market reference data and a 1% noise band around the SMA crossings.

---

# Part II — Strategy Playbook

# A. Alpaca Multi-Strategy Portfolio

## A0. Target allocation

| Strategy | Target weight |
|---|---:|
| HFEA | **18.29%** |
| SPXL 200-SMA | **18.29%** |
| 9-Sig | **6.10%** |
| Dual Momentum | **24.39%** |
| Regime SSO | **14.64%** |
| 7-Asset Rotator | **18.29%** |
| **Total** | **100.00%** |

Monthly investments are coordinated centrally and can tilt new capital toward underweight sleeves.

## A1. HFEA

**Alpaca target weight:** 18.29%

### Internal allocation

| Instrument | Weight | Role |
|---|---:|---|
| UPRO | **45%** | 3x daily S&P 500 exposure |
| TMF | **25%** | 3x long-duration US Treasury exposure |
| KMLM | **30%** | Managed-futures / trend diversification |

### Rebalancing

- New contributions are used to reduce underweights where possible.
- Additional **quarterly rebalancing** keeps the sleeve close to 45/25/30.

### Role

Multi-asset leveraged diversification across equities, duration and managed futures.

## A2. SPXL 200-SMA

**Alpaca target weight:** 18.29%

### Risk-on

**SPXL** — Direxion Daily S&P 500 Bull 3X Shares.

### Signal

US equity market relative to its **200-day SMA**, with an approximately **1% band** around the SMA to reduce whipsaw.

### Risk-off

**SGOV** — iShares 0-3 Month Treasury Bond ETF.

### Role

Leveraged US equity exposure with a simple long-term trend filter intended to reduce participation in prolonged bear markets.

## A3. 9-Sig

**Alpaca target weight:** 6.10%

### Instruments

- **TQQQ** — 3x Nasdaq-100
- **AGG** — broad US aggregate bond market

### Core rules

- 60% TQQQ / 40% AGG anchor / base-reset allocation
- 9% quarterly growth target
- 30-Down rule based on TQQQ relative to its rolling 8-quarter high
- Monthly contributions
- Quarterly signal evaluation

### Role

Offensive growth exposure combined with a rules-based growth path and bond counterweight.

## A4. Dual Momentum — 2x Best-of-3

**Alpaca target weight:** 24.39%

### Universe

| Signal market | Held instrument | Exposure |
|---|---|---|
| SPY / S&P 500 | **SPUU** | 2x S&P 500 |
| QQQ / Nasdaq-100 | **QLD** | 2x Nasdaq-100 |
| EFA / Developed ex-US | **EFO** | 2x developed ex-US equities |

### Defensive position

**BND** — Vanguard Total Bond Market ETF.

### Signal and risk controls

- Momentum uses 6-month and 12-month lookbacks.
- Winner must have sufficiently positive momentum; current minimum score is approximately +1%.
- **25% annualized volatility target** using a 60-trading-day realized-volatility window.
- **30% trailing-peak drawdown stop** forces the strategy defensive.

### Role

Relative and absolute momentum across major equity regions with 2x implementation, volatility targeting and drawdown protection.

## A5. Regime SSO

**Alpaca target weight:** 14.64%

### Risk-on

**SSO** — ProShares Ultra S&P 500, approximately 2x daily S&P 500.

### Risk-off

**USFR** — WisdomTree Floating Rate Treasury Fund.

### Signal

A composite regime detector built from **seven signals**. Each contributes -1, 0 or +1 to the composite score.

The system is intentionally slow and noise-resistant, with relatively infrequent regime changes.

Market breadth is one component; the implementation includes a 50-day SMA breadth measure with approximately >60% bullish and <40% bearish thresholds.

### Role

A slower multi-signal US-equity regime filter that complements the simpler SPXL 200-SMA strategy.

## A6. 7-Asset Rotator — AAA family

**Alpaca target weight:** 18.29%

### Universe

| Signal | Held instrument | Economic exposure |
|---|---|---|
| SPY | **NTSD** | Capital-efficient US + international equity stack |
| IWM | **SAA** | 2x US small caps |
| EEM | **EET** | 2x emerging markets |
| TLT | **UBT** | 2x long US Treasuries |
| IEF | **UST** | 2x intermediate US Treasuries |
| GLD | **UGL** | 2x gold |
| DBC | **DBC** | Broad commodities |

### Defensive position

**SHV** — iShares Short Treasury Bond ETF.

### Selection and weighting

- Monthly selection of the **top 3 assets by 6-month momentum** on the signal symbols.
- Top-3 positions are weighted by **inverse volatility**.
- Realized volatility window: approximately 60 trading days.
- **25% volatility target**.
- **30% drawdown stop**.

### Role

The broadest tactical asset-class diversifier in Alpaca, able to rotate among equities, Treasuries, gold and commodities.

---

# B. Scalable Capital

Scalable consists of two strategic sleeves:

`50% Euro-HFEA + 50% 2x ACWI SMA`

The Scalable securities-backed credit is managed at portfolio level and is not assigned to one sleeve.

## B1. Euro-HFEA

**Scalable target weight:** 50%

### Internal allocation

| Asset | Target weight within Euro-HFEA |
|---|---:|
| 2x MSCI World | **50%** |
| Gold | **25%** |
| Long-duration US government bonds | **12.5%** |
| Long-duration EUR government bonds | **12.5%** |

### Current implementation instruments

- **Amundi MSCI World (2x) Leveraged UCITS ETF Acc** — ISIN `FR0014010HV4`
- **EUWAX Gold II** — ISIN `DE000EWG2LD7`
- **iShares $ Treasury Bond 20+yr UCITS ETF** — ISIN `IE00BFM6TC58`
- **Amundi Euro Government Bond 25+Y UCITS ETF** — ISIN `LU1686832194`

### Role

Multi-asset strategic sleeve combining leveraged developed-market equities, gold and long-duration government bonds.

No separate fixed calendar rebalancing frequency is currently defined in this policy; the target weights themselves are binding.

## B2. 2x MSCI ACWI — 250-day SMA

**Scalable target weight:** 50%

### Risk-on

**Scalable MSCI AC World Leveraged Daily Swap UCITS ETF / 2x MSCI ACWI**  
ISIN: `LU3386643970`

### Signal

The **unleveraged MSCI ACWI EUR reference** is compared with its **250-day SMA**.

### Risk-off

**DBX0AN**

### State logic

- ACWI in Risk-on regime -> 2x MSCI ACWI
- ACWI in Risk-off regime -> DBX0AN

### Role

Broad global leveraged equity exposure with a slow trend filter designed to avoid holding 2x equity exposure through prolonged structural bear markets.

---

# C. Trade Republic

## C1. Target strategy: 2x MSCI World — 255-day SMA

Long-term target: **100% of Trade Republic strategy capital in this one strategy**.

### Risk-on

**Amundi MSCI World (2x) Leveraged UCITS ETF Acc**  
ISIN: `FR0014010HV4`

### Signal

Unleveraged MSCI World EUR reference relative to its **255-day SMA**.

### Risk-off

**DBX0AN**

### State logic

- World in Risk-on regime -> 2x MSCI World
- World in Risk-off regime -> DBX0AN

## C2. Legacy holdings

Trade Republic still contains legacy positions from previous portfolio approaches, including:

- 1x MSCI World
- Emerging Markets
- World Value
- World Quality
- World Momentum
- World Small Caps

These are **not separate long-term strategies anymore**. They are legacy holdings scheduled for migration.

## C3. Migration rule

The legacy holdings are intended to be migrated when a sufficiently attractive market correction occurs, roughly around -10% or more.

This is explicitly **discretionary** and not a mechanical signal.

At migration, the capital enters the current state of the 255-day-SMA strategy:

- If the World strategy is Risk-on -> capital moves into 2x MSCI World.
- If the World strategy is Risk-off -> capital moves into DBX0AN first.

A correction therefore does not automatically imply immediate 2x equity exposure.

---

# 8. Strategic risk-factor overview

| Strategy | Equities | Bonds | Gold | Commodities | Managed futures | Trend / momentum | Defensive asset |
|---|:---:|:---:|:---:|:---:|:---:|:---:|---|
| Alpaca HFEA | High | High | – | Indirect | High | Some | Rebalancing / diversification |
| Alpaca SPXL SMA | Very high | Short-duration | – | – | – | High | SGOV |
| Alpaca 9-Sig | Very high | Moderate | – | – | – | Rules-based | AGG |
| Alpaca Dual Momentum | High | Moderate | – | – | – | Very high | BND |
| Alpaca Regime SSO | High | Short-duration | – | – | – | High | USFR |
| Alpaca 7-Asset Rotator | High | High | High | Moderate | – | Very high | SHV |
| Scalable Euro-HFEA | High | Moderate | High | – | – | – | Multi-asset diversification |
| Scalable 2x ACWI | Very high | – | – | – | – | High | DBX0AN |
| Trade Republic 2x World | Very high | – | – | – | – | High | DBX0AN |

# 9. Portfolio philosophy in one sentence

The portfolio intentionally accepts high equity and leverage exposure, but attempts to manage that risk through multiple independent mechanisms: asset-class diversification, trend, momentum, regime filters, volatility targeting, drawdown stops, defensive assets and constrained broker margin.

# 10. Change management

Strategies may change over time, especially on Alpaca.

This document should be updated when any of the following materially changes:

1. A strategy is added or retired.
2. A target strategy weight changes.
3. A Risk-on asset changes.
4. A Risk-off asset changes.
5. A signal or lookback changes.
6. A leverage rule changes.
7. A margin rule changes.

Currently retired / not part of the live Alpaca portfolio include:

- World 40/30/30 / F4
- Regime World
- Earlier RSSB / WTIP compositions

# 11. Reference hierarchy for future analysis

## Alpaca

1. Production code
2. Strategy state / ledgers
3. Parqet for aggregate broker holdings and performance

## Scalable Capital

1. This Investment Policy
2. Broker / Parqet holdings for implementation checks

## Trade Republic

1. This Investment Policy
2. Broker / Parqet holdings for implementation checks

**Parqet shows what is held. The policy — and for Alpaca the production code — explains why it is held and which strategy it belongs to.**
