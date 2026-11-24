from datetime import datetime, timezone
import unittest

from app.trajectory_rules import DEFAULTS, Event, outcome


def instant(value):
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def scan(value, text, location):
    return Event(instant(value), value, text, location)


class TrajectoryRulesTest(unittest.TestCase):
    def test_export_cutoff_and_weekend_schedules(self):
        rule = DEFAULTS.rules[0]
        now = instant("2026-11-28T15:00:00+08:00")
        for warehouse, shipped, expected in (
            ("JY01", "2026-11-20T14:29:59", "2026-11-21"),
            ("JY02", "2026-11-20T14:30:00", "2026-11-23"),
            ("XC01", "2026-11-21T16:29:59", "2026-11-23"),
            ("XCWH01", "2026-11-21T16:30:00", "2026-11-24"),
            ("XC01", "2026-11-22T01:00:00", "2026-11-24"),
        ):
            with self.subTest(warehouse=warehouse, shipped=shipped):
                result = outcome(rule, datetime.fromisoformat(shipped), warehouse, (), now, now)
                self.assertEqual((result["state"], result["due"]), ("missing", expected))
        self.assertIsNone(outcome(rule, datetime(2026, 11, 20, 10), "WH01", (), now, now))

    def test_export_marks_late_according_to_china_wall_time(self):
        rule = DEFAULTS.rules[0]
        now = instant("2026-11-25T15:00:00+08:00")
        shipped = datetime(2026, 11, 20, 11)
        on_time = scan("2026-11-21T21:00:00+00:00", "departed from origin airport", "SHENZHEN CHINA")
        self.assertIsNone(outcome(rule, shipped, "JY01", (on_time,), now, now))
        late = scan("2026-11-22T00:00:00+08:00", "departed from origin airport", "SHENZHEN CHINA")
        self.assertEqual(outcome(rule, shipped, "JY01", (late,), now, now)["state"], "late")
        self.assertEqual(outcome(rule, shipped, "JY01", (), None, now)["state"], "unverified")

    def test_landing_friday_uses_monday_and_airport_timezone(self):
        rule = DEFAULTS.rules[1]
        shipped = datetime(2026, 10, 29, 10)
        # US daylight saving time ends on the weekend between the anchor and deadline.
        arrival = scan("2026-10-30T05:00:00-07:00", "arrived at destination airport", "Los Angeles United States")
        on_time = scan("2026-11-02T23:59:59-08:00", "departed from destination facility", "Los Angeles United States")
        late = scan("2026-11-03T00:00:00-08:00", "departed from destination facility", "Los Angeles United States")
        now = instant("2026-11-03T02:00:00-08:00")
        self.assertIsNone(outcome(rule, shipped, "XC01", (arrival, on_time), now, now))
        result = outcome(rule, shipped, "XC01", (arrival, late), now, now)
        self.assertEqual((result["state"], result["due"], result["deadline"]),
                         ("late", "2026-11-02", "2026-11-03T00:00:00-08:00"))

    def test_does_not_use_import_scan_to_satisfy_tuesday_deadline(self):
        rule = DEFAULTS.rules[1]
        shipped = datetime(2026, 11, 16, 9)
        arrival = scan("2026-11-17T03:00:00-05:00", "arrived at destination airport", "Jamaica US")
        customs = scan("2026-11-17T07:00:00-05:00", "import scan", "Jamaica US")
        now = instant("2026-11-18T10:00:00-05:00")
        result = outcome(rule, shipped, "XC01", (arrival, customs), now, now)
        self.assertEqual((result["state"], result["due"]), ("missing", "2026-11-17"))


if __name__ == "__main__":
    unittest.main()
