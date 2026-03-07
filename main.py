"""
EDGAR + GMR Stock Analysis Tool
=================================
Command-line entry point.

Modes
-----
full          Run EDGAR fundamental analysis + GMR technical analysis +
              composite signal + historical backtest.
fundamental   EDGAR 10-K fundamentals only (balance sheet, income statement,
              cash-flow → P/E, P/B, D/E, ROE, margins, growth, dividends).
technical     GMR Short-Term indicator only (win probability, VUp/VDown,
              43-day moving average trend).
backtest      Walk-forward backtest of the GMR strategy over N years of price
              history, with a buy-and-hold benchmark for comparison.

Usage examples
--------------
  python main.py --ticker AAPL
  python main.py --ticker MSFT --mode fundamental --edgar-years 10
  python main.py --ticker TSLA --mode technical
  python main.py --ticker AMZN --mode backtest --years 5 --capital 20000
  python main.py --ticker NVDA --mode full --years 3
  python main.py --ticker CUK  --mode full --edgar-identity you@example.com
"""

from __future__ import annotations

import argparse
import logging
import sys

# ── silence noisy third-party loggers ──────────────────────────────────────
logging.basicConfig(level=logging.WARNING)
for noisy in ("edgar", "httpx", "httpcore", "urllib3", "yfinance"):
    logging.getLogger(noisy).setLevel(logging.ERROR)

from src.data.edgar_fetcher import EdgarFetcher
from src.data.price_fetcher import PriceFetcher
from src.analysis.fundamental import FundamentalAnalyzer
from src.analysis.technical import TechnicalAnalyzer
from src.analysis.signals import SignalGenerator
from src.backtesting.engine import TechnicalBacktester
from src.utils.display import (
    console,
    print_fundamental_score,
    print_technical_score,
    print_composite_signal,
    print_backtest_results,
)


# ---------------------------------------------------------------------------
# Individual mode runners
# ---------------------------------------------------------------------------

def run_fundamental(
    ticker: str,
    edgar_identity: str,
    edgar_years: int = 10,
):
    """Fetch EDGAR 10-K data and run fundamental analysis."""
    console.rule(f"[bold cyan]Fundamental Analysis — {ticker}[/bold cyan]")

    edgar   = EdgarFetcher(identity=edgar_identity)
    prices  = PriceFetcher()

    # ── Fetch fundamentals ──────────────────────────────────────────
    console.print(f"[dim]Fetching {edgar_years} years of 10-K filings from EDGAR…[/dim]")
    try:
        fundamentals = edgar.fetch_fundamentals(ticker, years=edgar_years)
    except Exception as exc:
        console.print(f"[bold red]EDGAR error:[/bold red] {exc}")
        return None

    # ── Fetch current price & market data ──────────────────────────
    current_price = 0.0
    shares        = None
    div_yield     = None
    try:
        current_price = prices.get_current_price(ticker)
        shares        = prices.get_shares_outstanding(ticker)
        div_yield     = prices.get_dividend_yield(ticker)
    except Exception as exc:
        console.print(f"[yellow]Warning – could not fetch live price data:[/yellow] {exc}")

    # ── Analyse ─────────────────────────────────────────────────────
    analyzer = FundamentalAnalyzer()
    score    = analyzer.analyze(
        fundamentals=fundamentals,
        current_price=current_price,
        shares_outstanding=shares,
        dividend_yield=div_yield,
    )

    print_fundamental_score(score)
    return score


def run_technical(ticker: str, history_period: str = "1y"):
    """Fetch price history and run the GMR Short-Term indicator."""
    console.rule(f"[bold cyan]GMR Technical Analysis — {ticker}[/bold cyan]")

    prices = PriceFetcher()
    console.print(f"[dim]Fetching {history_period} of daily price history…[/dim]")
    try:
        history = prices.get_history(ticker, period=history_period)
    except Exception as exc:
        console.print(f"[bold red]Price fetch error:[/bold red] {exc}")
        return None

    analyzer = TechnicalAnalyzer()
    try:
        score = analyzer.analyze(price_history=history, ticker=ticker)
    except Exception as exc:
        console.print(f"[bold red]GMR analysis error:[/bold red] {exc}")
        return None

    print_technical_score(score)
    return score


