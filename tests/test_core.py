from __future__ import annotations

import base64
import contextlib
import io
import sqlite3
import subprocess
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

from clashbench.config import Provider, Settings, load_config
from clashbench.cli import main as cli_main
from clashbench.db import (
    add_measurements, begin_run, connect, finish_run, rotated_providers,
)
from clashbench.engine import FaceairAdapter, Measurement, materialize_provider, parse_faceair_tsv, subscription_format
from clashbench.enrich import MihomoEnricher, classify_chatgpt_response
from clashbench.environment import capture_network_environment
from clashbench.profile import benchmark_profile
from clashbench.regions import classify_region, region_filter_regex
from clashbench.report import (
    build_summary, chatgpt_summary, current_report, infrastructure_summary, summarize_rows, trend_report,
    write_reports,
)
from clashbench.schedule import launch_agent
from clashbench.util import percentile, redact


TSV = """序号\t节点名称\t类型\t延迟\t抖动\t丢包率\t下载速度\t上传速度
1.\t🇯🇵 Tokyo 01\tShadowsocks\t52ms\t5ms\t0.0%\t10.00MB/s\t2.00MB/s
2.\t香港 HK 01\tTrojan\tN/A\tN/A\t100.0%\tcontext deadline exceeded\tN/A
"""


def measurement(provider: str, name: str, proxy_type: str = "VLESS", region: str = "JP", *,
                available: bool = True, latency: float | None = 50, download: float | None = 100,
                exit_ip: str | None = None, asn: str | None = None) -> Measurement:
    return Measurement(
        provider=provider, node_name=name, node_key=f"{provider}-{name}", proxy_type=proxy_type,
        region=region, available=available, ttfb_ms=latency, jitter_ms=5 if available else None,
        packet_loss_pct=0 if available else 100, download_mbps=download, upload_mbps=20 if available else None,
        status="ok" if available else "failed", error=None if available else "latency probe failed",
        exit_ip=exit_ip, asn=asn, as_org=f"Org-{asn}" if asn else None,
        enrichment_status="ok" if exit_ip else "not_requested",
    )


