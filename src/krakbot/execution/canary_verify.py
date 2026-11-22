"""Verification of a closed LIVE canary tranche against Kraken (plan §5.3). Read-only on the exchange.

  fill    : every order of the tranche is terminal on Kraken (QueryOrders) with vol_exec == the DB filled qty, and
            every recorded fill matches Kraken's trade (QueryTrades: price, vol, fee, ordertxid); a 'filled' tranche
            must have fills
  ledger  : the tranche's spent / qty / fees equal the sum of its fills
  recon   : the whole LIVE DCA reconciliation passes
  balance : Kraken balance of the base asset - baseline (recorded at the first LIVE start) == total LIVE DCA qty

Any Kraken read error propagates (KrakenError / OSError): the caller records nothing and the halt stays.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from typing import Any

from krakbot.domain import TERMINAL_ORDER_STATUSES, Mode
from krakbot.execution.live import map_kraken_status
from krakbot.execution.recon import reconcile_dca
from krakbot.kraken_rest.client import KrakenRestClient
from krakbot.numeric import D
from krakbot.state.canary import CanaryCheck, CanaryRepo
from krakbot.state.dca import DcaRepo, Tranche
from krakbot.state.repo import Order, Store

__all__ = ["CanaryNotVerifiable", "verify_canary"]


class CanaryNotVerifiable(ValueError):
    pass


async def verify_canary(store: Store, rest: KrakenRestClient, tranche_id: str, *, base_asset: str,
                        total_budget: Decimal | None) -> CanaryCheck:
    dca = DcaRepo(store)
    t = dca.get(tranche_id)
    if t.mode != Mode.LIVE or not t.canary:
        raise CanaryNotVerifiable(f"{tranche_id} is not a LIVE canary tranche")
    if t.status in ("scheduled", "working"):
        raise CanaryNotVerifiable(f"{tranche_id} is still {t.status}: verify after it closes")
    orders = store.orders.for_trade(tranche_id)
    fills = store.fills.for_trade(tranche_id)
    details: dict[str, Any] = {"status": t.status, "qty": t.qty, "spent": t.spent, "orders": len(orders),
                               "fills": len(fills)}

    fp = await _fill_problems(rest, t, orders, fills)
    details["fill_problems"] = fp

    # ---- ledger
    spent = sum((D(f["cost"]) + D(f["fee"]) for f in fills), Decimal(0))
    qty = sum((D(f["qty"]) for f in fills), Decimal(0))
    fees = sum((D(f["fee"]) for f in fills), Decimal(0))
    ledger_ok = (spent, qty, fees) == (t.spent, t.qty, t.fees)
    details["ledger"] = {"fills": [str(spent), str(qty), str(fees)], "tranche": [str(t.spent), str(t.qty),
                                                                                str(t.fees)]}

    # ---- reconciliation
    recon_ok, rd = reconcile_dca(store, dca, Mode.LIVE, total_budget=total_budget)
    details["recon_problems"] = rd["problems"]

    # ---- balance
    baseline = CanaryRepo(store).baseline(base_asset)
    bal = await rest.balance_ex()
    actual = bal.get(base_asset, {}).get("balance", Decimal(0))
    _, bought = dca.totals(Mode.LIVE)
    expected = None if baseline is None else baseline + bought
    details["balance"] = {"asset": base_asset, "kraken": str(actual), "baseline": str(baseline),
                          "bought_live": str(bought), "expected": str(expected)}
    balance_ok = expected is not None and actual == expected
    return CanaryCheck(not fp, ledger_ok, recon_ok, balance_ok, details)


async def _fill_problems(rest: KrakenRestClient, t: Tranche, orders: list[Order],
                         fills: list[sqlite3.Row]) -> list[str]:
    fp: list[str] = []
    for o in orders:
        if o.status not in TERMINAL_ORDER_STATUSES:
            fp.append(f"{o.cl_ord_id} is {o.status} in the DB")
    with_txid = [o for o in orders if o.txid]
    info = await rest.query_orders([o.txid for o in with_txid if o.txid]) if with_txid else {}
    for o in with_txid:
        i = info.get(o.txid or "")
        if i is None:
            fp.append(f"{o.txid} unknown to Kraken")
            continue
        if map_kraken_status(i) not in TERMINAL_ORDER_STATUSES:
            fp.append(f"{o.txid} is still {i.get('status')} on Kraken")
        if D(str(i.get("vol_exec", "0"))) != o.filled_qty:
            fp.append(f"{o.txid} vol_exec {i.get('vol_exec')} != DB filled {o.filled_qty}")
        if i.get("cl_ord_id") not in (None, o.cl_ord_id):
            fp.append(f"{o.txid} cl_ord_id {i.get('cl_ord_id')} != {o.cl_ord_id}")
    txid_of = {o.local_id: o.txid for o in orders}
    trades = await rest.query_trades([str(f["exec_id"]) for f in fills]) if fills else {}
    for f in fills:
        k = trades.get(f["exec_id"])
        if k is None:
            fp.append(f"fill {f['exec_id']} unknown to Kraken")
            continue
        for ours, theirs in (("price", "price"), ("qty", "vol"), ("fee", "fee")):
            if D(f[ours]) != D(str(k[theirs])):
                fp.append(f"fill {f['exec_id']} {ours} {f[ours]} != Kraken {k[theirs]}")
        if k.get("ordertxid") != txid_of.get(f["local_id"]):
            fp.append(f"fill {f['exec_id']} belongs to {k.get('ordertxid')}, not {txid_of.get(f['local_id'])}")
    if t.status == "filled" and not fills:
        fp.append("tranche is 'filled' without fills")
    return fp
