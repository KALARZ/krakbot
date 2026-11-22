"""Pre-registered decision rules of the Donchian forward test (docs/plan.md §0.2). Pure functions, no I/O.

Written down in code BEFORE any forward-test trade exists, so the statistic, the threshold and the consequence
cannot be chosen after seeing the data. Any change to this file must bump config.DONCHIAN_STRATEGY_VERSION, which
changes the freeze hash and closes a running forward test.

Inputs are the closed forward-test trades with PnL computed at fee Tier 1 (0.40% maker / 0.80% taker) plus 5 bp
slippage per side plus book impact on taker exits, independently of the real account tier.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

__all__ = [
    "ALPHA", "CHECKPOINT_TRADES", "PF_ABANDON_BELOW", "CheckpointDecision", "CheckpointResult", "ClosedTrade",
    "SecondaryOutcome", "SecondaryResult", "checkpoint", "secondary_hypothesis", "student_t_sf", "welch_one_sided",
]

CHECKPOINT_TRADES = 100
PF_ABANDON_BELOW = Decimal("1.0")
ALPHA = 0.05
HALF = CHECKPOINT_TRADES // 2


@dataclass(frozen=True)
class ClosedTrade:
    trade_id: str
    entry_ts: int          # ms UTC
    exit_ts: int           # ms UTC
    stop_pct: Decimal      # 1 - initial_stop / entry_price  (distance of the initial stop, fraction of price)
    pnl_t1: Decimal        # EUR, at Tier 1 fees + 5 bp/side + impact
    ret_t1: Decimal        # pnl_t1 / entry notional


def _first_n_closed(trades: Sequence[ClosedTrade], n: int) -> list[ClosedTrade]:
    return sorted(trades, key=lambda t: (t.exit_ts, t.trade_id))[:n]


# ----------------------------------------------------------------------------- primary rule
class CheckpointDecision(StrEnum):
    NOT_YET = "not_yet"
    CONTINUE = "continue"
    ABANDON = "abandon"


@dataclass(frozen=True)
class CheckpointResult:
    decision: CheckpointDecision
    n: int
    profit_factor: Decimal | None  # None = no losing trade (PF = +inf)
    gross_profit: Decimal
    gross_loss: Decimal


def checkpoint(trades: Sequence[ClosedTrade]) -> CheckpointResult:
    """Primary pre-registered rule. On the FIRST 100 closed trades (by exit time, ties by trade_id):
    PF = sum(pnl_t1 > 0) / |sum(pnl_t1 <= 0)|. PF < 1.0 -> ABANDON (forward test closed); otherwise CONTINUE.
    Before 100 closed trades -> NOT_YET (no metric is used for decisions)."""
    if len(trades) < CHECKPOINT_TRADES:
        return CheckpointResult(CheckpointDecision.NOT_YET, len(trades), None, Decimal(0), Decimal(0))
    first = _first_n_closed(trades, CHECKPOINT_TRADES)
    gp = sum((t.pnl_t1 for t in first if t.pnl_t1 > 0), Decimal(0))
    gl = -sum((t.pnl_t1 for t in first if t.pnl_t1 <= 0), Decimal(0))
    pf = None if gl == 0 else gp / gl
    abandon = pf is not None and pf < PF_ABANDON_BELOW
    return CheckpointResult(CheckpointDecision.ABANDON if abandon else CheckpointDecision.CONTINUE,
                            CHECKPOINT_TRADES, pf, gp, gl)


# ----------------------------------------------------------------------------- secondary hypothesis
class SecondaryOutcome(StrEnum):
    NOT_TESTABLE = "not_testable"   # fewer than 100 closed trades when the test ends, or zero variance
    SUPPORTED = "supported"         # p < 0.05 (one-sided)
    NOT_SUPPORTED = "not_supported"


@dataclass(frozen=True)
class SecondaryResult:
    outcome: SecondaryOutcome
    n_low: int
    n_high: int
    mean_low: float | None
    mean_high: float | None
    t: float | None
    df: float | None
    p_one_sided: float | None
    stop_pct_split: Decimal | None  # stop_pct of the 50th trade by rank: the frozen filter value for a variant


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the regularized incomplete beta (modified Lentz)."""
    tiny, eps = 1e-300, 3e-16
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 400):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            return h
    raise ArithmeticError("incomplete beta did not converge")


def _ibeta(a: float, b: float, x: float) -> float:
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    ln_front = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x)
    front = math.exp(ln_front)
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def student_t_sf(t: float, df: float) -> float:
    """P(T > t) for Student's t with `df` degrees of freedom."""
    if df <= 0:
        raise ValueError("df must be > 0")
    tail = 0.5 * _ibeta(df / 2.0, 0.5, df / (df + t * t))
    return tail if t >= 0 else 1.0 - tail


def welch_one_sided(low: Sequence[float], high: Sequence[float]) -> tuple[float, float, float] | None:
    """Welch's t for H1: mean(low) > mean(high). Returns (t, df, one-sided p) or None if not computable."""
    n1, n2 = len(low), len(high)
    if n1 < 2 or n2 < 2:
        return None
    m1, m2 = sum(low) / n1, sum(high) / n2
    v1 = sum((x - m1) ** 2 for x in low) / (n1 - 1)
    v2 = sum((x - m2) ** 2 for x in high) / (n2 - 1)
    se2 = v1 / n1 + v2 / n2
    if se2 == 0.0:
        return None
    t = (m1 - m2) / math.sqrt(se2)
    df = se2**2 / ((v1 / n1) ** 2 / (n1 - 1) + (v2 / n2) ** 2 / (n2 - 1))
    return t, df, student_t_sf(t, df)


def secondary_hypothesis(trades: Sequence[ClosedTrade]) -> SecondaryResult:
    """Secondary pre-registered hypothesis (docs/research.md §K.5, docs/plan.md §0.2).

    Sample: the FIRST 100 closed trades (by exit time, ties by trade_id).
    Split: sort the 100 by (stop_pct, entry_ts, trade_id) ascending; LOW = ranks 1-50, HIGH = ranks 51-100.
    Statistic: Welch's t on ret_t1 (as float), H1: mean(LOW) > mean(HIGH), one-sided p from Student's t with
    Welch-Satterthwaite df. Threshold: SUPPORTED iff p < 0.05; otherwise NOT_SUPPORTED. Fewer than 100 trades or
    zero variance -> NOT_TESTABLE. Effect: none on the running forward test (see plan §0.2)."""
    if len(trades) < CHECKPOINT_TRADES:
        return SecondaryResult(SecondaryOutcome.NOT_TESTABLE, 0, 0, None, None, None, None, None, None)
    first = _first_n_closed(trades, CHECKPOINT_TRADES)
    ranked = sorted(first, key=lambda t: (t.stop_pct, t.entry_ts, t.trade_id))
    low = [float(t.ret_t1) for t in ranked[:HALF]]
    high = [float(t.ret_t1) for t in ranked[HALF:]]
    w = welch_one_sided(low, high)
    split = ranked[HALF - 1].stop_pct
    if w is None:
        return SecondaryResult(SecondaryOutcome.NOT_TESTABLE, HALF, HALF, None, None, None, None, None, split)
    t, df, p = w
    outcome = SecondaryOutcome.SUPPORTED if p < ALPHA else SecondaryOutcome.NOT_SUPPORTED
    return SecondaryResult(outcome, HALF, HALF, sum(low) / HALF, sum(high) / HALF, t, df, p, split)
