"""Logs redaction, alert sinks, heartbeat ping and DCA reconciliation."""

import io
import json
import logging
from decimal import Decimal

import httpx
import pytest
import respx
from pydantic import SecretStr

from krakbot.domain import Mode, OrderStatus
from krakbot.execution.recon import reconcile_dca
from krakbot.monitor import logs
from krakbot.monitor.alerts import AlertLevel, MemoryAlerts, TelegramAlerts
from krakbot.monitor.heartbeat import ExternalPing, sd_notify
from tests.unit.dca_harness import make

TOKEN = "123456789:AAH-fake_token_for_tests_only_0123456789"
KEY = "TESTKEY-NOT-REAL-0123456789"
SECRET = "kQH5HW/8p1uGOVjbgWA7FunAmGO8lsSUXNsu3eow76sz84Q18fWxnyRzBHCd3pd5nE9qa99HAZtuZuj6F1huXg=="


@pytest.fixture
def json_log():
    logs._secrets.clear()
    logs.register_secret(KEY)
    logs.register_secret(SECRET)
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    h.setFormatter(logs.JsonFormatter())
    h.addFilter(logs.RedactingFilter())
    lg = logging.getLogger("krakbot.test.redaction")
    lg.addHandler(h)
    lg.setLevel(logging.INFO)
    lg.propagate = False
    yield lg, buf
    lg.removeHandler(h)
    logs._secrets.clear()


def test_secrets_never_reach_the_log_line(json_log):
    lg, buf = json_log
    lg.info("calling with key %s", KEY, extra={"cl_ord_id": "abc", "headers": f"API-Sign: {SECRET} API-Key={KEY}"})
    lg.info(f"telegram https://api.telegram.org/bot{TOKEN}/sendMessage")
    try:
        raise RuntimeError(f"boom {SECRET}")
    except RuntimeError:
        lg.exception("failed")
    out = buf.getvalue()
    assert KEY not in out and SECRET not in out and TOKEN not in out and "AAH-fake" not in out
    lines = [json.loads(x) for x in out.strip().splitlines()]
    assert lines[0]["cl_ord_id"] == "abc" and lines[0]["level"] == "INFO" and lines[0]["ts"].endswith("Z")
    assert "***" in lines[0]["msg"] and "exc" in lines[2]


def test_redact_patterns():
    assert logs.redact(f"x API-Sign: {SECRET}") == "x API-Sign: ***"
    assert logs.redact(f"bot{TOKEN}") == "***"
    assert logs.redact("plain text 12345 stays") == "plain text 12345 stays"


async def test_memory_alerts_dedup_and_repeat():
    now = {"t": 0}
    a = MemoryAlerts(lambda: now["t"])
    assert await a.send("x", key="k", repeat_after_s=60)
    assert not await a.send("x", key="k", repeat_after_s=60)
    now["t"] += 60_000
    assert await a.send("x", key="k", repeat_after_s=60)
    assert await a.send("once", key="o")
    assert not await a.send("once", key="o")  # repeat_after_s=0: once per key for the process lifetime
    assert await a.send("no key") and await a.send("no key")
    assert len(a.sent) == 5


@respx.mock
async def test_telegram_sends_and_never_logs_the_token(caplog):
    route = respx.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage").mock(return_value=httpx.Response(200))
    async with httpx.AsyncClient() as http:
        t = TelegramAlerts(http, SecretStr(TOKEN), "-100123", now_ms=lambda: 0)
        assert await t.send("hello", level=AlertLevel.CRITICAL)
        body = json.loads(route.calls.last.request.content)
        assert body["chat_id"] == "-100123" and body["text"].startswith("[CRITICAL]") and TOKEN not in body["text"]
        respx.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage").mock(side_effect=httpx.ConnectError("x"))
        with caplog.at_level(logging.DEBUG):
            assert not await t.send("again")
        assert TOKEN not in caplog.text and "AAH-fake" not in caplog.text
        assert TOKEN not in repr(t.__dict__.get("_token"))


@respx.mock
async def test_external_ping_and_sd_notify(monkeypatch):
    respx.get("https://hc.example.invalid/ping/x").mock(return_value=httpx.Response(200))
    async with httpx.AsyncClient() as http:
        assert await ExternalPing(http, SecretStr("https://hc.example.invalid/ping/x")).ping()
        assert not await ExternalPing(http, None).ping()
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    assert sd_notify("WATCHDOG=1") is False  # no systemd: silently a no-op


async def test_reconciliation_ok_then_detects_tampering(tmp_path):
    h = make(tmp_path)
    try:
        await h.tick()
        await h.tick()
        h.trade("55500.0", "1")
        await h.tick()
        ok, details = reconcile_dca(h.store, h.dca, Mode.DRY_RUN, total_budget=h.plan.total_budget)
        assert ok, details
        t = h.dca.open_tranche(Mode.DRY_RUN)
        h.store.conn.execute("UPDATE dca_tranches SET spent = '1.00' WHERE tranche_id = ?", (t.tranche_id,))
        ok, details = reconcile_dca(h.store, h.dca, Mode.DRY_RUN, total_budget=h.plan.total_budget)
        assert not ok and "!= fills" in details["problems"][0]
        rows = h.store.conn.execute("SELECT ok FROM recon_checks WHERE strategy='dca' ORDER BY ts, id").fetchall()
        assert [r[0] for r in rows] == [1, 0]
    finally:
        h.close()


async def test_reconciliation_flags_open_order_on_closed_tranche(tmp_path):
    h = make(tmp_path)
    try:
        await h.tick()
        await h.tick()
        o = h.orders()[0]
        t = h.dca.open_tranche(Mode.DRY_RUN)
        h.store.conn.execute("UPDATE dca_tranches SET status='unfilled', carry_out='19.23' WHERE tranche_id = ?",
                             (t.tranche_id,))
        ok, details = reconcile_dca(h.store, h.dca, Mode.DRY_RUN, total_budget=Decimal("1000.00"))
        assert not ok and any(o.cl_ord_id in p and str(OrderStatus.OPEN) in p for p in details["problems"])
    finally:
        h.close()


def test_extra_fields_cannot_overwrite_reserved_keys(json_log):
    lg, buf = json_log
    lg.warning("x", extra={"level": "info", "ts": "fake", "alert_level": "critical"})
    line = json.loads(buf.getvalue().strip())
    assert line["level"] == "WARNING" and line["ts"] != "fake" and line["x_level"] == "info"
    assert line["alert_level"] == "critical"
