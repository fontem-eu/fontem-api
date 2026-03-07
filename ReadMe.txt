
# EDGAR + GMR Stock Analysis Tool
=================================

A Python tool that combines SEC EDGAR fundamental data with the GMR
short-term technical indicator to produce BUY / HOLD / SELL signals and
validate them against historical price data via backtesting.


## Project layout

    edgar-gmr-etl/
    ├── main.py                      ← CLI entry point
    ├── Requirements.txt
    ├── Dockerfile / docker-compose.yml
    └── src/
        ├── data/
        │   ├── edgar_fetcher.py     ← SEC EDGAR 10-K → balance sheet / income / cashflow
        │   └── price_fetcher.py     ← yfinance → OHLCV history + market stats
        ├── analysis/
        │   ├── fundamental.py       ← P/E, P/B, D/E, ROE, margins, growth, dividends
        │   ├── technical.py         ← GMR Short-Term (win prob, VUp/VDown, MAT)
        │   └── signals.py           ← weighted composite signal generator
        ├── backtesting/
        │   └── engine.py            ← walk-forward GMR backtest + B&H benchmark
        └── utils/
            └── display.py           ← rich terminal tables, panels, equity chart


## Quickstart (Docker — recommended)

    cd edgar-gmr-etl
    docker compose up -d --build
    docker compose exec edgar-gmr-etl bash

Inside the container:

    # Full analysis: EDGAR fundamentals + GMR technical + backtest
    python main.py --ticker AAPL

    # Fundamental analysis only (10 years of 10-K data)
    python main.py --ticker MSFT --mode fundamental

    # GMR technical indicator only (current signal)
    python main.py --ticker TSLA --mode technical

    # 5-year historical backtest, starting with $20 000
    python main.py --ticker AMZN --mode backtest --years 5 --capital 20000

    # Use your own EDGAR identity (SEC requirement)
    python main.py --ticker CUK --edgar-identity you@example.com


## Quickstart (local)

    pip install -r Requirements.txt
    python main.py --ticker AAPL


## Analysis modes

| Mode          | Data sources              | Output                                      |
|---------------|---------------------------|---------------------------------------------|
| fundamental   | EDGAR 10-K (edgartools)   | P/E, P/B, D/E, ROE, NPM, CR, CAGR, DY      |
| technical     | yfinance daily OHLCV      | Win prob, VUp, VDown, MAT, GMR signal       |
| backtest      | yfinance daily OHLCV      | Returns, alpha, Sharpe, drawdown, win rate  |
| full          | EDGAR + yfinance          | All of the above + composite signal         |


## The GMR indicator (short-term)

Ported from the original C# GMRShort class.  Uses 6 months of daily data:

  Win Probability  — percentile rank of the first positive return day.
                     > 50 % → statistically more up-days than down-days.

  VUp / VDown      — monthly maximum upside / downside price potential.
                     Identifies stocks with meaningful volatility in both
                     directions (required for the strategy to make sense).

  MAT              — 43-trading-day (≈ 2-month) simple moving average.
                     Price vs MAT tells you whether momentum is upward.

A stock must pass all four checks (win prob, VUp, VDown, MAT + volume)
for a STRONG_BUY signal.


## Fundamental thresholds (configurable)

These defaults match the original GMRLong / GMRTool appsettings.json:

  P/E  ≤ 20    P/B  ≤ 1.5    D/E  ≤ 1.5
  ROE  ≥ 15 %  NPM  ≥ 10 %   Current ratio ≥ 1.0
  Revenue CAGR > 0 %          Dividend yield ≥ 2 %


## Signal weighting

By default the composite signal weights fundamental analysis 60 % and
technical analysis 40 %.  Override with --fw / --tw flags:

    python main.py --ticker AAPL --fw 0.5 --tw 0.5   # equal weight
    python main.py --ticker TSLA --fw 0.0 --tw 1.0   # technical only


## Backtesting methodology

  • Walk-forward, no look-ahead bias: each monthly decision uses only
    data available up to that date.
  • Positions: binary (fully invested OR fully in cash).
  • Benchmark: buy-and-hold over the same period.
  • Metrics: total return, annualised return, alpha vs benchmark,
             max drawdown, Sharpe ratio (4 % risk-free), win rate.


## Roadmap / ideas

  • Fundamental backtesting: annual rebalancing triggered by 10-K release.
  • DCF / intrinsic value calculator (see akashaero/Intrinsic-Value-Calculator).
  • Multi-stock screener: run across a watchlist file in parallel.
  • Chart output: matplotlib equity curve saved to PNG.
  • Sector / industry filter using FinanceDatabase or EDGAR SIC codes.
  • Options overlay: use VUp/VDown to suggest covered calls or protective puts.
