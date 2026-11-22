import os
import re
from decimal import Decimal
from enum import Enum
from pathlib import Path

import pytest
from pydantic import BaseModel

from krakbot.config import (
    FREEZE_EXCLUDED,
    FREEZE_FROZEN,
    AppConfig,
    ConfigError,
    check_env_file_permissions,
    forward_test_freeze_hash,
    load_settings,
)
from krakbot.domain import Mode
from tests.conftest import write_env


def _load(d: Path):
    return load_settings(d / "config.toml", d / ".env")


def _patch(d: Path, old: str, new: str):
    p = d / "config.toml"
    s = p.read_text()
    assert old in s, old
    p.write_text(s.replace(old, new, 1))


def _problems(d: Path) -> str:
    with pytest.raises(ConfigError) as ei:
        _load(d)
    return "\n".join(ei.value.problems)


def test_repo_config_is_valid_and_dry_run_by_default(cfg_dir):
    s = _load(cfg_dir)
    assert Decimal("5000") == s.secrets.CAPITAL
    assert s.app.strategies.dca.mode == Mode.DRY_RUN
    assert s.app.strategies.donchian.mode == Mode.DRY_RUN
    assert not s.any_live
    assert s.db_path == cfg_dir / "data" / "krakbot.db"
    assert isinstance(s.app.risk.risk_per_trade, Decimal)


@pytest.mark.parametrize("old,new,needle", [
    ('risk_per_trade = "0.005"', 'risk_per_trade = "0.002"', "risk.risk_per_trade"),
    ('risk_per_trade = "0.005"', 'risk_per_trade = "0.011"', "risk.risk_per_trade"),
    ("max_positions = 3", "max_positions = 4", "risk.max_positions"),
    ('max_total_exposure = "0.30"', 'max_total_exposure = "0.31"', "risk.max_total_exposure"),
    ('kill_total_drawdown = "0.15"', 'kill_total_drawdown = "0.21"', "risk.kill_total_drawdown"),
    ("max_hold_hours = 48", "max_hold_hours = 49", "max_hold_hours"),
    ('timeframe = "4h"', 'timeframe = "1d"', "timeframe"),
    ('timeframe = "4h"', 'timeframe = "1m"', "timeframe"),
    ("universe_size = 4", "universe_size = 5", "universe_size"),
    ("universe_size = 4", "universe_size = 1", "universe_size"),
    ("dry_run_clean_days = 7", "dry_run_clean_days = 6", "dry_run_clean_days"),
    ("deadline_seconds = 10", "deadline_seconds = 61", "deadline_seconds"),
    ("dms_refresh_seconds = 20", "dms_refresh_seconds = 31", "dms_refresh_seconds"),
    ('dms_mode = "entry_only"', 'dms_mode = "off"', "dms_mode"),
    ('allocation = "0.20"', 'allocation = "0.30"', "allocation"),
    ('quote = "EUR"', 'quote = "USD"', "general.quote"),
    ('risk_per_trade = "0.005"', "risk_per_trade = 0.005", "floats are not allowed"),
])
def test_out_of_range_values_fail_fast(cfg_dir, old, new, needle):
    _patch(cfg_dir, old, new)
    assert needle in _problems(cfg_dir)


def test_unknown_keys_are_rejected(cfg_dir):
    _patch(cfg_dir, "[risk]\n", "[risk]\nleverage = 3\n")
    assert "leverage" in _problems(cfg_dir)


def test_cross_field_rules(cfg_dir):
    _patch(cfg_dir, 'max_position_exposure = "0.15"', 'max_position_exposure = "0.30"')
    _patch(cfg_dir, 'max_total_exposure = "0.30"', 'max_total_exposure = "0.20"')
    assert "max_position_exposure must be <= max_total_exposure" in _problems(cfg_dir)


def test_capital_split_cannot_exceed_one(cfg_dir):
    _patch(cfg_dir, 'paper_capital_fraction = "0.80"', 'paper_capital_fraction = "0.90"')
    assert "paper_capital_fraction must be <= 1" in _problems(cfg_dir)


def test_donchian_live_is_always_rejected(cfg_dir):
    _patch(cfg_dir, 'mode = "dry_run"                  # forward test only', 'mode = "live"  # forward test only')
    write_env(cfg_dir / ".env", "CAPITAL=5000\nKRAKEN_API_KEY=k\nKRAKEN_API_SECRET=s\nKRAKBOT_LIVE_ENABLED=true\n")
    assert "donchian is a DRY_RUN forward test" in _problems(cfg_dir)


