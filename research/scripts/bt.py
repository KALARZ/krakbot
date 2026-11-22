"""Research backtester (Phase 0). Deterministic, no randomness except seeded bootstrap in reports.

Two stages:
 1. simulate_candidates(): per pair, for EVERY entry signal on closed strategy-timeframe bars, simulate
    order execution on 5m bars: post-only limit entry at the bid with repricing and cancel, fill only
    when price trades THROUGH the limit; protective stop = server-side stop-loss (market on trigger,
    taker); discretionary exits (trailing/time/signal) = post-only limit at the ask with repricing,
    fallback to market. Stop updates only at closed strategy bars (as the live bot would AmendOrder).
    Same-5m-bar ambiguity is always resolved pessimistically.
 2. portfolio(): chronological pass over candidate trades applying one-position-per-pair, max concurrent
    positions, max total exposure, stop-distance sizing, per-pair cooldown after stops, loss-streak circuit
    breaker, daily/total drawdown kill switch, dynamic Kraken fee tier (rolling 30d volume OR assets),
    fixed slippage + book-impact slippage, ordermin/costmin feasibility. Mark-to-market hourly equity.
"""
import os, sys, math, datetime as dt
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from lib import MS

# Kraken Pro spot fee schedule verified on https://www.kraken.com/pro/fees on 2026-11-22.
# (min 30d spot volume USD, min assets USD or None, maker, taker)
FEE_TIERS = [
    (0, 0, 0.0040, 0.0080), (2_500, None, 0.0030, 0.0060), (10_000, 20_000, 0.0022, 0.0038),
    (25_000, 50_000, 0.0020, 0.0035), (50_000, 100_000, 0.0015, 0.0030), (100_000, 200_000, 0.0012, 0.0025),
    (250_000, 400_000, 0.0010, 0.0022), (500_000, 600_000, 0.0008, 0.0020), (1_000_000, 1_000_000, 0.0006, 0.0018),
]

def fee_tier(vol30_usd, assets_usd, fixed_tier=None):
    if fixed_tier is not None:
        return FEE_TIERS[fixed_tier - 1][2:]
    best = 0
    for i, (v, a, mk, tk) in enumerate(FEE_TIERS):
        if vol30_usd >= v or (a is not None and a > 0 and assets_usd >= a):
            best = i
    return FEE_TIERS[best][2:]

class Bars5:
    """5m bars with index lookup by timestamp."""
    def __init__(self, d):
        self.ts, self.o, self.h, self.l, self.c = d["ts"], d["o"], d["h"], d["l"], d["c"]
    def idx_at_or_after(self, t):
        return int(np.searchsorted(self.ts, t, side="left"))

