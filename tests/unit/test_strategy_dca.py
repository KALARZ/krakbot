from datetime import UTC, datetime
from decimal import Decimal

import pytest

from krakbot.kraken_rest.models import Instrument
from krakbot.strategies.dca import (
    DAY_MS,
    HOUR_MS,
    TrancheOutcome,
    closing_status,
    entry_price,
    fee_for,
    order_qty,
    plan,
    week_budget,
    window_at,
)

INST = Instrument(rest_key="XXBTZEUR", altname="XBTEUR", wsname="XBT/EUR", base="BTC", quote="EUR", pair_decimals=1,
                  lot_decimals=8, cost_decimals=5, tick_size=Decimal("0.1"), ordermin=Decimal("0.00006"),
                  costmin=Decimal("0.45"), status="online", symbol="BTC/EUR")
MIN_COST = INST.min_cost(Decimal("54557.1"))  # 3.273426


def ms(*a):
    return int(datetime(*a, tzinfo=UTC).timestamp() * 1000)


def test_window_monday_10_utc():
    # 2026-11-23 is a Monday
    s, e = window_at(ms(2026, 11, 23, 12, 0), weekday=0, hour_utc=10, window_hours=24)
    assert s == ms(2026, 11, 23, 10, 0) and e == ms(2026, 11, 24, 10, 0)
    s2, _ = window_at(ms(2026, 11, 23, 9, 59), weekday=0, hour_utc=10, window_hours=24)
    assert s2 == ms(2026, 11, 16, 10, 0)  # before Monday 10:00 -> previous week's slot (already ended)
    s3, e3 = window_at(ms(2026, 11, 22, 12, 0), weekday=0, hour_utc=10, window_hours=24)  # Sunday
    assert s3 == ms(2026, 11, 16, 10, 0) and e3 < ms(2026, 11, 22, 12, 0)
    assert window_at(ms(2026, 11, 24, 9, 59), weekday=0, hour_utc=10, window_hours=24)[1] == ms(2026, 11, 24, 10)


def test_plan_full_reduced_disabled():
    p = plan(capital=Decimal("5000"), allocation=Decimal("0.20"), tranches=52, min_cost=MIN_COST, safety=Decimal("1.5"))
    assert p.enabled and not p.reduced and p.tranches == 52 and p.nominal == Decimal("19.23")
    small = plan(capital=Decimal("600"), allocation=Decimal("0.20"), tranches=52, min_cost=MIN_COST,
                 safety=Decimal("1.5"))
    assert small.enabled and small.reduced and small.tranches == 24  # 120 / (1.5*3.273) = 24.4
    tiny = plan(capital=Decimal("20"), allocation=Decimal("0.20"), tranches=52, min_cost=MIN_COST,
                safety=Decimal("1.5"))
    assert not tiny.enabled and "capital below minimum" in tiny.disabled_reason


def test_week_budget_carry_and_cap():
    p = plan(capital=Decimal("5200"), allocation=Decimal("0.20"), tranches=52, min_cost=MIN_COST, safety=Decimal("1.5"))
    assert p.nominal == Decimal("20.00")
    w1 = week_budget(p, seq=1, spent_before=Decimal(0), max_multiple=2, min_cost=MIN_COST)
    assert (w1.nominal, w1.carry_in, w1.budget) == (Decimal("20.00"), Decimal(0), Decimal("20.00"))
    # week 1 unfilled -> week 2 can spend 40 (cap 2T = 40)
    w2 = week_budget(p, seq=2, spent_before=Decimal(0), max_multiple=2, min_cost=MIN_COST)
    assert (w2.carry_in, w2.budget) == (Decimal("20.00"), Decimal("40.00"))
    # 3 weeks unfilled -> spendable 60 but capped at 40; the excess stays due for later weeks
    w3 = week_budget(p, seq=3, spent_before=Decimal(0), max_multiple=2, min_cost=MIN_COST)
    assert w3.budget == Decimal("40.00") and w3.nominal + w3.carry_in == Decimal("60.00")
    # last tranche gets the rounding remainder; afterwards only carry remains
    last = week_budget(p, seq=52, spent_before=Decimal("1020.00"), max_multiple=2, min_cost=MIN_COST)
    assert last.nominal == Decimal("20.00") and last.budget == Decimal("20.00")
    after = week_budget(p, seq=53, spent_before=Decimal("1030.00"), max_multiple=2, min_cost=MIN_COST)
    assert after.nominal == 0 and after.budget == Decimal("10.00") and not after.complete
    done = week_budget(p, seq=54, spent_before=Decimal("1038.00"), max_multiple=2, min_cost=MIN_COST)
    assert done.complete  # 2 EUR left < min cost


def test_total_spend_never_exceeds_allocation():
    p = plan(capital=Decimal("5000"), allocation=Decimal("0.20"), tranches=52, min_cost=MIN_COST, safety=Decimal("1.5"))
    spent = Decimal(0)
    for k in range(1, 80):
        w = week_budget(p, seq=k, spent_before=spent, max_multiple=2, min_cost=MIN_COST)
        if w.complete:
            break
        spent += w.budget if k % 3 else Decimal(0)  # every 3rd week unfilled
        assert spent <= p.total_budget
    assert p.total_budget - spent < MIN_COST


def test_order_qty_includes_fee_and_respects_minimums():
    price, fee, budget = Decimal("54557.1"), Decimal("0.004"), Decimal("19.23")
    q = order_qty(budget, price, fee, INST)
    assert q == Decimal("0.00035107")

    def total(qty):
        return qty * price + fee_for(qty * price, fee, INST)  # fee rounded UP to cost decimals, as Kraken charges

    assert total(q) <= budget                    # fits, fee included
    assert total(q + INST.lot_step) > budget     # maximal: one more lot would not fit
    assert fee_for(Decimal("19.15"), fee, INST) == Decimal("0.07660")
    assert fee_for(Decimal("19.150001"), fee, INST) == Decimal("0.07661")  # rounded up, never down


def test_order_qty_accounts_for_fee_rounding_up():
    # exact-fee maximum would overshoot by one cost unit once the fee is rounded up: sizing steps back one lot
    fee = Decimal("0.004")
    for budget in ("19.23", "7.77", "38.46", "5.01", "100"):
        for price in ("54557.1", "55597.9", "61234.5", "49999.9"):
            b, p = Decimal(budget), Decimal(price)
            q = order_qty(b, p, fee, INST)
            if q is None:
                continue
            assert q * p + fee_for(q * p, fee, INST) <= b
            nxt = q + INST.lot_step
            assert nxt * p + fee_for(nxt * p, fee, INST) > b
    assert order_qty(Decimal("3.0"), Decimal("54557.1"), Decimal("0.004"), INST) is None  # below ordermin
    assert entry_price(Decimal("54557.19"), INST) == Decimal("54557.1")


@pytest.mark.parametrize("budget,spent,placed,expected", [
    ("20", "18", True, TrancheOutcome.FILLED),     # 2 EUR left < min cost
    ("20", "10", True, TrancheOutcome.PARTIAL),
    ("20", "0", True, TrancheOutcome.UNFILLED),
    ("2", "0", False, TrancheOutcome.SKIPPED_MIN),
    ("20", "0", False, TrancheOutcome.UNFILLED),    # e.g. chase cap all day: nothing placed, but budget was valid
])
def test_closing_status(budget, spent, placed, expected):
    assert closing_status(budget=Decimal(budget), spent=Decimal(spent), ever_placed=placed, min_cost=MIN_COST) == expected


def test_hour_constants():
    assert DAY_MS == 24 * HOUR_MS
