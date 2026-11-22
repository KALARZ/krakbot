"""CLI (status / halt / resume / resolve / gate / liquidity-derive) and an App start-step-close smoke test."""

import io
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import httpx
import pytest
import respx

from krakbot.app import App, LiveGateError
from krakbot.cli import EXIT_OK, EXIT_REFUSED, main
from krakbot.config import load_settings
from krakbot.domain import Mode, OrderRole, OrderStatus, OrderType, Side
from krakbot.kraken_rest.errors import OutcomeUnknownError
from krakbot.kraken_ws import OrderBook
from krakbot.monitor.alerts import MemoryAlerts
from krakbot.state import connect, migrate
from krakbot.state.canary import CanaryRepo
from krakbot.state.gates import GateResult
from krakbot.state.repo import Store
from tests.unit.dca_harness import MONDAY_10, make


def cli(h, *args, now=None):
    """Run the CLI as a separate invocation would: own thread, own event loop, own DB connection."""
    out = io.StringIO()
    argv = ["--config", str(h.settings.root / "config.toml"), "--env", str(h.settings.root / ".env"), *args]
    with ThreadPoolExecutor(max_workers=1) as ex:
        code = ex.submit(main, argv, out=out, now_ms=(lambda: now) if now is not None else h.clock).result()
    return code, out.getvalue()


async def _unknown_order(h):
    """An ambiguous send recorded by the engine whose process then died (the paper book is gone with it)."""
    await h.tick()
    h.broker.fail_next_place = OutcomeUnknownError("AddOrder", ["EGeneral:Internal error"])
    await h.tick()
    o = h.orders()[0]
    assert o.status == OrderStatus.UNKNOWN and h.store.risk.get("dca", Mode.DRY_RUN).halted
    return o


@pytest.fixture
def h(tmp_path):
    hh = make(tmp_path)
    yield hh
    hh.close()


async def test_resolve_refuses_without_evidence_and_keeps_the_halt(h):
    o = await _unknown_order(h)
    code, out = cli(h, "resolve", o.cl_ord_id)  # 1 s after the send: absence cannot be proven yet
    assert code == EXIT_REFUSED and "REFUSED: no evidence" in out
    assert h.store.risk.get("dca", Mode.DRY_RUN).halted
    assert h.store.orders.by_cl_ord_id(o.cl_ord_id).status == OrderStatus.UNKNOWN


async def test_resolve_dry_records_evidence_only(h):
    o = await _unknown_order(h)
    code, out = cli(h, "resolve", o.cl_ord_id, "--dry", now=o.created_at + 120_000)
    assert code == EXIT_OK and "evidence: absent" in out and "evidence_only" in out
    assert h.store.orders.by_cl_ord_id(o.cl_ord_id).status == OrderStatus.UNKNOWN
    assert h.store.risk.get("dca", Mode.DRY_RUN).halted


async def test_resolve_with_proof_of_absence_clears_the_halt(h):
    o = await _unknown_order(h)
    code, out = cli(h, "resolve", o.cl_ord_id, now=o.created_at + 120_000)
    assert code == EXIT_OK and "rejected_absent" in out and "halt cleared: True" in out
    assert h.store.orders.by_cl_ord_id(o.cl_ord_id).status == OrderStatus.REJECTED
    assert not h.store.risk.get("dca", Mode.DRY_RUN).halted
    code, out = cli(h, "resolve", o.cl_ord_id)
    assert code == EXIT_OK and "nothing to resolve" in out


async def test_operator_override_needs_reason_and_confirmation(h):
    o = await _unknown_order(h)
    code, out = cli(h, "resolve", o.cl_ord_id, "--operator-confirms-absent")
    assert code == EXIT_REFUSED and "requires --reason" in out
    code, out = cli(h, "resolve", o.cl_ord_id, "--operator-confirms-absent", "--reason", "checked kraken.com")
    assert code == EXIT_REFUSED and "--yes" in out and h.store.risk.get("dca", Mode.DRY_RUN).halted
    code, out = cli(h, "resolve", o.cl_ord_id, "--operator-confirms-absent", "--reason", "checked kraken.com", "--yes")
    assert code == EXIT_OK and "rejected_by_operator" in out
    assert not h.store.risk.get("dca", Mode.DRY_RUN).halted
    ev = h.events("RESOLVED_BY_OPERATOR")
    assert len(ev) == 1 and "checked kraken.com" in ev[0]["payload_json"]