def simulate_trade(b5, tfms, t_sig_close, p_sig_close, stop0, manager, spread, cfg):
    """Simulate one candidate trade starting right after the signal bar closes at t_sig_close.

    manager(j_tf_close_ts, close, high_since_entry, bars_since_entry, stop) -> (new_stop, exit_now, reason)
       is called at every closed strategy bar after entry.
    Returns dict or None (entry not filled)."""
    half = spread / 2.0
    thr = cfg["through_bp"] * 1e-4
    j = b5.idx_at_or_after(t_sig_close)  # first 5m bar opening at/after signal close
    n = len(b5.ts)
    lim = p_sig_close * (1 - half)
    cap = p_sig_close * (1 + cfg["max_chase"])
    entry = None
    for k in range(cfg["entry_reprices"] + 1):
        if j >= n:
            return None
        if b5.l[j] < lim * (1 - thr):
            entry = (j, lim)
            break
        # reprice to new bid at this 5m close, never chasing above cap
        lim = min(b5.c[j] * (1 - half), cap)
        j += 1
    if entry is None:
        return {"filled": False, "t_sig": int(t_sig_close)}
    j0, pin = entry
    if stop0 >= pin:  # stop above entry: invalid geometry -> skip
        return {"filled": False, "t_sig": int(t_sig_close), "invalid": True}
    stop = stop0
    tp = None
    t_entry = int(b5.ts[j0])
    hi = pin
    exit_px = None; reason = None; exit_liq = None
    # pessimistic: stop checked inside fill bar
    jj = j0
    exiting = False; ex_lim = None; ex_tries = 0
    max_t = t_entry + cfg["max_hold_ms"]
    while jj < n:
        o, h, l, c, t = b5.o[jj], b5.h[jj], b5.l[jj], b5.c[jj], b5.ts[jj]
        # 1) server-side stop (market on trigger)
        if l <= stop:
            fill = min(stop, o) if jj > j0 else stop
            exit_px = fill * (1 - half); exit_liq = "T"; reason = reason or "stop"
            if reason != "stop": reason = reason + "+stop"
            break
        # 2) pending maker exit order
        if exiting:
            if h > ex_lim * (1 + thr):
                exit_px = ex_lim; exit_liq = "M"; break
            ex_tries += 1
            if ex_tries > cfg["exit_reprices"]:
                # fallback market at next bar open
                if jj + 1 < n:
                    exit_px = b5.o[jj + 1] * (1 - half)
                else:
                    exit_px = c * (1 - half)
                exit_liq = "T"; reason += "+mkt"; break
            ex_lim = max(c * (1 + half), 0)
        hi = max(hi, h)
        # 3) strategy-bar close?
        t_close = t + MS["5m"]
        if (t_close % tfms == 0) and not exiting:
            bars_since = int((t_close - (t_entry - (t_entry % tfms))) // tfms)
            new_stop, ex, why = manager(t_close, c, hi, bars_since, stop)
            if new_stop is not None:
                stop = max(stop, new_stop)  # stops only ratchet up
            if t_close >= max_t:
                ex, why = True, "time"
            if ex:
                exiting = True; reason = why; ex_lim = c * (1 + half); ex_tries = 0
        jj += 1
    if exit_px is None:
        return None  # data ended
    return {"filled": True, "t_sig": int(t_sig_close), "t_entry": t_entry, "t_exit": int(b5.ts[jj]) + MS["5m"],
            "p_entry": float(pin), "p_exit": float(exit_px), "stop0": float(stop0), "exit_liq": exit_liq,
            "reason": reason, "gross": float(exit_px / pin - 1.0), "stop_dist": float(1 - stop0 / pin),
            "mfe": float(hi / pin - 1)}

def stats_from_returns(r):
    r = np.asarray(r)
    if len(r) == 0:
        return {}
    w, lo = r[r > 0], r[r <= 0]
    pf = w.sum() / -lo.sum() if lo.sum() < 0 else float("inf")
    return {"n": int(len(r)), "win": float((r > 0).mean()), "exp_pct": float(r.mean() * 100),
            "t": float(r.mean() / r.std(ddof=1) * math.sqrt(len(r))) if len(r) > 2 else float("nan"),
            "pf": float(pf), "avg_win_pct": float(w.mean() * 100) if len(w) else 0.0,
            "avg_loss_pct": float(lo.mean() * 100) if len(lo) else 0.0, "abs_mean_pct": float(np.abs(r).mean() * 100)}

def portfolio(cands, closes1h, cfg):
    """cands: list of filled candidate trades (dicts incl. 'pair'). closes1h: {pair: (ts, close)} for MTM.
    cfg keys: capital_usd, risk_pct, max_pos, max_expo, max_pos_expo, slip_bp, impact (pair->bp per 10k USD),
              fixed_tier, cooldown_ms, cb_half, cb_stop, cb_pause_ms, kill_daily, kill_total, ordermin_usd."""
    cands = sorted([c for c in cands if c.get("filled")], key=lambda c: (c["t_entry"], c["pair"]))
    eq = cfg["capital_usd"]; peak = eq
    open_pos = []  # (t_exit, pair, notional, trade)
    vol_hist = []  # (t, usd)
    cooldown = {}
    streak = 0; pause_until = 0
    day_start_eq = {}; killed = None
    out = []
    slip = cfg["slip_bp"] * 1e-4
    def realize_until(t):
        nonlocal eq, peak, streak, pause_until
        for p in sorted([p for p in open_pos if p[0] <= t], key=lambda x: x[0]):
            open_pos.remove(p)
            _, pair, notional, tr = p
            eq += tr["pnl_usd"]; peak = max(peak, eq)
            if tr["pnl_usd"] <= 0:
                streak += 1
                if streak >= cfg["cb_stop"]:
                    pause_until = max(pause_until, tr["t_exit"] + cfg["cb_pause_ms"])
            else:
                streak = 0
            if "stop" in tr["reason"]:
                cooldown[pair] = tr["t_exit"] + cfg["cooldown_ms"]
    for c in cands:
        t = c["t_entry"]
        realize_until(t)
        if killed is not None:
            break
        if cfg["kill_total"] and eq < peak * (1 - cfg["kill_total"]):
            killed = ("total_dd", t); break
        day = t // MS["1d"]
        if day not in day_start_eq:
            day_start_eq[day] = eq
        if cfg["kill_daily"] and eq < day_start_eq[day] * (1 - cfg["kill_daily"]):
            continue  # halted for the rest of the UTC day
        if t < pause_until or t < cooldown.get(c["pair"], 0):
            continue
        if any(p[1] == c["pair"] for p in open_pos) or len(open_pos) >= cfg["max_pos"]:
            continue
        expo = sum(p[2] for p in open_pos)
        size_mult = 0.5 if streak >= cfg["cb_half"] else 1.0
        notional = eq * cfg["risk_pct"] * size_mult / max(c["stop_dist"], 1e-4)
        notional = min(notional, eq * cfg["max_pos_expo"], eq * cfg["max_expo"] - expo)
        if notional < cfg["ordermin_usd"]:
            continue
        # fees from tier at entry time (rolling 30d volume or assets)
        vol_hist[:] = [x for x in vol_hist if x[0] >= t - 30 * MS["1d"]]
        vol30 = sum(v for tt, v in vol_hist if tt <= t)  # only volume already traded
        mk, tk = fee_tier(vol30, eq, cfg.get("fixed_tier"))
        imp = cfg["impact"].get(c["pair"], 0.0) * 1e-4 * (notional / 10_000.0)
        f_in = mk  # post-only entry always maker
        f_out = mk if c["exit_liq"] == "M" else tk
        exit_slip = (slip + imp * (2.0 if "stop" in c["reason"] else 1.0)) if c["exit_liq"] == "T" else slip
        p_in = c["p_entry"] * (1 + slip)  # adverse-selection haircut on maker fill
        p_out = c["p_exit"] * (1 - exit_slip)
        qty = notional / p_in
        pnl = qty * p_out * (1 - f_out) - notional * (1 + f_in)
        tr = dict(c); tr.update({"notional": notional, "pnl_usd": pnl, "ret_net": pnl / notional,
                                 "fee_in": f_in, "fee_out": f_out, "eq_at_entry": eq, "size_mult": size_mult})
        out.append(tr)
        open_pos.append((c["t_exit"], c["pair"], notional, tr))
        vol_hist.append((t, notional)); vol_hist.append((c["t_exit"], notional * p_out / p_in))
    realize_until(2**62)
    # hourly MTM equity curve
    curve = mtm_curve(out, closes1h, cfg["capital_usd"])
    return out, curve, killed

def mtm_curve(trades, closes1h, cap):
    if not trades:
        return None
    t0 = min(t["t_entry"] for t in trades) // MS["1h"] * MS["1h"]
    t1 = max(t["t_exit"] for t in trades) // MS["1h"] * MS["1h"] + MS["1h"]
    grid = np.arange(t0, t1 + 1, MS["1h"])
    pnl = np.zeros(len(grid))
    for tr in trades:
        ts, cl = closes1h[tr["pair"]]
        i0 = np.searchsorted(grid, tr["t_entry"], "right")
        i1 = np.searchsorted(grid, tr["t_exit"], "right")
        # unrealised between entry and exit using 1h closes (close at grid time g = bar ending at g)
        if i1 > i0:
            g = grid[i0:i1]
            k = np.searchsorted(ts + MS["1h"], g, "right") - 1
            k = np.clip(k, 0, len(cl) - 1)
            qty = tr["notional"] / tr["p_entry"]
            unreal = qty * cl[k] - tr["notional"] * (1 + tr["fee_in"])
            pnl[i0:i1] += unreal
        pnl[i1:] += tr["pnl_usd"]
    return grid, cap + pnl

def curve_stats(grid, eq, years=None):
    r = np.diff(eq) / eq[:-1]
    hours = len(r)
    ann = math.sqrt(24 * 365)
    sharpe = r.mean() / r.std() * ann if r.std() > 0 else 0.0
    dn = r[r < 0]
    sortino = r.mean() / math.sqrt((dn ** 2).sum() / len(r)) * ann if len(dn) else float("inf")
    peak = np.maximum.accumulate(eq)
    mdd = float(((peak - eq) / peak).max())
    yrs = hours / (24 * 365)
    cagr = (eq[-1] / eq[0]) ** (1 / yrs) - 1 if yrs > 0 else 0
    return {"sharpe": float(sharpe), "sortino": float(sortino), "max_dd": mdd, "cagr": float(cagr),
            "total_ret": float(eq[-1] / eq[0] - 1), "years": yrs}


# ---------------------------------------------------------------------------------------------
# portfolio2: hour-stepped portfolio with the risk rules exactly as specified for the live bot
# ---------------------------------------------------------------------------------------------
def portfolio2(cands, closes1h, cfg, costs):
    """Hour-stepped simulation. Differences vs portfolio():
    - mark-to-market equity every hour; kill switches act on MTM equity and FLATTEN all open positions
      (taker, spread + slippage + 2x book impact) then HALT: daily kill -> no entries for 24h;
      total kill -> permanent halt (simulation stops trading, curve continues flat).
    - circuit breaker: >= cb_half consecutive losses -> size x0.5 until the next winning trade;
      >= cb_stop -> pause cb_pause_ms, after which the streak is set back to cb_half (still half size).
    - per-pair cooldown after a stop-loss exit; per-pair minimum order (ordermin*price, costmin).
    - fee tier from rolling 30d executed volume (legs already executed only) or assets (equity).
    costs: {"spread": {pair: frac}, "impact": callable(pair, notional_usd)->frac, "min_usd": {pair: usd}}
    """
    H = MS["1h"]
    cands = sorted([c for c in cands if c.get("filled")], key=lambda c: (c["t_entry"], c["pair"]))
    if not cands:
        return [], None, None
    t0 = cands[0]["t_entry"] // H * H
    t1 = max(c["t_exit"] for c in cands) // H * H + 2 * H
    grid = np.arange(t0, t1, H)
    fee_mult = cfg.get("fee_mult", 1.0)
    slip = cfg["slip_bp"] * 1e-4
    eq_real = cfg["capital_usd"]; peak = eq_real
    curve = np.empty(len(grid))
    open_pos = []; done = []; vol_hist = []
    cooldown = {}; streak = 0; pause_until = 0; halt_until = 0; killed = None
    day_start = {}
    ci = 0
    def px_at(pair, g):
        ts, cl = closes1h[pair]
        k = np.searchsorted(ts + H, g, "right") - 1
        return cl[max(k, 0)]
    def close_pos(p, t, px_exit, liq, reason):
        nonlocal eq_real, streak, pause_until
        tr = p["tr"]
        f_out = (p["mk"] if liq == "M" else p["tk"])
        if liq == "T":
            imp = costs["impact"](tr["pair"], p["notional"]) * (2.0 if ("stop" in reason or reason == "kill") else 1.0)
            px = px_exit * (1 - slip - imp)
        else:
            px = px_exit * (1 - slip)
        pnl = p["qty"] * px * (1 - f_out) - p["notional"] * (1 + p["f_in"])
        eq_real += pnl
        vol_hist.append((t, p["qty"] * px))
        rec = dict(tr); rec.update({"t_exit": int(t), "p_exit_eff": float(px), "exit_liq": liq, "reason": reason,
                                    "notional": p["notional"], "pnl_usd": float(pnl), "ret_net": float(pnl / p["notional"]),
                                    "fee_in": p["f_in"], "fee_out": f_out, "size_mult": p["mult"]})
        done.append(rec)
        if pnl <= 0:
            streak += 1
            if streak >= cfg["cb_stop"]:
                pause_until = max(pause_until, t + cfg["cb_pause_ms"]); streak = cfg["cb_half"]
        else:
            streak = 0
        if "stop" in reason:
            cooldown[tr["pair"]] = t + cfg["cooldown_ms"]
    for gi, g in enumerate(grid):
        # 1) natural exits that happened up to g
        for p in sorted([p for p in open_pos if p["tr"]["t_exit"] <= g], key=lambda p: p["tr"]["t_exit"]):
            open_pos.remove(p)
            close_pos(p, p["tr"]["t_exit"], p["tr"]["p_exit"], p["tr"]["exit_liq"], p["tr"]["reason"])
        # 2) entries in (g-H, g]
        while ci < len(cands) and cands[ci]["t_entry"] <= g:
            c = cands[ci]; ci += 1
            if killed or c["t_entry"] < halt_until or c["t_entry"] < pause_until or c["t_entry"] < cooldown.get(c["pair"], 0):
                continue
            if any(p["tr"]["pair"] == c["pair"] for p in open_pos) or len(open_pos) >= cfg["max_pos"]:
                continue
            eq_mtm = eq_real + sum(p["qty"] * px_at(p["tr"]["pair"], g - H) - p["notional"] * (1 + p["f_in"]) for p in open_pos)
            expo = sum(p["notional"] for p in open_pos)
            mult = 0.5 if streak >= cfg["cb_half"] else 1.0
            notional = eq_mtm * cfg["risk_pct"] * mult / max(c["stop_dist"], 1e-4)
            notional = min(notional, eq_mtm * cfg["max_pos_expo"], eq_mtm * cfg["max_expo"] - expo)
            if notional < costs["min_usd"].get(c["pair"], 5.0):
                continue
            vol_hist[:] = [x for x in vol_hist if x[0] >= c["t_entry"] - 30 * MS["1d"]]
            vol30 = sum(v for tt, v in vol_hist if tt <= c["t_entry"])
            mk, tk = fee_tier(vol30, eq_mtm, cfg.get("fixed_tier"))
            mk *= fee_mult; tk *= fee_mult
            p_in = c["p_entry"] * (1 + slip)
            qty = math.floor(notional / p_in * 1e8) / 1e8  # lot_decimals = 8 on all candidate pairs
            open_pos.append({"tr": c, "qty": qty, "notional": qty * p_in, "f_in": mk, "mk": mk, "tk": tk, "mult": mult})
            vol_hist.append((c["t_entry"], qty * p_in))
        # 3) mark to market and kill switches
        unreal = sum(p["qty"] * px_at(p["tr"]["pair"], g) - p["notional"] * (1 + p["f_in"]) for p in open_pos)
        eq = eq_real + unreal
        day = g // MS["1d"]
        if day not in day_start:
            day_start[day] = eq
        peak = max(peak, eq)
        trig = None
        if not killed and cfg.get("kill_total") and eq < peak * (1 - cfg["kill_total"]):
            trig = "total"
        elif cfg.get("kill_daily") and eq < day_start[day] * (1 - cfg["kill_daily"]):
            trig = "daily"
        if trig and open_pos:
            for p in list(open_pos):
                open_pos.remove(p)
                pair = p["tr"]["pair"]
                close_pos(p, g, px_at(pair, g) * (1 - costs["spread"].get(pair, 6e-4) / 2), "T", "kill")
            eq = eq_real
        if trig == "total":
            killed = killed or ("total_dd", int(g))
        elif trig == "daily":
            halt_until = max(halt_until, g + MS["1d"])
        curve[gi] = eq
    return done, (grid, curve), killed
