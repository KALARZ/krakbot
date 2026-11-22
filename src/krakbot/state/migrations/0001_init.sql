-- 0001_init: initial schema (docs/plan.md §3). Prices/quantities/money are TEXT holding plain-notation decimals
-- (never floats, never exponent notation); timestamps are INTEGER epoch milliseconds UTC.
-- CHECK lists mirror src/krakbot/domain.py (kept in sync by tests/unit/test_schema.py).

CREATE TABLE meta (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  INTEGER NOT NULL
);

CREATE TABLE instruments (
    pair            TEXT PRIMARY KEY,                 -- wsname, e.g. 'XBT/EUR'
    altname         TEXT NOT NULL UNIQUE,             -- e.g. 'XBTEUR'
    rest_key        TEXT NOT NULL UNIQUE,             -- e.g. 'XXBTZEUR'
    base            TEXT NOT NULL,
    quote           TEXT NOT NULL,
    pair_decimals   INTEGER NOT NULL CHECK (pair_decimals BETWEEN 0 AND 18),
    lot_decimals    INTEGER NOT NULL CHECK (lot_decimals BETWEEN 0 AND 18),
    cost_decimals   INTEGER NOT NULL CHECK (cost_decimals BETWEEN 0 AND 18),
    tick_size       TEXT NOT NULL CHECK (tick_size GLOB '[0-9]*' AND tick_size NOT GLOB '*[^0-9.]*'),
    ordermin        TEXT NOT NULL CHECK (ordermin GLOB '[0-9]*' AND ordermin NOT GLOB '*[^0-9.]*'),
    costmin         TEXT NOT NULL CHECK (costmin GLOB '[0-9]*' AND costmin NOT GLOB '*[^0-9.]*'),
    status          TEXT NOT NULL,
    updated_at      INTEGER NOT NULL
);

CREATE TABLE candles (
    pair        TEXT NOT NULL,
    tf          TEXT NOT NULL CHECK (tf IN ('5m','15m','30m','1h','4h','1d')),
    ts_open     INTEGER NOT NULL,
    o           TEXT NOT NULL CHECK (o NOT GLOB '*[^0-9.]*'),
    h           TEXT NOT NULL CHECK (h NOT GLOB '*[^0-9.]*'),
    l           TEXT NOT NULL CHECK (l NOT GLOB '*[^0-9.]*'),
    c           TEXT NOT NULL CHECK (c NOT GLOB '*[^0-9.]*'),
    v           TEXT NOT NULL CHECK (v NOT GLOB '*[^0-9.]*'),
    n_trades    INTEGER NOT NULL DEFAULT 0,
    source      TEXT NOT NULL CHECK (source IN ('rest','ws')),
    PRIMARY KEY (pair, tf, ts_open)
) WITHOUT ROWID;

CREATE TABLE signals (
    signal_id         TEXT PRIMARY KEY,
    ts                INTEGER NOT NULL,
    strategy          TEXT NOT NULL CHECK (strategy IN ('dca','donchian')),
    strategy_version  TEXT NOT NULL,
    mode              TEXT NOT NULL CHECK (mode IN ('dry_run','live')),
    pair              TEXT NOT NULL,
    action            TEXT NOT NULL CHECK (action IN ('buy','exit','hold')),
    confidence        TEXT CHECK (confidence IS NULL OR confidence NOT GLOB '*[^0-9.]*'),
    sl                TEXT CHECK (sl IS NULL OR sl NOT GLOB '*[^0-9.]*'),
    tp                TEXT CHECK (tp IS NULL OR tp NOT GLOB '*[^0-9.]*'),
    reason_json       TEXT NOT NULL CHECK (json_valid(reason_json))
);
CREATE INDEX ix_signals_ts ON signals(strategy, mode, ts);