async def test_resume_never_clears_an_unknown_order_halt(h):
    o = await _unknown_order(h)
    code, out = cli(h, "resume")
    assert code == EXIT_REFUSED and "krakbot resolve" in out and h.store.risk.get("dca", Mode.DRY_RUN).halted
    code, out = cli(h, "status")
    assert code == EXIT_OK and f"UNRESOLVED order {o.cl_ord_id}" in out and "reason=unknown_order" in out


async def test_manual_halt_and_resume(h):
    code, _ = cli(h, "halt", "--reason", "holiday")
    assert code == EXIT_OK and h.store.risk.get("dca", Mode.DRY_RUN).halt_reason == "manual"
    code, _ = cli(h, "resume")
    assert code == EXIT_OK and not h.store.risk.get("dca", Mode.DRY_RUN).halted
    assert len(h.events("RESUMED_BY_OPERATOR")) == 1


async def test_resolve_live_order_without_keys_is_refused(h):
    await h.tick()
    t = h.dca.open_tranche(Mode.DRY_RUN)
    h.store.trades.create("dca-live-x", strategy="dca", mode=Mode.LIVE, pair="XBT/EUR", qty_target=Decimal(0))
    h.store.orders.intent(local_id="L-live", trade_id="dca-live-x", mode=Mode.LIVE, role=OrderRole.DCA,
                          cl_ord_id="live-0001", pair="XBT/EUR", side=Side.BUY, ordertype=OrderType.LIMIT,
                          qty=Decimal("0.0003"), price=Decimal("55000.0"), post_only=True)
    code, out = cli(h, "resolve", "live-0001")
    assert code == EXIT_REFUSED and "needs the Kraken API key" in out
    assert t is not None


async def test_gate_and_liquidity_derive_refuse_without_clean_days(h):
    code, out = cli(h, "gate")
    assert code == EXIT_REFUSED and '"allowed": false' in out
    code, out = cli(h, "liquidity-derive", "--yes")
    assert code == EXIT_REFUSED and "not all clean DRY_RUN days" in out


def test_run_live_requires_both_flags(cfg_dir):
    out = io.StringIO()
    code = main(["--config", str(cfg_dir / "config.toml"), "--env", str(cfg_dir / ".env"), "run", "--live"], out=out)
    assert code == EXIT_REFUSED and "--i-understand-live" in out.getvalue()


def test_flatten_never_sells_dca(cfg_dir):
    out = io.StringIO()
    code = main(["--config", str(cfg_dir / "config.toml"), "--env", str(cfg_dir / ".env"), "flatten"], out=out)
    assert code == EXIT_OK and "never sold" in out.getvalue()


# ------------------------------------------------------------------------------------------------------------ app
ASSET_PAIRS = {"XXBTZEUR": {"altname": "XBTEUR", "wsname": "XBT/EUR", "base": "XXBT", "quote": "ZEUR",
                            "pair_decimals": 1, "lot_decimals": 8, "cost_decimals": 5, "tick_size": "0.1",
                            "ordermin": "0.00006", "costmin": "0.45", "status": "online"},
               "XETHZEUR": {"altname": "ETHEUR", "wsname": "ETH/EUR", "base": "XETH", "quote": "ZEUR",
                            "pair_decimals": 2, "lot_decimals": 8, "cost_decimals": 5, "tick_size": "0.01",
                            "ordermin": "0.002", "costmin": "0.45", "status": "online"}}