def test_dca_live_requires_keys_and_interlock(cfg_dir):
    _patch(cfg_dir, 'mode = "dry_run"                  # off | dry_run | live', 'mode = "live"')
    msg = _problems(cfg_dir)
    assert "requires KRAKEN_API_KEY and KRAKEN_API_SECRET" in msg
    assert "KRAKBOT_LIVE_ENABLED=true" in msg
    write_env(cfg_dir / ".env", "CAPITAL=5000\nKRAKEN_API_KEY=k\nKRAKEN_API_SECRET=s\nKRAKBOT_LIVE_ENABLED=true\n")
    s = _load(cfg_dir)
    assert s.app.strategies.dca.mode == Mode.LIVE and s.any_live


@pytest.mark.parametrize("capital", ["0", "-5", "abc", "", "1e400", "nan"])
def test_capital_must_be_a_positive_finite_decimal(cfg_dir, capital):
    write_env(cfg_dir / ".env", f"CAPITAL={capital}\n")
    assert ".env.CAPITAL" in _problems(cfg_dir)


def test_capital_missing(cfg_dir):
    write_env(cfg_dir / ".env", "KRAKEN_API_KEY=\n")
    assert "CAPITAL" in _problems(cfg_dir)


def test_env_file_must_be_600(cfg_dir):
    os.chmod(cfg_dir / ".env", 0o644)
    assert "chmod 600" in _problems(cfg_dir)
    os.chmod(cfg_dir / ".env", 0o640)
    assert check_env_file_permissions(cfg_dir / ".env")


def test_env_symlink_rejected(cfg_dir, tmp_path):
    real = write_env(tmp_path / "real.env", "CAPITAL=1\n")
    (cfg_dir / ".env").unlink()
    (cfg_dir / ".env").symlink_to(real)
    assert "regular file" in _problems(cfg_dir)


def test_secrets_never_leak_in_repr_or_json(cfg_dir):
    write_env(cfg_dir / ".env", "CAPITAL=5000\nKRAKEN_API_KEY=APIKEY123456\nKRAKEN_API_SECRET=c2VjcmV0c2VjcmV0\n"
                                "TELEGRAM_BOT_TOKEN=123:ABCdef\nTELEGRAM_CHAT_ID=42\nHEALTHCHECK_URL=https://hc.example/x/tok\n")
    s = _load(cfg_dir)
    dumped = repr(s) + str(s) + s.model_dump_json() + repr(s.secrets)
    for secret in ("APIKEY123456", "c2VjcmV0c2VjcmV0", "ABCdef", "hc.example/x/tok"):
        assert secret not in dumped
    assert s.secrets.KRAKEN_API_KEY.get_secret_value() == "APIKEY123456"


def test_empty_optional_secrets_are_none(cfg_dir):
    write_env(cfg_dir / ".env", "CAPITAL=100\nKRAKEN_API_KEY=\nTELEGRAM_BOT_TOKEN=\nTELEGRAM_CHAT_ID=\n")
    s = _load(cfg_dir)
    assert s.secrets.KRAKEN_API_KEY is None and not s.secrets.has_kraken_keys


def test_telegram_needs_both_values(cfg_dir):
    write_env(cfg_dir / ".env", "CAPITAL=100\nTELEGRAM_BOT_TOKEN=1:x\n")
    assert "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID" in _problems(cfg_dir)


def test_healthcheck_must_be_https(cfg_dir):
    write_env(cfg_dir / ".env", "CAPITAL=100\nHEALTHCHECK_URL=http://insecure\n")
    assert "https" in _problems(cfg_dir)


def test_all_problems_are_reported_together(cfg_dir):
    _patch(cfg_dir, "max_positions = 3", "max_positions = 9")
    _patch(cfg_dir, "max_hold_hours = 48", "max_hold_hours = 72")
    write_env(cfg_dir / ".env", "CAPITAL=-1\n")
    msg = _problems(cfg_dir)
    assert re.search("max_positions", msg) and re.search("max_hold_hours", msg) and re.search("CAPITAL", msg)


def test_env_example_is_safe():
    root = Path(__file__).resolve().parents[2]
    text = (root / ".env.example").read_text()
    assert "KRAKBOT_LIVE_ENABLED=false" in text
    for line in text.splitlines():
        if line.startswith(("KRAKEN_API_KEY=", "KRAKEN_API_SECRET=", "TELEGRAM_BOT_TOKEN=")):
            assert line.split("=", 1)[1] == "", "example must not contain credentials"
    assert ".env" in (root / ".gitignore").read_text().splitlines()


@pytest.mark.parametrize("old,new", [
    ("reading_fee_tier = 1", "reading_fee_tier = 2"),
    ("checkpoint_trades = 100", "checkpoint_trades = 120"),
    ('abandon_if_pf_below = "1.0"', 'abandon_if_pf_below = "0.9"'),
    ('unlock_min_pf = "1.3"', 'unlock_min_pf = "1.2"'),
    ("unlock_min_trades = 300", "unlock_min_trades = 200"),
    ("abandon_on_total_kill = true", "abandon_on_total_kill = false"),
    ("slippage_bp = 5", "slippage_bp = 2"),
    ('secondary_hypothesis_alpha = "0.05"', 'secondary_hypothesis_alpha = "0.10"'),
    ('secondary_hypothesis_statistic = "welch_t_one_sided_ret_t1"', 'secondary_hypothesis_statistic = "mann_whitney"'),
])
def test_forward_test_rules_are_pre_registered(cfg_dir, old, new):
    _patch(cfg_dir, old, new)
    assert "forward_test" in _problems(cfg_dir)