CREATE TABLE trades (
    trade_id      TEXT PRIMARY KEY,
    signal_id     TEXT REFERENCES signals(signal_id),
    strategy      TEXT NOT NULL CHECK (strategy IN ('dca','donchian')),
    mode          TEXT NOT NULL CHECK (mode IN ('dry_run','live')),
    pair          TEXT NOT NULL,
    state         TEXT NOT NULL CHECK (state IN ('SIGNALLED','ENTRY_WORKING','ENTRY_CANCELLED','OPEN_PROTECTED',
                                                 'UNPROTECTED','EXITING','EXIT_MARKET','FLATTENING','STOPPED',
                                                 'CLOSED','HELD')),
    qty_target    TEXT NOT NULL CHECK (qty_target NOT GLOB '*[^0-9.]*'),
    qty_filled    TEXT NOT NULL DEFAULT '0' CHECK (qty_filled NOT GLOB '*[^0-9.]*'),
    avg_entry     TEXT CHECK (avg_entry IS NULL OR avg_entry NOT GLOB '*[^0-9.]*'),
    stop_price    TEXT CHECK (stop_price IS NULL OR stop_price NOT GLOB '*[^0-9.]*'),
    tp_price      TEXT CHECK (tp_price IS NULL OR tp_price NOT GLOB '*[^0-9.]*'),
    guard_price   TEXT CHECK (guard_price IS NULL OR guard_price NOT GLOB '*[^0-9.]*'),
    opened_at     INTEGER,
    closed_at     INTEGER,
    exit_reason   TEXT,
    realized_pnl  TEXT CHECK (realized_pnl IS NULL OR realized_pnl NOT GLOB '*[^0-9.-]*'),
    fees          TEXT NOT NULL DEFAULT '0' CHECK (fees NOT GLOB '*[^0-9.]*'),
    version       INTEGER NOT NULL DEFAULT 0,       -- optimistic locking
    created_at    INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL
);
-- "one position per pair, never pyramid": at most one non-terminal trade per (strategy, mode, pair)
CREATE UNIQUE INDEX ux_trades_one_open ON trades(strategy, mode, pair)
    WHERE state NOT IN ('ENTRY_CANCELLED','STOPPED','CLOSED','HELD');
CREATE INDEX ix_trades_state ON trades(state);

CREATE TABLE orders (
    local_id       TEXT PRIMARY KEY,
    trade_id       TEXT NOT NULL REFERENCES trades(trade_id),
    mode           TEXT NOT NULL CHECK (mode IN ('dry_run','live')),
    role           TEXT NOT NULL CHECK (role IN ('entry','stop','exit','tp','flatten','recover','restop','dca')),
    cl_ord_id      TEXT NOT NULL UNIQUE,
    txid           TEXT UNIQUE,
    pair           TEXT NOT NULL,
    side           TEXT NOT NULL CHECK (side IN ('buy','sell')),
    ordertype      TEXT NOT NULL CHECK (ordertype IN ('limit','market','stop-loss')),
    price          TEXT CHECK (price IS NULL OR price NOT GLOB '*[^0-9.]*'),
    trigger_price  TEXT CHECK (trigger_price IS NULL OR trigger_price NOT GLOB '*[^0-9.]*'),
    qty            TEXT NOT NULL CHECK (qty NOT GLOB '*[^0-9.]*'),
    filled_qty     TEXT NOT NULL DEFAULT '0' CHECK (filled_qty NOT GLOB '*[^0-9.]*'),
    avg_price      TEXT CHECK (avg_price IS NULL OR avg_price NOT GLOB '*[^0-9.]*'),
    status         TEXT NOT NULL CHECK (status IN ('intent','sent','open','partially_filled','filled','canceled',
                                                   'expired','rejected','unknown')),
    attempt        INTEGER NOT NULL DEFAULT 0,
    post_only      INTEGER NOT NULL DEFAULT 0 CHECK (post_only IN (0,1)),
    expire_at      INTEGER,
    cancel_reason  TEXT,
    created_at     INTEGER NOT NULL,
    updated_at     INTEGER NOT NULL,
    last_error     TEXT,
    CHECK (ordertype != 'stop-loss' OR trigger_price IS NOT NULL),
    CHECK (ordertype != 'limit' OR price IS NOT NULL),
    CHECK (post_only = 0 OR ordertype = 'limit')
);
CREATE INDEX ix_orders_trade ON orders(trade_id);
CREATE INDEX ix_orders_status ON orders(status);

CREATE TABLE fills (
    exec_id    TEXT PRIMARY KEY,                     -- Kraken trade id (live) or simulated id (dry_run)
    local_id   TEXT NOT NULL REFERENCES orders(local_id),
    txid       TEXT,
    trade_id   TEXT NOT NULL REFERENCES trades(trade_id),
    mode       TEXT NOT NULL CHECK (mode IN ('dry_run','live')),
    ts         INTEGER NOT NULL,
    price      TEXT NOT NULL CHECK (price NOT GLOB '*[^0-9.]*'),
    qty        TEXT NOT NULL CHECK (qty NOT GLOB '*[^0-9.]*'),
    cost       TEXT NOT NULL CHECK (cost NOT GLOB '*[^0-9.]*'),
    fee        TEXT NOT NULL CHECK (fee NOT GLOB '*[^0-9.]*'),
    fee_ccy    TEXT NOT NULL,
    liquidity  TEXT NOT NULL CHECK (liquidity IN ('m','t'))
);
CREATE INDEX ix_fills_trade ON fills(trade_id, ts);