ASSET_PAIRS_V1 = {"BTC/EUR": {"altname": "XBTEUR"}, "ETH/EUR": {"altname": "ETHEUR"}}
TICKER = {"XXBTZEUR": {"a": ["55597.9", "1", "1.0"], "b": ["55566.8", "1", "1.0"], "c": ["55580.0", "0.1"]}}


def _mock_public():
    def asset_pairs(request):
        v1 = request.url.params.get("assetVersion") == "1"
        return httpx.Response(200, json={"error": [], "result": ASSET_PAIRS_V1 if v1 else ASSET_PAIRS})
    respx.get("https://api.kraken.com/0/public/AssetPairs").mock(side_effect=asset_pairs)
    respx.get("https://api.kraken.com/0/public/Ticker").mock(
        return_value=httpx.Response(200, json={"error": [], "result": TICKER}))


@respx.mock
async def test_app_start_step_heartbeat_and_clean_close(cfg_dir):
    _mock_public()
    settings = load_settings(cfg_dir / "config.toml", cfg_dir / ".env")
    now = {"t": MONDAY_10 + 30_000}
    async with httpx.AsyncClient() as http:
        alerts = MemoryAlerts(lambda: now["t"])
        app = App(settings, live=False, http=http, now_ms=lambda: now["t"], alerts=alerts)
        await app.start()
        assert set(app.instruments) == {"XBT/EUR", "ETH/EUR"}  # missing sample pairs are skipped with a warning
        assert app.plan.nominal == Decimal("19.23") and app.engine is not None
        await app.step()  # no WS book yet: REST fallback only -> not healthy, no heartbeat
        assert not app.healthy and app.store.conn.execute("SELECT COUNT(*) FROM heartbeats").fetchone()[0] == 0
        for _ in range(3):
            now["t"] += 1_000
            b = OrderBook("BTC/EUR", 10)
            b.snapshot([{"price": Decimal("55566.8"), "qty": Decimal("1")}],
                       [{"price": Decimal("55597.9"), "qty": Decimal("1")}])
            app.md.on_book("BTC/EUR", b)
            await app.step()
        assert app.healthy and app.loop_errors == 0
        assert app.store.conn.execute("SELECT COUNT(*) FROM heartbeats").fetchone()[0] == 1
        assert app.store.conn.execute("SELECT COUNT(*) FROM recon_checks WHERE ok = 1").fetchone()[0] == 1
        assert app.dca.open_tranche(Mode.DRY_RUN) is not None and len(app.store.orders.working(Mode.DRY_RUN)) == 1
        await app.close(clean=True, reason="test")
    conn = connect(settings.db_path)
    try:
        r = conn.execute("SELECT clean_exit, modes_json FROM runs").fetchone()
        assert r[0] == 1 and '"dca": "dry_run"' in r[1]
        assert Store(conn).orders.working(Mode.LIVE) == []  # nothing ever left the paper broker
    finally:
        conn.close()


@respx.mock
async def test_app_live_is_refused_before_any_network_call_when_config_is_dry_run(cfg_dir):
    settings = load_settings(cfg_dir / "config.toml", cfg_dir / ".env")
    async with httpx.AsyncClient() as http:
        app = App(settings, live=True, http=http)
        with pytest.raises(LiveGateError, match="mode is not 'live'"):
            await app.start()
        app.conn.close()
    assert len(respx.calls) == 0


@respx.mock
async def test_app_refreshes_kraken_minimums_every_24h(cfg_dir):
    _mock_public()
    settings = load_settings(cfg_dir / "config.toml", cfg_dir / ".env")
    now = {"t": MONDAY_10 - 3_600_000}  # outside the DCA window: only the refresh is exercised
    async with httpx.AsyncClient() as http:
        app = App(settings, live=False, http=http, now_ms=lambda: now["t"], alerts=MemoryAlerts(lambda: now["t"]))
        await app.start()
        assert app.inst.ordermin == Decimal("0.00006")
        ASSET_PAIRS["XXBTZEUR"]["ordermin"] = "0.0001"
        try:
            now["t"] += 3_600_000
            await app.step()
            assert app.inst.ordermin == Decimal("0.00006")  # not yet 24 h
            now["t"] += 86_400_000
            await app.step()
            assert app.inst.ordermin == Decimal("0.0001") and app.engine.inst.ordermin == Decimal("0.0001")
            assert app.broker._inst["XBT/EUR"].ordermin == Decimal("0.0001")
            assert len(app.store.events.by_kind("INSTRUMENTS_CHANGED")) == 1
        finally:
            ASSET_PAIRS["XXBTZEUR"]["ordermin"] = "0.00006"
            await app.close(clean=True, reason="test")


