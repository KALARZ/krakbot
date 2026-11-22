import os
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SECRET_VARS = ["CAPITAL", "KRAKEN_API_KEY", "KRAKEN_API_SECRET", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
               "HEALTHCHECK_URL", "KRAKBOT_LIVE_ENABLED"]


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    for v in SECRET_VARS:
        monkeypatch.delenv(v, raising=False)


@pytest.fixture
def cfg_dir(tmp_path):
    """A temp dir with a copy of the repo config.toml and a valid .env (chmod 600)."""
    shutil.copy(ROOT / "config.toml", tmp_path / "config.toml")
    env = tmp_path / ".env"
    env.write_text("CAPITAL=5000\n")
    os.chmod(env, 0o600)
    return tmp_path


def write_env(path: Path, text: str, mode: int = 0o600) -> Path:
    path.write_text(text)
    os.chmod(path, mode)
    return path
