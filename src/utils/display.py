"""
Rich Terminal Display
======================
Pretty-prints analysis results to the terminal using the ``rich`` library.
All public functions accept the typed result objects from the analysis and
backtesting modules and render them as formatted tables and panels.
"""

from __future__ import annotations

from typing import Optional

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from ..analysis.fundamental import FundamentalScore
from ..analysis.technical import TechnicalScore
from ..analysis.signals import CompositeSignal
from ..backtesting.engine import BacktestResults

console = Console()

# Map signal strings to Rich colour names
_SIGNAL_COLOUR = {
    "STRONG_BUY":  "bold green",
    "BUY":         "green",
    "HOLD":        "yellow",
    "SELL":        "red",
    "STRONG_SELL": "bold red",
    "NEUTRAL":     "dim",
}


def _check(passed: Optional[bool]) -> str:
    if passed is None:
        return "—"
    return "[green]✅[/green]" if passed else "[red]❌[/red]"


def _score_bar(score: float, width: int = 36) -> str:
    """Unicode progress-bar representation of a 0–100 score."""
    filled = int(round(score / 100 * width))
    bar    = "█" * filled + "░" * (width - filled)
    if score >= 60:
        colour = "green"
    elif score >= 40:
        colour = "yellow"
    else:
        colour = "red"
    return f"[{colour}]{bar}[/{colour}]  [{colour}]{score:.0f}/100[/{colour}]"


# ---------------------------------------------------------------------------
# Fundamental analysis display
# ---------------------------------------------------------------------------

def print_fundamental_score(score: FundamentalScore) -> None:
    """Render a fundamental analysis result table."""
    tbl = Table(
        title=f"📊  Fundamental Analysis — {score.ticker}",
        box=box.ROUNDED,
        show_lines=False,
    )
    tbl.add_column("Metric",         style="cyan",  min_width=22)
    tbl.add_column("Value",          justify="right", min_width=14)
    tbl.add_column("Threshold",      justify="right", style="dim", min_width=10)
    tbl.add_column("Pass?",          justify="center", min_width=6)

    rows = [
        ("Current price",       f"${score.current_price:.2f}",                   "—",     None),
        ("P/E ratio",           f"{score.pe_ratio:.1f}"           if score.pe_ratio           else "N/A", "≤ 20",  score.checks.get("pe")),
        ("P/B ratio",           f"{score.pb_ratio:.2f}"           if score.pb_ratio           else "N/A", "≤ 1.5", score.checks.get("pb")),
        ("Debt / Equity",       f"{score.debt_equity:.2f}"        if score.debt_equity        else "N/A", "≤ 1.5", score.checks.get("de")),
        ("ROE",                 f"{score.roe * 100:.1f} %"        if score.roe                else "N/A", "≥ 15%", score.checks.get("roe")),
        ("Net profit margin",   f"{score.net_profit_margin*100:.1f} %" if score.net_profit_margin else "N/A", "≥ 10%", score.checks.get("npm")),
        ("Revenue CAGR (5y)",   f"{score.revenue_cagr_5y*100:.1f} %"  if score.revenue_cagr_5y   else "N/A", "> 0%",  score.checks.get("revenue_cagr")),
        ("Net income CAGR (5y)",f"{score.net_income_cagr_5y*100:.1f} %" if score.net_income_cagr_5y else "N/A", "—",    None),
        ("Current ratio",       f"{score.current_ratio:.2f}"      if score.current_ratio      else "N/A", "≥ 1.0", score.checks.get("current_ratio")),
        ("Dividend yield",      f"{score.dividend_yield * 100:.2f} %",                        "≥ 2%",  score.checks.get("div_yield")),
        ("Consec. profit yrs",  str(score.consecutive_profit_yrs),                            "≥ 3",   None),
    ]

    for metric, value, threshold, passed in rows:
        tbl.add_row(metric, value, threshold, _check(passed))

    console.print(tbl)
    console.print(f"\n[bold]Fundamental score:[/bold]  {_score_bar(score.score)}")
    colour = _SIGNAL_COLOUR.get(score.signal_strength, "white")
    console.print(f"[bold]Signal:[/bold]  [{colour}]{score.signal_strength}[/{colour}]\n")


# ---------------------------------------------------------------------------
# Technical (GMR) analysis display
# ---------------------------------------------------------------------------

