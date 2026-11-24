import unittest
from datetime import datetime

import duckdb

from app.regional_pickup import build_pickups


class RegionalPickupsTest(unittest.TestCase):
    def test_first_physical_scan_for_matching_carrier(self):
        with duckdb.connect() as con:
            con.execute("""CREATE TABLE regional_scope(履约单号 VARCHAR,单号归一 VARCHAR);
                           INSERT INTO regional_scope VALUES ('A','01'),('B','02'),('C','03');
                           CREATE TABLE trk_status(单号 VARCHAR,carrier INTEGER);
                           INSERT INTO trk_status VALUES ('01',100),('02',100),('03',191376);
                           CREATE TABLE trk_event(单号 VARCHAR,运输商代码 INTEGER,事件时间 TIMESTAMP,
                               当地时间 VARCHAR,地点 VARCHAR,主状态 VARCHAR,子状态 VARCHAR,描述 VARCHAR);
                           INSERT INTO trk_event VALUES
                             ('01',100,'2026-11-01 00:00:00',NULL,NULL,'PickedUp',NULL,'label created'),
                             ('01',200,'2026-11-01 01:00:00',NULL,NULL,'PickedUp',NULL,'picked up'),
                             ('01',100,'2026-11-01 02:00:00',NULL,NULL,'PickedUp',NULL,'shipment pickup'),
                             ('01',100,'2026-11-02 00:00:00',NULL,NULL,'PickedUp',NULL,'shipment pickup'),
                             ('02',100,'2026-11-01 00:00:00',NULL,NULL,'Arrival',NULL,'arrived at facility');
                           CREATE TABLE trk_archive_event(tracking_no VARCHAR,carrier_code INTEGER,event_time_utc VARCHAR,
                               event_time_raw VARCHAR,location VARCHAR,stage VARCHAR,substatus VARCHAR,description VARCHAR);
                           INSERT INTO trk_archive_event VALUES
                             ('03',191376,'2026-11-01T09:00:00+00:00',
                              '{"date":"2026-11-01","time":"09:00:00","timezone":"+00:00"}',
                              'SHENZHEN CHINA','PickedUp','InTransit_PickedUp','shipment pickup');""")
            build_pickups(con)
            self.assertEqual(con.execute("SELECT * FROM regional_pickups ORDER BY 1").fetchall(), [
                ("A", datetime(2026, 11, 1, 10)),
                ("C", datetime(2026, 11, 1, 9)),
            ])


if __name__ == "__main__":
    unittest.main()