def run_backtest(
    ticker:  str,
    years:   int   = 3,
    capital: float = 10_000.0,
):
    """Run a walk-forward GMR backtest and compare against buy-and-hold."""
    console.rule(f"[bold cyan]Backtest — {ticker}  ({years} years)[/bold cyan]")

    prices = PriceFetcher()
    # Fetch an extra year as warm-up for the GMR 6-month window
    period = f"{years + 1}y"
    console.print(f"[dim]Fetching {period} of daily price history…[/dim]")
    try:
        history = prices.get_history(ticker, period=period)
    except Exception as exc:
        console.print(f"[bold red]Price fetch error:[/bold red] {exc}")
        return None

    backtester = TechnicalBacktester(initial_capital=capital)
    results    = backtester.run(ticker=ticker, price_history=history)

    print_backtest_results(results)
    return results


def run_full(
    ticker:         str,
    edgar_identity: str,
    edgar_years:    int   = 10,
    backtest_years: int   = 3,
    capital:        float = 10_000.0,
    fw:             float = 0.60,
    tw:             float = 0.40,
):
    """
    Full pipeline:
      1. EDGAR fundamental analysis
      2. GMR technical analysis
      3. Composite signal
      4. Historical backtest
    """
    console.rule(f"[bold blue] Full Analysis — {ticker} [/bold blue]")

    # 1. Fundamentals
    fund_score = run_fundamental(ticker, edgar_identity, edgar_years)

    console.print()

    # 2. Technical
    tech_score = run_technical(ticker)

    # 3. Composite signal
    if fund_score is not None or tech_score is not None:
        console.print()
        gen    = SignalGenerator(fundamental_weight=fw, technical_weight=tw)
        signal = gen.generate(fundamental=fund_score, technical=tech_score)
        print_composite_signal(signal)

    # 4. Backtest
    console.print()
    run_backtest(ticker, years=backtest_years, capital=capital)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="main.py",
        description="EDGAR + GMR Stock Analysis Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    p.add_argument(
        "--ticker", "-t",
        required=True,
        metavar="SYMBOL",
        help="Stock ticker symbol, e.g. AAPL, MSFT, CUK",
    )
    p.add_argument(
        "--mode", "-m",
        choices=["full", "fundamental", "technical", "backtest"],
        default="full",
        help="Analysis mode (default: full)",
    )
    p.add_argument(
        "--years", "-y",
        type=int,
        default=3,
        metavar="N",
        help="Years of history for the backtest (default: 3)",
    )
    p.add_argument(
        "--capital", "-c",
        type=float,
        default=10_000.0,
        metavar="AMOUNT",
        help="Starting capital for the backtest in USD (default: 10000)",
    )
    p.add_argument(
        "--edgar-years",
        type=int,
        default=10,
        metavar="N",
        help="Number of 10-K annual reports to fetch (default: 10)",
    )
    p.add_argument(
        "--edgar-identity",
        default="bemar-edgar@research.com",
        metavar="EMAIL",
        help="E-mail address sent to the SEC EDGAR API as identity "
             "(use your own; default: bemar-edgar@research.com)",
    )
    p.add_argument(
        "--fw",
        type=float,
        default=0.60,
        metavar="WEIGHT",
        help="Fundamental weight in composite signal 0-1 (default: 0.60)",
    )
    p.add_argument(
        "--tw",
        type=float,
        default=0.40,
        metavar="WEIGHT",
        help="Technical weight in composite signal 0-1 (default: 0.40)",
    )
    p.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable DEBUG logging",
    )
    return p


def main() -> None:
    parser = _build_parser()
    args   = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Validate weights
    if abs(args.fw + args.tw - 1.0) > 1e-4:
        parser.error("--fw and --tw must sum to 1.0")

    ticker = args.ticker.upper()

    if args.mode == "fundamental":
        result = run_fundamental(
            ticker,
            edgar_identity=args.edgar_identity,
            edgar_years=args.edgar_years,
        )

    elif args.mode == "technical":
        result = run_technical(ticker)

    elif args.mode == "backtest":
        result = run_backtest(
            ticker,
            years=args.years,
            capital=args.capital,
        )

    else:  # full
        run_full(
            ticker,
            edgar_identity=args.edgar_identity,
            edgar_years=args.edgar_years,
            backtest_years=args.years,
            capital=args.capital,
            fw=args.fw,
            tw=args.tw,
        )


if __name__ == "__main__":
    main()
