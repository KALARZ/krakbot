"""LIVE-mode DCA behaviour on the paper broker: dead man's switch (plan §5.2) and the pre-registered canary (§5.3)."""

from decimal import Decimal

import httpx
import pytest
import respx

from krakbot.domain import Mode, OrderStatus
from krakbot.kraken_rest.errors import OutcomeUnknownError
from krakbot.monitor.alerts import AlertLevel
from krakbot.state.canary import CANARY_CLEAN_TRANCHES, CanaryCheck, CanaryRepo
from krakbot.strategies.dca import WaitReason, canary_budget, min_qty
from tests.unit.dca_harness import INST, MONDAY_10, WEEK_MS, FakeDms, RecordingPaper, make
from tests.unit.test_cli_app import _mock_public, cli


def live(tmp_path, *, fail_dms=False):
    log: list[str] = []
    h = make(tmp_path, mode=Mode.LIVE, dms=FakeDms(log, fail=fail_dms))
    rec = RecordingPaper(h.md, {"XBT/EUR": INST}, fee_rate=Decimal("0.004"), now_ms=h.clock, log=log)
    h.engine.broker = h.broker = rec
    return h, log


def ok_check():
    return CanaryCheck(True, True, True, True, {"test": True})


async def _canary_week(h, week, *, fill=True):
    h.clock.t = MONDAY_10 + week * WEEK_MS + 30_000
    await h.tick(0)
    await h.tick()
    if fill:
        h.trade("55000.0", "1")
        await h.tick()
    h.clock.t = h.dca.open_tranche(Mode.LIVE).window_end + 1_000
    await h.tick(0)
    return h.dca.latest(Mode.LIVE)


# ---------------------------------------------------------------------------------------------------- DMS
async def test_dms_is_armed_before_the_live_order_is_sent_and_disarmed_after(tmp_path):
    h, log = live(tmp_path)
    try:
        await h.tick()
        await h.tick()
        assert log[:2] == ["arm", "place"]  # never a send without an armed switch
        o = h.orders()[0]
        h.trade("55000.0", "1")
        await h.tick()
        assert h.store.orders.by_cl_ord_id(o.cl_ord_id).status == OrderStatus.FILLED
        assert log[-1] == "disarm"  # nothing can rest on the book any more
    finally:
        h.close()


async def test_no_live_order_if_the_switch_cannot_be_armed(tmp_path):
    h, log = live(tmp_path, fail_dms=True)
    try:
        await h.tick()
        await h.tick()
        assert h.orders() == [] and "place" not in log and h.engine.last_wait == WaitReason.DMS_UNAVAILABLE
        assert any("dead man's switch could not be armed" in t for t in h.texts())
    finally:
        h.close()


async def test_dms_stays_armed_while_an_ambiguous_order_may_rest(tmp_path):
    h, log = live(tmp_path)
    try:
        await h.tick()
        h.broker.fail_next_place = OutcomeUnknownError("AddOrder", ["EGeneral:Internal error"])
        h.broker.lookup_unavailable = True
        await h.tick()
        assert h.orders()[0].status == OrderStatus.UNKNOWN and h.store.risk.get("dca", Mode.LIVE).halted
        for _ in range(5):
            await h.tick(5)
        assert h.engine.dms.armed and "disarm" not in log  # an order that MAY be resting stays covered
    finally:
        h.close()


# ---------------------------------------------------------------------------------------------------- canary
async def test_first_live_tranche_is_one_minimum_order_then_halts_for_verification(tmp_path):
    h, _ = live(tmp_path)
    try:
        await h.tick()
        t = h.dca.open_tranche(Mode.LIVE)
        mid = (h.bid + h.ask) / 2
        assert t.canary and t.budget == canary_budget(mid, max_chase=Decimal("0.02"), fee_rate=Decimal("0.004"),
                                                      inst=INST)
        assert t.budget == Decimal("3.42")
        await h.tick()
        o = h.orders()[0]
        assert o.qty == min_qty(o.price, INST) == Decimal("0.00006")  # Kraken's absolute minimum, nothing more
        h.trade("55000.0", "1")
        await h.tick()
        await h.tick()
        assert len(h.orders()) == 1  # no second order in a canary tranche
        h.clock.t = t.window_end + 1_000
        await h.tick(0)
        t = h.dca.get(t.tranche_id)
        rs = h.store.risk.get("dca", Mode.LIVE)
        assert t.status == "filled" and rs.halted and rs.halt_reason == "canary" and rs.halt_ref == t.tranche_id
        assert any(lvl == AlertLevel.CRITICAL and "canary-verify" in txt for lvl, txt, _ in h.alerts.sent)
        # next week: still halted -> no tranche at all until the operator verifies
        h.clock.t = MONDAY_10 + WEEK_MS + 30_000
        await h.tick(0)
        assert h.dca.latest(Mode.LIVE).tranche_id == t.tranche_id
    finally:
        h.close()


