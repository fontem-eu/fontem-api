"""
Signal Generator
=================
Combines a ``FundamentalScore`` and/or a ``TechnicalScore`` into a single
composite ``BUY / HOLD / SELL`` recommendation with human-readable reasoning.

Weighting philosophy
--------------------
* Fundamental analysis is the primary lens (default 60 % weight) because it
  captures the *quality* of the underlying business.
* Technical / GMR analysis supplies timing context (default 40 % weight)
  because even great businesses can be bought at bad moments.

Both weights are fully configurable; you can run either analysis alone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from .fundamental import FundamentalScore
from .technical import TechnicalScore

# Emoji labels shown in the terminal
_SIGNAL_LABELS = {
    "STRONG_BUY":  "🟢  STRONG BUY",
    "BUY":         "🟩  BUY",
    "HOLD":        "🟡  HOLD",
    "SELL":        "🟥  SELL",
    "STRONG_SELL": "🔴  STRONG SELL",
    "NEUTRAL":     "⬜  NEUTRAL",
}


@dataclass
class CompositeSignal:  # pylint: disable=too-many-instance-attributes
    """Combined fundamental + technical recommendation."""

    ticker:        str
    current_price: float

    fundamental_score:  Optional[float] = None
    fundamental_signal: str             = "NEUTRAL"

    technical_score:    Optional[float] = None
    technical_signal:   str             = "NEUTRAL"

    composite_score: float = 0.0
    final_signal:    str   = "NEUTRAL"

    reasoning: List[str] = field(default_factory=list)

    @property
    def final_signal_label(self) -> str:
        """Human-readable label for the final signal."""
        return _SIGNAL_LABELS.get(self.final_signal, self.final_signal)


class SignalGenerator:  # pylint: disable=too-few-public-methods
    """
    Merges fundamental and technical scores into one actionable signal.

    Parameters
    ----------
    fundamental_weight:
        Weight of the fundamental score (0–1).  Must sum to 1.0 with
        ``technical_weight``.
    technical_weight:
        Weight of the technical (GMR) score.
    """

    def __init__(
        self,
        fundamental_weight: float = 0.60,
        technical_weight:   float = 0.40,
    ):
        if abs(fundamental_weight + technical_weight - 1.0) > 1e-6:
            raise ValueError("fundamental_weight + technical_weight must equal 1.0")
        self.fw = fundamental_weight
        self.tw = technical_weight

    # ------------------------------------------------------------------
    def _fundamental_reasons(self, fundamental: FundamentalScore) -> List[str]:
        """Build human-readable reasoning strings for fundamental analysis."""
        reasons: List[str] = []
        c = fundamental.checks

        if fundamental.pe_ratio is not None:
            ok = c.get("pe")
            reasons.append(
                f"{'✓' if ok else '✗'} P/E {fundamental.pe_ratio:.1f} "
                f"({'attractive' if ok else 'expensive'})"
            )
        if fundamental.pb_ratio is not None:
            ok = c.get("pb")
            reasons.append(
                f"{'✓' if ok else '✗'} P/B {fundamental.pb_ratio:.2f} "
                f"({'near book value' if ok else 'premium to book'})"
            )
        if fundamental.debt_equity is not None:
            ok = c.get("de")
            reasons.append(
                f"{'✓' if ok else '✗'} D/E {fundamental.debt_equity:.2f} "
                f"({'manageable debt' if ok else 'high leverage'})"
            )
        if fundamental.roe is not None:
            ok = c.get("roe")
            reasons.append(
                f"{'✓' if ok else '✗'} ROE {fundamental.roe * 100:.1f}% "
                f"({'strong' if ok else 'weak'} capital returns)"
            )
        if fundamental.net_profit_margin is not None:
            ok = c.get("npm")
            reasons.append(
                f"{'✓' if ok else '✗'} Net margin {fundamental.net_profit_margin * 100:.1f}%"
            )
        if fundamental.revenue_cagr_5y is not None:
            ok = c.get("revenue_cagr")
            reasons.append(
                f"{'✓' if ok else '✗'} Revenue CAGR "
                f"{fundamental.revenue_cagr_5y * 100:.1f}% (5y)"
            )
        if fundamental.net_income_cagr_5y is not None:
            g = fundamental.net_income_cagr_5y
            reasons.append(f"  ↳ Net income CAGR {g * 100:.1f}% (5y)")
        if fundamental.consecutive_profit_yrs >= 3:
            reasons.append(
                f"✓ {fundamental.consecutive_profit_yrs} consecutive profitable years"
            )
        if fundamental.dividend_yield > 0:
            ok = c.get("div_yield")
            reasons.append(
                f"{'✓' if ok else '✗'} Dividend yield "
                f"{fundamental.dividend_yield * 100:.2f}%"
            )
        return reasons

    def _technical_reasons(self, technical: TechnicalScore) -> List[str]:
        """Build human-readable reasoning strings for technical analysis."""
        reasons: List[str] = []
        c = technical.checks

        if technical.win_probability is not None:
            ok = c.get("win_prob")
            reasons.append(
                f"{'✓' if ok else '✗'} Win probability "
                f"{technical.win_probability * 100:.1f}% "
                f"({'favours buyers' if ok else 'favours sellers'})"
            )
        if technical.mat_diff_pct is not None:
            ok   = c.get("mat_diff")
            sign = "above" if technical.mat_diff_pct >= 0 else "below"
            reasons.append(
                f"{'✓' if ok else '✗'} Price {abs(technical.mat_diff_pct) * 100:.1f}% "
                f"{sign} 43-day MA — MAT ${technical.mat:.2f}"
            )
        if technical.avg_vup is not None and technical.avg_vdown is not None:
            ok_up   = c.get("avg_vup")
            ok_down = c.get("avg_vdown")
            reasons.append(
                f"{'✓' if (ok_up and ok_down) else '✗'} "
                f"Monthly volatility  VUp {technical.avg_vup * 100:.1f}% / "
                f"VDown {technical.avg_vdown * 100:.1f}%"
            )
        if technical.current_volume > 0:
            ok = c.get("volume")
            reasons.append(
                f"{'✓' if ok else '✗'} Volume {technical.current_volume:,.0f}"
            )
        return reasons

    def generate(
        self,
        fundamental: Optional[FundamentalScore] = None,
        technical:   Optional[TechnicalScore]   = None,
    ) -> CompositeSignal:
        """
        Produce a :class:`CompositeSignal` from available analysis results.
        At least one of *fundamental* or *technical* must be provided.
        """
        if fundamental is None and technical is None:
            raise ValueError("At least one of fundamental / technical must be provided")

        anchor = fundamental or technical
        signal = CompositeSignal(
            ticker=anchor.ticker,
            current_price=anchor.current_price,
        )
        reasons: List[str] = []

        if fundamental:
            signal.fundamental_score  = fundamental.score
            signal.fundamental_signal = fundamental.signal_strength
            reasons.extend(self._fundamental_reasons(fundamental))

        if technical:
            signal.technical_score  = technical.score
            signal.technical_signal = technical.signal_strength
            reasons.extend(self._technical_reasons(technical))

        # ── Weighted composite score ───────────────────────────────────
        if fundamental and technical:
            composite = self.fw * fundamental.score + self.tw * technical.score
        elif fundamental:
            composite = fundamental.score
        else:
            composite = technical.score

        signal.composite_score = composite
        signal.reasoning       = reasons

        # ── Final signal ──────────────────────────────────────────────
        if composite >= 75:
            signal.final_signal = "STRONG_BUY"
        elif composite >= 60:
            signal.final_signal = "BUY"
        elif composite >= 40:
            signal.final_signal = "HOLD"
        elif composite >= 25:
            signal.final_signal = "SELL"
        else:
            signal.final_signal = "STRONG_SELL"

        return signal
