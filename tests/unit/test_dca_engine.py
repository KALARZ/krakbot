"""End-to-end DCA engine on the paper broker (block 4). Every scenario of plan §0.1 / §4.7 that the engine owns."""

import inspect
import uuid
from decimal import Decimal

import pytest

import krakbot.execution.dca_engine as engine_mod
from krakbot.domain import Mode, OrderRole, OrderStatus, OrderType, Side, TradeState
from krakbot.execution.dca_engine import ACTIVE_RESOLUTION_MS, DcaEngine
from krakbot.execution.paper import PaperBroker
from krakbot.execution.resolve import ResolveAction, resolve_order
from krakbot.kraken_rest.errors import OrderRejectedError, OutcomeUnknownError
from krakbot.monitor.alerts import AlertLevel
from krakbot.strategies.dca import WaitReason, fee_for
from tests.unit.dca_harness import INST, MONDAY_10, WEEK_MS, make


@pytest.fixture
def h(tmp_path):
    hh = make(tmp_path)
    yield hh
    hh.close()


async def _open_and_place(h):
    await h.tick()  # creates the tranche for this window
    await h.tick()  # starts it (ref mid) and places the first maker order
    orders = h.orders()
    assert len(orders) == 1 and orders[0].status == OrderStatus.OPEN
    return orders[0]


async def test_full_tranche_fill_and_close(h):
    o = await _open_and_place(h)
    t = h.dca.open_tranche(Mode.DRY_RUN)
    assert t.budget == Decimal("19.23") and t.status == "working" and t.ref_mid == (h.bid + h.ask) / 2
    assert o.price == Decimal("55566.8") and o.post_only and o.side == Side.BUY
    assert o.cl_ord_id == str(uuid.uuid5(engine_mod._NS, f"{t.tranche_id}:0"))  # deterministic, never reused
    # a print AT our price does not fill us (queue ahead); a print strictly below does
    h.trade("55566.8", "1")
    await h.tick()
    assert h.store.orders.by_cl_ord_id(o.cl_ord_id).filled_qty == 0
    h.trade("55566.7", "0.0001")
    await h.tick()
    o1 = h.store.orders.by_cl_ord_id(o.cl_ord_id)
    assert o1.filled_qty == Decimal("0.0001") and o1.status == OrderStatus.PARTIALLY_FILLED
    h.trade("55560.0", "1")
    await h.tick()
    o2 = h.store.orders.by_cl_ord_id(o.cl_ord_id)
    assert o2.status == OrderStatus.FILLED and o2.filled_qty == o.qty
    t = h.dca.get(t.tranche_id)
    cost1, cost2 = o.price * Decimal("0.0001"), o.price * (o.qty - Decimal("0.0001"))
    assert t.spent == cost1 + cost2 + fee_for(cost1, Decimal("0.004"), INST) + fee_for(cost2, Decimal("0.004"), INST)
    assert t.spent <= t.budget and t.qty == o.qty
    await h.tick()
    assert len(h.orders()) == 1  # what is left is below Kraken's minimum: no dust order
    h.clock.t = t.window_end + 1_000
    await h.tick(0)
    t = h.dca.get(t.tranche_id)
    assert t.status == "filled" and t.carry_out == t.nominal - t.spent
    assert h.store.trades.get(t.tranche_id).state == TradeState.HELD
    assert all(x.side == Side.BUY for x in h.orders())


async def test_unfilled_order_expires_and_is_replaced_at_the_new_bid(h):
    o = await _open_and_place(h)
    h.book(bid="55570.0", ask="55590.0")
    await h.tick(300)  # GTD lifetime reached: paper broker expires it, engine re-places for the remainder
    orders = h.orders()
    assert [x.status for x in orders] == [OrderStatus.EXPIRED, OrderStatus.OPEN]
    assert orders[1].price == Decimal("55570.0") and orders[1].attempt == 1 and orders[1].cl_ord_id != o.cl_ord_id


async def test_chase_cap_blocks_orders_above_two_percent_of_window_mid(h):
    await _open_and_place(h)
    ref = h.dca.open_tranche(Mode.DRY_RUN).ref_mid
    h.book(bid=str((ref * Decimal("1.03")).quantize(Decimal("0.1"))), ask=str((ref * Decimal("1.0302")).quantize(
        Decimal("0.1"))))
    await h.tick(300)
    assert len(h.orders()) == 1 and h.engine.last_wait == WaitReason.CHASE_CAP
    h.book(bid="55600.0", ask="55610.0")
    await h.tick()
    assert len(h.orders()) == 2 and h.engine.last_wait is None


async def test_spread_pause_from_derived_threshold(h):
    c = h.store.conn
    c.execute("INSERT INTO liquidity_threshold_sets(set_id, ref_start, ref_end, rule_json, derived_at) "
              "VALUES ('s1', 0, 604800000, '{}', 1)")
    c.execute("INSERT INTO liquidity_thresholds(set_id, pair, n_samples, coverage, eligible, spread_bp_max, "
              "spread_bp_pause, bid_depth_min, ask_depth_min) VALUES ('s1','XBT/EUR',60480,'1.0000',1,'4','5',"
              "'1000','1000')")
    await h.tick()
    h.book(bid="55560.0", ask="55600.0")  # 7.2 bp > p99 pause threshold 5 bp
    await h.tick()
    assert h.orders() == [] and h.engine.last_wait == WaitReason.SPREAD_PAUSE
    h.book(bid="55566.8", ask="55590.0")  # 4.2 bp
    await h.tick()
    assert len(h.orders()) == 1


