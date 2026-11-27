import unittest
from unittest.mock import patch

import duckdb

from app import us_analysis


def regional_facts():
    con = duckdb.connect()
    con.execute("""
        CREATE TABLE fct_analytics(
            履约单号 VARCHAR, 单号归一 VARCHAR, 目的国 VARCHAR, 目的国中文 VARCHAR, C端承运商 VARCHAR,
            支付时间 TIMESTAMP, 创建时间 TIMESTAMP, 签出时间 TIMESTAMP, 统计妥投北京时间 TIMESTAMP,
            妥投统计来源 VARCHAR, analytics_online TIMESTAMP, analytics_state VARCHAR, analytics_region VARCHAR
        );
        INSERT INTO fct_analytics
        SELECT id,id,'United States','美国','GOFO','2026-11-01','2026-11-01 00:00:00',
               '2026-11-01 01:00:00','2026-11-02','17TRACK',scan,geo,area
        FROM (VALUES ('A','2026-11-01 02:00:00','CA','太平洋'),
                     ('B','2026-11-01 04:00:00','CA','太平洋'),
                     ('C','2026-11-01 00:30:00','CO','山地'),
                     ('D','2026-10-31 23:00:00','CO','山地'),
                     ('E',NULL,'CO','山地')) t(id, scan, geo, area);
        CREATE TABLE fct_analytics_info(archive_version VARCHAR);
        INSERT INTO fct_analytics_info VALUES (NULL);
    """)
    return con


class RegionalAnalysisTest(unittest.TestCase):
    def test_saved_alaska_region_uses_the_current_geography(self):
        with regional_facts() as con:
            con.execute("UPDATE fct_analytics SET analytics_state='AK' WHERE 履约单号='A'")
            with patch("app.core.duck", return_value=con):
                report = us_analysis.report.__wrapped__(None, "shipped", "shipped", "lastmile", "", (), "")
        self.assertEqual(report["geography"]["AK"]["region"], "美国属地")
        self.assertEqual(report["regions"]["美国属地"]["orders"], 1)
        self.assertEqual(report["regions"]["太平洋"]["orders"], 1)

    @patch("app.core.duck", side_effect=regional_facts)
    def test_online_samples_and_p95_use_the_same_durations(self, _duck):
        report = us_analysis.report.__wrapped__(None, "shipped", "ship_pickup", "lastmile", "", (), "")
        segments = us_analysis.segments.__wrapped__(None, "us", "shipped", "lastmile", "", (), "", "all", "", "")
        rows = {metric["key"]: metric for metric in segments["segments"]}

        self.assertEqual(segments["total"], 5)
        self.assertEqual(rows["ship_pickup"]["samples"], 2)
        self.assertEqual(rows["pickup_delivered"]["samples"], 3)
        for result in (report["national"], report["states"]["CA"], report["regions"]["太平洋"], report["channels"][0]):
            self.assertEqual(result["samples"], 2)
            self.assertAlmostEqual(result["p90"], 168 / 1440)
            self.assertAlmostEqual(result["p95"], 174 / 1440)
        self.assertIsNone(report["states"]["CO"]["p95"])


if __name__ == "__main__":
    unittest.main()