async def test_full_size_only_after_three_consecutive_clean_canary_tranches(tmp_path):
    h, _ = live(tmp_path)
    canary = CanaryRepo(h.store)
    try:
        for week in range(CANARY_CLEAN_TRANCHES):
            t = await _canary_week(h, week)
            assert t.canary and t.status == "filled"
            canary.record(t.tranche_id, ok_check())
            assert h.store.risk.clear_halt("dca", Mode.LIVE, reason="canary", ref=t.tranche_id)
            assert canary.streak() == week + 1
        assert canary.phase_complete()
        h.clock.t = MONDAY_10 + 3 * WEEK_MS + 30_000
        await h.tick(0)
        t4 = h.dca.open_tranche(Mode.LIVE)
        assert not t4.canary and t4.budget == Decimal("38.46")  # due-based catch-up, capped at 2 x nominal
    finally:
        h.close()


async def test_a_failed_check_or_an_unfilled_canary_resets_the_streak(tmp_path):
    h, _ = live(tmp_path)
    canary = CanaryRepo(h.store)
    try:
        t1 = await _canary_week(h, 0)
        canary.record(t1.tranche_id, ok_check())
        h.store.risk.clear_halt("dca", Mode.LIVE, reason="canary", ref=t1.tranche_id)
        t2 = await _canary_week(h, 1)
        canary.record(t2.tranche_id, CanaryCheck(True, True, True, False, {"balance": "mismatch"}))
        canary.record(t2.tranche_id, ok_check())  # a later pass cannot make a failed tranche clean
        h.store.risk.clear_halt("dca", Mode.LIVE, reason="canary", ref=t2.tranche_id)
        assert canary.streak() == 0
        t3 = await _canary_week(h, 2, fill=False)
        assert t3.canary and t3.status == "unfilled"
        canary.record(t3.tranche_id, ok_check())
        h.store.risk.clear_halt("dca", Mode.LIVE, reason="canary", ref=t3.tranche_id)
        assert canary.streak() == 0 and not canary.phase_complete()
        h.clock.t = MONDAY_10 + 3 * WEEK_MS + 30_000
        await h.tick(0)
        assert h.dca.open_tranche(Mode.LIVE).canary  # still minimum size
    finally:
        h.close()


async def test_dry_run_never_uses_the_canary(tmp_path):
    h = make(tmp_path)
    try:
        await h.tick()
        assert not h.dca.open_tranche(Mode.DRY_RUN).canary
    finally:
        h.close()


def test_min_qty_and_canary_budget():
    assert min_qty(Decimal("55000"), INST) == Decimal("0.00006")             # ordermin dominates for BTC
    assert min_qty(Decimal("5000"), INST) == Decimal("0.00009")              # costmin 0.45 / 5000, lot rounded up
    b = canary_budget(Decimal("55000"), max_chase=Decimal("0.02"), fee_rate=Decimal("0.004"), inst=INST)
    assert b == Decimal("3.38")  # 0.00006 x 56100 = 3.366 + fee 0.01347 -> 3.37947 -> 3.38


@pytest.mark.parametrize("reason", ["canary"])
def test_resume_refuses_a_canary_halt(tmp_path, reason):
    h = make(tmp_path)
    try:
        h.store.risk.halt("dca", Mode.LIVE, reason, ref="dca-live-1")
        code, out = cli(h, "resume", "--mode", "live")
        assert code == 2 and "canary-verify dca-live-1" in out and h.store.risk.get("dca", Mode.LIVE).halted
    finally:
        h.close()


