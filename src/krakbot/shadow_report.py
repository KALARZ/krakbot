"""What the shadow run has exercised (read-only over the shadow database)."""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from typing import Any

from krakbot.numeric import D

__all__ = ["shadow_report"]


def shadow_report(conn: sqlite3.Connection, now_ms: int) -> dict[str, Any]:
    q = conn.execute
    since = q("SELECT MIN(started_at) FROM runs").fetchone()[0]
    minutes = q("SELECT COUNT(*) FROM heartbeats").fetchone()[0]
    span = max(1, (now_ms - since) // 60_000) if since else 1
    tranches = {r[0]: r[1] for r in q("SELECT status, COUNT(*) FROM dca_tranches GROUP BY status")}
    orders = {r[0]: r[1] for r in q("SELECT status, COUNT(*) FROM orders GROUP BY status")}
    rows = q("SELECT qty, filled_qty, attempt, status, cancel_reason FROM orders").fetchall()
    partial_orders = sum(1 for r in rows if 0 < D(r["filled_qty"]) < D(r["qty"]))
    reprices = sum(1 for r in rows if r["attempt"] >= 1)
    expired = sum(1 for r in rows if r["status"] == "expired")
    post_only = sum(1 for r in rows if r["status"] == "rejected" and "post-only" in (r["cancel_reason"] or ""))
    carry = q("SELECT COUNT(*) FROM dca_tranches WHERE CAST(carry_in AS REAL) > 0").fetchone()[0]
    fills = q("SELECT price, qty, cost, fee FROM fills").fetchall()
    spent = sum((D(f["cost"]) + D(f["fee"]) for f in fills), Decimal(0))
    qty = sum((D(f["qty"]) for f in fills), Decimal(0))
    recon = q("SELECT COUNT(*), COALESCE(SUM(ok), 0) FROM recon_checks").fetchone()
    runs = q("SELECT COUNT(*), COALESCE(SUM(CASE WHEN ended_at IS NOT NULL AND clean_exit = 0 THEN 1 ELSE 0 END), 0) "
             "FROM runs").fetchone()
    halts = q("SELECT COUNT(*) FROM events WHERE kind = 'HALT'").fetchone()[0]
    return {
        "since": since, "uptime": f"{Decimal(minutes) / Decimal(span):.4f}", "runs": runs[0], "unclean_runs": runs[1],
        "recon": recon[0], "recon_ok": recon[1], "halts": halts, "tranches": tranches, "orders": orders,
        "carry_in_tranches": carry, "fills": len(fills), "spent": spent, "qty": qty,
        "coverage": {"repricing (attempt>=1)": reprices, "GTD expiry": expired, "partial fill (order)": partial_orders,
                     "partial tranche": tranches.get("partial", 0), "carry to next window": carry,
                     "unfilled tranche": tranches.get("unfilled", 0), "post-only reject": post_only},
    }
