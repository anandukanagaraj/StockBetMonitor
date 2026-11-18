import json
import logging
import os
from datetime import datetime
from pathlib import Path


logger = logging.getLogger(__name__)
BASE_DIR = Path(__file__).resolve().parents[1]
HEARTBEAT_PATH = BASE_DIR / "data" / "scheduler_heartbeat.json"


_MISSING = object()


def write_heartbeat(status="ok", last_market_check_at=_MISSING):
    if last_market_check_at is _MISSING:
        existing = read_heartbeat()
        last_market_check_at = existing.get("last_market_check_at") if existing else None

    payload = {
        "at": datetime.now().replace(microsecond=0).isoformat(),
        "status": status,
        "pid": os.getpid(),
        "last_market_check_at": last_market_check_at,
    }
    HEARTBEAT_PATH.parent.mkdir(parents=True, exist_ok=True)
    HEARTBEAT_PATH.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return payload


def read_heartbeat():
    if not HEARTBEAT_PATH.exists():
        return None
    try:
        return json.loads(HEARTBEAT_PATH.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("Could not read scheduler heartbeat: %s", HEARTBEAT_PATH)
        return None


def scheduler_process_running(heartbeat):
    if not heartbeat:
        return False
    pid = heartbeat.get("pid")
    if not pid:
        return False
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if not handle:
                return False
            try:
                exit_code = wintypes.DWORD()
                if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return False
                return exit_code.value == STILL_ACTIVE
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        except Exception:
            logger.exception("Could not verify scheduler process pid=%s", pid)
            return False
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def heartbeat_age_minutes(heartbeat):
    if not heartbeat or not heartbeat.get("at"):
        return None
    try:
        heartbeat_at = datetime.fromisoformat(heartbeat["at"])
    except ValueError:
        return None
    return round((datetime.now() - heartbeat_at).total_seconds() / 60, 1)


def is_heartbeat_stale(heartbeat, stale_minutes):
    age = heartbeat_age_minutes(heartbeat)
    if age is None:
        return True
    return age > stale_minutes


def market_check_age_minutes(heartbeat):
    if not heartbeat or not heartbeat.get("last_market_check_at"):
        return None
    try:
        checked_at = datetime.fromisoformat(heartbeat["last_market_check_at"])
    except ValueError:
        return None
    return round((datetime.now() - checked_at).total_seconds() / 60, 1)
