from configparser import ConfigParser
from datetime import datetime, time

import scheduler


def market_config(holidays="2026-09-14"):
    config = ConfigParser()
    config.read_dict(
        {
            "MARKET": {
                "START_TIME": "09:15",
                "END_TIME": "15:30",
                "REPORT_TIME": "15:35",
                "HOLIDAYS": holidays,
            }
        }
    )
    return config


def test_market_time_rejects_configured_nse_holiday():
    config = market_config()

    assert not scheduler.is_trading_day(config, datetime(2026, 9, 14, 12, 0))
    assert not scheduler.is_market_time(config, datetime(2026, 9, 14, 12, 0))


def test_market_time_accepts_regular_weekday():
    config = market_config()

    assert scheduler.is_trading_day(config, datetime(2026, 9, 15, 12, 0))
    assert scheduler.is_market_time(config, datetime(2026, 9, 15, 12, 0))


def test_next_trading_day_skips_holiday():
    config = market_config()
    now = datetime(2026, 9, 11, 16, 0)  # Friday after market close.

    assert scheduler.next_trading_day_at(config, time(9, 15), now) == datetime(2026, 9, 15, 9, 15)


def test_scheduler_wakeup_skips_holiday_events():
    config = market_config()
    now = datetime(2026, 9, 14, 12, 0)

    assert scheduler.next_scheduler_wakeup(config, None, now) == 60