CREATE TABLE equity (
    ts          INTEGER NOT NULL,
    strategy    TEXT NOT NULL CHECK (strategy IN ('dca','donchian','account')),
    mode        TEXT NOT NULL CHECK (mode IN ('dry_run','live')),
    equity      TEXT NOT NULL CHECK (equity NOT GLOB '*[^0-9.-]*'),
    cash        TEXT NOT NULL CHECK (cash NOT GLOB '*[^0-9.-]*'),
    exposure    TEXT NOT NULL CHECK (exposure NOT GLOB '*[^0-9.]*'),
    unrealized  TEXT NOT NULL CHECK (unrealized NOT GLOB '*[^0-9.-]*'),
    realized    TEXT NOT NULL CHECK (realized NOT GLOB '*[^0-9.-]*'),
    peak        TEXT NOT NULL CHECK (peak NOT GLOB '*[^0-9.-]*'),
    dd          TEXT NOT NULL CHECK (dd NOT GLOB '*[^0-9.]*'),
    PRIMARY KEY (ts, strategy, mode)
) WITHOUT ROWID;

CREATE TABLE risk_state (
    strategy            TEXT NOT NULL CHECK (strategy IN ('dca','donchian','account')),
    mode                TEXT NOT NULL CHECK (mode IN ('dry_run','live')),
    consecutive_losses  INTEGER NOT NULL DEFAULT 0 CHECK (consecutive_losses >= 0),
    size_mult           TEXT NOT NULL DEFAULT '1' CHECK (size_mult IN ('1','0.5')),
    pause_until         INTEGER NOT NULL DEFAULT 0,
    halted              INTEGER NOT NULL DEFAULT 0 CHECK (halted IN (0,1)),
    halt_reason         TEXT CHECK (halt_reason IS NULL OR halt_reason IN ('daily','total','manual')),
    day                 INTEGER,
    day_start_equity    TEXT CHECK (day_start_equity IS NULL OR day_start_equity NOT GLOB '*[^0-9.-]*'),
    peak_equity         TEXT CHECK (peak_equity IS NULL OR peak_equity NOT GLOB '*[^0-9.-]*'),
    updated_at          INTEGER NOT NULL,
    PRIMARY KEY (strategy, mode),
    CHECK (halted = 0 OR halt_reason IS NOT NULL)
);

CREATE TABLE cooldowns (
    strategy  TEXT NOT NULL,
    mode      TEXT NOT NULL CHECK (mode IN ('dry_run','live')),
    pair      TEXT NOT NULL,
    until     INTEGER NOT NULL,
    PRIMARY KEY (strategy, mode, pair)
);

CREATE TABLE events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            INTEGER NOT NULL,
    level         TEXT NOT NULL CHECK (level IN ('debug','info','warning','error','critical')),
    kind          TEXT NOT NULL,
    strategy      TEXT,
    mode          TEXT CHECK (mode IS NULL OR mode IN ('dry_run','live')),
    signal_id     TEXT,
    trade_id      TEXT,
    payload_json  TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(payload_json))
);
CREATE INDEX ix_events_ts ON events(ts);
-- the audit log is append-only
CREATE TRIGGER events_no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT, 'events is append-only'); END;
CREATE TRIGGER events_no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT, 'events is append-only'); END;

-- process runs: an unfinished run that is not the current process = unclean exit (crash) for the live gate
CREATE TABLE runs (
    run_id      TEXT PRIMARY KEY,
    started_at  INTEGER NOT NULL,
    ended_at    INTEGER,
    clean_exit  INTEGER CHECK (clean_exit IS NULL OR clean_exit IN (0,1)),
    exit_reason TEXT,
    version     TEXT NOT NULL,
    modes_json  TEXT NOT NULL CHECK (json_valid(modes_json))
);

-- one row per healthy minute of the main loop (epoch minute); coverage per UTC day = rows / 1440
CREATE TABLE heartbeats (
    minute  INTEGER PRIMARY KEY,
    run_id  TEXT NOT NULL REFERENCES runs(run_id)
) WITHOUT ROWID;

-- reconciliation results (live: exchange vs DB; dry_run: simulated broker vs DB)
CREATE TABLE recon_checks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            INTEGER NOT NULL,
    strategy      TEXT NOT NULL,
    mode          TEXT NOT NULL CHECK (mode IN ('dry_run','live')),
    ok            INTEGER NOT NULL CHECK (ok IN (0,1)),
    details_json  TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(details_json))
);
CREATE INDEX ix_recon_ts ON recon_checks(strategy, mode, ts);
