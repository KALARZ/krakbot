-- 0002: liquidity sampling & derived thresholds (plan §6), forward tests (§0.2), DCA tranches (§0.1),
-- halt reasons for ambiguous orders and failed reconciliation (§4.7).

-- risk_state: extend halt_reason (SQLite cannot alter a CHECK: rebuild the table, keep the rows)
CREATE TABLE risk_state_new (
    strategy            TEXT NOT NULL CHECK (strategy IN ('dca','donchian','account')),
    mode                TEXT NOT NULL CHECK (mode IN ('dry_run','live')),
    consecutive_losses  INTEGER NOT NULL DEFAULT 0 CHECK (consecutive_losses >= 0),
    size_mult           TEXT NOT NULL DEFAULT '1' CHECK (size_mult IN ('1','0.5')),
    pause_until         INTEGER NOT NULL DEFAULT 0,
    halted              INTEGER NOT NULL DEFAULT 0 CHECK (halted IN (0,1)),
    halt_reason         TEXT CHECK (halt_reason IS NULL OR halt_reason IN ('daily','total','manual','unknown_order','recon')),
    halt_ref            TEXT,                         -- e.g. the cl_ord_id of the ambiguous order
    day                 INTEGER,
    day_start_equity    TEXT CHECK (day_start_equity IS NULL OR day_start_equity NOT GLOB '*[^0-9.-]*'),
    peak_equity         TEXT CHECK (peak_equity IS NULL OR peak_equity NOT GLOB '*[^0-9.-]*'),
    updated_at          INTEGER NOT NULL,
    PRIMARY KEY (strategy, mode),
    CHECK (halted = 0 OR halt_reason IS NOT NULL)
);
INSERT INTO risk_state_new (strategy, mode, consecutive_losses, size_mult, pause_until, halted, halt_reason, day,
                            day_start_equity, peak_equity, updated_at)
    SELECT strategy, mode, consecutive_losses, size_mult, pause_until, halted, halt_reason, day, day_start_equity,
           peak_equity, updated_at FROM risk_state;
DROP TABLE risk_state;
ALTER TABLE risk_state_new RENAME TO risk_state;

-- top-of-book samples every `liquidity.sample_seconds` (fixed interval)
CREATE TABLE book_samples (
    pair        TEXT NOT NULL,
    ts          INTEGER NOT NULL,
    mid         TEXT NOT NULL CHECK (mid NOT GLOB '*[^0-9.]*' AND mid != ''),
    spread_bp   TEXT NOT NULL CHECK (spread_bp NOT GLOB '*[^0-9.]*' AND spread_bp != ''),
    bid_depth   TEXT NOT NULL CHECK (bid_depth NOT GLOB '*[^0-9.]*' AND bid_depth != ''),   -- quote within depth_bps
    ask_depth   TEXT NOT NULL CHECK (ask_depth NOT GLOB '*[^0-9.]*' AND ask_depth != ''),
    PRIMARY KEY (pair, ts)
) WITHOUT ROWID;

-- per-pair per-UTC-day percentiles (nearest-rank), the daily report requested for the DRY_RUN
CREATE TABLE spread_daily (
    pair           TEXT NOT NULL,
    day            INTEGER NOT NULL,                  -- epoch day (UTC)
    n              INTEGER NOT NULL CHECK (n >= 0),
    spread_p50     TEXT, spread_p90 TEXT, spread_p95 TEXT, spread_p99 TEXT, spread_max TEXT,
    bid_depth_p5   TEXT, bid_depth_p50 TEXT, ask_depth_p5 TEXT, ask_depth_p50 TEXT,
    computed_at    INTEGER NOT NULL,
    PRIMARY KEY (pair, day)
) WITHOUT ROWID;

