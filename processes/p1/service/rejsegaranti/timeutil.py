from __future__ import annotations

from datetime import date, datetime, timezone

try:
    from zoneinfo import ZoneInfo

    CPH = ZoneInfo("Europe/Copenhagen")
except Exception:  # pragma: no cover - tzdata missing (some Windows setups)
    CPH = None


def parse_ts(value) -> datetime:
    """Parse an ISO-8601 timestamp; naive values are taken as Copenhagen time."""
    if isinstance(value, datetime):
        dt = value
    else:
        s = str(value).strip().replace("Z", "+00:00")
        # Camunda writes "+0100" without a colon
        if len(s) > 5 and s[-5] in "+-" and s[-4:].isdigit() and ":" not in s[-5:]:
            s = s[:-2] + ":" + s[-2:]
        dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CPH or timezone.utc)
    return dt


def local_date(dt: datetime) -> date:
    return dt.astimezone(CPH).date() if CPH else dt.date()


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def camunda_ts(dt: datetime) -> str:
    """Camunda REST date format: yyyy-MM-dd'T'HH:mm:ss.SSSZ"""
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000%z")


def now() -> datetime:
    return datetime.now(timezone.utc)