def print_technical_score(score: TechnicalScore) -> None:
    """Render a GMR technical analysis result table."""
    tbl = Table(
        title=f"📈  GMR Technical Analysis — {score.ticker}  "
              f"(as of {score.analysis_date.strftime('%Y-%m-%d')})",
        box=box.ROUNDED,
        show_lines=False,
    )
    tbl.add_column("Metric",     style="cyan",  min_width=22)
    tbl.add_column("Value",      justify="right", min_width=14)
    tbl.add_column("Threshold",  justify="right", style="dim", min_width=10)
    tbl.add_column("Pass?",      justify="center", min_width=6)

    rows = [
        ("Current price",      f"${score.current_price:.2f}",                               "—",        None),
        ("Win probability",    f"{score.win_probability * 100:.1f} %" if score.win_probability is not None else "N/A", "> 50%",   score.checks.get("win_prob")),
        ("Avg monthly VUp",    f"{score.avg_vup * 100:.1f} %"        if score.avg_vup        is not None else "N/A", "> 30%",   score.checks.get("avg_vup")),
        ("Avg monthly VDown",  f"{score.avg_vdown * 100:.1f} %"      if score.avg_vdown      is not None else "N/A", "< −30%",  score.checks.get("avg_vdown")),
        ("43-day MA (MAT)",    f"${score.mat:.2f}"                    if score.mat            is not None else "N/A", "—",        None),
        ("Price vs MAT",       f"{score.mat_diff_pct * 100:+.2f} %"  if score.mat_diff_pct   is not None else "N/A", "> −2.5%", score.checks.get("mat_diff")),
        ("Daily volume",       f"{score.current_volume:,.0f}",                               "> 1 M",    score.checks.get("volume")),
    ]

    for metric, value, threshold, passed in rows:
        tbl.add_row(metric, value, threshold, _check(passed))

    console.print(tbl)

    # Monthly VUp / VDown breakdown
    if score.monthly_vup:
        mtbl = Table(title="Monthly Volatility", box=box.SIMPLE, show_header=True)
        mtbl.add_column("Month",  style="dim", min_width=10)
        mtbl.add_column("VUp",    justify="right", style="green", min_width=8)
        mtbl.add_column("VDown",  justify="right", style="red",   min_width=8)
        for month in sorted(score.monthly_vup):
            vup   = score.monthly_vup.get(month, 0)
            vdown = score.monthly_vdown.get(month, 0)
            mtbl.add_row(month, f"{vup * 100:.1f}%", f"{vdown * 100:.1f}%")
        console.print(mtbl)

    console.print(f"\n[bold]Technical score:[/bold]  {_score_bar(score.score)}")
    colour = _SIGNAL_COLOUR.get(score.signal_strength, "white")
    console.print(f"[bold]Signal:[/bold]  [{colour}]{score.signal_strength}[/{colour}]\n")


# ---------------------------------------------------------------------------
# Composite signal display
# ---------------------------------------------------------------------------

def print_composite_signal(signal: CompositeSignal) -> None:
    """Render the final composite BUY / HOLD / SELL recommendation panel."""
    colour = _SIGNAL_COLOUR.get(signal.final_signal, "white")

    lines = [f"[{colour}]{signal.final_signal_label}[/{colour}]  —  ${signal.current_price:.2f}\n"]

    if signal.fundamental_score is not None:
        lines.append(
            f"  Fundamental  {_score_bar(signal.fundamental_score, 20)}  "
            f"({signal.fundamental_signal})"
        )
    if signal.technical_score is not None:
        lines.append(
            f"  Technical    {_score_bar(signal.technical_score, 20)}  "
            f"({signal.technical_signal})"
        )

    lines.append(f"\n  Composite    {_score_bar(signal.composite_score, 20)}\n")

    if signal.reasoning:
        lines.append("[bold]Key factors[/bold]")
        for reason in signal.reasoning:
            lines.append(f"  {reason}")

    console.print(
        Panel(
            "\n".join(lines),
            title=f"[bold]Final Signal — {signal.ticker}[/bold]",
            border_style=colour.split()[-1],   # last word is always the colour name
            padding=(1, 2),
        )
    )


# ---------------------------------------------------------------------------
# Backtest results display
# ---------------------------------------------------------------------------

def print_backtest_results(results: BacktestResults) -> None:
    """Render backtest performance metrics and trade history."""
    console.print(results.summary())

    if not results.trades:
        console.print("[dim]No trades were executed.[/dim]")
        return

    # Trade history table (latest 30)
    ttbl = Table(
        title=f"Trade History  (showing last {min(30, len(results.trades))})",
        box=box.SIMPLE,
    )
    ttbl.add_column("Date",         style="dim",   min_width=12)
    ttbl.add_column("Action",       min_width=8)
    ttbl.add_column("Price",        justify="right", min_width=10)
    ttbl.add_column("Value",        justify="right", min_width=14)
    ttbl.add_column("Score",        justify="right", min_width=7)
    ttbl.add_column("Reason",       style="dim",   min_width=12)

    for trade in results.trades[-30:]:
        colour = "green" if trade.action == "BUY" else "red"
        ttbl.add_row(
            str(trade.date.date()),
            f"[{colour}]{trade.action}[/{colour}]",
            f"${trade.price:.2f}",
            f"${trade.value:,.2f}",
            f"{trade.signal_score:.0f}",
            trade.reason,
        )

    console.print(ttbl)

    # Equity vs benchmark sparkline using ASCII
    _print_equity_chart(results)


def _print_equity_chart(results: BacktestResults) -> None:
    """Print a compact ASCII equity curve alongside the benchmark."""
    eq  = results.equity_curve
    bh  = results.benchmark_curve

    if eq.empty:
        return

    # Normalise to 100 at start
    eq_norm = eq / eq.iloc[0] * 100
    console.print("\n[bold]Equity curve[/bold] (normalised to 100 at start)")

    # Monthly resampled for compactness
    eq_m = eq_norm.resample("ME").last().dropna()

    bars = []
    prev = 100.0
    for val in eq_m:
        delta = val - prev
        if   delta >  5:  bars.append("[green]▲[/green]")
        elif delta >  0:  bars.append("[green]△[/green]")
        elif delta > -5:  bars.append("[red]▽[/red]")
        else:             bars.append("[red]▼[/red]")
        prev = val

    console.print("  " + " ".join(bars))

    final_eq = eq_norm.iloc[-1]
    colour   = "green" if final_eq >= 100 else "red"
    console.print(
        f"  Strategy: [{colour}]{final_eq:.1f}[/{colour}]  "
        f"(start 100 → final [{colour}]{final_eq:.1f}[/{colour}])"
    )

    if not bh.empty:
        bh_norm  = bh / bh.iloc[0] * 100
        final_bh = bh_norm.iloc[-1]
        c2 = "green" if final_bh >= 100 else "red"
        console.print(
            f"  Benchmark: [{c2}]{final_bh:.1f}[/{c2}]  "
            f"(buy-and-hold)"
        )
    console.print("")
