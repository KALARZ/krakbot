"""Ambiguous-order resolution (docs/plan.md §4.7), shared by the engines (automatic, first 2 minutes) and by
`krakbot resolve` (manual). Evidence rules:
  FOUND       -> adopt (txid, real status, real fills)
  ABSENT      -> rejected: only with successful queries after deadline + 60 s
  UNAVAILABLE -> NO change. Silence or errors from Kraken are never evidence of absence. The only override is an
                 explicit operator confirmation through `resolve --operator-confirms-absent --reason ...`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from krakbot.domain import TERMINAL_ORDER_STATUSES, EventLevel, Mode, OrderRole, OrderStatus
from krakbot.execution.types import Broker, Lookup, LookupKind, OrderUpdate
from krakbot.state.dca import DcaRepo
from krakbot.state.repo import Order, Store

__all__ = ["ResolveAction", "ResolveReport", "apply_update", "resolve_order", "unresolved_for"]

UNRESOLVED = (OrderStatus.INTENT, OrderStatus.SENT, OrderStatus.UNKNOWN)


class ResolveAction(StrEnum):
    NOTHING_TO_RESOLVE = "nothing_to_resolve"
    ADOPTED = "adopted"
    REJECTED_ABSENT = "rejected_absent"
    REJECTED_BY_OPERATOR = "rejected_by_operator"
    REFUSED_NO_EVIDENCE = "refused_no_evidence"
    EVIDENCE_ONLY = "evidence_only"


@dataclass(frozen=True)
class ResolveReport:
    cl_ord_id: str
    action: ResolveAction
    evidence: Lookup | None
    halt_cleared: bool


def apply_update(store: Store, dca: DcaRepo | None, order: Order, upd: OrderUpdate) -> Order:
    """Record fills (idempotent) and move the order to the exchange-reported status, in one transaction.
    DCA fills also update the tranche spend (budget invariant enforced by DcaRepo)."""
    with store.tx():
        for f in upd.fills:
            inserted, _ = store.fills.record(exec_id=f.exec_id, cl_ord_id=order.cl_ord_id, ts=f.ts_ms, price=f.price,
                                             qty=f.qty, fee=f.fee, fee_ccy=f.fee_ccy, liquidity=f.liquidity)
            if inserted and dca is not None and order.role == OrderRole.DCA:
                dca.add_spend(order.trade_id, cost=f.price * f.qty, fee=f.fee, qty=f.qty)
        cur = store.orders.by_cl_ord_id(order.cl_ord_id)
        target = upd.status
        if target in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
            # fills drive these statuses; if the exchange says filled but fills are not all visible yet, wait
            target = cur.status if cur.status not in UNRESOLVED else OrderStatus.OPEN
            if cur.filled_qty > 0 and cur.status in UNRESOLVED:
                target = OrderStatus.PARTIALLY_FILLED
        if cur.status in TERMINAL_ORDER_STATUSES:
            return cur
        if target != cur.status or (upd.txid and cur.txid is None):
            if target == cur.status:
                store.orders.mark(cur.cl_ord_id, cur.status, txid=upd.txid)
            else:
                store.orders.mark(cur.cl_ord_id, target, txid=upd.txid, cancel_reason=upd.reason)
    return store.orders.by_cl_ord_id(order.cl_ord_id)


def unresolved_for(store: Store, strategy: str, mode: Mode) -> list[Order]:
    return [o for o in store.orders.unresolved(mode) if store.trades.get(o.trade_id).strategy == strategy]


async def resolve_order(store: Store, dca: DcaRepo | None, broker: Broker, cl_ord_id: str, *, strategy: str,
                        deadline_s: int, operator_confirms_absent: bool = False, operator_reason: str | None = None,
                        evidence_only: bool = False) -> ResolveReport:
    order = store.orders.by_cl_ord_id(cl_ord_id)
    if order.status not in UNRESOLVED:
        return ResolveReport(cl_ord_id, ResolveAction.NOTHING_TO_RESOLVE, None, False)
    sent_at = order.created_at
    look = await broker.lookup(cl_ord_id, sent_at_ms=sent_at, deadline_ms=sent_at + deadline_s * 1000)
    store.events.append("ORDER_EVIDENCE", EventLevel.WARNING,
                        {"cl_ord_id": cl_ord_id, "kind": str(look.kind), "detail": look.detail,
                         "status": str(look.update.status) if look.update else None,
                         "txid": look.update.txid if look.update else None},
                        strategy=strategy, mode=order.mode, trade_id=order.trade_id)
    if evidence_only:
        return ResolveReport(cl_ord_id, ResolveAction.EVIDENCE_ONLY, look, False)
    if order.status == OrderStatus.INTENT:
        store.orders.mark(cl_ord_id, OrderStatus.UNKNOWN, last_error="intent without confirmed send")
        order = store.orders.by_cl_ord_id(cl_ord_id)
    if look.kind == LookupKind.FOUND and look.update is not None:
        apply_update(store, dca, order, look.update)
        action = ResolveAction.ADOPTED
    elif look.kind == LookupKind.ABSENT:
        store.orders.mark(cl_ord_id, OrderStatus.REJECTED, cancel_reason=f"absent_proven: {look.detail}")
        action = ResolveAction.REJECTED_ABSENT
    elif operator_confirms_absent and operator_reason and operator_reason.strip():
        store.orders.mark(cl_ord_id, OrderStatus.REJECTED, cancel_reason=f"operator: {operator_reason.strip()}")
        store.events.append("RESOLVED_BY_OPERATOR", EventLevel.CRITICAL,
                            {"cl_ord_id": cl_ord_id, "reason": operator_reason.strip(), "evidence": look.detail},
                            strategy=strategy, mode=order.mode, trade_id=order.trade_id)
        action = ResolveAction.REJECTED_BY_OPERATOR
    else:
        return ResolveReport(cl_ord_id, ResolveAction.REFUSED_NO_EVIDENCE, look, False)
    cleared = False
    rs = store.risk.get(strategy, order.mode)
    if rs.halted and rs.halt_reason == "unknown_order" and not unresolved_for(store, strategy, order.mode):
        cleared = store.risk.clear_halt(strategy, order.mode, reason="unknown_order")
    store.events.append("ORDER_RESOLVED", EventLevel.WARNING,
                        {"cl_ord_id": cl_ord_id, "action": str(action), "halt_cleared": cleared},
                        strategy=strategy, mode=order.mode, trade_id=order.trade_id)
    return ResolveReport(cl_ord_id, action, look, cleared)