# ---------------------------------------------------------------------------------------------------- canary-verify
SECRET = "kQH5HW/8p1uGOVjbgWA7FunAmGO8lsSUXNsu3eow76sz84Q18fWxnyRzBHCd3pd5nE9qa99HAZtuZuj6F1huXg=="
KEYS = f"KRAKEN_API_KEY=TESTKEY-NOT-REAL-0123456789\nKRAKEN_API_SECRET={SECRET}\n"
PRIV = "https://api.kraken.com/0/private/"


def _ok(result):
    return httpx.Response(200, json={"error": [], "result": result})


def _mock_kraken(h, t, *, balance):

    _mock_public()
    o = h.store.orders.for_trade(t.tranche_id)[0]
    f = h.store.fills.for_trade(t.tranche_id)[0]
    respx.post(PRIV + "QueryOrders").mock(return_value=_ok({o.txid: {
        "status": "closed", "vol": str(o.qty), "vol_exec": str(o.filled_qty), "cl_ord_id": o.cl_ord_id}}))
    respx.post(PRIV + "QueryTrades").mock(return_value=_ok({f["exec_id"]: {
        "price": f["price"], "vol": f["qty"], "fee": f["fee"], "ordertxid": o.txid, "pair": "XXBTZEUR"}}))
    respx.post(PRIV + "BalanceEx").mock(return_value=_ok({"XXBT": {"balance": balance, "hold_trade": "0"},
                                                          "ZEUR": {"balance": "996.58", "hold_trade": "0"}}))


async def _verified_setup(tmp_path):
    log: list[str] = []
    h = make(tmp_path, mode=Mode.LIVE, dms=FakeDms(log), env_extra=KEYS)
    CanaryRepo(h.store).set_baseline_once("XXBT", Decimal("0.5"), "run-test")
    t = await _canary_week(h, 0)
    assert t.status == "filled" and t.qty == Decimal("0.00006")
    return h, t


async def test_canary_verify_requires_yes_then_clears_the_halt(tmp_path):

    h, t = await _verified_setup(tmp_path)
    try:
        with respx.mock:
            _mock_kraken(h, t, balance="0.50006")
            code, out = cli(h, "canary-verify", t.tranche_id)
            assert code == 2 and "re-run with --yes" in out and out.count(" OK") == 4
            assert CanaryRepo(h.store).checks(t.tranche_id) == [] and h.store.risk.get("dca", Mode.LIVE).halted
            code, out = cli(h, "canary-verify", t.tranche_id, "--yes")
        assert code == 0 and "halt cleared: True" in out and "streak 1/3" in out and "minimum size" in out
        assert CanaryRepo(h.store).checks(t.tranche_id) == [True]
        assert not h.store.risk.get("dca", Mode.LIVE).halted
    finally:
        h.close()


async def test_canary_verify_balance_mismatch_is_recorded_and_keeps_the_halt(tmp_path):

    h, t = await _verified_setup(tmp_path)
    try:
        with respx.mock:
            _mock_kraken(h, t, balance="0.50005")  # 0.00001 BTC missing
            code, out = cli(h, "canary-verify", t.tranche_id, "--yes")
        assert code == 2 and "balance         FAILED" in out and '"expected": "0.50006"' in out
        assert CanaryRepo(h.store).checks(t.tranche_id) == [False]
        assert h.store.risk.get("dca", Mode.LIVE).halt_reason == "canary"
        assert len(h.events("CANARY_CHECK_FAILED")) == 1
    finally:
        h.close()


async def test_canary_verify_with_kraken_down_records_nothing(tmp_path):

    h, t = await _verified_setup(tmp_path)
    try:
        with respx.mock:
            _mock_kraken(h, t, balance="0.50006")
            respx.post(PRIV + "QueryOrders").mock(return_value=httpx.Response(200, json={
                "error": ["EGeneral:Permission denied"]}))
            code, out = cli(h, "canary-verify", t.tranche_id, "--yes")
        assert code == 2 and "nothing recorded" in out
        assert CanaryRepo(h.store).checks(t.tranche_id) == [] and h.store.risk.get("dca", Mode.LIVE).halted
    finally:
        h.close()