@respx.mock
async def test_app_live_wiring_records_the_balance_baseline_once_and_disarms_the_dms(cfg_dir, monkeypatch):
    """LIVE wiring with Kraken mocked (the gate itself is tested in test_gates): no order is placed here."""
    _mock_public()
    priv = "https://api.kraken.com/0/private/"
    dms = respx.post(priv + "CancelAllOrdersAfter").mock(return_value=httpx.Response(200, json={
        "error": [], "result": {"currentTime": "x", "triggerTime": "0"}}))
    respx.post(priv + "TradeVolume").mock(return_value=httpx.Response(200, json={"error": [], "result": {
        "fees_maker": {"XXBTZEUR": {"fee": "0.2500"}}}}))
    respx.post(priv + "BalanceEx").mock(return_value=httpx.Response(200, json={"error": [], "result": {
        "XXBT": {"balance": "0.12345678", "hold_trade": "0"}}}))
    (cfg_dir / ".env").write_text("CAPITAL=5000\nKRAKEN_API_KEY=TESTKEY-NOT-REAL-0123456789\nKRAKEN_API_SECRET="
                                  "kQH5HW/8p1uGOVjbgWA7FunAmGO8lsSUXNsu3eow76sz84Q18fWxnyRzBHCd3pd5nE9qa99HAZtuZuj6F1huXg=="
                                  "\nKRAKBOT_LIVE_ENABLED=true\n")
    cfg = (cfg_dir / "config.toml").read_text().replace('mode = "dry_run"                  # off | dry_run | live',
                                                         'mode = "live"', 1)
    (cfg_dir / "config.toml").write_text(cfg)
    settings = load_settings(cfg_dir / "config.toml", cfg_dir / ".env")
    monkeypatch.setattr(App, "_check_live_allowed", lambda self: None)
    now = {"t": MONDAY_10 - 3_600_000}
    for balance_seen in ("0.12345678", "0.99"):
        async with httpx.AsyncClient() as http:
            app = App(settings, live=True, http=http, now_ms=lambda: now["t"], alerts=MemoryAlerts(lambda: now["t"]))
            await app.start()
            assert app.engine is not None and app.engine.dms is not None and app._fee == Decimal("0.0025")
            assert CanaryRepo(app.store).baseline("XXBT") == Decimal("0.12345678")  # first start only
            await app.close(clean=True, reason="test")
        respx.post(priv + "BalanceEx").mock(return_value=httpx.Response(200, json={"error": [], "result": {
            "XXBT": {"balance": balance_seen, "hold_trade": "0"}}}))
    assert [dict(urllib.parse.parse_qsl(c.request.content.decode()))["timeout"] for c in dms.calls] == ["0", "0"]
    assert not any("AddOrder" in str(c.request.url) for c in respx.calls)



