"""Configuration loading. Secrets come from env / .env only — never config.toml."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


class ConfigError(Exception):
    pass


@dataclass
class Config:
    ha_ws_url: str
    hk_sse_url: str
    hk_home_id: str | None
    ha_token: str


def _load_dotenv(path: Path) -> dict[str, str]:
    """Minimal KEY=VALUE parser. Does not overwrite already-set env vars."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key:
            out[key] = val
    return out


def load_config(base_dir: str | Path = ".") -> Config:
    base = Path(base_dir)
    cfg_path = base / "config.toml"
    if not cfg_path.exists():
        raise ConfigError(
            f"Missing {cfg_path}. Copy config.example.toml to config.toml."
        )
    data = tomllib.loads(cfg_path.read_text())

    try:
        ha_ws_url = data["ha"]["ws_url"]
        hk_sse_url = data["homekit"]["sse_url"]
    except KeyError as e:
        raise ConfigError(f"config.toml missing required key: {e}") from e
    hk_home_id = (data.get("homekit", {}).get("home_id") or "").strip() or None

    # Token: real env wins; fall back to .env in the project dir.
    dotenv = _load_dotenv(base / ".env")
    token = os.environ.get("HA_TOKEN") or dotenv.get("HA_TOKEN")
    if not token:
        raise ConfigError(
            "HA_TOKEN not found. Set it in the environment or in "
            f"{base / '.env'} (HA_TOKEN=...)."
        )
    return Config(ha_ws_url, hk_sse_url, hk_home_id, token)