async def test_post_only_rejection_replaces_immediately_other_rejections_cool_down(h):
    await h.tick()
    h.book(bid="55580.0", ask="55580.0")  # locked book: a buy at the bid would take liquidity
    await h.tick()
    assert [x.status for x in h.orders()] == [OrderStatus.REJECTED]
    h.book(bid="55566.8", ask="55597.9")
    await h.tick()
    assert [x.status for x in h.orders()] == [OrderStatus.REJECTED, OrderStatus.OPEN]  # no cooldown


async def test_kraken_rejection_cools_down_for_a_minute(tmp_path):
    h = make(tmp_path)
    try:
        await h.tick()
        h.broker.fail_next_place = OrderRejectedError("AddOrder", ["EOrder:Order minimum not met"])
        await h.tick()
        assert [x.status for x in h.orders()] == [OrderStatus.REJECTED]
        await h.tick(30)
        assert len(h.orders()) == 1
        await h.tick(31)
        assert [x.status for x in h.orders()] == [OrderStatus.REJECTED, OrderStatus.OPEN]
        assert any(lvl == AlertLevel.WARNING and "rejected" in txt for lvl, txt, _ in h.alerts.sent)
    finally:
        h.close()


async def test_ambiguous_send_halts_and_auto_resolves_on_evidence_within_two_minutes(h):
    await h.tick()
    h.broker.fail_next_place = OutcomeUnknownError("AddOrder", ["EGeneral:Internal error"])
    await h.tick()
    o = h.orders()[0]
    rs = h.store.risk.get("dca", Mode.DRY_RUN)
    assert o.status == OrderStatus.UNKNOWN and rs.halted and rs.halt_reason == "unknown_order" and rs.halt_ref == o.cl_ord_id
    assert any(lvl == AlertLevel.CRITICAL and "UNKNOWN" in txt for lvl, txt, _ in h.alerts.sent)
    assert len(h.events("ORDER_OUTCOME_UNKNOWN")) == 1 and h.events("ORDER_OUTCOME_UNKNOWN")[0]["level"] == "critical"
    await h.tick()  # evidence: the order IS on the (paper) exchange -> adopted, halt cleared
    o = h.store.orders.by_cl_ord_id(o.cl_ord_id)
    assert o.status == OrderStatus.OPEN and o.txid is not None
    assert not h.store.risk.get("dca", Mode.DRY_RUN).halted
    assert [r["kind"] for r in h.store.conn.execute(
        "SELECT kind FROM events WHERE kind IN ('ORDER_EVIDENCE', 'ORDER_RESOLVED') ORDER BY id")] == [
        "ORDER_EVIDENCE", "ORDER_RESOLVED"]
    assert len(h.orders()) == 1  # never a second order while the first was ambiguous


async def test_kraken_unreachable_beyond_two_minutes_keeps_the_halt_until_resolve(h):
    await h.tick()
    h.broker.fail_next_place = OutcomeUnknownError("AddOrder", ["EService:Unavailable"])
    h.broker.lookup_unavailable = True
    await h.tick()
    cl = h.orders()[0].cl_ord_id
    for _ in range(30):  # 150 s of an unreachable exchange
        await h.tick(5)
    rs = h.store.risk.get("dca", Mode.DRY_RUN)
    assert rs.halted and rs.halt_reason == "unknown_order"
    assert h.store.orders.by_cl_ord_id(cl).status == OrderStatus.UNKNOWN  # never assumed "not placed"
    assert len(h.events("HALT_PERSISTENT")) == 1
    assert any("PERSISTENT" in t for t in h.texts())
    # the exchange comes back: the engine keeps collecting evidence but does NOT clear the halt by itself
    h.broker.lookup_unavailable = False
    for _ in range(30):
        await h.tick(5)
    assert h.store.risk.get("dca", Mode.DRY_RUN).halted
    assert h.store.orders.by_cl_ord_id(cl).status == OrderStatus.UNKNOWN
    kinds = [r["payload_json"] for r in h.events("ORDER_EVIDENCE")]
    assert any('"found"' in k for k in kinds)
    assert len(h.orders()) == 1  # still no new purchase
    # the window ends while halted: the tranche is NOT closed (a late fill must still find it open)
    h.clock.t = h.dca.open_tranche(Mode.DRY_RUN).window_end + 5_000
    await h.tick(0)
    assert h.dca.open_tranche(Mode.DRY_RUN) is not None
    # operator path (`krakbot resolve`): evidence -> adopted (expired on the exchange) -> halt cleared
    rep = await resolve_order(h.store, h.dca, h.broker, cl, strategy="dca", deadline_s=10)
    assert rep.action == ResolveAction.ADOPTED and rep.halt_cleared
    assert h.store.orders.by_cl_ord_id(cl).status == OrderStatus.EXPIRED
    await h.tick()
    assert h.dca.latest(Mode.DRY_RUN).status == "unfilled"


