import json
import unittest
from unittest.mock import patch

import duckdb

from app import ops_assistant, track_analysis


class TrackAnalysisTest(unittest.TestCase):
    def test_combines_sources_and_distinguishes_waiting_from_physical_scan(self):
        with duckdb.connect() as con:
            con.execute("""
                CREATE TABLE analysis_orders AS SELECT 'P1' AS id,'N1' AS num,100996 AS carrier,
                    timestamp '2025-11-01 08:43:53' AS shipped, 'GOFO' AS carrier_name,
                    'KY' AS warehouse,'WF5' AS channel,'InTransit' AS official,
                    'InTransit_Other' AS latest_sub,timestamp '2025-11-06' AS checked,false AS wms_delivered
            """)
            con.execute("""
                CREATE TABLE trk_event (单号 VARCHAR,运输商代码 INTEGER,事件时间 TIMESTAMP,主状态 VARCHAR,
                    子状态 VARCHAR,描述 VARCHAR,地点 VARCHAR);
                INSERT INTO trk_event VALUES
                    ('N1',100996,'2025-11-01 00:52:54','PickedUp','InfoReceived',
                        'Picked up by Shipping Partner, GOFO Awaiting Item','Origin'),
                    ('N1',111,'2025-11-01 04:00:00','PickedUp','InTransit_PickedUp','Shipment Pickup','Other'),
                    ('N1',100996,'2025-11-03 02:13:53',NULL,'InTransit_Other','Arrived at GOFO Regional Hub','Hub');
                CREATE TABLE trk_archive_event (tracking_no VARCHAR,carrier_code INTEGER,event_time_utc VARCHAR,
                    stage VARCHAR,substatus VARCHAR,description VARCHAR,location VARCHAR);
                INSERT INTO trk_archive_event VALUES
                    ('N1',100996,'2025-11-03T02:13:53+00:00',NULL,'InTransit_Other','Arrived at GOFO Regional Hub','Hub')
            """)
            report = track_analysis.report(con)
            self.assertEqual(con.execute("SELECT count(*) FROM analysis_events").fetchone()[0], 2)
        self.assertEqual(report["summary"]["tracked"], 1)
        self.assertEqual(report["summary"]["lateScans"], 1)
        scan, stale, *_ = report["groups"]
        self.assertEqual(scan["rows"][0]["hours"], 49.5)
        self.assertEqual(stale["rows"][0]["last_seen"], "2025-11-03T10:13:53")
        self.assertEqual(stale["rows"][0]["examples"], ["P1"])

    def test_model_receives_aggregates_without_waybills_or_event_locations(self):
        report = {"asOf": "2026-11-26", "summary": {"total": 3}, "groups": [
            {"label": "同地点轨迹断更", "rows": [{"affected": 3, "examples": ["P1"], "place": "A street"}]}
        ]}
        with patch.object(ops_assistant, "models", return_value=[{"id": "test"}]), patch.object(
            ops_assistant, "chat", return_value=("发现 3 单断更。", "chat")
        ) as ask:
            self.assertEqual(ops_assistant.trajectory_summary(report), "发现 3 单断更。")
        payload = json.loads(ask.call_args.args[1][0]["content"])
        self.assertEqual(payload["groups"][0]["rows"], [{"affected": 3}])