@respx.mock
async def test_heartbeat_marks_every_healthy_minute_and_watchdog_ignores_market_health(cfg_dir, monkeypatch):
    _mock_public()
    sent = []
    monkeypatch.setattr("krakbot.app.sd_notify", lambda state: sent.append(state) or True)
    settings = load_settings(cfg_dir / "config.toml", cfg_dir / ".env")
    now = {"t": MONDAY_10 - 3_600_000}
    async with httpx.AsyncClient() as http:
        app = App(settings, live=False, http=http, now_ms=lambda: now["t"], alerts=MemoryAlerts(lambda: now["t"]))
        await app.start()
        for _ in range(3 * 3600):  # 3 hours of 1.0x-second loop steps with a slowly drifting period
            now["t"] += 1_003
            b = OrderBook("BTC/EUR", 10)
            b.snapshot([{"price": Decimal("55566.8"), "qty": Decimal("1")}],
                       [{"price": Decimal("55597.9"), "qty": Decimal("1")}])
            app.md.on_book("BTC/EUR", b)
            await app.step()
        minutes = app.store.conn.execute("SELECT MIN(minute), MAX(minute), COUNT(*) FROM heartbeats").fetchone()
        assert minutes[2] == minutes[1] - minutes[0] + 1  # no gap: every minute counted
        healthy_pings = sum(1 for x in sent if x.startswith("WATCHDOG=1"))
        app.md.on_book("BTC/EUR", None)  # WS book lost: degraded, yet the watchdog keeps being fed
        for _ in range(120):
            now["t"] += 1_000
            await app.step()
        assert not app.healthy
        assert sum(1 for x in sent if x.startswith("WATCHDOG=1")) >= healthy_pings + 11
        assert any("degraded" in x for x in sent)
        assert app.store.conn.execute("SELECT COUNT(*) FROM heartbeats").fetchone()[0] == minutes[2]
        await app.close(clean=True, reason="test")
    assert sent[0] == "READY=1" and sent[-1] == "STOPPING=1"



async def test_backup_is_consistent_and_never_overwrites(h, tmp_path):
    await h.tick()
    dest = tmp_path / "bk" / "copy.db"
    dest.parent.mkdir()
    code, out = cli(h, "backup", str(dest))
    assert code == EXIT_OK and "integrity_check: ok" in out and oct(dest.stat().st_mode & 0o777) == "0o600"
    c = connect(dest)
    try:
        assert c.execute("SELECT COUNT(*) FROM dca_tranches").fetchone()[0] == 1
    finally:
        c.close()
    code, out = cli(h, "backup", str(dest))
    assert code == EXIT_REFUSED and "never overwritten" in out



async def test_live_gate_is_a_one_time_unlock(cfg_dir, monkeypatch):
    (cfg_dir / ".env").write_text("CAPITAL=5000\nKRAKEN_API_KEY=TESTKEY-NOT-REAL-0123456789\nKRAKEN_API_SECRET="
                                  "kQH5HW/8p1uGOVjbgWA7FunAmGO8lsSUXNsu3eow76sz84Q18fWxnyRzBHCd3pd5nE9qa99HAZtuZuj6F1huXg=="
                                  "\nKRAKBOT_LIVE_ENABLED=true\n")
    cfg = (cfg_dir / "config.toml").read_text().replace('mode = "dry_run"                  # off | dry_run | live',
                                                         'mode = "live"', 1)
    (cfg_dir / "config.toml").write_text(cfg)
    settings = load_settings(cfg_dir / "config.toml", cfg_dir / ".env")
    verdict = {"allowed": False}

    def fake_gate(conn, now_ms, *, pairs, rule=None, current_run_id=None):
        return GateResult(verdict["allowed"], [] if verdict["allowed"] else ["day 1: not clean"], [], (0, 1))

    monkeypatch.setattr("krakbot.app.dca_live_gate", fake_gate)
    async with httpx.AsyncClient() as http:
        app = App(settings, live=True, http=http)
        app.conn = connect(settings.db_path)
        migrate(app.conn)
        app.store = Store(app.conn, app.now)
        with pytest.raises(LiveGateError):
            app._check_live_allowed()
        verdict["allowed"] = True
        app._check_live_allowed()  # first pass: unlock recorded
        assert len(app.store.events.by_kind("LIVE_UNLOCKED")) == 1
        verdict["allowed"] = False  # e.g. the window now contains LIVE runs
        app._check_live_allowed()   # a restart after the unlock is not refused
        assert len(app.store.events.by_kind("LIVE_UNLOCKED")) == 1
        app.conn.close()