async def test_intent_left_by_a_crash_is_ambiguous_then_proven_absent(h):
    await h.tick()
    t = h.dca.start(h.dca.open_tranche(Mode.DRY_RUN).tranche_id, ref_mid=Decimal("55582.35"))
    h.store.orders.intent(local_id="L-crash", trade_id=t.tranche_id, mode=Mode.DRY_RUN, role=OrderRole.DCA,
                          cl_ord_id="crash-0001", pair="XBT/EUR", side=Side.BUY, ordertype=OrderType.LIMIT,
                          qty=Decimal("0.0003"), price=Decimal("55566.8"), post_only=True)
    await h.tick()
    assert h.store.orders.by_cl_ord_id("crash-0001").status == OrderStatus.UNKNOWN
    assert h.store.risk.get("dca", Mode.DRY_RUN).halted
    for _ in range(10):  # the (new) paper book has no such order; absence is provable only after deadline + 60 s
        await h.tick(5)
    assert h.store.orders.by_cl_ord_id("crash-0001").status == OrderStatus.UNKNOWN
    for _ in range(6):
        await h.tick(5)
    o = h.store.orders.by_cl_ord_id("crash-0001")
    assert o.status == OrderStatus.REJECTED and o.cancel_reason.startswith("absent_proven")
    assert not h.store.risk.get("dca", Mode.DRY_RUN).halted
    await h.tick()
    assert len(h.orders()) == 2 and h.orders()[1].attempt == 1


async def test_manual_halt_blocks_new_orders_but_records_fills(h):
    o = await _open_and_place(h)
    h.store.risk.halt("dca", Mode.DRY_RUN, "manual", ref="operator test")
    h.trade("55500.0", "1")
    await h.tick()
    assert h.store.orders.by_cl_ord_id(o.cl_ord_id).status == OrderStatus.FILLED  # fill still recorded
    await h.tick(400)
    assert len(h.orders()) == 1
    assert any(lvl == AlertLevel.CRITICAL and "HALT active (manual" in txt for lvl, txt, _ in h.alerts.sent)


async def test_unfilled_week_carries_to_next_week_with_cap(h):
    await _open_and_place(h)
    t1 = h.dca.open_tranche(Mode.DRY_RUN)
    h.clock.t = t1.window_end + 1_000
    await h.tick(0)
    t1 = h.dca.get(t1.tranche_id)
    assert t1.status == "unfilled" and t1.carry_out == Decimal("19.23")
    h.clock.t = MONDAY_10 + WEEK_MS + 30_000
    await h.tick(0)
    t2 = h.dca.open_tranche(Mode.DRY_RUN)
    assert t2.seq == 2 and t2.nominal == Decimal("19.23") and t2.carry_in == Decimal("19.23")
    assert t2.budget == Decimal("38.46")  # capped at 2 x nominal


async def test_four_unfilled_weeks_raise_dca_stalled(h):
    for week in range(4):
        h.clock.t = MONDAY_10 + week * WEEK_MS + 30_000
        await h.tick(0)
        await h.tick()
        h.clock.t = h.dca.open_tranche(Mode.DRY_RUN).window_end + 1_000
        await h.tick(0)
    assert [t.status for t in h.dca.all(Mode.DRY_RUN)] == ["unfilled"] * 4
    assert any(lvl == AlertLevel.CRITICAL and "DCA_STALLED" in txt for lvl, txt, _ in h.alerts.sent)


async def test_tranche_skipped_when_kraken_minimum_exceeds_budget(h):
    h.book(bid="400000.0", ask="400010.0")  # ordermin 0.00006 BTC * 400k = 24 EUR > 19.23 budget
    await h.tick()
    t = h.dca.latest(Mode.DRY_RUN)
    assert t.status == "skipped_min" and t.carry_out == Decimal("19.23") and h.orders() == []


async def test_no_fresh_book_no_order(h):
    await h.tick()
    h.clock.advance(15)  # the book is now stale (> 10 s) and is not refreshed
    await h.engine.tick()
    assert h.orders() == [] and h.engine.last_wait == WaitReason.NO_FRESH_BOOK


def test_engine_has_no_sell_or_market_path():
    src = inspect.getsource(engine_mod)
    assert "Side.SELL" not in src and "OrderType.MARKET" not in src
    assert "Side.BUY" in src and "OrderType.LIMIT" in src
    assert DcaEngine.__init__ is not None and ACTIVE_RESOLUTION_MS == 120_000


async def test_paper_broker_restart_loses_orders_as_gtd_would(h):
    o = await _open_and_place(h)
    fresh = PaperBroker(h.md, {"XBT/EUR": INST}, fee_rate=Decimal("0.004"), now_ms=h.clock)
    upd = await fresh.refresh(o.cl_ord_id, o.txid)
    assert upd.status == OrderStatus.EXPIRED
