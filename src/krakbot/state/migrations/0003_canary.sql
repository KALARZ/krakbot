-- 0003: pre-registered LIVE canary for the DCA (plan §5.3): canary tranches, operator verifications, balance baseline,
-- and the 'canary' halt reason (only `krakbot canary-verify` clears it; `resume` never does).

-- risk_state: extend halt_reason with 'canary' (SQLite cannot alter a CHECK: rebuild the table, keep the rows)
CREATE TABLE risk_state_new (
    strategy            TEXT NOT NULL CHECK (strategy IN ('dca','donchian','account')),
    mode                TEXT NOT NULL CHECK (mode IN ('dry_run','live')),
    consecutive_losses  INTEGER NOT NULL DEFAULT 0 CHECK (consecutive_losses >= 0),
    size_mult           TEXT NOT NULL DEFAULT '1' CHECK (size_mult IN ('1','0.5')),
    pause_until         INTEGER NOT NULL DEFAULT 0,
    halted              INTEGER NOT NULL DEFAULT 0 CHECK (halted IN (0,1)),
    halt_reason         TEXT CHECK (halt_reason IS NULL OR halt_reason IN ('daily','total','manual','unknown_order','recon','canary')),
    halt_ref            TEXT,
    day                 INTEGER,
    day_start_equity    TEXT CHECK (day_start_equity IS NULL OR day_start_equity NOT GLOB '*[^0-9.-]*'),
    peak_equity         TEXT CHECK (peak_equity IS NULL OR peak_equity NOT GLOB '*[^0-9.-]*'),
    updated_at          INTEGER NOT NULL,
    PRIMARY KEY (strategy, mode),
    CHECK (halted = 0 OR halt_reason IS NOT NULL)
);
INSERT INTO risk_state_new (strategy, mode, consecutive_losses, size_mult, pause_until, halted, halt_reason, halt_ref,
                            day, day_start_equity, peak_equity, updated_at)
    SELECT strategy, mode, consecutive_losses, size_mult, pause_until, halted, halt_reason, halt_ref, day,
           day_start_equity, peak_equity, updated_at FROM risk_state;
DROP TABLE risk_state;
ALTER TABLE risk_state_new RENAME TO risk_state;

-- a canary tranche buys exactly one order of Kraken's absolute minimum size
ALTER TABLE dca_tranches ADD COLUMN canary INTEGER NOT NULL DEFAULT 0 CHECK (canary IN (0,1));

-- operator verification of a closed LIVE canary tranche: fill / ledger / reconciliation / balance
CREATE TABLE dca_canary_checks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    tranche_id    TEXT NOT NULL REFERENCES dca_tranches(tranche_id),
    checked_at    INTEGER NOT NULL,
    ok            INTEGER NOT NULL CHECK (ok IN (0,1)),
    fill_ok       INTEGER NOT NULL CHECK (fill_ok IN (0,1)),
    ledger_ok     INTEGER NOT NULL CHECK (ledger_ok IN (0,1)),
    recon_ok      INTEGER NOT NULL CHECK (recon_ok IN (0,1)),
    balance_ok    INTEGER NOT NULL CHECK (balance_ok IN (0,1)),
    details_json  TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(details_json)),
    CHECK (ok = (fill_ok AND ledger_ok AND recon_ok AND balance_ok))
);
CREATE INDEX ix_canary_tranche ON dca_canary_checks(tranche_id, checked_at);

-- base-asset balance on Kraken when LIVE first started: the balance check is balance - baseline == bought qty
CREATE TABLE dca_live_baseline (
    asset        TEXT PRIMARY KEY,
    balance      TEXT NOT NULL CHECK (balance NOT GLOB '*[^0-9.]*' AND balance != ''),
    recorded_at  INTEGER NOT NULL,
    run_id       TEXT NOT NULL
);
