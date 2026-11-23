"""Inclusive calendar-day or minute inputs as half-open Beijing-time ranges."""
import re
from datetime import date, datetime, time, timedelta

from fastapi import HTTPException


def time_bounds(start, end):
    def parse(value, upper):
        if not value:
            return None
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            day = date.fromisoformat(value)
            return day + timedelta(days=1) if upper else day
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}", value):
            minute = datetime.fromisoformat(value)
            return minute + timedelta(minutes=1) if upper else minute
        raise ValueError

    try:
        lower, upper = parse(start, False), parse(end, True)
    except (ValueError, OverflowError):
        raise HTTPException(422, "时间范围应填写有效的日期和分钟") from None
    if lower is not None and upper is not None:
        first, last = (value if isinstance(value, datetime) else datetime.combine(value, time.min)
                       for value in (lower, upper))
        if first >= last:
            raise HTTPException(422, "开始时间不能晚于结束时间")
    return lower, upper
