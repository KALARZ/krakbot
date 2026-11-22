from decimal import Decimal

import pytest

from krakbot.config import ForwardTest
from krakbot.forward_test import (
    ALPHA,
    CHECKPOINT_TRADES,
    PF_ABANDON_BELOW,
    CheckpointDecision,
    ClosedTrade,
    SecondaryOutcome,
    checkpoint,
    secondary_hypothesis,
    student_t_sf,
    welch_one_sided,
)

# reference values computed with scipy 1.18 (stats.t.sf / ttest_ind(equal_var=False, alternative="greater"))
SF_REF = [((1.0, 10), 0.170446566151), ((2.5, 97.3), 0.007045990423), ((-0.7, 55.5), 0.756574373133),
          ((0.0, 30), 0.5), ((4.2, 3.3), 0.010183331570)]
LOW = [0.021, -0.012, 0.034, 0.008, -0.004, 0.017, 0.029, -0.020, 0.011, 0.006]
HIGH = [-0.015, 0.003, -0.027, 0.012, -0.009, -0.031, 0.004, -0.018, 0.010, -0.006]


def test_constants_match_the_locked_config():
    ft = ForwardTest()
    assert CHECKPOINT_TRADES == ft.checkpoint_trades == 100
    assert PF_ABANDON_BELOW == ft.abandon_if_pf_below == Decimal("1.0")
    assert Decimal(str(ALPHA)) == ft.secondary_hypothesis_alpha


@pytest.mark.parametrize("args,ref", SF_REF)
def test_student_t_sf_matches_scipy(args, ref):
    assert student_t_sf(*args) == pytest.approx(ref, abs=1e-9)


def test_welch_matches_scipy():
    t, df, p = welch_one_sided(LOW, HIGH)
    assert t == pytest.approx(2.303576103687, abs=1e-9)
    assert df == pytest.approx(17.650194304894, abs=1e-9)
    assert p == pytest.approx(0.016820049434, abs=1e-9)
    _, _, p_rev = welch_one_sided(HIGH, LOW)
    assert p_rev == pytest.approx(0.983179950566, abs=1e-9)
    assert welch_one_sided([1.0], [2.0, 3.0]) is None
    assert welch_one_sided([1.0, 1.0], [1.0, 1.0]) is None


def _trades(rets, stops=None, pnls=None):
    out = []
    for i, r in enumerate(rets):
        out.append(ClosedTrade(trade_id=f"t{i:03d}", entry_ts=1_000 + i, exit_ts=2_000 + i,
                               stop_pct=Decimal(str(stops[i] if stops else 0.05)),
                               pnl_t1=Decimal(str(pnls[i] if pnls else r * 100)), ret_t1=Decimal(str(r))))
    return out


def test_checkpoint_not_before_100_trades():
    assert checkpoint(_trades([0.01] * 99)).decision == CheckpointDecision.NOT_YET


def test_checkpoint_abandon_below_pf_one():
    # 40 winners of +1.0, 60 losers of -0.7 -> PF = 40 / 42 = 0.952 -> abandon
    r = checkpoint(_trades([0.01] * 40 + [-0.007] * 60))
    assert r.decision == CheckpointDecision.ABANDON and r.profit_factor < 1


def test_checkpoint_continue_at_exactly_pf_one_and_above():
    r = checkpoint(_trades([0.01] * 50 + [-0.01] * 50))
    assert r.profit_factor == Decimal(1) and r.decision == CheckpointDecision.CONTINUE
    assert checkpoint(_trades([0.01] * 100)).decision == CheckpointDecision.CONTINUE  # no losers: PF = inf


def test_checkpoint_uses_only_the_first_100_closed_trades():
    trades = _trades([0.01] * 50 + [-0.01] * 50 + [-0.05] * 30)  # the 30 later losers must be ignored
    assert checkpoint(trades).decision == CheckpointDecision.CONTINUE


def test_secondary_split_by_stop_rank_and_decision():
    # low-stop half clearly better -> SUPPORTED; split value = stop of the 50th ranked trade
    rets = [0.02 + 0.001 * (i % 5) for i in range(50)] + [-0.01 + 0.001 * (i % 5) for i in range(50)]
    stops = [0.02 + i * 0.0001 for i in range(50)] + [0.08 + i * 0.0001 for i in range(50)]
    r = secondary_hypothesis(_trades(rets, stops))
    assert r.outcome == SecondaryOutcome.SUPPORTED and r.p_one_sided < ALPHA
    assert r.n_low == r.n_high == 50 and r.stop_pct_split == Decimal(str(stops[49]))
    # reversed relation -> NOT_SUPPORTED (one-sided)
    r2 = secondary_hypothesis(_trades(list(reversed(rets)), stops))
    assert r2.outcome == SecondaryOutcome.NOT_SUPPORTED and r2.p_one_sided > 0.5


def test_secondary_not_testable_cases():
    assert secondary_hypothesis(_trades([0.01] * 99)).outcome == SecondaryOutcome.NOT_TESTABLE
    assert secondary_hypothesis(_trades([0.01] * 100)).outcome == SecondaryOutcome.NOT_TESTABLE  # zero variance
