import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import duckdb

from app import core, tracking_freshness, tracking_full
from init_ops import SCHEMA


class TrackingFullImportTest(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(TemporaryDirectory())
        self.enterContext(patch.object(core, "OPS_DB", Path(folder) / "ops.db"))
        with core.ops() as db:
            db.executescript(SCHEMA)

    def test_reimport_only_changes_tracking_scope_for_eligible_pairs(self):
        with duckdb.connect() as con:
            con.execute("""
                CREATE TABLE fct_order(履约单号 VARCHAR, 单号归一 VARCHAR, 物流商 VARCHAR, C端承运商 VARCHAR,
                                      签出时间 TIMESTAMP, 已签收 INTEGER, 状态 VARCHAR);
                CREATE TABLE trk_status(单号 VARCHAR, carrier INTEGER, 最新事件时间 TIMESTAMP, 状态 VARCHAR);
                CREATE TABLE trk_event(单号 VARCHAR, 运输商代码 INTEGER);
                INSERT INTO fct_order VALUES
                ('A','TRACK00001','carrier','carrier',current_timestamp AT TIME ZONE 'Asia/Shanghai',0,'待收货'),
                ('B','TRACK00002','carrier','carrier',NULL,0,'待出库'),
                ('C','TRACK00003','carrier','carrier',current_timestamp AT TIME ZONE 'Asia/Shanghai',0,'待收货');
            """)
            before = tracking_freshness.eligible_orders(con)
            con.execute("""
                UPDATE fct_order SET 签出时间=签出时间-INTERVAL '1 minute' WHERE 履约单号='A';
                UPDATE fct_order SET 签出时间=current_timestamp AT TIME ZONE 'Asia/Shanghai' WHERE 履约单号='B';
                UPDATE fct_order SET 单号归一='TRACK00004' WHERE 履约单号='C';
            """)
            self.assertEqual(tracking_freshness.eligible_orders(con) - before,
                             {("B", "TRACK00002"), ("C", "TRACK00004")})

    def test_import_prioritizes_new_waybills_and_extends_active_run(self):
        with core.ops() as db:
            self.assertIsNone(tracking_full.enqueue_import(db, []))
            self.assertIsNone(tracking_full.latest(db))
            run_id = tracking_full.enqueue_import(db, ["NEW"])
        self.assertEqual(tracking_full.seed(run_id, ["OLD", "NEW"]), 2)
        with core.ops() as db:
            self.assertEqual(tracking_full.next_numbers(db, run_id, 1), ["NEW"])
            tracking_full.complete_batch(db, run_id, ["NEW"], {"with_events": 1})
            self.assertEqual(tracking_full.enqueue_import(db, ["OLD", "LATER"]), run_id)
            self.assertEqual(tracking_full.next_numbers(db, run_id, 5), ["OLD", "LATER"])
            detail = tracking_full.latest(db)["detail"]
        self.assertEqual(detail["trigger"], "import")
        self.assertEqual(detail["processed"], 1)
        self.assertEqual(detail["total"], 3)
        self.assertEqual(detail["lastImportWaybills"], 2)

    def test_failed_publish_rolls_back_enqueue(self):
        with self.assertRaises(RuntimeError), core.ops() as db:
            db.execute("BEGIN IMMEDIATE")
            tracking_full.enqueue_import(db, ["NEW"])
            raise RuntimeError("snapshot replacement failed")
        self.assertIsNone(tracking_full.get_job())