-- derived threshold sets (audit trail); a forward test freezes one set_id into its hash
CREATE TABLE liquidity_threshold_sets (
    set_id      TEXT PRIMARY KEY,
    ref_start   INTEGER NOT NULL,
    ref_end     INTEGER NOT NULL CHECK (ref_end > ref_start),
    rule_json   TEXT NOT NULL CHECK (json_valid(rule_json)),
    derived_at  INTEGER NOT NULL
);
CREATE TABLE liquidity_thresholds (
    set_id          TEXT NOT NULL REFERENCES liquidity_threshold_sets(set_id),
    pair            TEXT NOT NULL,
    n_samples       INTEGER NOT NULL,
    coverage        TEXT NOT NULL CHECK (coverage NOT GLOB '*[^0-9.]*'),
    eligible        INTEGER NOT NULL CHECK (eligible IN (0,1)),
    spread_bp_max   TEXT CHECK (spread_bp_max IS NULL OR spread_bp_max NOT GLOB '*[^0-9.]*'),
    spread_bp_pause TEXT CHECK (spread_bp_pause IS NULL OR spread_bp_pause NOT GLOB '*[^0-9.]*'),
    bid_depth_min   TEXT CHECK (bid_depth_min IS NULL OR bid_depth_min NOT GLOB '*[^0-9.]*'),
    ask_depth_min   TEXT CHECK (ask_depth_min IS NULL OR ask_depth_min NOT GLOB '*[^0-9.]*'),
    PRIMARY KEY (set_id, pair),
    CHECK (eligible = 0 OR (spread_bp_max IS NOT NULL AND bid_depth_min IS NOT NULL AND ask_depth_min IS NOT NULL))
);

CREATE TABLE forward_tests (
    run_id            TEXT PRIMARY KEY,
    started_at        INTEGER NOT NULL,
    freeze_hash       TEXT NOT NULL CHECK (length(freeze_hash) = 64),
    threshold_set_id  TEXT NOT NULL REFERENCES liquidity_threshold_sets(set_id),
    status            TEXT NOT NULL CHECK (status IN ('running','abandoned_pf','abandoned_kill',
                                                      'abandoned_config_changed','unlock_eligible','failed_gates',
                                                      'closed_manual')),
    checkpoint_json   TEXT CHECK (checkpoint_json IS NULL OR json_valid(checkpoint_json)),
    secondary_json    TEXT CHECK (secondary_json IS NULL OR json_valid(secondary_json)),
    closed_at         INTEGER,
    CHECK ((status = 'running') = (closed_at IS NULL))
);
CREATE UNIQUE INDEX ux_forward_tests_one_running ON forward_tests(status) WHERE status = 'running';

-- DCA tranches: budget accounting; the tranche's orders/fills hang off trades(trade_id = tranche_id)
CREATE TABLE dca_tranches (
    tranche_id    TEXT PRIMARY KEY REFERENCES trades(trade_id),
    seq           INTEGER NOT NULL CHECK (seq >= 1),
    mode          TEXT NOT NULL CHECK (mode IN ('dry_run','live')),
    window_start  INTEGER NOT NULL,
    window_end    INTEGER NOT NULL CHECK (window_end > window_start),
    nominal       TEXT NOT NULL CHECK (nominal NOT GLOB '*[^0-9.]*'),
    carry_in      TEXT NOT NULL CHECK (carry_in NOT GLOB '*[^0-9.]*'),
    budget        TEXT NOT NULL CHECK (budget NOT GLOB '*[^0-9.]*'),
    spent         TEXT NOT NULL DEFAULT '0' CHECK (spent NOT GLOB '*[^0-9.]*'),     -- quote incl. fees
    qty           TEXT NOT NULL DEFAULT '0' CHECK (qty NOT GLOB '*[^0-9.]*'),
    fees          TEXT NOT NULL DEFAULT '0' CHECK (fees NOT GLOB '*[^0-9.]*'),
    ref_mid       TEXT CHECK (ref_mid IS NULL OR ref_mid NOT GLOB '*[^0-9.]*'),
    status        TEXT NOT NULL CHECK (status IN ('scheduled','working','filled','partial','unfilled','skipped_min')),
    carry_out     TEXT CHECK (carry_out IS NULL OR carry_out NOT GLOB '*[^0-9.]*'),
    created_at    INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL,
    UNIQUE (mode, seq),
    CHECK (status IN ('scheduled','working') OR carry_out IS NOT NULL)
);
-- at most one open (scheduled/working) tranche per mode
CREATE UNIQUE INDEX ux_dca_one_open ON dca_tranches(mode) WHERE status IN ('scheduled','working');
