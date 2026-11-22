"""Command line (docs/plan.md §4.7, §8 runbook). Every command that changes state writes an event; nothing here ever
needs a hand-edited database.

  krakbot dry-run                         run with the DCA forced to DRY_RUN (paper broker, zero real orders)
  krakbot run --live --i-understand-live  real DCA orders (config mode=live + .env interlock + 7-day gate)
  krakbot status                          halts, unresolved orders, current tranche, totals
  krakbot halt --reason TEXT              manual halt of new DCA purchases (never sells anything)
  krakbot resume                          clear a manual/recon halt (never an unknown_order halt: use resolve)
  krakbot resolve CL_ORD_ID [--dry]       resolve an ambiguous order from exchange evidence
          [--operator-confirms-absent --reason TEXT [--yes]]
  krakbot spreads [--days N]              per-pair daily spread / depth percentiles recorded in DRY_RUN
  krakbot liquidity-derive [--yes]        derive liquidity thresholds from the last N complete UTC days
  krakbot gate                            7-clean-days DRY_RUN gate for live DCA
  krakbot canary-verify TRANCHE_ID [--yes] verify a closed LIVE canary tranche (fill, ledger, recon, balance)
  krakbot backup PATH                     consistent online copy of the database (safe while the bot runs)
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import signal
import sqlite3
import sys
import time
from collections.abc import Callable, Sequence
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import TextIO

import httpx

from krakbot.app import DCA_PAIR, DRY_RUN_MAKER_FEE, App, LiveGateError
from krakbot.config import ConfigError, Settings, load_settings
from krakbot.domain import EventLevel, Mode
from krakbot.execution.canary_verify import CanaryNotVerifiable, verify_canary
from krakbot.execution.live import KrakenBroker
from krakbot.execution.paper import PaperBroker
from krakbot.execution.resolve import UNRESOLVED, ResolveAction, resolve_order, unresolved_for
from krakbot.execution.types import Broker
from krakbot.kraken_rest.client import KrakenRestClient
from krakbot.kraken_rest.errors import KrakenError
from krakbot.kraken_rest.nonce import NonceSource, SqliteNonceStore
from krakbot.market_data import MarketData
from krakbot.monitor.alerts import Alerts, NullAlerts, TelegramAlerts
from krakbot.monitor.logs import configure_logging
from krakbot.state import connect, migrate
from krakbot.state.canary import CANARY_CLEAN_TRANCHES, CanaryRepo
from krakbot.state.dca import DcaRepo
from krakbot.state.gates import DAY_MS, dca_live_gate
from krakbot.state.liquidity import LiquidityRepo
from krakbot.state.repo import NotFound, StateError, Store

__all__ = ["main"]

EXIT_OK, EXIT_ERROR, EXIT_REFUSED = 0, 1, 2
NowMs = Callable[[], int]


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


class _Ctx:
    def __init__(self, args: argparse.Namespace, out: TextIO, now_ms: NowMs) -> None:
        self.args, self.out, self.now = args, out, now_ms
        self.settings: Settings = load_settings(Path(args.config), Path(args.env) if args.env else None)

    def store(self) -> Store:
        conn = connect(self.settings.db_path)
        migrate(conn)
        return Store(conn, self.now)

    def p(self, *parts: object) -> None:
        print(*parts, file=self.out)


def _active_run_id(store: Store, now_ms: int) -> str | None:
    """The run of a bot process that is alive right now (open run with a heartbeat in the last 3 minutes): the gate
    must not count it as an unclean exit just because it has not ended yet."""
    r = store.conn.execute(
        "SELECT r.run_id FROM runs r JOIN heartbeats h ON h.run_id = r.run_id WHERE r.ended_at IS NULL "
        "GROUP BY r.run_id HAVING MAX(h.minute) >= ? ORDER BY r.started_at DESC LIMIT 1",
        (now_ms // 60_000 - 3,)).fetchone()
    return None if r is None else str(r[0])


def _utc(ms: int | None) -> str:
    return "-" if ms is None else time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ms / 1000))


def _mode_arg(ctx: _Ctx) -> Mode:
    return Mode(ctx.args.mode) if getattr(ctx.args, "mode", None) else Mode.DRY_RUN


# ---------------------------------------------------------------------------------------------------------- run
def _alerts(s: Settings, http: httpx.AsyncClient, now_ms: NowMs) -> Alerts:
    sec = s.secrets
    if s.app.monitor.telegram_enabled and sec.TELEGRAM_BOT_TOKEN is not None and sec.TELEGRAM_CHAT_ID:
        return TelegramAlerts(http, sec.TELEGRAM_BOT_TOKEN, sec.TELEGRAM_CHAT_ID, now_ms=now_ms)
    return NullAlerts()


async def _run(ctx: _Ctx, *, live: bool) -> int:
    s = ctx.settings
    sec = s.secrets
    configure_logging(s.app.general.log_level, secrets=[
        v.get_secret_value() if v is not None else None
        for v in (sec.KRAKEN_API_KEY, sec.KRAKEN_API_SECRET, sec.TELEGRAM_BOT_TOKEN, sec.HEALTHCHECK_URL)])
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as http:
        app = App(s, live=live, http=http, alerts=_alerts(s, http, ctx.now), now_ms=ctx.now)
        try:
            await app.start()
        except LiveGateError as exc:
            ctx.p(f"REFUSED: {exc}")
            return EXIT_REFUSED
        clean, reason = True, "signal"
        try:
            await app.run(stop)
        except Exception as exc:  # pragma: no cover - logged and re-raised so systemd restarts us
            clean, reason = False, f"crash: {type(exc).__name__}"
            raise
        finally:
            await app.close(clean=clean, reason=reason)
    return EXIT_OK


def cmd_dry_run(ctx: _Ctx) -> int:
    if ctx.settings.app.strategies.dca.mode == Mode.LIVE:
        ctx.p("note: config has [strategies.dca].mode = 'live'; `dry-run` forces DRY_RUN (paper broker).")
    return asyncio.run(_run(ctx, live=False))


def cmd_run(ctx: _Ctx) -> int:
    if not ctx.args.live:
        return cmd_dry_run(ctx)
    if not ctx.args.i_understand_live:
        ctx.p("REFUSED: real orders need both --live and --i-understand-live.")
        return EXIT_REFUSED
    return asyncio.run(_run(ctx, live=True))


# ---------------------------------------------------------------------------------------------------------- status
def cmd_status(ctx: _Ctx) -> int:
    store = ctx.store()
    dca = DcaRepo(store, DCA_PAIR)
    for mode in (Mode.DRY_RUN, Mode.LIVE):
        rs = store.risk.get("dca", mode)
        spent, qty = dca.totals(mode)
        t = dca.latest(mode)
        unresolved = unresolved_for(store, "dca", mode)
        ctx.p(f"[dca {mode}] halted={rs.halted} reason={rs.halt_reason} ref={rs.halt_ref}")
        ctx.p(f"  spent={spent} EUR  qty={qty} BTC  tranches={len(dca.all(mode))}")
        if t is not None:
            ctx.p(f"  latest tranche #{t.seq} {t.tranche_id} status={t.status} budget={t.budget} spent={t.spent} "
                  f"carry_out={t.carry_out}")
        if mode == Mode.LIVE:
            c = CanaryRepo(store)
            ctx.p(f"  canary: clean streak {c.streak()}/{CANARY_CLEAN_TRANCHES}, "
                  f"phase {'complete (full size)' if c.phase_complete() else 'ACTIVE (minimum size)'}")
        for o in unresolved:
            ctx.p(f"  UNRESOLVED order {o.cl_ord_id} status={o.status} price={o.price} qty={o.qty} "
                  f"error={o.last_error!r} -> `krakbot resolve {o.cl_ord_id} --dry`")
    r = store.conn.execute("SELECT run_id, started_at, ended_at, clean_exit FROM runs ORDER BY started_at DESC "
                           "LIMIT 1").fetchone()
    if r is not None:
        hb = store.conn.execute("SELECT MAX(minute) FROM heartbeats WHERE run_id = ?", (r[0],)).fetchone()[0]
        ctx.p(f"last run {r[0][:8]} started={_utc(r[1])} ended={_utc(r[2])} clean={r[3]} "
              f"last_healthy_minute={_utc(hb * 60_000 if hb is not None else None)}")
    store.conn.close()
    return EXIT_OK


# ---------------------------------------------------------------------------------------------------------- halt
def cmd_halt(ctx: _Ctx) -> int:
    store = ctx.store()
    mode = _mode_arg(ctx)
    store.risk.halt("dca", mode, "manual", ref=ctx.args.reason[:200])
    ctx.p(f"dca {mode}: manual halt set (no new purchases; nothing is ever sold).")
    store.conn.close()
    return EXIT_OK


def cmd_resume(ctx: _Ctx) -> int:
    store = ctx.store()
    mode = _mode_arg(ctx)
    rs = store.risk.get("dca", mode)
    try:
        if not rs.halted:
            ctx.p(f"dca {mode}: not halted.")
            return EXIT_OK
        if rs.halt_reason == "unknown_order" or unresolved_for(store, "dca", mode):
            ctx.p(f"REFUSED: dca {mode} has an ambiguous order ({rs.halt_ref}). Use `krakbot resolve <cl_ord_id>`; "
                  "resume never clears an unknown-order halt.")
            return EXIT_REFUSED
        if rs.halt_reason == "canary":
            ctx.p(f"REFUSED: canary halt on {rs.halt_ref}: only `krakbot canary-verify {rs.halt_ref} --yes` clears it.")
            return EXIT_REFUSED
        if rs.halt_reason not in ("manual", "recon"):
            ctx.p(f"REFUSED: halt reason {rs.halt_reason!r} is not resumable from here.")
            return EXIT_REFUSED
        store.risk.clear_halt("dca", mode, reason=str(rs.halt_reason))
        store.events.append("RESUMED_BY_OPERATOR", EventLevel.WARNING, {"previous_reason": rs.halt_reason},
                            strategy="dca", mode=mode)
        ctx.p(f"dca {mode}: halt ({rs.halt_reason}) cleared.")
        return EXIT_OK
    finally:
        store.conn.close()


# ---------------------------------------------------------------------------------------------------------- resolve
async def _resolve_broker(ctx: _Ctx, mode: Mode, store: Store, http: httpx.AsyncClient) -> Broker | None:
    if mode == Mode.DRY_RUN:
        # the paper book lived in the (dead or other) bot process: a lookup here can only prove absence after
        # deadline + 60 s, exactly like the exchange would for a GTD order that never reached it
        return PaperBroker(MarketData({}, depth_bps=25, now_ms=ctx.now), {}, fee_rate=DRY_RUN_MAKER_FEE,
                           now_ms=ctx.now)
    sec = ctx.settings.secrets
    if not sec.has_kraken_keys:
        ctx.p("REFUSED: resolving a LIVE order needs the Kraken API key (query permissions) in .env.")
        return None
    public = KrakenRestClient(http=http)
    instruments = await public.asset_pairs()
    private = KrakenRestClient(http=http, api_key=sec.KRAKEN_API_KEY, api_secret=sec.KRAKEN_API_SECRET,
                               nonce=NonceSource(SqliteNonceStore(store.conn)), trading_enabled=False)
    return KrakenBroker(private, instruments, now_ms=ctx.now)


async def _resolve(ctx: _Ctx) -> int:
    a = ctx.args
    store = ctx.store()
    try:
        try:
            order = store.orders.by_cl_ord_id(a.cl_ord_id)
        except NotFound:
            ctx.p(f"no order with cl_ord_id {a.cl_ord_id}")
            return EXIT_ERROR
        strategy = store.trades.get(order.trade_id).strategy
        if order.status not in UNRESOLVED:
            ctx.p(f"order {a.cl_ord_id} is {order.status}: nothing to resolve.")
            return EXIT_OK
        if a.operator_confirms_absent:
            if not (a.reason and a.reason.strip()):
                ctx.p("REFUSED: --operator-confirms-absent requires --reason (kept in the event log).")
                return EXIT_REFUSED
            if not a.yes:
                ctx.p(f"You are declaring that order {a.cl_ord_id} was NEVER placed on Kraken, without proof.")
                ctx.p("If it was placed and fills later, the ledger will be wrong. Re-run with --yes to confirm.")
                return EXIT_REFUSED
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as http:
            broker = await _resolve_broker(ctx, order.mode, store, http)
            if broker is None:
                return EXIT_REFUSED
            dca = DcaRepo(store, order.pair) if strategy == "dca" else None
            rep = await resolve_order(store, dca, broker, a.cl_ord_id, strategy=strategy,
                                      deadline_s=ctx.settings.app.execution.deadline_seconds,
                                      operator_confirms_absent=bool(a.operator_confirms_absent),
                                      operator_reason=a.reason, evidence_only=bool(a.dry))
        ev = rep.evidence
        ctx.p(f"evidence: {ev.kind if ev else None} {ev.detail if ev else ''}".rstrip())
        if ev is not None and ev.update is not None:
            ctx.p(f"  exchange status={ev.update.status} txid={ev.update.txid} fills={len(ev.update.fills)}")
        ctx.p(f"action: {rep.action}; halt cleared: {rep.halt_cleared}")
        if rep.action == ResolveAction.REFUSED_NO_EVIDENCE:
            ctx.p("REFUSED: no evidence either way (Kraken unreachable or too early). The halt stays. Retry later, "
                  "or, only if you have verified on kraken.com that the order does not exist, use "
                  "--operator-confirms-absent --reason '...' --yes.")
            return EXIT_REFUSED
        if not rep.halt_cleared and rep.action not in (ResolveAction.EVIDENCE_ONLY,):
            remaining = unresolved_for(store, strategy, order.mode)
            if remaining:
                ctx.p("still unresolved:", ", ".join(o.cl_ord_id for o in remaining))
        return EXIT_OK
    except KrakenError as exc:
        ctx.p(f"REFUSED: Kraken query failed ({exc}); nothing changed, the halt stays.")
        return EXIT_REFUSED
    finally:
        store.conn.close()


def cmd_resolve(ctx: _Ctx) -> int:
    return asyncio.run(_resolve(ctx))


# ---------------------------------------------------------------------------------------------------------- canary
async def _canary_verify(ctx: _Ctx) -> int:
    a = ctx.args
    sec = ctx.settings.secrets
    store = ctx.store()
    try:
        try:
            DcaRepo(store).get(a.tranche_id)
        except StateError:
            ctx.p(f"no tranche {a.tranche_id}")
            return EXIT_ERROR
        if not sec.has_kraken_keys:
            ctx.p("REFUSED: canary verification queries Kraken: the API key (query permissions) is needed in .env.")
            return EXIT_REFUSED
        cfg = ctx.settings.app.strategies.dca
        total = (sec.CAPITAL * cfg.allocation).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as http:
            public = KrakenRestClient(http=http)
            inst = (await public.asset_pairs())[cfg.pair]
            private = KrakenRestClient(http=http, api_key=sec.KRAKEN_API_KEY, api_secret=sec.KRAKEN_API_SECRET,
                                       nonce=NonceSource(SqliteNonceStore(store.conn)), trading_enabled=False)
            try:
                chk = await verify_canary(store, private, a.tranche_id, base_asset=inst.base, total_budget=total)
            except CanaryNotVerifiable as exc:
                ctx.p(f"REFUSED: {exc}")
                return EXIT_REFUSED
        for name, ok in (("fill", chk.fill_ok), ("ledger", chk.ledger_ok), ("reconciliation", chk.recon_ok),
                         ("balance", chk.balance_ok)):
            ctx.p(f"  {name:<15} {'OK' if ok else 'FAILED'}")
        ctx.p(json.dumps(chk.details, indent=2, default=str))
        canary = CanaryRepo(store)
        if not chk.ok:
            canary.record(a.tranche_id, chk)  # a definitive mismatch is recorded: this tranche can never be clean
            ctx.p("REFUSED: canary check failed; the halt stays. Investigate before anything else.")
            return EXIT_REFUSED
        if not a.yes:
            ctx.p("All checks passed. Look at the order and the balance on kraken.com, then re-run with --yes.")
            return EXIT_REFUSED
        canary.record(a.tranche_id, chk)
        cleared = store.risk.clear_halt("dca", Mode.LIVE, reason="canary", ref=a.tranche_id)
        streak = canary.streak()
        ctx.p(f"verified; halt cleared: {cleared}; clean canary streak {streak}/{CANARY_CLEAN_TRANCHES}; "
              + ("canary phase COMPLETE: next tranche at full size." if canary.phase_complete()
                 else "next tranche stays at the minimum size."))
        if canary.phase_complete():
            store.events.append("CANARY_COMPLETE", EventLevel.WARNING, {"streak": streak}, strategy="dca",
                                mode=Mode.LIVE)
        return EXIT_OK
    except KrakenError as exc:
        ctx.p(f"REFUSED: Kraken query failed ({exc}); nothing recorded, the halt stays.")
        return EXIT_REFUSED
    finally:
        store.conn.close()


def cmd_canary_verify(ctx: _Ctx) -> int:
    return asyncio.run(_canary_verify(ctx))


# ---------------------------------------------------------------------------------------------------------- liquidity
def cmd_spreads(ctx: _Ctx) -> int:
    store = ctx.store()
    since = ctx.now() // DAY_MS - ctx.args.days
    rows = store.conn.execute("SELECT * FROM spread_daily WHERE day >= ? ORDER BY pair, day", (since,)).fetchall()
    ctx.p("pair       day         n      p50    p90    p95    p99    max   bid_d_p5  ask_d_p5  (spread in bp)")
    for r in rows:
        day = time.strftime("%Y-%m-%d", time.gmtime(r["day"] * 86_400))
        ctx.p(f"{r['pair']:<10} {day} {r['n']:>6} {r['spread_p50']!s:>6} {r['spread_p90']!s:>6} "
              f"{r['spread_p95']!s:>6} {r['spread_p99']!s:>6} {r['spread_max']!s:>6} {r['bid_depth_p5']!s:>9} "
              f"{r['ask_depth_p5']!s:>9}")
    store.conn.close()
    return EXIT_OK


def cmd_liquidity_derive(ctx: _Ctx) -> int:
    s = ctx.settings
    rules = s.app.liquidity
    store = ctx.store()
    try:
        end = (ctx.now() // DAY_MS) * DAY_MS  # last complete UTC day boundary
        start = end - rules.reference_days * DAY_MS
        set_id = f"liq-{time.strftime('%Y%m%d', time.gmtime(start / 1000))}-{rules.reference_days}d"
        g = dca_live_gate(store.conn, ctx.now(), pairs=list(rules.sample_pairs),
                          current_run_id=_active_run_id(store, ctx.now()))
        if g.window != (start, end):
            raise ValueError(f"gate window {g.window} != reference window {(start, end)}")
        if not g.allowed:  # plan §6: thresholds come from CLEAN DRY_RUN days only
            ctx.p("REFUSED: the reference days are not all clean DRY_RUN days:")
            for reason in g.reasons[:20]:
                ctx.p("  -", reason)
            return EXIT_REFUSED
        if not ctx.args.yes:
            ctx.p(f"would derive set {set_id} over [{start}, {end}) for {list(rules.sample_pairs)}; add --yes")
            return EXIT_REFUSED
        th = LiquidityRepo(store.conn, ctx.now).derive(set_id, list(rules.sample_pairs), start, end, rules=rules,
                                                        pause_percentile=s.app.strategies.dca.pause_spread_percentile)
        for p, t in th.items():
            ctx.p(f"{p:<10} n={t.n_samples} coverage={t.coverage} eligible={t.eligible} spread_max={t.spread_bp_max} "
                  f"pause={t.spread_bp_pause} bid_min={t.bid_depth_min} ask_min={t.ask_depth_min}")
        store.events.append("LIQUIDITY_DERIVED", EventLevel.WARNING, {"set_id": set_id, "ref": [start, end]})
        ctx.p(f"set {set_id} stored.")
        return EXIT_OK
    except ValueError as exc:
        ctx.p(f"REFUSED: {exc}")
        return EXIT_REFUSED
    finally:
        store.conn.close()


def cmd_gate(ctx: _Ctx) -> int:
    store = ctx.store()
    pairs = list(ctx.settings.app.liquidity.sample_pairs)
    g = dca_live_gate(store.conn, ctx.now(), pairs=pairs, current_run_id=_active_run_id(store, ctx.now()))
    ctx.p("day         uptime  halted  min_samples(pair)          recon  unclean  clean")
    for d in g.days:
        worst = min(d.sample_coverage.items(), key=lambda kv: kv[1])
        ctx.p(f"{time.strftime('%Y-%m-%d', time.gmtime(d.day_start_ms / 1000))}  {d.coverage:.4f}  "
              f"{'yes' if d.halted else 'no':<6}  {worst[1]:.4f} ({worst[0]:<9})  {d.recon_checks - d.recon_failures}/"
              f"{d.recon_checks:<4} {d.unclean_runs:<7}  {'CLEAN' if d.clean() else 'no'}")
    ctx.p(json.dumps({"allowed": g.allowed, "reasons": g.reasons}, indent=2))
    store.conn.close()
    return EXIT_OK if g.allowed else EXIT_REFUSED


def cmd_backup(ctx: _Ctx) -> int:
    dest = Path(ctx.args.path)
    if dest.exists():
        ctx.p(f"REFUSED: {dest} exists (never overwritten)")
        return EXIT_REFUSED
    src = sqlite3.connect(ctx.settings.db_path)
    try:
        out = sqlite3.connect(dest)
        try:
            src.backup(out)  # SQLite online backup API: a consistent snapshot even with the bot writing (WAL)
        finally:
            out.close()
        dest.chmod(0o600)
        chk = sqlite3.connect(dest)
        try:
            ok = chk.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            chk.close()
    finally:
        src.close()
    ctx.p(f"backup written to {dest} (integrity_check: {ok})")
    return EXIT_OK if ok == "ok" else EXIT_ERROR


def cmd_flatten(ctx: _Ctx) -> int:
    ctx.p("flatten: nothing to do. The Donchian book is DRY_RUN only (no real positions) and the DCA holdings are "
          "never sold by the bot (plan §6, kill_switch_sells_holdings = false). Sell manually on Kraken if you "
          "decide to.")
    return EXIT_OK


def cmd_backtest(ctx: _Ctx) -> int:
    ctx.p("backtest: the research backtests live in research/scripts (see docs/research.md sections C-K); "
          "the bot itself ships no backtest command.")
    return EXIT_OK


# ---------------------------------------------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="krakbot", description="Kraken Spot DCA bot (DRY_RUN by default)")
    ap.add_argument("--config", default="config.toml")
    ap.add_argument("--env", default=".env")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("dry-run").set_defaults(fn=cmd_dry_run)
    r = sub.add_parser("run")
    r.add_argument("--live", action="store_true")
    r.add_argument("--i-understand-live", action="store_true")
    r.set_defaults(fn=cmd_run)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    h = sub.add_parser("halt")
    h.add_argument("--reason", required=True)
    h.add_argument("--mode", choices=["dry_run", "live"])
    h.set_defaults(fn=cmd_halt)
    re_ = sub.add_parser("resume")
    re_.add_argument("--mode", choices=["dry_run", "live"])
    re_.set_defaults(fn=cmd_resume)
    rs = sub.add_parser("resolve")
    rs.add_argument("cl_ord_id")
    rs.add_argument("--dry", action="store_true", help="query the exchange and record evidence; change nothing")
    rs.add_argument("--operator-confirms-absent", action="store_true")
    rs.add_argument("--reason")
    rs.add_argument("--yes", action="store_true")
    rs.set_defaults(fn=cmd_resolve)
    sp = sub.add_parser("spreads")
    sp.add_argument("--days", type=int, default=7)
    sp.set_defaults(fn=cmd_spreads)
    ld = sub.add_parser("liquidity-derive")
    ld.add_argument("--yes", action="store_true")
    ld.set_defaults(fn=cmd_liquidity_derive)
    sub.add_parser("gate").set_defaults(fn=cmd_gate)
    cv = sub.add_parser("canary-verify")
    cv.add_argument("tranche_id")
    cv.add_argument("--yes", action="store_true")
    cv.set_defaults(fn=cmd_canary_verify)
    bk = sub.add_parser("backup")
    bk.add_argument("path")
    bk.set_defaults(fn=cmd_backup)
    sub.add_parser("flatten").set_defaults(fn=cmd_flatten)
    sub.add_parser("backtest").set_defaults(fn=cmd_backtest)
    return ap


def main(argv: Sequence[str] | None = None, *, out: TextIO | None = None, now_ms: NowMs | None = None) -> int:
    args = build_parser().parse_args(argv)
    stream = out or sys.stdout
    try:
        ctx = _Ctx(args, stream, now_ms or _now_ms)
    except ConfigError as exc:
        print(str(exc), file=stream)
        return EXIT_ERROR
    fn: Callable[[_Ctx], int] = args.fn
    return fn(ctx)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
