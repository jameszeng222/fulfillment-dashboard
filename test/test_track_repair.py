import unittest

import duckdb

from app import track_lookup
from app.order_time import wms_clock_basis
from track17 import carrier_lookup


class TrackRepairTests(unittest.TestCase):
    def test_registered_channel_aliases(self):
        self.assertEqual(carrier_lookup("WYT-USPS", "YANWEN", None, False), (190012, 191278))
        self.assertEqual(carrier_lookup("XS_Express", "XS Express", None, False), (190477, 191281))
        self.assertEqual(carrier_lookup("Hete-Logistics", "3PE Express", None, False), (191376, 190008))
        self.assertEqual(carrier_lookup("WYT-USPS", "YANWEN", 191278, True)[0], 191278)
        self.assertNotIn(100644, carrier_lookup("WYT-USPS", "USPS", None, False))

    def test_warehouse_and_carrier_clock(self):
        with duckdb.connect() as con:
            rows = con.execute(f"""SELECT {wms_clock_basis('o')} FROM (VALUES
                ('USWC2-WINIT','YANWEN'), ('USKY3-WINIT','GOFO'), ('JY01','3PE Express'),
                ('USWC2-WINIT','SpeedX'), ('UNKNOWN','YANWEN')) o(仓库,C端承运商)""").fetchall()
        self.assertEqual(rows, [("北京时间",), ("目的地当地时间",), ("北京时间",), ("北京时间",), (None,)])

    def test_track_status_uses_official_state_first(self):
        with duckdb.connect() as con:
            con.execute("""CREATE TABLE fct_order AS SELECT * FROM (VALUES
                ('WMS','wms',1,false), ('INVALID','invalid',0,true), ('CONFLICT','conflict',1,false),
                ('CARRIER','carrier',1,true), ('SHIPPED','shipped',0,false))
                a(履约单号,单号归一,已签收,WMS签收异常)""")
            con.execute("ALTER TABLE fct_order ADD COLUMN 签出时间 TIMESTAMP DEFAULT TIMESTAMP '2026-11-23'")
            con.execute("""CREATE TABLE trk_status (单号 VARCHAR, carrier INTEGER, 状态 VARCHAR, 状态中文 VARCHAR,
                子状态 VARCHAR, 签收时间 TIMESTAMP, 最新事件时间 TIMESTAMP, 最新描述 VARCHAR, 地点 VARCHAR,
                停滞天数 INTEGER, 查询时间 TIMESTAMP)""")
            con.execute("INSERT INTO trk_status(单号,carrier,状态,状态中文) VALUES ('carrier',191278,'InTransit','运输中'),('conflict',100644,'NotFound','查无此单')")
            con.execute("CREATE TABLE trk_missing (单号 VARCHAR)")
            con.execute("CREATE TABLE trk_verification (number VARCHAR, outcome VARCHAR)")
            con.execute("INSERT INTO trk_verification VALUES ('conflict','carrier_conflict')")
            track_lookup.build(con)
            rows = dict(con.execute("SELECT 履约单号,状态标签 FROM fct_track_lookup").fetchall())
        self.assertEqual(rows, {"WMS":"WMS 已签收 · 待核实", "INVALID":"WMS 签收时间异常",
                                "CONFLICT":"承运商待核对", "CARRIER":"运输中", "SHIPPED":"已出库 · 待同步轨迹"})
