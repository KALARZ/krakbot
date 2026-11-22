"""Shadow run (plan §5.4): the DCA rules on a compressed calendar, in DRY_RUN, as a SEPARATE process.

Isolation from the main instance and from the 7-day gate, by construction:
  * its own SQLite file (Settings.shadow_db_path; the config refuses a shared data_dir): runs, heartbeats,
    tranches, orders and reconciliations of the shadow never enter the database the gate reads;
  * ZERO REST calls: no HTTP client exists in this process. Instrument reference data (tick, lot, minimums) comes
    from the WS v2 `instrument` channel; there is no ticker fallback and no AssetPairs refresh. The public REST limit
    is per IP and shared with the main instance, which must not lose a single call;
  * one WebSocket connection with a small, slow connection budget (Cloudflare bans an IP for 10 minutes after ~150
    attempts in 10 minutes; the ban would also hit the main instance);
  * no liquidity sampling, paper broker only, no alerts to Telegram (log lines only).
Same DCA rules and the same nominal tranche as [strategies.dca]; hourly windows instead of weekly.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any

from krakbot import __version__
from krakbot.app import DCA_PAIR, DRY_RUN_MAKER_FEE, WATCHDOG_EVERY_MS
from krakbot.config import Settings
from krakbot.domain import Mode
from krakbot.execution.dca_engine import DcaEngine
from krakbot.execution.paper import PaperBroker
from krakbot.execution.recon import reconcile_dca
from krakbot.kraken_rest.models import Instrument, instrument_from_ws_v2
from krakbot.kraken_ws.client import PublicWs
from krakbot.market_data import MarketData
from krakbot.monitor.alerts import Alerts, NullAlerts
from krakbot.monitor.heartbeat import sd_notify
from krakbot.state import connect, migrate
from krakbot.state.dca import DcaRepo
from krakbot.state.liquidity import LiquidityRepo
from krakbot.state.repo import Store
from krakbot.strategies.dca import DcaPlan, periodic_window, plan

__all__ = ["DCA_WS_SYMBOL", "ShadowApp", "ShadowIsolationError"]

log = logging.getLogger("krakbot.shadow")
DCA_WS_SYMBOL = "BTC/EUR"  # WS v2 symbol of XBT/EUR (the main app reads the same mapping from AssetPairs v1)
_PLACEHOLDER = Instrument(rest_key="", altname="", wsname=DCA_PAIR, base="BTC", quote="EUR", pair_decimals=1,
                          lot_decimals=8, cost_decimals=5, tick_size=Decimal("0.1"), ordermin=Decimal("1"),
                          costmin=Decimal("1"), status="unknown", symbol=DCA_WS_SYMBOL)


class ShadowIsolationError(RuntimeError):
    pass


class ShadowApp:
    def __init__(self, settings: Settings, *, ws_connect: Callable[[str], Awaitable[Any]] | None = None,
                 now_ms: Callable[[], int] | None = None, alerts: Alerts | None = None) -> None:
        self.s = settings
        self.cfg = settings.app.shadow
        self.db_path = settings.shadow_db_path
        if self.db_path.resolve() == settings.db_path.resolve():
            raise ShadowIsolationError("the shadow must never use the main database")
        self.ws_connect = ws_connect
        self.now: Callable[[], int] = now_ms or (lambda: time.time_ns() // 1_000_000)
        self.alerts: Alerts = alerts or NullAlerts()
        self.run_id = str(uuid.uuid4())
        self.inst: Instrument | None = None
        self.engine: DcaEngine | None = None
        self.plan: DcaPlan | None = None
        self.healthy = False
        self.loop_errors = 0
        self._last = {"heartbeat": -1, "watchdog": 0, "recon": 0}

    async def start(self) -> None:
        self.conn = connect(self.db_path)
        migrate(self.conn)
        self.store = Store(self.conn, self.now)
        self.store.runs.start(self.run_id, __version__, {"dca": "dry_run", "shadow": "hourly"})
        self.md = MarketData({DCA_PAIR: _PLACEHOLDER}, depth_bps=self.s.app.liquidity.depth_bps, now_ms=self.now)
        self.broker = PaperBroker(self.md, {DCA_PAIR: _PLACEHOLDER}, fee_rate=DRY_RUN_MAKER_FEE, now_ms=self.now)
        self.dca = DcaRepo(self.store, DCA_PAIR)
        self.liq = LiquidityRepo(self.conn, self.now)  # stays empty: no sampling in the shadow, no spread pause
        self.ws = PublicWs([DCA_WS_SYMBOL], on_book=self.md.on_book, on_trade=self.md.on_trades,
                           on_alive=self.md.on_ws_alive, on_instrument=self._on_instrument,
                           depth=self.cfg.book_depth, connect=self.ws_connect,
                           max_attempts_per_window=self.cfg.max_connects_per_10min,
                           min_backoff_s=float(self.cfg.min_reconnect_seconds))
        sd_notify("READY=1")
        log.warning("shadow started", extra={"db": str(self.db_path), "period_min": self.cfg.period_minutes,
                                             "window_min": self.cfg.window_minutes})

    # ------------------------------------------------------------------ instrument reference data (WS only)
    def _on_instrument(self, pair: dict[str, Any]) -> None:
        if pair.get("symbol") != DCA_WS_SYMBOL:
            return
        inst = instrument_from_ws_v2(pair, DCA_PAIR)
        if inst == self.inst:
            return
        self.inst = inst
        self.md.update_instruments({DCA_PAIR: inst})
        self.broker.update_instruments({DCA_PAIR: inst})
        if self.engine is not None:
            self.engine.inst = inst
        log.warning("shadow instrument", extra={"ordermin": str(inst.ordermin), "costmin": str(inst.costmin),
                                                "tick": str(inst.tick_size), "status": inst.status})

    def _build_engine(self) -> None:
        top = self.md.top(DCA_PAIR, require_ws=True)
        if self.inst is None or top is None:
            return  # waiting for the instrument snapshot and a first valid book
        cfg = self.s.app.strategies.dca
        min_cost = self.inst.min_cost(top.mid)
        real = plan(capital=self.s.secrets.CAPITAL, allocation=cfg.allocation, tranches=cfg.tranches,
                    min_cost=min_cost, safety=cfg.min_tranche_safety_factor)
        if not real.enabled:
            log.error("shadow: the real DCA plan is disabled", extra={"reason": real.disabled_reason})
            return
        n = self.cfg.tranches  # same nominal tranche as the real DCA, n hourly tranches of paper money
        self.plan = plan(capital=real.nominal * n, allocation=Decimal(1), tranches=n, min_cost=min_cost,
                         safety=cfg.min_tranche_safety_factor)
        period_ms, window_ms = self.cfg.period_minutes * 60_000, self.cfg.window_minutes * 60_000
        self.engine = DcaEngine(settings=self.s, store=self.store, dca=self.dca, liq=self.liq, md=self.md,
                                broker=self.broker, inst=self.inst, mode=Mode.DRY_RUN, plan=self.plan,
                                fee_rate=lambda: DRY_RUN_MAKER_FEE, alerts=self.alerts, now_ms=self.now,
                                schedule=lambda now: periodic_window(now, period_ms=period_ms, window_ms=window_ms))
        reconcile_dca(self.store, self.dca, Mode.DRY_RUN, total_budget=self.plan.total_budget)
        self._last["recon"] = self.now()
        log.warning("shadow engine ready", extra={"nominal": str(self.plan.nominal), "tranches": self.plan.tranches})

    # ------------------------------------------------------------------ loop
    async def step(self) -> None:
        now = self.now()
        try:
            if self.engine is None:
                self._build_engine()
            if self.engine is not None:
                await self.engine.tick()
                if now - self._last["recon"] >= 3_600_000 and self.plan is not None:
                    reconcile_dca(self.store, self.dca, Mode.DRY_RUN, total_budget=self.plan.total_budget)
                    self._last["recon"] = now
            self.healthy = self.engine is not None and self.md.top(DCA_PAIR, require_ws=True) is not None
        except Exception:  # the shadow must never die on a bug in one tick
            self.loop_errors += 1
            self.healthy = False
            log.exception("shadow loop error")
        if now - self._last["watchdog"] >= WATCHDOG_EVERY_MS:
            self._last["watchdog"] = now
            sd_notify(f"WATCHDOG=1\nSTATUS=shadow {'healthy' if self.healthy else 'waiting/degraded'}")
        minute = now // 60_000
        if self.healthy and minute != self._last["heartbeat"]:
            self._last["heartbeat"] = minute
            self.store.runs.heartbeat(self.run_id)

    async def run(self, stop: asyncio.Event) -> None:
        ws_task = asyncio.create_task(self.ws.run(stop))
        try:
            while not stop.is_set():
                await self.step()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=1.0)
        finally:
            stop.set()
            ws_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await ws_task

    async def close(self, *, clean: bool, reason: str) -> None:
        sd_notify("STOPPING=1")
        self.store.runs.end(self.run_id, clean=clean, reason=reason)
        self.conn.close()
