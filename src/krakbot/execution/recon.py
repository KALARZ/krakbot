"""DCA reconciliation (hourly + at boot; at least one per UTC day is required by the live gate).
Checks the DB against itself and against the broker's view: tranche spend/qty/fees == sum of fills, every order's
filled_qty == sum of its fills, no working/unresolved order on a closed tranche,
total spend <= allocation (tolerance: OVERSPEND_HALT_FRACTION, see state/dca.py)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from krakbot.domain import Mode
from krakbot.numeric import D
from krakbot.state.dca import OVERSPEND_HALT_FRACTION, DcaRepo
from krakbot.state.repo import Store

__all__ = ["reconcile_dca"]

_OPEN_ORDER = {"intent", "sent", "unknown", "open", "partially_filled"}


def reconcile_dca(store: Store, dca: DcaRepo, mode: Mode, *,
                  total_budget: Decimal | None) -> tuple[bool, dict[str, Any]]:
    problems: list[str] = []
    total = Decimal(0)
    for t in dca.all(mode):
        fills = store.fills.for_trade(t.tranche_id)
        spent = sum((D(f["cost"]) + D(f["fee"]) for f in fills), Decimal(0))
        qty = sum((D(f["qty"]) for f in fills), Decimal(0))
        fees = sum((D(f["fee"]) for f in fills), Decimal(0))
        if (spent, qty, fees) != (t.spent, t.qty, t.fees):
            problems.append(f"{t.tranche_id}: tranche spent/qty/fees {t.spent}/{t.qty}/{t.fees} "
                            f"!= fills {spent}/{qty}/{fees}")
        for o in store.orders.for_trade(t.tranche_id):
            oq = sum((D(f["qty"]) for f in fills if f["local_id"] == o.local_id), Decimal(0))
            if oq != o.filled_qty:
                problems.append(f"{o.cl_ord_id}: filled_qty {o.filled_qty} != fills {oq}")
            if t.status not in ("scheduled", "working") and str(o.status) in _OPEN_ORDER:
                problems.append(f"{o.cl_ord_id}: {o.status} on closed tranche {t.tranche_id}")
        if t.spent - t.budget > t.budget * OVERSPEND_HALT_FRACTION:  # fee-rounding overspend is logged, not a fault
            problems.append(f"{t.tranche_id}: spent {t.spent} > budget {t.budget} beyond tolerance")
        total += t.spent
    if total_budget is not None and total - total_budget > total_budget * OVERSPEND_HALT_FRACTION:
        problems.append(f"total spent {total} > allocation {total_budget} beyond tolerance")
    ok = not problems
    details: dict[str, Any] = {"problems": problems, "total_spent": str(total)}
    store.recon.add("dca", mode, ok, details)
    return ok, details
