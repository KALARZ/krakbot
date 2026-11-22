"""DCA engine (docs/plan.md §0.1, §4.7). One instance per mode (dry_run uses the paper broker, live the Kraken
broker). Driven by `tick()` (about once per second). Invariants:
* never a market order, never a sell; one maker post-only GTD buy at a time, re-placed for the remainder only;
* no new order while the DCA is halted; fills of already-working orders are still recorded;
* an ambiguous send (OutcomeUnknownError) -> order UNKNOWN + DCA halt + critical alert; automatic resolution only
  inside the first 2 minutes and only on evidence; afterwards the halt is persistent until `krakbot resolve`;
* a tranche is never closed while it still has unresolved or working orders.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from decimal import Decimal

from krakbot.config import Settings
from krakbot.domain import EventLevel, Mode, OrderRole, OrderStatus, OrderType, Side
from krakbot.execution.dms import DmsController
from krakbot.execution.resolve import UNRESOLVED, apply_update, resolve_order
from krakbot.execution.types import Broker, PlaceRequest
from krakbot.kraken_rest.errors import KrakenError, OutcomeUnknownError
from krakbot.kraken_rest.models import Instrument
from krakbot.market_data import MarketData
from krakbot.monitor.alerts import AlertLevel, Alerts
from krakbot.state.canary import CANARY_CLEAN_TRANCHES, CanaryRepo
from krakbot.state.dca import DcaRepo, Tranche
from krakbot.state.liquidity import LiquidityRepo
from krakbot.state.repo import Order, Store
from krakbot.strategies.dca import (
    DcaPlan,
    TrancheOutcome,
    WaitReason,
    canary_budget,
    closing_status,
    entry_price,
    fee_for,
    min_qty,
    order_qty,
    week_budget,
    window_at,
)

__all__ = ["ACTIVE_RESOLUTION_MS", "DcaEngine"]

log = logging.getLogger("krakbot.dca")
_NS = uuid.UUID("6b1f8c2e-9a51-4c3e-8f0e-2d7a4b9c1e55")
ACTIVE_RESOLUTION_MS = 120_000
RESOLVE_EVERY_MS = 5_000
EVIDENCE_EVERY_MS = 60_000
HALT_ALERT_REPEAT_S = 900
STRATEGY = "dca"


class DcaEngine:
    def __init__(self, *, settings: Settings, store: Store, dca: DcaRepo, liq: LiquidityRepo, md: MarketData,
                 broker: Broker, inst: Instrument, mode: Mode, plan: DcaPlan, fee_rate: Callable[[], Decimal],
                 alerts: Alerts, now_ms: Callable[[], int], poll_ms: int = 1_000,
                 dms: DmsController | None = None) -> None:
        self.cfg = settings.app.strategies.dca
        self.exe = settings.app.execution
        self.store, self.dca, self.liq, self.md, self.broker = store, dca, liq, md, broker
        self.inst, self.mode, self.plan, self.fee_rate, self.alerts = inst, mode, plan, fee_rate, alerts
        self.now = now_ms
        self.poll_ms = poll_ms
        self.dms = dms
        self.pair = inst.wsname
        self.last_wait: WaitReason | None = None
        self._last_poll: dict[str, int] = {}
        self._last_resolve: dict[str, int] = {}
        self._persistent_announced: set[str] = set()
        self._done_placing: set[str] = set()
        self._cooldown_until = 0
        self._announced_complete = False
        self.canary = CanaryRepo(store)

    # ------------------------------------------------------------------ helpers
    def _dca_orders(self, statuses: tuple[OrderStatus, ...]) -> list[Order]:
        marks = ",".join("?" * len(statuses))  # placeholders only: values are bound, never interpolated
        sql = ("SELECT o.cl_ord_id FROM orders o JOIN trades t ON t.trade_id = o.trade_id "  # noqa: S608
               f"WHERE t.strategy = 'dca' AND o.mode = ? AND o.status IN ({marks}) ORDER BY o.created_at")
        rows = self.store.conn.execute(sql, (str(self.mode), *[str(s) for s in statuses])).fetchall()
        return [self.store.orders.by_cl_ord_id(r[0]) for r in rows]

    def _working(self) -> list[Order]:
        return self._dca_orders((OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED))

    def _unresolved(self) -> list[Order]:
        return self._dca_orders(UNRESOLVED)

    def _min_cost(self) -> Decimal:
        top = self.md.top(self.pair)
        price = top.mid if top is not None else None
        return self.inst.min_cost(price) if price is not None else self.plan.min_cost

    async def _alert(self, text: str, level: AlertLevel = AlertLevel.INFO, key: str | None = None,
                     repeat_after_s: int = 0) -> None:
        await self.alerts.send(f"DCA {self.mode}: {text}", level=level, key=key, repeat_after_s=repeat_after_s)

    # ------------------------------------------------------------------ main loop step
    async def tick(self) -> None:
        now = self.now()
        await self._intent_leftovers()
        rs = self.store.risk.get(STRATEGY, self.mode)
        if rs.halted:
            if rs.halt_reason == "unknown_order":
                await self._halt_resolution(now)
            await self._refresh_working(now)
            await self._alert(f"HALT active ({rs.halt_reason}, ref {rs.halt_ref}): no new purchases. "
                              f"Use `krakbot status` / `krakbot resolve`.", AlertLevel.CRITICAL,
                              key=f"halt:{rs.halt_reason}:{rs.halt_ref}", repeat_after_s=HALT_ALERT_REPEAT_S)
            await self._maintain_dms()
            return
        await self._refresh_working(now)
        await self._manage_tranche(now)
        await self._maintain_dms()

    async def _maintain_dms(self) -> None:
        """LIVE (plan §5.2): armed whenever an order may rest on the book (working, in flight or ambiguous), so a
        dead bot leaves nothing behind for more than `dms_timeout_seconds`; disarmed when nothing can rest."""
        if self.dms is not None:
            may_rest = bool(self._working() or self._unresolved())
            await self.dms.maintain(entries_working=may_rest, stops_resting=False)

    async def _intent_leftovers(self) -> None:
        """An INTENT order found at tick time means a crash between write-ahead and send: ambiguous by definition."""
        for o in self._dca_orders((OrderStatus.INTENT,)):
            self.store.orders.mark(o.cl_ord_id, OrderStatus.UNKNOWN, last_error="found in INTENT state after restart")
            await self._halt_unknown(o.cl_ord_id, "intent without confirmed send (crash between write-ahead and send)")

    async def _halt_unknown(self, cl_ord_id: str, why: str) -> None:
        o = self.store.orders.by_cl_ord_id(cl_ord_id)
        self.store.events.append("ORDER_OUTCOME_UNKNOWN", EventLevel.CRITICAL,
                                 {"cl_ord_id": cl_ord_id, "why": why, "pair": o.pair, "side": str(o.side),
                                  "qty": o.qty, "price": o.price}, strategy=STRATEGY, mode=self.mode,
                                 trade_id=o.trade_id)
        rs = self.store.risk.get(STRATEGY, self.mode)
        if not rs.halted:
            self.store.risk.halt(STRATEGY, self.mode, "unknown_order", ref=cl_ord_id)
        await self._alert(f"ORDER OUTCOME UNKNOWN {cl_ord_id} ({o.side} {o.qty} {o.pair} @ {o.price}): {why}. "
                          "Purchases halted; resolving.",
                          AlertLevel.CRITICAL, key=f"unknown:{cl_ord_id}")

    async def _halt_resolution(self, now: int) -> None:
        for o in self._unresolved():
            since = o.updated_at
            if now - since <= ACTIVE_RESOLUTION_MS:
                if now - self._last_resolve.get(o.cl_ord_id, 0) < RESOLVE_EVERY_MS:
                    continue
                self._last_resolve[o.cl_ord_id] = now
                rep = await resolve_order(self.store, self.dca, self.broker, o.cl_ord_id, strategy=STRATEGY,
                                          deadline_s=self.exe.deadline_seconds)
                if rep.halt_cleared:
                    await self._alert(f"order {o.cl_ord_id} resolved ({rep.action}); halt cleared", AlertLevel.WARNING)
            else:
                if o.cl_ord_id not in self._persistent_announced:
                    self._persistent_announced.add(o.cl_ord_id)
                    self.store.events.append("HALT_PERSISTENT", EventLevel.CRITICAL, {"cl_ord_id": o.cl_ord_id},
                                             strategy=STRATEGY, mode=self.mode, trade_id=o.trade_id)
                    await self._alert(f"order {o.cl_ord_id} not resolved within 2 minutes: halt is now PERSISTENT. "
                                      f"Only `krakbot resolve {o.cl_ord_id}` can clear it.", AlertLevel.CRITICAL)
                if now - self._last_resolve.get(o.cl_ord_id, 0) >= EVIDENCE_EVERY_MS:
                    self._last_resolve[o.cl_ord_id] = now
                    await resolve_order(self.store, self.dca, self.broker, o.cl_ord_id, strategy=STRATEGY,
                                        deadline_s=self.exe.deadline_seconds, evidence_only=True)

    async def _refresh_working(self, now: int) -> None:
        for o in self._working() + self._dca_orders((OrderStatus.SENT,)):
            if o.txid is None or now - self._last_poll.get(o.cl_ord_id, 0) < self.poll_ms:
                continue
            self._last_poll[o.cl_ord_id] = now
            try:
                upd = await self.broker.refresh(o.cl_ord_id, o.txid)
            except KrakenError as exc:  # a failed READ is not ambiguity about our action: retry next poll
                log.warning("refresh failed", extra={"cl_ord_id": o.cl_ord_id, "error": str(exc)})
                continue
            before = o.filled_qty
            after = apply_update(self.store, self.dca, o, upd)
            if after.filled_qty > before:
                await self._alert(f"bought {after.filled_qty - before} BTC @ {o.price} ({after.status})")

    # ------------------------------------------------------------------ tranche lifecycle
    async def _manage_tranche(self, now: int) -> None:
        start, end = window_at(now, weekday=self.cfg.weekday, hour_utc=self.cfg.hour_utc,
                               window_hours=self.cfg.tranche_window_hours)
        t = self.dca.open_tranche(self.mode)
        if t is not None:
            await self._manage_open(t, now)
            return
        if not start <= now < end:
            return
        last = self.dca.latest(self.mode)
        if last is not None and last.window_start == start:
            return  # this window was already handled
        if not self.plan.enabled:
            await self._alert(f"disabled: {self.plan.disabled_reason}", AlertLevel.WARNING, key="dca-disabled")
            return
        spent, _ = self.dca.totals(self.mode)
        min_cost = self._min_cost()
        w = week_budget(self.plan, seq=self.dca.next_seq(self.mode), spent_before=spent,
                        max_multiple=self.cfg.max_tranche_multiple, min_cost=min_cost)
        if w.complete:
            if not self._announced_complete:
                self._announced_complete = True
                await self._alert(f"program complete: spent {spent} of {self.plan.total_budget} EUR")
            return
        tid = f"dca-{self.mode}-{start // 1000}"
        budget, canary = w.budget, False
        if self.mode == Mode.LIVE and not self.canary.phase_complete():
            top = self.md.top(self.pair)
            if top is None:
                self.last_wait = WaitReason.NO_FRESH_BOOK
                return  # the canary budget needs a real price; retry next tick (still inside the window)
            canary = True
            budget = min(w.budget, canary_budget(top.mid, max_chase=self.cfg.max_chase, fee_rate=self.fee_rate(),
                                                 inst=self.inst))
        t = self.dca.create(tranche_id=tid, mode=self.mode, window_start=start, window_end=end, nominal=w.nominal,
                            carry_in=w.carry_in, budget=budget, canary=canary)
        if budget < min_cost:
            self.dca.close(tid, str(TrancheOutcome.SKIPPED_MIN))
            await self._alert(f"tranche {t.seq} skipped: budget {budget} < Kraken minimum {min_cost:.4f} EUR",
                              AlertLevel.WARNING)
            return
        if canary:
            await self._alert(f"CANARY tranche {t.seq} opened: ONE order of Kraken's minimum size, budget {budget} EUR "
                              f"(clean canary streak {self.canary.streak()}/{CANARY_CLEAN_TRANCHES}). Watch it; "
                              "`krakbot halt --mode live --reason ...` is ready.", AlertLevel.WARNING)
            return
        await self._alert(f"tranche {t.seq} opened: budget {w.budget} EUR (carry {w.carry_in})")

    async def _manage_open(self, t: Tranche, now: int) -> None:
        if now >= t.window_end:
            await self._finalize(t, now)
            return
        if t.status == "scheduled":
            top = self.md.top(self.pair)
            if top is None:
                self.last_wait = WaitReason.NO_FRESH_BOOK
                return
            t = self.dca.start(t.tranche_id, ref_mid=top.mid)
        if self._working() or self._unresolved() or t.tranche_id in self._done_placing or now < self._cooldown_until:
            return
        await self._place(t, now)

    async def _finalize(self, t: Tranche, now: int) -> None:
        if self._unresolved():
            return  # never close a tranche whose orders are not resolved (a late fill must find it open)
        working = [o for o in self._working() if o.trade_id == t.tranche_id]
        if working:
            grace = self.cfg.reprice_seconds * 2000
            if now >= t.window_end + grace:
                for o in working:  # GTD should already have expired it: belt and braces
                    try:
                        await self.broker.cancel(o.cl_ord_id, o.txid, pair=self.pair,
                                                 order_age_s=(now - o.created_at) / 1000)
                    except KrakenError as exc:
                        log.warning("cancel failed", extra={"cl_ord_id": o.cl_ord_id, "error": str(exc)})
                    self._last_poll.pop(o.cl_ord_id, None)
            return
        t = self.dca.get(t.tranche_id)
        ever = bool(self.store.orders.for_trade(t.tranche_id))
        status = closing_status(budget=t.budget, spent=t.spent, ever_placed=ever, min_cost=self._min_cost())
        t = self.dca.close(t.tranche_id, str(status))
        level = AlertLevel.INFO if status == TrancheOutcome.FILLED else AlertLevel.WARNING
        await self._alert(f"tranche {t.seq} {status}: spent {t.spent} EUR for {t.qty} BTC; carry {t.carry_out}",
                          level)
        self._done_placing.discard(t.tranche_id)
        if t.canary:  # plan §5.3: nothing more is bought until the operator has verified this tranche
            self.store.risk.halt(STRATEGY, self.mode, "canary", ref=t.tranche_id)
            await self._alert(f"CANARY tranche {t.seq} closed ({status}, {t.qty} BTC for {t.spent} EUR). Purchases "
                              f"halted until `krakbot canary-verify {t.tranche_id} --yes` checks fill, ledger, "
                              "reconciliation and balance.", AlertLevel.CRITICAL)
        recent = self.dca.recent_closed(self.mode, self.cfg.stall_alert_weeks)
        if len(recent) == self.cfg.stall_alert_weeks and all(r.status == "unfilled" for r in recent):
            await self._alert(f"DCA_STALLED: {self.cfg.stall_alert_weeks} consecutive unfilled tranches",
                              AlertLevel.CRITICAL, key=f"stalled:{recent[0].seq}")

    async def _place(self, t: Tranche, now: int) -> None:
        top = self.md.top(self.pair)
        if top is None:
            self.last_wait = WaitReason.NO_FRESH_BOOK
            return
        price = entry_price(top.bid, self.inst)
        if t.ref_mid is not None and price > t.ref_mid * (1 + self.cfg.max_chase):
            self.last_wait = WaitReason.CHASE_CAP
            return
        set_id = self.liq.latest_set_id()
        if set_id is not None:
            th = self.liq.thresholds(set_id).get(self.pair)
            if th is not None and th.eligible and th.spread_bp_pause is not None and top.spread_bp > th.spread_bp_pause:
                self.last_wait = WaitReason.SPREAD_PAUSE
                return
        qty = order_qty(t.budget - t.spent, price, self.fee_rate(), self.inst)
        if qty is not None and t.canary:
            q_min = min_qty(price, self.inst)  # exactly Kraken's minimum, never more
            fits = q_min * price + fee_for(q_min * price, self.fee_rate(), self.inst) <= t.budget - t.spent
            qty = q_min if fits else None
        if qty is None:
            self.last_wait = WaitReason.BELOW_MINIMUM
            self._done_placing.add(t.tranche_id)
            return
        if self.dms is not None:  # arm BEFORE the send: a crash right after AddOrder must not leave a resting buy
            await self.dms.maintain(entries_working=True, stops_resting=False)
            if not self.dms.armed:
                self.last_wait = WaitReason.DMS_UNAVAILABLE
                await self._alert("dead man's switch could not be armed: no order sent", AlertLevel.WARNING,
                                  key="dms-unavailable", repeat_after_s=900)
                return
        self.last_wait = None
        attempt = len(self.store.orders.for_trade(t.tranche_id))
        cl = str(uuid.uuid5(_NS, f"{t.tranche_id}:{attempt}"))
        expire_at = now + self.cfg.reprice_seconds * 1000
        order = self.store.orders.intent(local_id=f"L-{cl}", trade_id=t.tranche_id, mode=self.mode, role=OrderRole.DCA,
                                         cl_ord_id=cl, pair=self.pair, side=Side.BUY, ordertype=OrderType.LIMIT,
                                         qty=qty, price=price, post_only=True, expire_at=expire_at, attempt=attempt)
        self.store.orders.mark(cl, OrderStatus.SENT)
        req = PlaceRequest(cl_ord_id=cl, pair=self.pair, price=price, qty=qty, expire_after_s=self.cfg.reprice_seconds,
                           deadline_s=self.exe.deadline_seconds)
        try:
            upd = await self.broker.place_post_only_buy(req)
        except OutcomeUnknownError as exc:
            self.store.orders.mark(cl, OrderStatus.UNKNOWN, last_error=str(exc))
            await self._halt_unknown(cl, str(exc))
            return
        except KrakenError as exc:  # classified as NOT executed (auth, permission, rate limit after retries, ...)
            self.store.orders.mark(cl, OrderStatus.REJECTED, last_error=str(exc))
            self._cooldown_until = now + 60_000
            await self._alert(f"order rejected by Kraken: {exc}", AlertLevel.WARNING, key=f"rej:{type(exc).__name__}",
                              repeat_after_s=900)
            return
        apply_update(self.store, self.dca, order, upd)
        if upd.status == OrderStatus.REJECTED:
            log.info("dca order rejected", extra={"cl_ord_id": cl, "reason": upd.reason})
            if not (upd.reason and "post-only" in upd.reason):
                self._cooldown_until = now + 60_000  # post-only rejection: re-place next tick at the new bid
