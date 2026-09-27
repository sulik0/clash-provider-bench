from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from clashbench.db import add_measurements, begin_run, connect, finish_run
from clashbench.engine import parse_faceair_tsv
from clashbench.regions import classify_region, region_filter_regex
from clashbench.report import build_summary, write_reports
from clashbench.schedule import launch_agent
from clashbench.util import percentile, redact


TSV = """序号\t节点名称\t类型\t延迟\t抖动\t丢包率\t下载速度\t上传速度
1.\t🇯🇵 Tokyo 01\tShadowsocks\t52ms\t5ms\t0.0%\t10.00MB/s\t2.00MB/s
2.\t香港 HK 01\tTrojan\tN/A\tN/A\t100.0%\tcontext deadline exceeded\tN/A
"""


class CoreTests(unittest.TestCase):
    def test_regions(self):
        self.assertEqual(classify_region("🇯🇵 Tokyo 01"), "JP")
        self.assertEqual(classify_region("香港 HK 01"), "HK")
        self.assertRegex("🇸🇬 Singapore", region_filter_regex(["SG"]))

    def test_parser_and_units(self):
        values = parse_faceair_tsv(TSV, "demo", {})
        self.assertEqual(len(values), 2)
        self.assertAlmostEqual(values[0].download_mbps, 83.88608)
        self.assertTrue(values[0].available)
        self.assertFalse(values[1].available)
        self.assertNotIn("http", values[1].error or "")

    def test_redaction(self):
        value = redact("failed https://host/sub?token=abc token=abc")
        self.assertNotIn("host", value); self.assertNotIn("abc", value)

    def test_percentile(self):
        self.assertEqual(percentile([1, 2, 3], .5), 2)
        self.assertAlmostEqual(percentile([1, 2, 3], .95), 2.9)

    def test_db_report_and_schedule(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); conn = connect(root / "bench.db")
            run = begin_run(conn, "digest", "mock", ["JP", "HK"])
            add_measurements(conn, run, parse_faceair_tsv(TSV, "demo", {})); finish_run(conn, run, "ok")
            summary, anomalies = build_summary(conn, 7)
            self.assertEqual(len(summary), 2)
            md, page = write_reports(conn, root / "reports", 7)
            self.assertIn("demo", md.read_text()); self.assertIn("<table>", page.read_text())
            plist = launch_agent(root / "bench.toml", root, ["09:00", "00:00"])
            self.assertIn(b"StartCalendarInterval", plist)


if __name__ == "__main__": unittest.main()

