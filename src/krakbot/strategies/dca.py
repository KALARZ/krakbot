"""DCA decision logic (docs/plan.md §0.1). Pure functions, Decimal only, no I/O.

Ledger rule (no separate carry bookkeeping that could drift): with total budget B, n tranches of nominal T,
  due_k       = B if k >= n else k*T                       (what should have been spent by tranche k)
  spendable_k = due_k - spent_before_k                     (implicit carry = what earlier tranches did not spend)
  budget_k    = min(spendable_k, max_tranche_multiple * T) (weekly cap: no catch-up buying spree)
The program is complete once B - spent < min_cost (nothing more can be bought within Kraken's minimums).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from enum import StrEnum

from krakbot.kraken_rest.models import Instrument
from krakbot.numeric import ceil_to_step, floor_to_step, round_price_passive

__all__ = [
    "DAY_MS",
    "HOUR_MS",
    "DcaPlan",
    "TrancheOutcome",
    "WaitReason",
    "WeekBudget",
    "canary_budget",
    "closing_status",
    "entry_price",
    "fee_for",
    "min_qty",
    "order_qty",
    "plan",
    "week_budget",
    "window_at",
]

HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS
_CENT = Decimal("0.01")


def window_at(now_ms: int, *, weekday: int, hour_utc: int, window_hours: int) -> tuple[int, int]:
    """The weekly window [start, end) of the most recent slot start <= now (Monday=0). `now` may be past `end`."""
    day = now_ms // DAY_MS
    dow = (day + 3) % 7  # 1970-01-01 was a Thursday
    start = (day - ((dow - weekday) % 7)) * DAY_MS + hour_utc * HOUR_MS
    if start > now_ms:
        start -= 7 * DAY_MS
    return start, start + window_hours * HOUR_MS


@dataclass(frozen=True)
class DcaPlan:
    total_budget: Decimal
    tranches: int
    nominal: Decimal
    min_cost: Decimal
    reduced: bool                 # fewer tranches than configured (capital too small for the configured count)
    disabled_reason: str | None   # set => DCA must not run

    @property
    def enabled(self) -> bool:
        return self.disabled_reason is None


def plan(*, capital: Decimal, allocation: Decimal, tranches: int, min_cost: Decimal, safety: Decimal) -> DcaPlan:
    """Startup check (plan §0.1): tranche >= safety * min_cost, else fewer tranches; 0 tranches => disabled."""
    total = (capital * allocation).quantize(_CENT, rounding=ROUND_DOWN)
    need = safety * min_cost
    n = tranches
    reduced = False
    if total / tranches < need:
        n = int(total / need)
        reduced = True
    if n < 1:
        required = (need / allocation).quantize(_CENT)
        return DcaPlan(total, 0, Decimal(0), min_cost, True,
                       f"capital below minimum: need >= {required} EUR for one tranche of {need:.2f} EUR")
    nominal = (total / n).quantize(_CENT, rounding=ROUND_DOWN)
    return DcaPlan(total, n, nominal, min_cost, reduced, None)


@dataclass(frozen=True)
class WeekBudget:
    seq: int
    nominal: Decimal      # due_k - due_{k-1}
    carry_in: Decimal     # spendable_k - nominal
    budget: Decimal       # min(spendable_k, cap)
    complete: bool        # program finished: nothing left to buy


def week_budget(p: DcaPlan, *, seq: int, spent_before: Decimal, max_multiple: int, min_cost: Decimal) -> WeekBudget:
    if seq < 1:
        raise ValueError("seq starts at 1")
    def due(k: int) -> Decimal:
        return p.total_budget if k >= p.tranches else p.nominal * k
    spendable = max(Decimal(0), due(seq) - spent_before)
    nominal = due(seq) - due(seq - 1) if seq >= 1 else Decimal(0)
    nominal = max(Decimal(0), nominal)
    carry_in = max(Decimal(0), spendable - nominal)
    budget = min(spendable, p.nominal * max_multiple)
    complete = p.total_budget - spent_before < min_cost
    return WeekBudget(seq, nominal, carry_in, budget, complete)


def entry_price(bid: Decimal, inst: Instrument) -> Decimal:
    """Post-only buy at the best bid, rounded to the tick on the passive side."""
    return round_price_passive(bid, inst.tick_size, "buy")


def fee_for(cost: Decimal, fee_rate: Decimal, inst: Instrument) -> Decimal:
    """Maker fee in quote, rounded UP to the pair's cost decimals (the conservative side: never under-reserve)."""
    return (cost * fee_rate).quantize(Decimal(1).scaleb(-inst.cost_decimals), rounding=ROUND_UP)


def order_qty(remaining: Decimal, price: Decimal, fee_rate: Decimal, inst: Instrument) -> Decimal | None:
    """Largest lot-multiple qty whose cost PLUS the maker fee (rounded up to cost decimals) fits the remaining
    budget; None if below Kraken's real minimums (qty >= ordermin and qty*price >= costmin)."""
    if price <= 0 or remaining <= 0:
        return None
    qty = floor_to_step(remaining / (price * (1 + fee_rate)), inst.lot_step)
    while qty > 0 and qty * price + fee_for(qty * price, fee_rate, inst) > remaining:
        qty -= inst.lot_step  # fee rounding can push the exact-fee maximum over by one cost unit
    if qty < inst.ordermin or qty * price < inst.costmin:
        return None
    return qty


def min_qty(price: Decimal, inst: Instrument) -> Decimal:
    """Kraken's absolute minimum order size at `price`: max(ordermin, costmin / price rounded UP to the lot)."""
    return max(inst.ordermin, ceil_to_step(inst.costmin / price, inst.lot_step))


def canary_budget(mid: Decimal, *, max_chase: Decimal, fee_rate: Decimal, inst: Instrument) -> Decimal:
    """Budget of a LIVE canary tranche (plan §5.3): enough for ONE minimum-size order anywhere up to the chase cap,
    fee included, rounded up to the cent. Never more: the canary must not be able to buy more than the minimum."""
    cap = mid * (1 + max_chase)
    cost = max(inst.ordermin * cap, inst.costmin + inst.lot_step * cap)
    return (cost + fee_for(cost, fee_rate, inst)).quantize(_CENT, rounding=ROUND_UP)


class WaitReason(StrEnum):
    NO_FRESH_BOOK = "no_fresh_book"
    CHASE_CAP = "chase_cap"
    SPREAD_PAUSE = "spread_pause"
    BELOW_MINIMUM = "below_minimum"
    DMS_UNAVAILABLE = "dms_unavailable"      # LIVE: the dead man's switch could not be armed: no order


class TrancheOutcome(StrEnum):
    FILLED = "filled"
    PARTIAL = "partial"
    UNFILLED = "unfilled"
    SKIPPED_MIN = "skipped_min"


def closing_status(*, budget: Decimal, spent: Decimal, ever_placed: bool, min_cost: Decimal) -> TrancheOutcome:
    """End of window (plan §0.1): FILLED if what is left cannot buy Kraken's minimum, PARTIAL if something was
    bought, UNFILLED if nothing was bought, SKIPPED_MIN if no order could ever be placed (budget < minimum)."""
    remaining = budget - spent
    if spent > 0:
        return TrancheOutcome.FILLED if remaining < min_cost else TrancheOutcome.PARTIAL
    if not ever_placed and budget < min_cost:
        return TrancheOutcome.SKIPPED_MIN
    return TrancheOutcome.UNFILLED
