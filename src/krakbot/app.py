"""Composition root for the running bot (single process). DCA vertical: market data (WS v2 + REST fallback),
liquidity sampling, DCA engine (paper in DRY_RUN, Kraken in LIVE), heartbeats, reconciliation.

LIVE is possible only for the DCA, only if the config asks for it, the .env interlock is on, the CLI flag was
given AND the 7-clean-DRY_RUN-days gate passes (checked again here: defense in depth)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any

import httpx

from krakbot import __version__
from krakbot.config import Settings
from krakbot.domain import EventLevel, Mode
from krakbot.execution.dca_engine import DcaEngine
from krakbot.execution.dms import DmsController
from krakbot.execution.live import KrakenBroker
from krakbot.execution.paper import PaperBroker
from krakbot.execution.recon import reconcile_dca
from krakbot.execution.types import Broker
from krakbot.kraken_rest.client import KrakenRestClient
from krakbot.kraken_rest.errors import KrakenError
from krakbot.kraken_rest.models import Instrument
from krakbot.kraken_rest.nonce import NonceSource, SqliteNonceStore
from krakbot.kraken_ws.client import PublicWs
from krakbot.market_data import LiquiditySampler, MarketData
from krakbot.monitor.alerts import AlertLevel, Alerts, NullAlerts
from krakbot.monitor.heartbeat import ExternalPing, sd_notify
from krakbot.state import connect, migrate
from krakbot.state.canary import CanaryRepo
from krakbot.state.dca import DcaRepo
from krakbot.state.gates import dca_live_gate
from krakbot.state.liquidity import LiquidityRepo
from krakbot.state.repo import Store
from krakbot.strategies.dca import DcaPlan, plan

__all__ = ["DRY_RUN_MAKER_FEE", "App", "BootAborted", "LiveGateError"]

log = logging.getLogger("krakbot.app")
DRY_RUN_MAKER_FEE = Decimal("0.0040")  # Tier 1 maker, pre-registered for DRY_RUN (plan §0.1)
WATCHDOG_EVERY_MS = 10_000  # systemd WatchdogSec=120 (deploy/systemd): 12 missed pings before a restart
DCA_PAIR = "XBT/EUR"


class LiveGateError(RuntimeError):
    pass


class BootAborted(RuntimeError):
    pass


class App:
    def __init__(self, settings: Settings, *, live: bool, http: httpx.AsyncClient,
                 ws_connect: Callable[[str], Awaitable[Any]] | None = None, now_ms: Callable[[], int] | None = None,
                 alerts: Alerts | None = None) -> None:
        self.s = settings
        self.live = live
        self.http = http
        self.ws_connect = ws_connect
        self.now: Callable[[], int] = now_ms or (lambda: time.time_ns() // 1_000_000)
        self.alerts: Alerts = alerts or NullAlerts()
        self.run_id = str(uuid.uuid4())
        self.mode = Mode.LIVE if live else Mode.DRY_RUN
        self._last = {"heartbeat": -1, "watchdog": 0, "recon": 0, "fee": 0, "rest_fallback": 0, "asset_pairs": 0}
        self.loop_errors = 0
        self.healthy = False
        self.boot_failures = 0
        self.boot_backoff_s = (5.0, 60.0)

    # ------------------------------------------------------------------ setup
    async def start(self, stop: asyncio.Event | None = None) -> None:
        """DB + run registration, then the network bootstrap. Kraken unreachable at boot (VPS reboot before the
        network is up, Kraken maintenance) must NOT crash the process: systemd would give up after 5 restarts and
        the bot would stay dead. The bootstrap is retried with backoff until it works or `stop` is set; systemd is
        told READY + STATUS meanwhile, and no heartbeat is written (the downtime counts against the uptime)."""
        self.conn = connect(self.s.db_path)
        migrate(self.conn)
        self.store = Store(self.conn, self.now)
        if self.live:
            self._check_live_allowed()
        modes = {"dca": str(self.mode), "donchian": "dry_run"}
        self.store.runs.start(self.run_id, __version__, modes)
        self.public = KrakenRestClient(http=self.http)
        self.private: KrakenRestClient | None = None
        sec = self.s.secrets
        if sec.has_kraken_keys:
            self.private = KrakenRestClient(http=self.http, api_key=sec.KRAKEN_API_KEY,
                                            api_secret=sec.KRAKEN_API_SECRET,
                                            nonce=NonceSource(SqliteNonceStore(self.conn)), trading_enabled=self.live)
        delay, (lo, hi) = 0.0, self.boot_backoff_s
        while True:
            try:
                await self._bootstrap()
                break
            except (KrakenError, OSError, httpx.HTTPError) as exc:
                delay = min(hi, max(lo, delay * 2))
                self.boot_failures += 1
                log.warning("Kraken unreachable at startup: retrying", extra={"error": str(exc)[:200],
                                                                             "retry_in_s": delay})
                sd_notify(f"READY=1\nWATCHDOG=1\nSTATUS=waiting for Kraken ({type(exc).__name__}), retry in "
                          f"{delay:.0f}s")
                if self.boot_failures == 1:
                    await self.alerts.send(f"Kraken unreachable at startup ({exc}); retrying", key="boot-wait",
                                           level=AlertLevel.WARNING, repeat_after_s=1800)
                if stop is None:
                    await asyncio.sleep(delay)
                else:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(stop.wait(), timeout=delay)
                    if stop.is_set():
                        raise BootAborted("stopped while waiting for Kraken at startup") from exc
        sd_notify("READY=1\nSTATUS=running")
        await self.alerts.send(f"started ({self.mode}), run {self.run_id[:8]}", level=AlertLevel.INFO)

    async def _bootstrap(self) -> None:
        sec = self.s.secrets
        await self._load_market()
        self.md = MarketData(self.instruments, depth_bps=self.s.app.liquidity.depth_bps, now_ms=self.now)
        self.liq = LiquidityRepo(self.conn, self.now)
        self.sampler = LiquiditySampler(self.md, self.liq, list(self.instruments),
                                        sample_seconds=self.s.app.liquidity.sample_seconds, now_ms=self.now)
        self.ws = PublicWs(self.md.ws_symbols, on_book=self.md.on_book, on_trade=self.md.on_trades,
                           on_alive=self.md.on_ws_alive, connect=self.ws_connect)
        self.dca = DcaRepo(self.store, DCA_PAIR)
        await self._build_engine()
        self.ping = ExternalPing(self.http, sec.HEALTHCHECK_URL)
        reconcile_dca(self.store, self.dca, self.mode, total_budget=self.plan.total_budget)
        self._last["recon"] = self.now()

    async def _load_market(self) -> None:
        """AssetPairs (precisions/minimums) for the sampled pairs and the DCA plan from the live minimum."""
        all_inst = await self.public.asset_pairs()
        pairs = [p for p in self.s.app.liquidity.sample_pairs if p in all_inst]
        missing = sorted(set(self.s.app.liquidity.sample_pairs) - set(pairs))
        if missing:
            log.warning("sample pairs not listed on Kraken", extra={"missing": missing})
        self.instruments: dict[str, Instrument] = {p: all_inst[p] for p in pairs}
        self.inst = self.instruments[DCA_PAIR]
        self._last["asset_pairs"] = self.now()
        ticker = await self.public.ticker([self.inst.altname])
        price = next(iter(ticker.values())).mid
        cfg = self.s.app.strategies.dca
        self.plan: DcaPlan = plan(capital=self.s.secrets.CAPITAL, allocation=cfg.allocation, tranches=cfg.tranches,
                                  min_cost=self.inst.min_cost(price), safety=cfg.min_tranche_safety_factor)
        if not self.plan.enabled:
            await self.alerts.send(f"DCA disabled: {self.plan.disabled_reason}", level=AlertLevel.WARNING)
        elif self.plan.reduced:
            await self.alerts.send(f"DCA: capital allows only {self.plan.tranches} tranches of {self.plan.nominal} EUR "
                                   f"(configured {cfg.tranches})", level=AlertLevel.WARNING)

    async def _build_engine(self) -> None:
        self._fee = DRY_RUN_MAKER_FEE
        self.engine: DcaEngine | None = None
        if not self.live and self.s.app.strategies.dca.mode == Mode.OFF:
            log.warning("DCA mode is 'off': market data and liquidity sampling only")
            return
        dms: DmsController | None = None
        broker: Broker
        if self.live:
            assert self.private is not None
            exe = self.s.app.execution
            dms = DmsController(self.private, mode=exe.dms_mode, timeout_s=exe.dms_timeout_seconds,
                                refresh_s=exe.dms_refresh_seconds, now_ms=self.now)
            await dms.disarm()  # plan §4.6 step 4: never let a stale timer cancel what recovery is about to place
            broker = KrakenBroker(self.private, self.instruments, now_ms=self.now)
            await self._refresh_fee()
            # plan §5.3: the balance check of every canary is (balance - baseline) == bought qty
            bal = await self.private.balance_ex()
            base = CanaryRepo(self.store).set_baseline_once(
                self.inst.base, bal.get(self.inst.base, {}).get("balance", Decimal(0)), self.run_id)
            log.warning("live baseline", extra={"asset": self.inst.base, "baseline": str(base)})
        else:
            broker = PaperBroker(self.md, self.instruments, fee_rate=DRY_RUN_MAKER_FEE, now_ms=self.now)
        self.broker = broker
        self.engine = DcaEngine(settings=self.s, store=self.store, dca=self.dca, liq=self.liq, md=self.md,
                                broker=broker, inst=self.inst, mode=self.mode, plan=self.plan,
                                fee_rate=lambda: self._fee, alerts=self.alerts, now_ms=self.now,
                                poll_ms=10_000 if self.live else 1_000, dms=dms)

    def _check_live_allowed(self) -> None:
        if self.s.app.strategies.dca.mode != Mode.LIVE:
            raise LiveGateError("config [strategies.dca].mode is not 'live'")
        if not (self.s.secrets.has_kraken_keys and self.s.secrets.KRAKBOT_LIVE_ENABLED):
            raise LiveGateError("live requires Kraken keys and KRAKBOT_LIVE_ENABLED=true")
        if self.store.events.by_kind("LIVE_UNLOCKED"):
            return  # the 7-day gate is a one-time unlock: later LIVE restarts (crash, deploy) must not be refused
        g = dca_live_gate(self.conn, self.now(), pairs=list(self.s.app.liquidity.sample_pairs))
        if not g.allowed:
            raise LiveGateError("7-day DRY_RUN gate not passed: " + "; ".join(g.reasons[:10]))
        self.store.events.append("LIVE_UNLOCKED", EventLevel.CRITICAL,
                                 {"window": list(g.window), "days": [d.day_start_ms for d in g.days]},
                                 strategy="dca", mode=Mode.LIVE)

    async def _refresh_fee(self) -> None:
        if self.private is None:
            return
        try:
            tv = await self.private.trade_volume([self.inst.altname])
            maker = tv.get("fees_maker", {}) or {}
            entry = maker.get(self.inst.rest_key) or next(iter(maker.values()), None)
            if entry is not None:
                self._fee = Decimal(str(entry["fee"])) / 100
        except KrakenError as exc:
            log.warning("TradeVolume failed; keeping the previous maker fee", extra={"error": str(exc)})
        self._last["fee"] = self.now()

    async def _refresh_instruments(self) -> None:
        """Plan §0.1: ordermin/costmin/tick/lot re-read from AssetPairs every 24 h, never constants."""
        self._last["asset_pairs"] = self.now()
        try:
            fresh = await self.public.asset_pairs()
        except KrakenError as exc:
            log.warning("AssetPairs refresh failed; keeping the current instruments", extra={"error": str(exc)})
            return
        changed = {p: fresh[p] for p in self.instruments if p in fresh and fresh[p] != self.instruments[p]}
        if not changed:
            return
        self.instruments.update(changed)
        self.md.update_instruments(changed)
        self.broker.update_instruments(changed)
        if DCA_PAIR in changed:
            self.inst = changed[DCA_PAIR]
            if self.engine is not None:
                self.engine.inst = self.inst
        self.store.events.append("INSTRUMENTS_CHANGED", EventLevel.WARNING,
                                 {p: {"ordermin": i.ordermin, "costmin": i.costmin, "tick": i.tick_size,
                                      "lot_decimals": i.lot_decimals, "status": i.status} for p, i in changed.items()})
        await self.alerts.send(f"Kraken instrument parameters changed: {sorted(changed)}", level=AlertLevel.WARNING)

    # ------------------------------------------------------------------ loop
    async def step(self) -> None:
        now = self.now()
        try:
            if self.md.needs_rest_fallback(DCA_PAIR) and now - self._last["rest_fallback"] >= 5_000:
                self._last["rest_fallback"] = now
                try:
                    t = await self.public.ticker([self.inst.altname])
                    self.md.on_rest_ticker(DCA_PAIR, next(iter(t.values())))
                except KrakenError as exc:
                    log.warning("ticker fallback failed", extra={"error": str(exc)})
            if self.engine is not None:
                await self.engine.tick()
            self.sampler.tick()
            if now - self._last["asset_pairs"] >= 86_400_000:
                await self._refresh_instruments()
            if self.live and now - self._last["fee"] >= 3_600_000:
                await self._refresh_fee()
            if now - self._last["recon"] >= 3_600_000:
                ok, details = reconcile_dca(self.store, self.dca, self.mode, total_budget=self.plan.total_budget)
                self._last["recon"] = now
                if not ok:
                    self.store.risk.halt("dca", self.mode, "recon")
                    await self.alerts.send(f"RECONCILIATION FAILED: {details['problems'][:3]}",
                                           level=AlertLevel.CRITICAL, key="recon", repeat_after_s=900)
            self.healthy = self.md.top(DCA_PAIR, require_ws=True) is not None
        except Exception as exc:
            self.loop_errors += 1
            self.healthy = False
            log.exception("main loop error")
            await self.alerts.send(f"main loop error: {type(exc).__name__}: {exc}", level=AlertLevel.CRITICAL,
                                   key=f"loop:{type(exc).__name__}", repeat_after_s=900)
        # systemd watchdog = the loop is alive (NOT market health: a Kraken WS outage must not cause restart loops)
        if now - self._last["watchdog"] >= WATCHDOG_EVERY_MS:
            self._last["watchdog"] = now
            sd_notify(f"WATCHDOG=1\nSTATUS={'healthy' if self.healthy else 'degraded (no fresh WS book)'}")
        # uptime of the clean-day rule: one mark per UTC minute in which the loop ran healthy at least once
        # (minute-aligned: a ">= 60 s since the last write" rule would skip ~1 minute in 60 and fail every day)
        minute = now // 60_000
        if self.healthy and minute != self._last["heartbeat"]:
            self._last["heartbeat"] = minute
            self.store.runs.heartbeat(self.run_id)
            await self.ping.ping()

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
        await self.alerts.send(f"stopped ({reason})", level=AlertLevel.WARNING if not clean else AlertLevel.INFO)
        self.conn.close()
