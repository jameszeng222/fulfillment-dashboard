from datetime import date, datetime
import unittest

import duckdb

from app.channel_timing import workday_minutes
from app.pages_api import DELIVERY_WINDOWS, _period_is_partial, delivery_rates, delivery_window_counts


class DeliveryRatesTest(unittest.TestCase):
    def test_trend_marks_cutoff_and_calendar_filter_edges_partial(self):
        first, latest = datetime(2026, 1, 1, 1), datetime(2026, 11, 27, 10)
        def partial(start, end, bounds=(None, None), months=(), weeks=()):
            return _period_is_partial(date.fromisoformat(start), date.fromisoformat(end),
                                      first, latest, bounds, months, weeks)
        self.assertFalse(partial("2026-10-01", "2026-11-01", months=("2026-10",)))
        self.assertTrue(partial("2026-11-01", "2026-12-01"))
        self.assertTrue(partial("2026-09-27", "2026-10-04", months=("2026-10",)))
        self.assertTrue(partial("2026-10-01", "2026-11-01", (datetime(2026, 10, 1, 8), None)))
        self.assertTrue(partial("2026-10-01", "2026-11-01", weeks=((date(2026, 10, 4), date(2026, 10, 11)),)))

    def report(self, durations):
        with duckdb.connect() as db:
            total, *counts = db.execute(f"""
                SELECT count(*), {delivery_window_counts("minutes")}
                FROM unnest(?::DOUBLE[]) AS orders(minutes)
            """, [durations]).fetchone()
        return delivery_rates(dict(zip(DELIVERY_WINDOWS, counts, strict=True)), total)

    def test_all_orders_are_in_denominator(self):
        durations = (
            [2880] * 40 + [4320] * 15 + [7200] * 15 + [8640] * 5
            + [10080] * 3 + [10080.001] * 2 + [None] * 18 + [0, -1]
        )
        self.assertEqual(self.report(durations), {
            "2": 40.0, "3": 55.0, "5": 70.0, "6": 75.0, "7": 78.0,
        })

    def test_zero_confirmed_versus_no_orders(self):
        self.assertEqual(self.report([None, None]), {str(day): 0.0 for day in DELIVERY_WINDOWS})
        self.assertEqual(self.report([]), {str(day): None for day in DELIVERY_WINDOWS})

    def test_weekday_time_uses_only_elapsed_monday_to_friday_hours(self):
        with duckdb.connect() as db:
            durations = db.execute(f"""
                SELECT {workday_minutes("started", "finished")} FROM (VALUES
                    (TIMESTAMP '2026-11-20 10:00:00', TIMESTAMP '2026-11-23 10:00:00'),
                    (TIMESTAMP '2026-11-20 10:00:00.123456', TIMESTAMP '2026-11-24 10:00:00.123456'),
                    (TIMESTAMP '2026-11-20 10:00:00.123456', TIMESTAMP '2026-11-24 10:00:00.123457'),
                    (TIMESTAMP '2026-11-21 10:00:00', TIMESTAMP '2026-11-22 10:00:00'),
                    (TIMESTAMP '2026-11-16 12:00:00', TIMESTAMP '2026-11-30 12:00:00')
                ) AS times(started, finished)
            """).fetchall()
        self.assertEqual(durations[0], (1440.0,))
        self.assertEqual(durations[1], (2880.0,))
        self.assertGreater(durations[2][0], 2880.0)
        self.assertEqual(durations[3:], [(0.0,), (14400.0,)])

    def test_workday_window_counts_weekend_deliveries_and_keeps_missing_in_total(self):
        with duckdb.connect() as db:
            total, *counts = db.execute(f"""
                SELECT count(*), {delivery_window_counts("minutes", prefix="w", include_zero=True)}
                FROM (VALUES (0), (2880), (2880.001), (NULL)) AS orders(minutes)
            """).fetchone()
        rates = delivery_rates(dict(zip(DELIVERY_WINDOWS, counts, strict=True)), total)
        self.assertEqual(rates["2"], 50.0)
        self.assertEqual(rates["3"], 75.0)
