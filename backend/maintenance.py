"""Local schedule for starting game sessions; never interrupts an active session."""

from datetime import UTC, datetime
from math import ceil
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")
MAINTENANCE_MESSAGE = "华立每日04:00–07:00维护，请07:00后重试"


def utc_now():
    return datetime.now(UTC)


def maintenance_status(at: datetime):
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("maintenance clock must return a timezone-aware datetime")
    local = at.astimezone(SHANGHAI)
    active = 4 <= local.hour < 7
    retry_at = local.replace(hour=7, minute=0, second=0, microsecond=0) if active else None
    return {
        "active": active,
        "timezone": "Asia/Shanghai",
        "start_time": "04:00",
        "end_time": "07:00",
        "retry_at": retry_at.isoformat() if retry_at else None,
        "retry_after_seconds": ceil((retry_at - local).total_seconds()) if retry_at else 0,
        "message": MAINTENANCE_MESSAGE if active else None,
    }
