"""Numeric settings read from config.ini (environment variables take precedence)."""
import configparser
import os

import configparser
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)
CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.ini"


DEFAULT_WHATIF_TRADE_LIMIT = 100


DEFAULT_STOCK_MOVE_BACKFILL_LIMIT = 5


def configured_int(section, key, default, env_name=None, minimum=None):
    raw_value = os.getenv(env_name) if env_name else None
    if raw_value is None:
        config = configparser.ConfigParser()
        config.read(CONFIG_PATH)
        raw_value = config.get(section, key, fallback=None)
    try:
        value = int(raw_value) if raw_value not in {None, ""} else int(default)
    except (TypeError, ValueError):
        logger.warning("Invalid integer config %s.%s=%r; using %s", section, key, raw_value, default)
        value = int(default)
    if minimum is not None:
        value = max(minimum, value)
    return value


def configured_float(section, key, default, env_name=None, minimum=None, maximum=None):
    raw_value = os.getenv(env_name) if env_name else None
    if raw_value is None:
        config = configparser.ConfigParser()
        config.read(CONFIG_PATH)
        raw_value = config.get(section, key, fallback=None)
    try:
        value = float(raw_value) if raw_value not in {None, ""} else float(default)
    except (TypeError, ValueError):
        logger.warning("Invalid float config %s.%s=%r; using %s", section, key, raw_value, default)
        value = float(default)
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


WHATIF_TRADE_LIMIT = configured_int(
    "OPTIONS",
    "WHATIF_TRADE_LIMIT",
    DEFAULT_WHATIF_TRADE_LIMIT,
    env_name="WHATIF_TRADE_LIMIT",
    minimum=1,
)


STOCK_MOVE_BACKFILL_LIMIT = configured_int(
    "OPTIONS",
    "STOCK_MOVE_BACKFILL_LIMIT",
    DEFAULT_STOCK_MOVE_BACKFILL_LIMIT,
    env_name="STOCK_MOVE_BACKFILL_LIMIT",
    minimum=0,
)


STCG_TAX_RATE = configured_float("OPTIONS", "STCG_TAX_RATE", 20, env_name="STCG_TAX_RATE", minimum=0, maximum=100)


BUSINESS_INCOME_TAX_RATE = configured_float(
    "OPTIONS",
    "BUSINESS_INCOME_TAX_RATE",
    30,
    env_name="BUSINESS_INCOME_TAX_RATE",
    minimum=0,
    maximum=100,
)


REPORT_PRICE_REFRESH_MAX_AGE_MINUTES = configured_int(
    "APP",
    "REPORT_PRICE_REFRESH_MAX_AGE_MINUTES",
    30,
    env_name="REPORT_PRICE_REFRESH_MAX_AGE_MINUTES",
    minimum=0,
)


REPORT_KEEP_DAYS = configured_int("APP", "REPORT_KEEP_DAYS", 14, env_name="REPORT_KEEP_DAYS", minimum=1)


SCHEDULER_STALE_MINUTES = configured_int(
    "APP",
    "SCHEDULER_STALE_MINUTES",
    45,
    env_name="SCHEDULER_STALE_MINUTES",
    minimum=5,
)


APP_PORT = configured_int("APP", "PORT", 5002, env_name="PORT", minimum=1)
