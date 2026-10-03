"""Resolve a public query account without embedding personal addresses."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config.local.json"
ACCOUNT_ENV = "HYPERLIQUID_ACCOUNT_ADDRESS"


def resolve_account_address(user: str | None = None) -> str:
    """Use an explicit account, then environment, then local configuration."""
    if user is None:
        user = os.environ.get(ACCOUNT_ENV)
    if user is None:
        try:
            config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except FileNotFoundError:
            config = {}
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("unable to read config.local.json as JSON") from exc
        if not isinstance(config, dict):
            raise ValueError("config.local.json must contain a JSON object")
        user = config.get("hyperliquid_account_address")
    if user is None:
        raise ValueError("account required: use --user, HYPERLIQUID_ACCOUNT_ADDRESS or config.local.json")
    if not isinstance(user, str) or re.fullmatch(r"0x[0-9a-fA-F]{40}", user) is None:
        raise ValueError("account must be a 42-character 0x address")
    return user
