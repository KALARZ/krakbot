"""Configuration: secrets + CAPITAL from `.env` (mode 600), strategy/limits from `config.toml` (versioned).

Fail-fast: every problem is collected and raised together as ConfigError; the bot never starts with a value outside
the ranges fixed in docs/plan.md §2/§6 and the user's constraints (risk 0.25-1%, DD <= 20%, holding <= 48 h,
1-3 positions, 2-4 pairs, timeframe 5m-4h, Kraken Spot only, DRY_RUN default, Donchian never live).
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tomllib
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from krakbot.domain import Mode
from krakbot.state.gates import CLEAN_DAY_RULE

__all__ = [
    "FREEZE_EXCLUDED", "FREEZE_FROZEN", "AppConfig", "ConfigError", "Secrets", "Settings", "check_env_file_permissions",
    "forward_test_freeze_hash", "load_settings",
]

# BTC maximum drawdown measured in the 2019-2026 sample (docs/research.md §F): bounds the DCA allocation so the
# account drawdown from the DCA sleeve stays <= the 20% user limit.
BTC_SAMPLE_MAX_DRAWDOWN = Decimal("0.766")
USER_MAX_DRAWDOWN = Decimal("0.20")

Dec = Annotated[Decimal, Field(allow_inf_nan=False)]


class ConfigError(Exception):
    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("invalid configuration:\n  - " + "\n  - ".join(problems))


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=False)

    @field_validator("*", mode="before")
    @classmethod
    def _no_float(cls, v: Any) -> Any:
        # TOML floats are rejected for every field: numeric limits must be written as strings or integers,
        # so no binary rounding ever enters a Decimal limit.
        if isinstance(v, float):
            raise ValueError("floats are not allowed: write decimals as strings, e.g. \"0.005\"")
        return v


class General(_Strict):
    quote: Literal["EUR"] = "EUR"
    data_dir: Path = Path("data")
    db_file: str = Field(default="krakbot.db", pattern=r"^[A-Za-z0-9_.-]+\.db$")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"


class Risk(_Strict):
    risk_per_trade: Dec = Field(ge=Decimal("0.0025"), le=Decimal("0.01"))
    max_total_exposure: Dec = Field(gt=0, le=Decimal("0.30"))
    max_position_exposure: Dec = Field(gt=0, le=Decimal("0.30"))
    max_positions: int = Field(ge=1, le=3)
    kill_total_drawdown: Dec = Field(gt=0, le=USER_MAX_DRAWDOWN)
    kill_daily_loss: Dec = Field(gt=0, le=Decimal("0.10"))
    breaker_half_after: int = Field(ge=1, le=10)
    breaker_pause_after: int = Field(ge=2, le=20)
    breaker_pause_hours: int = Field(ge=1, le=168)
    cooldown_bars: int = Field(ge=0, le=20)
    min_order_safety_factor: Dec = Field(ge=1, le=5)

    @model_validator(mode="after")
    def _coherent(self) -> Risk:
        if self.max_position_exposure > self.max_total_exposure:
            raise ValueError("max_position_exposure must be <= max_total_exposure")
        if self.kill_daily_loss >= self.kill_total_drawdown:
            raise ValueError("kill_daily_loss must be < kill_total_drawdown")
        if self.breaker_half_after >= self.breaker_pause_after:
            raise ValueError("breaker_half_after must be < breaker_pause_after")
        return self


class Execution(_Strict):
    entry_reprice_seconds: int = Field(ge=10, le=600)
    entry_max_reprices: int = Field(ge=0, le=10)
    entry_gtd_seconds: int = Field(ge=30, le=3600)
    max_chase: Dec = Field(ge=0, le=Decimal("0.01"))
    exit_reprice_seconds: int = Field(ge=10, le=600)
    exit_max_reprices: int = Field(ge=0, le=10)
    deadline_seconds: int = Field(ge=2, le=60)  # Kraken: deadline in [now+2s, now+60s]
    stop_trigger: Literal["last", "index"] = "last"
    dms_mode: Literal["entry_only", "always"] = "entry_only"
    dms_timeout_seconds: int = Field(ge=30, le=300)
    dms_refresh_seconds: int = Field(ge=5, le=150)

    @model_validator(mode="after")
    def _coherent(self) -> Execution:
        if self.dms_refresh_seconds * 2 > self.dms_timeout_seconds:
            raise ValueError(
                "dms_refresh_seconds must be <= dms_timeout_seconds / 2 (Kraken: refresh 15-30s, timeout 60s)"
            )
        if self.entry_gtd_seconds < self.entry_reprice_seconds + 5:
            raise ValueError(
                "entry_gtd_seconds must exceed entry_reprice_seconds (the GTD must outlive one reprice step)"
            )
        return self


class Liquidity(_Strict):
    reference_days: int = Field(ge=7, le=30)
    sample_seconds: int = Field(ge=1, le=60)
    spread_percentile: int = Field(ge=50, le=99)
    depth_bps: int = Field(ge=1, le=100)
    depth_percentile: int = Field(ge=1, le=50)
    depth_mult_min: Dec = Field(ge=1, le=100)
    min_sample_coverage: Dec = Field(ge=Decimal("0.5"), le=1)
    sample_pairs: tuple[str, ...] = ("XBT/EUR",)

    @field_validator("sample_pairs")
    @classmethod
    def _pairs(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        if "XBT/EUR" not in v:
            raise ValueError("sample_pairs must include the DCA pair XBT/EUR")
        if not 1 <= len(v) <= 12 or len(set(v)) != len(v):
            raise ValueError("sample_pairs: 1-12 distinct WS v1 pair names")
        if any(not p.endswith("/EUR") for p in v):
            raise ValueError("sample_pairs: EUR pairs only (quote currency of the bot)")
        return v


class Monitor(_Strict):
    telegram_enabled: bool = True
    heartbeat_seconds: int = Field(ge=10, le=300)
    ws_down_alert_seconds: int = Field(ge=5, le=600)


class LiveGate(_Strict):
    """Pre-registered clean-day rule (state/gates.py, plan §5.1): only the registered values are accepted."""

    dry_run_clean_days: int
    min_heartbeat_coverage: Dec
    min_sample_coverage: Dec

    @model_validator(mode="after")
    def _registered(self) -> LiveGate:
        r = CLEAN_DAY_RULE
        got = (self.dry_run_clean_days, self.min_heartbeat_coverage, self.min_sample_coverage)
        if got != (r.days, r.min_uptime, r.min_sample_coverage):
            raise ValueError(f"live_gate is pre-registered: dry_run_clean_days={r.days}, "
                             f"min_heartbeat_coverage={r.min_uptime}, min_sample_coverage={r.min_sample_coverage}")
        return self


class Dca(_Strict):
    mode: Mode = Mode.DRY_RUN
    pair: Literal["XBT/EUR"] = "XBT/EUR"
    allocation: Dec = Field(gt=0, le=Decimal("0.25"))
    tranches: int = Field(ge=1, le=520)
    weekday: int = Field(ge=0, le=6)
    hour_utc: int = Field(ge=0, le=23)
    reprice_seconds: int = Field(ge=30, le=3600)
    tranche_window_hours: int = Field(ge=1, le=72)
    max_chase: Dec = Field(ge=0, le=Decimal("0.05"))
    max_tranche_multiple: int = Field(ge=1, le=4)
    stall_alert_weeks: int = Field(ge=1, le=12)
    min_tranche_safety_factor: Dec = Field(ge=1, le=5)
    pause_spread_percentile: int = Field(ge=90, le=100)
    kill_switch_sells_holdings: bool = False  # confirmed off: kill switches never sell DCA holdings (plan §6)
    canary_clean_tranches: int = 3  # pre-registered LIVE canary (plan §5.3): min size until 3 clean tranches in a row

    @field_validator("canary_clean_tranches")
    @classmethod
    def _canary_registered(cls, v: int) -> int:
        if v != 3:
            raise ValueError("canary_clean_tranches is pre-registered at 3 (plan §5.3)")
        return v

    @model_validator(mode="after")
    def _dd_bound(self) -> Dca:
        if self.allocation * BTC_SAMPLE_MAX_DRAWDOWN > USER_MAX_DRAWDOWN:
            raise ValueError(
                f"allocation {self.allocation} x BTC sample max drawdown {BTC_SAMPLE_MAX_DRAWDOWN} "
                "exceeds the 20% account limit"
            )
        return self


class Donchian(_Strict):
    mode: Mode = Mode.DRY_RUN
    timeframe: Literal["5m", "15m", "30m", "1h", "4h"] = "4h"
    channel_bars: int = Field(ge=5, le=400)
    k_stop_atr: Dec = Field(gt=0, le=10)
    k_trail_atr: Dec = Field(ge=0, le=10)
    regime_sma_bars: int = Field(ge=0, le=1000)
    universe_size: int = Field(ge=2, le=4)  # user constraint: 2-4 pairs
    max_hold_hours: int = Field(ge=1, le=48)  # user constraint: holding <= 48 h
    paper_capital_fraction: Dec = Field(gt=0, le=1)

    @field_validator("mode")
    @classmethod
    def _never_live(cls, v: Mode) -> Mode:
        if v == Mode.LIVE:
            raise ValueError(
                "donchian is a DRY_RUN forward test: live is locked "
                "(walk-forward OOS PF 0.95 < 1.3, docs/research.md §E-G)"
            )
        return v


class ForwardTest(_Strict):
    """Pre-registered forward-test rules (docs/plan.md §0.2). Only the registered values validate."""

    reading_fee_tier: Literal[1] = 1
    slippage_bp: Literal[5] = 5
    checkpoint_trades: Literal[100] = 100
    abandon_if_pf_below: Dec = Decimal("1.0")
    abandon_on_total_kill: Literal[True] = True
    unlock_min_trades: Literal[300] = 300
    unlock_min_pf: Dec = Decimal("1.3")
    unlock_min_sharpe: Dec = Decimal("1.0")
    unlock_max_drawdown: Dec = Decimal("0.20")
    unlock_min_positive_months: Dec = Decimal("0.80")
    unlock_fee_stress_mult: Literal[2] = 2
    secondary_hypothesis_alpha: Dec = Decimal("0.05")
    secondary_hypothesis_statistic: Literal["welch_t_one_sided_ret_t1"] = "welch_t_one_sided_ret_t1"
    secondary_hypothesis_split: Literal["rank_halves_by_stop_pct"] = "rank_halves_by_stop_pct"

    @model_validator(mode="after")
    def _registered(self) -> ForwardTest:
        registered = {"abandon_if_pf_below": Decimal("1.0"), "unlock_min_pf": Decimal("1.3"),
                      "unlock_min_sharpe": Decimal("1.0"), "unlock_max_drawdown": Decimal("0.20"),
                      "unlock_min_positive_months": Decimal("0.80"), "secondary_hypothesis_alpha": Decimal("0.05")}
        for name, value in registered.items():
            if getattr(self, name) != value:
                raise ValueError(f"forward_test.{name} is pre-registered at {value} and cannot be changed")
        return self


class Strategies(_Strict):
    dca: Dca
    donchian: Donchian


class AppConfig(_Strict):
    general: General = General()
    risk: Risk
    execution: Execution
    liquidity: Liquidity
    monitor: Monitor
    live_gate: LiveGate
    strategies: Strategies
    forward_test: ForwardTest = ForwardTest()

    @model_validator(mode="after")
    def _capital_split(self) -> AppConfig:
        if self.strategies.dca.allocation + self.strategies.donchian.paper_capital_fraction > 1:
            raise ValueError("dca.allocation + donchian.paper_capital_fraction must be <= 1")
        return self

    @model_validator(mode="after")
    def _sampling_matches_clean_day_rule(self) -> AppConfig:
        r = CLEAN_DAY_RULE  # the clean-day sample count and the threshold eligibility use the registered grid
        liq = self.liquidity
        if liq.sample_seconds != r.sample_seconds or liq.min_sample_coverage != r.min_sample_coverage:
            raise ValueError(f"liquidity.sample_seconds={r.sample_seconds} and liquidity.min_sample_coverage="
                             f"{r.min_sample_coverage} are pre-registered (clean-day rule)")
        return self


class Secrets(BaseSettings):
    """Values from `.env` / process environment. Secret fields never appear in repr/str/model_dump_json."""

    model_config = SettingsConfigDict(env_file=None, extra="ignore", case_sensitive=True, frozen=True)

    CAPITAL: Dec = Field(gt=0, le=Decimal("100000000"))
    KRAKEN_API_KEY: SecretStr | None = None
    KRAKEN_API_SECRET: SecretStr | None = None
    TELEGRAM_BOT_TOKEN: SecretStr | None = None
    TELEGRAM_CHAT_ID: str | None = Field(default=None, pattern=r"^-?[0-9]{1,20}$")
    HEALTHCHECK_URL: SecretStr | None = None
    KRAKBOT_LIVE_ENABLED: bool = False

    @field_validator("KRAKEN_API_KEY", "KRAKEN_API_SECRET", "TELEGRAM_BOT_TOKEN", "HEALTHCHECK_URL",
                     "TELEGRAM_CHAT_ID", mode="before")
    @classmethod
    def _empty_is_none(cls, v: Any) -> Any:
        return None if isinstance(v, str) and v.strip() == "" else v

    @field_validator("CAPITAL", mode="before")
    @classmethod
    def _capital_str(cls, v: Any) -> Any:
        if isinstance(v, float):
            raise ValueError("CAPITAL must be a decimal string, not a float")
        return v

    @field_validator("HEALTHCHECK_URL")
    @classmethod
    def _https(cls, v: SecretStr | None) -> SecretStr | None:
        if v is not None and not v.get_secret_value().startswith("https://"):
            raise ValueError("HEALTHCHECK_URL must be https://")
        return v

    @property
    def has_kraken_keys(self) -> bool:
        return self.KRAKEN_API_KEY is not None and self.KRAKEN_API_SECRET is not None


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True)
    secrets: Secrets
    app: AppConfig
    root: Path

    @property
    def db_path(self) -> Path:
        d = self.app.general.data_dir
        return (d if d.is_absolute() else self.root / d) / self.app.general.db_file

    def modes(self) -> dict[str, Mode]:
        return {"dca": self.app.strategies.dca.mode, "donchian": self.app.strategies.donchian.mode}

    @property
    def any_live(self) -> bool:
        return any(m == Mode.LIVE for m in self.modes().values())


DONCHIAN_STRATEGY_VERSION = "donchian-1"  # bump on any change to the strategy or forward_test.py: closes a running test

# Field-by-field scope of the forward-test freeze hash (docs/plan.md §0.2). Every field of AppConfig must appear in
# exactly one of the two maps (tests/unit/test_config.py enforces it), so a new field cannot silently escape.
# FROZEN: everything that changes the Donchian's simulated PnL or its decision rules.
FREEZE_FROZEN: dict[str, frozenset[str]] = {
    "strategies.donchian": frozenset({"timeframe", "channel_bars", "k_stop_atr", "k_trail_atr", "regime_sma_bars",
                                      "universe_size", "max_hold_hours", "paper_capital_fraction"}),
    "forward_test": frozenset(ForwardTest.model_fields),
    "risk": frozenset(Risk.model_fields),
    "liquidity": frozenset(Liquidity.model_fields),
    "execution": frozenset({"entry_reprice_seconds", "entry_max_reprices", "entry_gtd_seconds", "max_chase",
                            "exit_reprice_seconds", "exit_max_reprices", "stop_trigger"}),
}
# NOT FROZEN: the DCA, operational settings with no effect on simulated fills, and the mode switch.
FREEZE_EXCLUDED: dict[str, frozenset[str]] = {
    "strategies.donchian": frozenset({"mode"}),
    "strategies.dca": frozenset(Dca.model_fields),
    "execution": frozenset({"deadline_seconds", "dms_mode", "dms_timeout_seconds", "dms_refresh_seconds"}),
    "general": frozenset(General.model_fields),
    "monitor": frozenset(Monitor.model_fields),
    "live_gate": frozenset(LiveGate.model_fields),
}


def _section(app: AppConfig, path: str) -> BaseModel:
    obj: Any = app
    for part in path.split("."):
        obj = getattr(obj, part)
    assert isinstance(obj, BaseModel)
    return obj


def forward_test_freeze_hash(app: AppConfig, liquidity_thresholds: dict[str, dict[str, str]] | None = None,
                             strategy_version: str = DONCHIAN_STRATEGY_VERSION) -> str:
    """SHA-256 of exactly the FREEZE_FROZEN fields + the per-pair liquidity thresholds derived from the 7-day
    DRY_RUN + the strategy code version. Stored at forward-test start; any later difference closes the test
    (ABANDONED_CONFIG_CHANGED). DCA fields are deliberately NOT included: tuning the DCA never resets the test."""
    payload: dict[str, Any] = {}
    for path, fields in sorted(FREEZE_FROZEN.items()):
        dumped = _section(app, path).model_dump(mode="json")
        payload[path] = {f: dumped[f] for f in sorted(fields)}
    thresholds = liquidity_thresholds or {}
    payload["liquidity_thresholds"] = {p: dict(sorted(v.items())) for p, v in sorted(thresholds.items())}
    payload["strategy_version"] = strategy_version
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()


def check_env_file_permissions(path: Path) -> list[str]:
    """`.env` must be a regular file owned by the current user and not accessible by group/others (chmod 600)."""
    problems: list[str] = []
    st = path.lstat()
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        problems.append(f"{path} must be a regular file (not a symlink)")
        return problems
    if st.st_mode & 0o077:
        problems.append(f"{path} has permissions {oct(st.st_mode & 0o777)}: run `chmod 600 {path}`")
    if hasattr(os, "getuid") and st.st_uid != os.getuid():
        problems.append(f"{path} is not owned by the user running the bot")
    return problems


def _format_validation(prefix: str, err: ValidationError) -> list[str]:
    out = []
    for e in err.errors():
        loc = ".".join(str(x) for x in e["loc"])
        out.append(f"{prefix}{'.' + loc if loc else ''}: {e['msg']}")
    return out


def load_settings(toml_path: Path, env_path: Path | None) -> Settings:
    """Load and validate everything; raise ConfigError with ALL problems found."""
    problems: list[str] = []
    app: AppConfig | None = None
    secrets: Secrets | None = None

    try:
        with toml_path.open("rb") as fh:
            raw = tomllib.load(fh)
        app = AppConfig.model_validate(raw)
    except FileNotFoundError:
        problems.append(f"config file not found: {toml_path}")
    except tomllib.TOMLDecodeError as exc:
        problems.append(f"{toml_path}: invalid TOML: {exc}")
    except ValidationError as exc:
        problems += _format_validation("config", exc)

    if env_path is not None and env_path.exists():
        problems += check_env_file_permissions(env_path)
    try:
        secrets = Secrets(_env_file=env_path if env_path is not None and env_path.exists() else None)
    except ValidationError as exc:
        problems += _format_validation(".env", exc)

    if app is not None and secrets is not None:
        live = [name for name, m in (("dca", app.strategies.dca.mode),) if m == Mode.LIVE]
        if live:
            if not secrets.has_kraken_keys:
                problems.append("dca.mode=live requires KRAKEN_API_KEY and KRAKEN_API_SECRET in .env")
            if not secrets.KRAKBOT_LIVE_ENABLED:
                problems.append("dca.mode=live requires KRAKBOT_LIVE_ENABLED=true in .env (safety interlock)")
        if app.monitor.telegram_enabled and (secrets.TELEGRAM_BOT_TOKEN is None) != (secrets.TELEGRAM_CHAT_ID is None):
            problems.append("set both TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID, or neither")

    if problems:
        raise ConfigError(problems)
    assert app is not None and secrets is not None
    return Settings(secrets=secrets, app=app, root=toml_path.resolve().parent)
