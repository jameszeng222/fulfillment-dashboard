import unittest

import duckdb

from app.pages_api import DELIVERY_WINDOWS, delivery_rates, delivery_window_counts


class DeliveryRatesTest(unittest.TestCase):
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
