"""Inclusive calendar-day or minute inputs as half-open Beijing-time ranges."""
import re
from datetime import date, datetime, time, timedelta

from fastapi import HTTPException


REPORTING_WEEK_YEAR = 2026
_jan_first = date(REPORTING_WEEK_YEAR, 1, 1)
_first_sunday = _jan_first - timedelta(days=(_jan_first.weekday() + 1) % 7)
REPORTING_WEEKS = {
    f"W{index + 1}": (_first_sunday + timedelta(weeks=index), _first_sunday + timedelta(weeks=index + 1))
    for index in range((date(REPORTING_WEEK_YEAR, 12, 31) - _first_sunday).days // 7 + 1)
}


def week_filter(column, values):
    """Build a half-open date predicate for the selected Sunday-to-Saturday reporting weeks."""
    if not values:
        return "", []
    try:
        selected = sorted({REPORTING_WEEKS[value] for value in values})
    except KeyError:
        raise HTTPException(422, f"请选择有效的 {REPORTING_WEEK_YEAR} 年周次") from None
    ranges = []
    for start, end in selected:
        if ranges and ranges[-1][1] == start:
            ranges[-1] = (ranges[-1][0], end)
        else:
            ranges.append((start, end))
    term = f"({column} >= ? AND {column} < ?)"
    return "(" + " OR ".join([term] * len(ranges)) + ")", [day for pair in ranges for day in pair]


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