class CoreTests(unittest.TestCase):
    def test_regions(self):
        self.assertEqual(classify_region("🇯🇵 Tokyo 01"), "JP")
        self.assertEqual(classify_region("香港 HK 01"), "HK")
        self.assertRegex("🇸🇬 Singapore", region_filter_regex(["SG"]))

    def test_config_validates_chatgpt_checks(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "bench.toml"
            path.write_text("""
[[providers]]
name = "demo"
source_env = "DEMO_URL"
[enrichment]
checks = ["chatgpt", "not-a-service"]
""", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Unknown enrichment checks"):
                load_config(path)

    def test_parser_and_units(self):
        values = parse_faceair_tsv(TSV, "demo", {})
        self.assertEqual(len(values), 2)
        self.assertAlmostEqual(values[0].download_mbps, 83.88608)
        self.assertTrue(values[0].available)
        self.assertFalse(values[1].available)

    def test_faceair_adapter_emits_heartbeat_progress(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "provider.yaml"
            config.write_text("proxies:\n  - {name: '🇯🇵 Tokyo 01', type: ss, server: test, port: 443}\n", encoding="utf-8")
            settings = Settings(
                root=root, database=root / "test.db", providers=[], regions=["JP"],
                engine={"binary": "fake", "speed_mode": "download", "progress_interval_seconds": 1},
            )
            process = MagicMock()
            process.communicate.side_effect = [
                subprocess.TimeoutExpired(["fake"], 1),
                (TSV, ""),
            ]
            process.returncode = 0
            messages = []
            with patch("clashbench.engine.subprocess.Popen", return_value=process):
                values = FaceairAdapter(settings).run(Provider("demo"), config, ["JP"], messages.append)
            self.assertEqual(len(values), 2)
            self.assertTrue(any("已匹配 1 个节点" in message for message in messages))
            self.assertTrue(any("已用时" in message for message in messages))
            self.assertTrue(any("测速内核完成" in message for message in messages))

    def test_subscription_formats_and_user_agent_fallback(self):
        yaml_bytes = b"proxies:\n  - {name: JP, type: ss, server: example.test, port: 443}\n"
        uri_list = b"vless://id@example.test:443#JP"
        self.assertEqual(subscription_format(yaml_bytes), "clash-yaml")
        self.assertEqual(subscription_format(uri_list), "uri-list")
        self.assertEqual(subscription_format(base64.b64encode(uri_list)), "base64-uri-list")
        rejected = urllib.error.HTTPError("https://redacted.invalid", 403, "Forbidden", {}, None)
        with tempfile.TemporaryDirectory() as temp, \
             patch("clashbench.engine.provider_source", return_value="https://redacted.invalid"), \
             patch("clashbench.engine.urllib.request.urlopen", side_effect=[rejected, io.BytesIO(yaml_bytes)]):
            path = materialize_provider(Provider("demo", source_env="DEMO", user_agent="rejected"), Path(temp), "clash.meta")
            self.assertEqual(subscription_format(path.read_bytes()), "clash-yaml")

    def test_redaction_and_percentile(self):
        value = redact("failed https://host/sub?token=abc token=abc")
        self.assertNotIn("host", value)
        self.assertNotIn("abc", value)
        self.assertEqual(percentile([1, 2, 3], .5), 2)
        self.assertAlmostEqual(percentile([1, 2, 3], .95), 2.9)

    def test_old_database_migrates_without_losing_runs(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "old.db"
            old = sqlite3.connect(path)
            old.executescript("""
            CREATE TABLE runs (id TEXT PRIMARY KEY, started_at TEXT NOT NULL, ended_at TEXT, status TEXT NOT NULL,
              config_digest TEXT NOT NULL, engine TEXT NOT NULL, regions_json TEXT NOT NULL, error TEXT);
            CREATE TABLE measurements (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
              tested_at TEXT NOT NULL, provider TEXT NOT NULL, node_name TEXT NOT NULL, node_key TEXT NOT NULL,
              proxy_type TEXT, region TEXT NOT NULL, available INTEGER NOT NULL, ttfb_ms REAL, jitter_ms REAL,
              packet_loss_pct REAL, download_mbps REAL, upload_mbps REAL, status TEXT NOT NULL, error TEXT,
              exit_ip TEXT, exit_country TEXT, exit_region TEXT, asn TEXT, as_org TEXT, chatgpt TEXT, youtube TEXT,
              netflix TEXT, raw_json TEXT NOT NULL);
            INSERT INTO runs VALUES ('legacy','2026-01-01T00:00:00+00:00',NULL,'ok','old','faceair','[]',NULL);
            """)
            old.commit(); old.close()
            conn = connect(path)
            self.assertEqual(conn.execute("SELECT id FROM runs").fetchone()["id"], "legacy")
            self.assertIn("comparison_key", {row["name"] for row in conn.execute("PRAGMA table_info(runs)")})
            self.assertIn("enrichment_status", {row["name"] for row in conn.execute("PRAGMA table_info(measurements)")})
            conn.close()

    def test_statistics_report_effective_sample_counts_and_protocols(self):
        rows = [
            {"provider": "a", "region": "JP", "proxy_type": "VLESS", "available": 1, "ttfb_ms": 40,
             "jitter_ms": 4, "packet_loss_pct": 0, "download_mbps": 100, "upload_mbps": 20,
             "status": "ok", "tested_at": "2026-09-27T01:00:00+00:00"},
            {"provider": "a", "region": "JP", "proxy_type": "VLESS", "available": 1, "ttfb_ms": 60,
             "jitter_ms": None, "packet_loss_pct": 10, "download_mbps": None, "upload_mbps": 10,
             "status": "failed", "tested_at": "2026-09-27T06:00:00+00:00"},
            {"provider": "a", "region": "JP", "proxy_type": "Hysteria2", "available": 0, "ttfb_ms": None,
             "jitter_ms": None, "packet_loss_pct": 100, "download_mbps": None, "upload_mbps": None,
             "status": "failed", "tested_at": "2026-09-27T12:00:00+00:00"},
        ]
        summary = summarize_rows(rows, ("provider", "region"))[0]
        self.assertEqual((summary["samples"], summary["available_n"], summary["success_n"], summary["failure_n"]), (3, 2, 1, 2))
        self.assertEqual((summary["ttfb_n"], summary["jitter_n"], summary["download_n"], summary["loss_n"]), (2, 1, 1, 3))
        protocols = summarize_rows(rows, ("provider", "region", "proxy_type"))
        self.assertEqual({item["proxy_type"] for item in protocols}, {"VLESS", "Hysteria2"})

    def test_comparison_key_changes_with_real_test_conditions(self):
        settings = Settings(
            root=Path("/tmp"), database=Path("/tmp/test.db"), providers=[], regions=["JP"],
            engine={"speed_mode": "download", "server_url": "https://speed.test/file?token=secret",
                    "download_size_mb": 5, "timeout_seconds": 10, "concurrent": 2},
            enrichment={"enabled": False},
        )
        env = {"default_interface": "en0", "system_proxy": {"http_enabled": False},
               "tunnel_interfaces_active": []}
        key, params = benchmark_profile(settings, ["JP"], "v1", env)
        self.assertNotIn("secret", str(params))
        different_version = benchmark_profile(settings, ["JP"], "v2", env)[0]
        different_tunnel = benchmark_profile(settings, ["JP"], "v1", {**env, "tunnel_interfaces_active": ["utun2"]})[0]
        self.assertNotEqual(key, different_version)
        self.assertNotEqual(key, different_tunnel)

    def test_current_report_isolated_and_trend_only_matches_profile(self):
        with tempfile.TemporaryDirectory() as temp:
            conn = connect(Path(temp) / "bench.db")
            run_a = begin_run(conn, "a", "faceair", ["JP"], comparison_key="profile-a", parameters={"speed_mode": "full"})
            add_measurements(conn, run_a, [measurement("provider", "a", latency=40, download=100)])
            finish_run(conn, run_a, "ok")
            run_b = begin_run(conn, "b", "faceair", ["JP"], comparison_key="profile-b", parameters={"speed_mode": "fast"})
            add_measurements(conn, run_b, [measurement("provider", "b", latency=999, download=1)])
            finish_run(conn, run_b, "ok")
            run_partial = begin_run(conn, "a", "faceair", ["JP"], comparison_key="profile-a", parameters={"speed_mode": "full"})
            add_measurements(conn, run_partial, [measurement("provider", "current", latency=70, download=70)])
            finish_run(conn, run_partial, "partial")

            current, _ = build_summary(conn, 7, run_partial)
            trend, _ = build_summary(conn, 7)
            self.assertEqual({row["provider"] for row in current}, {"provider"})
            # Measurement.provider is deliberately independent from the node name above.
            self.assertEqual(current[0]["ttfb_p50"], 70)
            self.assertEqual(trend[0]["ttfb_p50"], 40)
            self.assertNotIn("999.0", trend_report(conn, run_partial, 7))
            self.assertIn("状态：**partial**", current_report(conn, run_partial))
            conn.close()

    def test_enrichment_failure_does_not_change_probe_result(self):
        item = measurement("a", "node")
        with patch("clashbench.enrich._free_port", side_effect=[18080, 19090]):
            enricher = MihomoEnricher("missing", Path("/tmp/unused"), Path("/tmp/unused.yaml"))
        with patch.object(enricher, "_api", side_effect=RuntimeError("offline")):
            enricher.enrich([item])
        self.assertTrue(item.available)
        self.assertEqual(item.status, "ok")
        self.assertIsNone(item.error)
        self.assertEqual(item.enrichment_status, "failed")
        self.assertEqual(item.enrichment_error, "select:RuntimeError")

    def test_chatgpt_response_classification_and_summary(self):
        self.assertEqual(classify_chatgpt_response(200, b"ok", {}, "US"), "available:US")
        self.assertEqual(
            classify_chatgpt_response(200, b"ok", {}, "HK", ["HK"]),
            "unsupported-country:HK",
        )
        self.assertEqual(
            classify_chatgpt_response(403, b"", {"Cf-Mitigated": "challenge"}, "JP"),
            "challenge:JP",
        )
        self.assertEqual(
            classify_chatgpt_response(403, b"Sorry, you have been blocked", {}, "SG"),
            "blocked:403:SG",
        )
        rows = [
            {"provider": "a", "region": "US", "chatgpt": "available:US"},
            {"provider": "a", "region": "US", "chatgpt": "challenge:US"},
            {"provider": "a", "region": "US", "chatgpt": None},
        ]
        item = chatgpt_summary(rows)[0]
        self.assertEqual((item["samples"], item["checked"], item["available"], item["challenge"]), (3, 2, 1, 1))
        self.assertEqual(item["availability"], 50)

    def test_chatgpt_check_still_runs_when_ip_lookup_fails(self):
        item = measurement("a", "node")
        with patch("clashbench.enrich._free_port", side_effect=[18080, 19090]):
            enricher = MihomoEnricher(
                "missing", Path("/tmp/unused"), Path("/tmp/unused.yaml"),
                unlock=True, checks=["chatgpt"],
            )
        with patch.object(enricher, "_api", return_value={}), \
             patch.object(enricher, "_proxied", side_effect=urllib.error.URLError("offline")), \
             patch.object(enricher, "_chatgpt", return_value="available:JP"), \
             patch("clashbench.enrich.time.sleep"):
            enricher.enrich([item])
        self.assertEqual(item.chatgpt, "available:JP")
        self.assertEqual(item.enrichment_status, "partial")
        self.assertIn("ip:URLError", item.enrichment_error)

    def test_chatgpt_probe_requires_homepage_and_backend_reachability(self):
        with patch("clashbench.enrich._free_port", side_effect=[18080, 19090]):
            enricher = MihomoEnricher(
                "missing", Path("/tmp/unused"), Path("/tmp/unused.yaml"),
                unlock=True, checks=["chatgpt"],
            )
        with patch.object(enricher, "_browser_proxied", side_effect=[
            (200, b"homepage", {}),
            (403, b'{"detail":"authentication required"}', {}),
        ]):
            self.assertEqual(enricher._chatgpt("US"), "available:US")
        with patch.object(enricher, "_browser_proxied", side_effect=[
            (200, b"homepage", {}),
            (403, b"challenge", {"Cf-Mitigated": "challenge"}),
        ]):
            self.assertEqual(enricher._chatgpt("JP"), "challenge:JP")

    def test_mihomo_ports_are_distinct(self):
        with patch("clashbench.enrich._free_port", side_effect=[18080, 18080, 19090]):
            enricher = MihomoEnricher("missing", Path("/tmp/unused"), Path("/tmp/unused.yaml"))
        self.assertEqual((enricher.mixed_port, enricher.controller_port), (18080, 19090))

    def test_infrastructure_diversity_uses_latest_per_node(self):
        with tempfile.TemporaryDirectory() as temp:
            conn = connect(Path(temp) / "bench.db")
            run = begin_run(conn, "a", "faceair", ["JP"], comparison_key="p")
            add_measurements(conn, run, [
                measurement("a", "n1", exit_ip="1.1.1.1", asn="AS1"),
                measurement("a", "n2", exit_ip="1.1.1.1", asn="AS1"),
                measurement("a", "n3", exit_ip="2.2.2.2", asn="AS2"),
            ])
            finish_run(conn, run, "ok")
            rows = conn.execute("SELECT * FROM measurements").fetchall()
            infra = infrastructure_summary(rows)[0]
            self.assertEqual((infra["unique_ips"], infra["unique_asns"], infra["unique_orgs"]), (2, 2, 2))
            self.assertAlmostEqual(infra["top_asn_share"], 2 / 3 * 100)
            conn.close()

    def test_rotation_environment_and_report_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); conn = connect(root / "bench.db")
            self.assertEqual(rotated_providers(conn, ["a", "b", "c"], "p"), ["a", "b", "c"])
            run = begin_run(conn, "a", "mock", ["JP"], comparison_key="p", parameters={"speed_mode": "mock"},
                            environment={}, provider_order=["a", "b", "c"])
            add_measurements(conn, run, [measurement("a", "n")]); finish_run(conn, run, "ok")
            self.assertEqual(rotated_providers(conn, ["a", "b", "c"], "p"), ["b", "c", "a"])
            reports = write_reports(conn, root / "reports", 7, run)
            self.assertTrue(all(path.exists() for path in reports.values()))
            self.assertIn("n=1", reports["latest_md"].read_text())
            plist = launch_agent(root / "bench.toml", root, ["09:00", "00:00"])
            self.assertIn(b"StartCalendarInterval", plist)
            conn.close()

        outputs = {
            ("scutil", "--proxy"): "HTTPEnable : 1\nHTTPProxy : proxy.local\nSOCKSEnable : 0\n",
            ("route", "-n", "get", "default"): "interface: en0\n",
            ("ifconfig", "-l"): "lo0 en0 utun2",
            ("scutil", "--nwi"): "Network information\n utun2 : flags",
        }
        env = capture_network_environment(lambda command: outputs.get(tuple(command), ""))
        self.assertTrue(env["system_proxy"]["http_enabled"])
        self.assertEqual(env["default_interface"], "en0")
        self.assertEqual(env["tunnel_interfaces_active"], ["utun2"])

    def test_mock_cli_creates_independent_and_trend_reports(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "bench.toml"
            config.write_text("""
database = "data/bench.sqlite3"
regions = ["JP", "HK"]
[[providers]]
name = "alpha"
source_env = "ALPHA_URL"
[[providers]]
name = "beta"
source_env = "BETA_URL"
[engine]
speed_mode = "full"
[report]
output = "reports"
days = 3
""", encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(cli_main(["run", "--config", str(config), "--mock"]), 0)
            self.assertIn("[run] 开始", output.getvalue())
            self.assertIn("[1/2 alpha] 生成 mock 测试数据", output.getvalue())
            self.assertIn("[report] 生成", output.getvalue())
            conn = connect(root / "data/bench.sqlite3")
            run = conn.execute("SELECT * FROM runs").fetchone()
            self.assertTrue(run["comparison_key"])
            self.assertEqual(len(conn.execute("SELECT * FROM provider_runs").fetchall()), 2)
            run_id = run["id"]
            conn.close()
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli_main(["report", "--config", str(config), "--days", "3", "--run-id", run_id]), 0)
            self.assertTrue((root / "reports/latest.md").exists())
            self.assertTrue((root / "reports/trend-3d.md").exists())
            self.assertEqual(len(list((root / "reports/runs").glob("*.md"))), 1)


if __name__ == "__main__":
    unittest.main()
