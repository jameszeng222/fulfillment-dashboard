"""Keep carrier wall clocks separate from the UTC instants used for calculations."""
from datetime import date, datetime, time, timezone
import json


def carrier_wall_time(raw, iso=None):
    """Prefer the carrier's original date and time, with any supplied timezone."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = None
    date_only = None
    if isinstance(raw, dict):
        try:
            date_only = date.fromisoformat(raw.get("date") or "").isoformat()
            clock = time.fromisoformat(raw.get("time") or "").isoformat()
            zone = raw.get("timezone")
            return f"{date_only} {clock}" + (f" {zone}" if zone else "")
        except (TypeError, ValueError):
            pass
    if isinstance(iso, str):
        try:
            if len(iso.strip()) >= 16:
                return datetime.fromisoformat(iso.strip().replace("Z", "+00:00")).isoformat(sep=" ")
        except ValueError:
            pass
    return date_only


def utc_key(value):
    """Match stored naive UTC timestamps to the archive's offset-bearing format."""
    if value is None or str(value) == "NaT":
        return None
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    aware = moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment.astimezone(timezone.utc)
    return aware.isoformat()


def ensure_display_columns(con):
    con.execute("ALTER TABLE trk_event ADD COLUMN IF NOT EXISTS 当地时间 VARCHAR")
    con.execute("ALTER TABLE trk_milestone ADD COLUMN IF NOT EXISTS 当地时间 VARCHAR")
    con.execute("ALTER TABLE trk_status ADD COLUMN IF NOT EXISTS 最新事件当地时间 VARCHAR")
    con.execute("ALTER TABLE trk_status ADD COLUMN IF NOT EXISTS 签收当地时间 VARCHAR")