def test_forward_test_defaults_loaded(cfg_dir):
    ft = _load(cfg_dir).app.forward_test
    assert ft.reading_fee_tier == 1 and ft.checkpoint_trades == 100 and ft.abandon_if_pf_below == Decimal("1.0")


@pytest.mark.parametrize("old,new,needle", [
    ('max_chase = "0.02"', 'max_chase = "0.06"', "dca.max_chase"),
    ("max_tranche_multiple = 2", "max_tranche_multiple = 5", "max_tranche_multiple"),
    ("reprice_seconds = 300", "reprice_seconds = 10", "reprice_seconds"),
    ('min_tranche_safety_factor = "1.5"', 'min_tranche_safety_factor = "0.9"', "min_tranche_safety_factor"),
    ("spread_percentile = 95", "spread_percentile = 100", "spread_percentile"),
    ("reference_days = 7", "reference_days = 3", "reference_days"),
])
def test_dca_execution_ranges(cfg_dir, old, new, needle):
    _patch(cfg_dir, old, new)
    assert needle in _problems(cfg_dir)


def _all_fields():
    out = []

    def walk(model_cls, prefix):
        for name, f in model_cls.model_fields.items():
            ann = f.annotation
            if isinstance(ann, type) and issubclass(ann, BaseModel):
                walk(ann, f"{prefix}{name}.")
            else:
                out.append((prefix.rstrip("."), name))
    walk(AppConfig, "")
    return out


def test_every_config_field_is_classified_exactly_once():
    for section, name in _all_fields():
        frozen = name in FREEZE_FROZEN.get(section, frozenset())
        excluded = name in FREEZE_EXCLUDED.get(section, frozenset())
        assert frozen != excluded, f"{section}.{name}: frozen={frozen} excluded={excluded}"
    known = set(_all_fields())
    for m in (FREEZE_FROZEN, FREEZE_EXCLUDED):
        for section, names in m.items():
            for n in names:
                assert (section, n) in known, f"stale entry {section}.{n}"


def _perturbed(app, section, name):
    parts = section.split(".")
    objs = [app]
    for part in parts:
        objs.append(getattr(objs[-1], part))
    old = getattr(objs[-1], name)
    if isinstance(old, Enum):
        new = next(m for m in type(old) if m != old)
    elif isinstance(old, bool):
        new = not old
    elif isinstance(old, int):
        new = old + 1
    elif isinstance(old, Decimal):
        new = old + Decimal("0.0001")
    elif isinstance(old, tuple):
        new = (*old, "ZZZ/EUR")
    elif isinstance(old, Path):
        new = Path(str(old) + "_x")
    else:
        new = str(old) + "_x"
    leaf = objs[-1].model_copy(update={name: new})  # model_copy does not validate: fine for hashing
    for parent, part in zip(reversed(objs[:-1]), reversed(parts), strict=True):
        leaf = parent.model_copy(update={part: leaf})
    return leaf


@pytest.mark.parametrize("section,name", _all_fields())
def test_freeze_hash_covers_exactly_the_frozen_fields(cfg_dir, section, name):
    app = _load(cfg_dir).app
    before = forward_test_freeze_hash(app)
    after = forward_test_freeze_hash(_perturbed(app, section, name))
    frozen = name in FREEZE_FROZEN.get(section, frozenset())
    assert (before != after) == frozen, f"{section}.{name} frozen={frozen}"


def test_freeze_hash_dca_never_resets_the_forward_test(cfg_dir):
    assert not any(k.startswith("strategies.dca") for k in FREEZE_FROZEN)


def test_freeze_hash_includes_liquidity_thresholds_and_version(cfg_dir):
    app = _load(cfg_dir).app
    a = forward_test_freeze_hash(app, {"XBT/EUR": {"spread_bp_max": "1.2", "depth_min": "50000"}})
    b = forward_test_freeze_hash(app, {"XBT/EUR": {"spread_bp_max": "1.3", "depth_min": "50000"}})
    c = forward_test_freeze_hash(app, {"XBT/EUR": {"depth_min": "50000", "spread_bp_max": "1.2"}})
    assert a != b and a == c  # order-insensitive, value-sensitive
    assert forward_test_freeze_hash(app) != forward_test_freeze_hash(app, strategy_version="donchian-2")
    assert forward_test_freeze_hash(app) == forward_test_freeze_hash(_load(cfg_dir).app)  # stable across loads
