from __future__ import annotations

import base64
import contextlib
import io
import json
import plistlib
import re
import sqlite3
import subprocess
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

from clashbench.config import Provider, Settings, load_config
from clashbench.cli import main as cli_main, merge_two_stage
from clashbench.db import (
    add_measurements, begin_run, connect, finish_run, rotated_providers,
)
from clashbench.engine import FaceairAdapter, Measurement, materialize_provider, parse_faceair_tsv, subscription_format
from clashbench.enrich import (
    TLS, MihomoEnricher, MihomoEnricherPool, WebSocketStabilityResult, classify_chatgpt_response,
    classify_reachability_response,
)
from clashbench.environment import capture_network_environment
from clashbench.notifications import notification_text, send_macos_notification
from clashbench.profile import benchmark_profile
from clashbench.regions import classify_region, region_filter_regex
from clashbench.report import (
    build_summary, chatgpt_summary, current_report, infrastructure_summary, summarize_rows, trend_report,
    websocket_summary, write_reports,
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

    def test_config_validates_report_timezone(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "bench.toml"
            path.write_text("""
[[providers]]
name = "demo"
source_env = "DEMO_URL"
[report]
timezone = "Not/A-Timezone"
""", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Unknown report.timezone"):
                load_config(path)

    def test_config_validates_enrichment_workers(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "bench.toml"
            path.write_text("""
[[providers]]
name = "demo"
source_env = "DEMO_URL"
[enrichment]
workers = 0
""", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "enrichment.workers"):
                load_config(path)

    def test_config_validates_websocket_stability_window(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "bench.toml"
            path.write_text("""
[[providers]]
name = "demo"
source_env = "DEMO_URL"
[enrichment]
websocket_hold_seconds = 2
""", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "websocket_hold_seconds"):
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

    def test_faceair_adapter_can_target_exact_nodes_for_second_stage(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "provider.yaml"
            config.write_text("proxies:\n  - {name: 'JP [A]+', type: ss, server: test, port: 443}\n", encoding="utf-8")
            settings = Settings(
                root=root, database=root / "test.db", providers=[], regions=["JP"],
                engine={"binary": "fake", "speed_mode": "download"},
            )
            process = MagicMock(returncode=0)
            process.communicate.return_value = (TSV, "")
            with patch("clashbench.engine.subprocess.Popen", return_value=process) as popen:
                FaceairAdapter(settings).run(
                    Provider("demo"), config, ["JP"], speed_mode="download",
                    node_names={"JP [A]+"}, download_size_mb=50, upload_size_mb=20,
                )
            args = popen.call_args.args[0]
            self.assertEqual(args[args.index("-f") + 1], r"^(?:JP \[A\]\+)$")
            self.assertEqual(args[args.index("-download-size") + 1], str(50 * 1024 * 1024))

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
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(measurements)")}
            self.assertIn("enrichment_status", columns)
            self.assertIn("chatgpt_websocket", columns)
            self.assertIn("chatgpt_websocket_stability", columns)
            self.assertIn("throughput_attempted", columns)
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

    def test_two_stage_statistics_exclude_screened_nodes_from_failure_rate(self):
        rows = [
            {"provider": "a", "region": "JP", "proxy_type": "VLESS", "available": 1,
             "ttfb_ms": 40, "jitter_ms": 4, "packet_loss_pct": 0, "download_mbps": 100,
             "upload_mbps": 20, "status": "ok", "throughput_attempted": 1,
             "tested_at": "2026-09-27T01:00:00+00:00"},
            {"provider": "a", "region": "JP", "proxy_type": "VLESS", "available": 1,
             "ttfb_ms": 60, "jitter_ms": 5, "packet_loss_pct": 0, "download_mbps": None,
             "upload_mbps": None, "status": "ok", "throughput_attempted": 0,
             "tested_at": "2026-09-27T01:00:00+00:00"},
        ]
        summary = summarize_rows(rows, ("provider", "region"))[0]
        self.assertEqual((summary["samples"], summary["throughput_n"], summary["success_n"]), (2, 1, 1))
        self.assertEqual(summary["failure_rate"], 0)

    def test_websocket_summary_separates_first_try_recovery_and_failure(self):
        rows = [
            {"provider": "a", "region": "JP", "chatgpt_websocket": "upgrade-101",
             "chatgpt_websocket_seconds": 15.0, "chatgpt_websocket_disconnects": 0,
             "chatgpt_websocket_stability": "stable"},
            {"provider": "a", "region": "JP", "chatgpt_websocket": "upgrade-101",
             "chatgpt_websocket_seconds": 18.0, "chatgpt_websocket_disconnects": 1,
             "chatgpt_websocket_stability": "stable-after-reconnect"},
            {"provider": "a", "region": "JP", "chatgpt_websocket": "http-403",
             "chatgpt_websocket_seconds": 0.0, "chatgpt_websocket_disconnects": 0,
             "chatgpt_websocket_stability": "handshake-failed"},
            {"provider": "a", "region": "JP", "chatgpt_websocket": "reachable:http-401",
             "chatgpt_websocket_seconds": 0.0, "chatgpt_websocket_disconnects": 0,
             "chatgpt_websocket_stability": "api-auth-boundary"},
        ]
        summary = websocket_summary(rows)[0]
        self.assertEqual((summary["candidates"], summary["evaluated"], summary["handshake_ok"]), (4, 3, 2))
        self.assertEqual(summary["boundary_only"], 1)
        self.assertEqual((summary["stable"], summary["recovered"], summary["handshake_failed"]), (1, 1, 1))
        self.assertAlmostEqual(summary["stable_rate"], 2 / 3 * 100)

    def test_report_time_buckets_use_configured_timezone(self):
        rows = [{
            "provider": "a", "region": "US", "proxy_type": "VLESS", "available": 1,
            "ttfb_ms": 50, "jitter_ms": 5, "packet_loss_pct": 0,
            "download_mbps": 100, "upload_mbps": 20, "status": "ok",
            "tested_at": "2026-09-27T12:00:00+00:00",
        }]
        shanghai = summarize_rows(rows, ("provider", "region"), "Asia/Shanghai")[0]
        utc = summarize_rows(rows, ("provider", "region"), "UTC")[0]
        self.assertEqual((shanghai["evening_n"], shanghai["daytime_n"]), (1, 0))
        self.assertEqual((utc["evening_n"], utc["daytime_n"]), (0, 0))

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
        self.assertEqual(params["evaluation_timezone"], "Asia/Shanghai")
        different_version = benchmark_profile(settings, ["JP"], "v2", env)[0]
        different_tunnel = benchmark_profile(settings, ["JP"], "v1", {**env, "tunnel_interfaces_active": ["utun2"]})[0]
        settings.report["timezone"] = "UTC"
        different_timezone = benchmark_profile(settings, ["JP"], "v1", env)[0]
        self.assertNotEqual(key, different_version)
        self.assertNotEqual(key, different_tunnel)
        self.assertNotEqual(key, different_timezone)

        settings.enrichment = {
            "enabled": True, "unlock": True, "checks": ["chatgpt"],
            "openai_api_key_env": "TEST_OPENAI_API_KEY",
        }
        with patch.dict("os.environ", {"TEST_OPENAI_API_KEY": ""}):
            anonymous_key, anonymous_params = benchmark_profile(settings, ["JP"], "v1", env)
        with patch.dict("os.environ", {"TEST_OPENAI_API_KEY": "sensitive-value"}):
            authenticated_key, authenticated_params = benchmark_profile(settings, ["JP"], "v1", env)
        self.assertNotEqual(anonymous_key, authenticated_key)
        self.assertNotIn("sensitive-value", str(authenticated_params))
        self.assertEqual(
            anonymous_params["chatgpt_probe"]["websocket_auth_mode"], "auth-boundary-only",
        )

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
            current_item = measurement("provider", "current", latency=70, download=70)
            current_item.chatgpt = "available:JP"
            current_item.chatgpt_websocket = "reachable:http-401"
            current_item.chatgpt_websocket_seconds = 0.0
            current_item.chatgpt_websocket_disconnects = 0
            current_item.chatgpt_websocket_reconnect = "not-attempted"
            current_item.chatgpt_websocket_stability = "api-auth-boundary"
            add_measurements(conn, run_partial, [current_item])
            finish_run(conn, run_partial, "partial")

            current, _ = build_summary(conn, 7, run_partial)
            trend, _ = build_summary(conn, 7)
            self.assertEqual({row["provider"] for row in current}, {"provider"})
            # Measurement.provider is deliberately independent from the node name above.
            self.assertEqual(current[0]["ttfb_p50"], 70)
            self.assertEqual(trend[0]["ttfb_p50"], 40)
            self.assertNotIn("999.0", trend_report(conn, run_partial, 7))
            report = current_report(conn, run_partial)
            self.assertIn("状态：**partial**", report)
            self.assertIn("仅认证边界", report)
            self.assertIn("`api-auth-boundary`", report)
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
        self.assertEqual(classify_reachability_response(401, b"auth required"), "reachable:http-401")
        self.assertEqual(classify_reachability_response(404, b"not found"), "reachable:http-404")
        self.assertEqual(classify_reachability_response(503, b"down"), "http-503")

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
            (403, b"auth required", {}),
            (404, b"not found", {}),
        ]):
            result = enricher._chatgpt("US")
            self.assertEqual(result.overall, "available:US")
            self.assertEqual(result.auth, "reachable:http-403")
            self.assertEqual(result.static, "reachable:http-404")
        with patch.object(enricher, "_browser_proxied", side_effect=[
            (200, b"homepage", {}),
            (403, b"challenge", {"Cf-Mitigated": "challenge"}),
        ]):
            self.assertEqual(enricher._chatgpt("JP").overall, "challenge:JP")

    def test_websocket_stability_distinguishes_stable_recovered_and_handshake_failure(self):
        with patch("clashbench.enrich._free_port", side_effect=[18080, 19090]):
            enricher = MihomoEnricher(
                "missing", Path("/tmp/unused"), Path("/tmp/unused.yaml"),
                openai_api_key="test-key",
            )
        with patch.object(enricher, "_open_realtime_websocket", return_value=(MagicMock(), "upgrade-101", b"")), \
             patch.object(enricher, "_hold_chatgpt_websocket", return_value=(15.0, True)):
            stable = enricher._websocket_stability(15, 1)
        self.assertEqual((stable.handshake, stable.stability, stable.reconnect), (
            "upgrade-101", "stable", "not-needed",
        ))

        with patch.object(enricher, "_open_realtime_websocket", return_value=(MagicMock(), "upgrade-101", b"")), \
             patch.object(enricher, "_hold_chatgpt_websocket", side_effect=[(2.0, False), (15.0, True)]):
            recovered = enricher._websocket_stability(15, 1)
        self.assertEqual(recovered.stability, "stable-after-reconnect")
        self.assertEqual(recovered.abnormal_disconnects, 1)
        self.assertEqual(recovered.reconnect, "succeeded:1")
        self.assertEqual(recovered.connected_seconds, 15.0)

        with patch.object(enricher, "_open_realtime_websocket", return_value=(None, "http-404", b"")):
            failed = enricher._websocket_stability(15, 1)
        self.assertEqual(failed.stability, "handshake-failed")
        self.assertEqual(failed.reconnect, "not-attempted")

        with patch("clashbench.enrich._free_port", side_effect=[18080, 19090]):
            anonymous = MihomoEnricher("missing", Path("/tmp/unused"), Path("/tmp/unused.yaml"))
        with patch.object(
            anonymous, "_open_realtime_websocket",
            return_value=(None, "reachable:http-401", b""),
        ):
            boundary = anonymous._websocket_stability(15, 1)
        self.assertEqual(boundary.stability, "api-auth-boundary")

        unauthenticated_socket = MagicMock()
        with patch.object(
            anonymous, "_open_realtime_websocket",
            return_value=(unauthenticated_socket, "upgrade-101", b""),
        ), patch.object(anonymous, "_send_websocket_frame"):
            upgraded_boundary = anonymous._websocket_stability(15, 1)
        self.assertEqual(upgraded_boundary.stability, "api-auth-boundary")
        self.assertEqual(upgraded_boundary.abnormal_disconnects, 0)
        unauthenticated_socket.close.assert_called_once()

    def test_realtime_websocket_uses_documented_endpoint_and_api_key(self):
        with patch("clashbench.enrich._free_port", side_effect=[18080, 19090]):
            enricher = MihomoEnricher(
                "missing", Path("/tmp/unused"), Path("/tmp/unused.yaml"),
                openai_api_key="secret-key", realtime_model="gpt-realtime-2.1",
            )
        raw_sock = MagicMock()
        raw_sock.recv.return_value = b"HTTP/1.1 200 Connection established\r\n\r\n"
        tls_sock = MagicMock()
        tls_sock.recv.return_value = b"HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\n\r\n"
        with patch("clashbench.enrich.socket.create_connection", return_value=raw_sock), \
             patch.object(TLS, "wrap_socket", return_value=tls_sock):
            sock, state, buffered = enricher._open_realtime_websocket()
        self.assertIsNone(sock)
        self.assertEqual((state, buffered), ("reachable:http-401", b""))
        raw_request = raw_sock.sendall.call_args.args[0].decode()
        websocket_request = tls_sock.sendall.call_args.args[0].decode()
        self.assertIn("CONNECT api.openai.com:443", raw_request)
        self.assertIn("GET /v1/realtime?model=gpt-realtime-2.1", websocket_request)
        self.assertIn("Authorization: Bearer secret-key", websocket_request)

    def test_websocket_probe_fields_are_independent_from_base_chatgpt_result(self):
        item = measurement("a", "node")
        item.chatgpt = "available:JP"
        result = WebSocketStabilityResult(
            "upgrade-101", 17.0, 1, "succeeded:1", "stable-after-reconnect",
        )
        with patch("clashbench.enrich._free_port", side_effect=[18080, 19090]):
            enricher = MihomoEnricher("missing", Path("/tmp/unused"), Path("/tmp/unused.yaml"))
        with patch.object(enricher, "_api", return_value={}), \
             patch.object(enricher, "_websocket_stability", return_value=result), \
             patch("clashbench.enrich.time.sleep"):
            enricher.probe_websocket_stability([item], 15, 1)
        self.assertEqual(item.chatgpt, "available:JP")
        self.assertEqual(item.chatgpt_websocket_stability, "stable-after-reconnect")
        self.assertEqual(item.chatgpt_websocket_disconnects, 1)

    def test_two_stage_merge_only_marks_chatgpt_available_nodes_attempted(self):
        passed = measurement("a", "passed", download=None)
        passed.chatgpt = "available:JP"
        blocked = measurement("a", "blocked", download=None)
        blocked.chatgpt = "challenge:JP"
        result = measurement("a", "passed", download=500)
        result.throughput_attempted = True
        merge_two_stage([passed, blocked], [result])
        self.assertTrue(passed.throughput_attempted)
        self.assertEqual(passed.download_mbps, 500)
        self.assertFalse(blocked.throughput_attempted)
        self.assertIsNone(blocked.download_mbps)

    def test_mihomo_ports_are_distinct(self):
        with patch("clashbench.enrich._free_port", side_effect=[18080, 18080, 19090]):
            enricher = MihomoEnricher("missing", Path("/tmp/unused"), Path("/tmp/unused.yaml"))
        self.assertEqual((enricher.mixed_port, enricher.controller_port), (18080, 19090))

    def test_mihomo_worker_drops_unused_rules_from_temporary_config(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.yaml"
            source.write_text("""
proxies:
  - {name: demo, type: ss, server: 127.0.0.1, port: 1, cipher: aes-128-gcm, password: test}
rules:
  - GEOIP,CN,DIRECT
rule-providers:
  demo: {type: http, url: https://example.invalid/rules.yaml}
""", encoding="utf-8")
            process = MagicMock()
            process.poll.return_value = None
            with patch("clashbench.enrich._free_port", side_effect=[18080, 19090]), \
                 patch("clashbench.enrich.subprocess.Popen", return_value=process), \
                 patch.object(MihomoEnricher, "_api", return_value={}):
                with MihomoEnricher("/bin/echo", root, source):
                    generated = yaml.safe_load((root / "mihomo/config.yaml").read_text(encoding="utf-8"))
            self.assertEqual(generated["rules"], [])
            self.assertEqual(generated["rule-providers"], {})

    def test_mihomo_pool_partitions_nodes_across_isolated_workers(self):
        workers = [MagicMock(), MagicMock()]
        chunks = []
        for worker in workers:
            worker.__enter__.return_value = worker
            worker.checks = {"chatgpt"}

            def enrich(values, progress, *, _worker=worker):
                chunks.append([item.node_name for item in values])
                for index, item in enumerate(values, 1):
                    progress(index, len(values), item, "start")
                    progress(index, len(values), item, "done")

            worker.enrich.side_effect = enrich
        values = [measurement("a", f"n{index}") for index in range(4)]
        phases = []
        with patch("clashbench.enrich.MihomoEnricher", side_effect=workers):
            with MihomoEnricherPool(
                "mihomo", Path("/tmp/runtime"), Path("/tmp/config"), workers=2,
            ) as pool:
                self.assertEqual(pool.active_workers, 2)
                pool.enrich(values, lambda index, total, item, phase: phases.append((index, total, phase)))
        self.assertCountEqual(chunks, [["n0", "n2"], ["n1", "n3"]])
        self.assertEqual(sum(phase == "start" for _, _, phase in phases), 4)
        self.assertEqual(sum(phase == "done" for _, _, phase in phases), 4)

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
            plist = launch_agent(
                root / "bench.toml", root, ["09:00", "00:00"], regions="JP,SG,US",
            )
            self.assertIn(b"StartCalendarInterval", plist)
            payload = plistlib.loads(plist)
            self.assertEqual(payload["ProgramArguments"][:2], ["/usr/bin/caffeinate", "-i"])
            self.assertIn("--two-stage", payload["ProgramArguments"])
            self.assertEqual(payload["ProgramArguments"][-2:], ["--regions", "JP,SG,US"])
            self.assertEqual(payload["ProcessType"], "Standard")
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

    def test_native_notification_summarizes_chatgpt_by_provider(self):
        with tempfile.TemporaryDirectory() as temp:
            conn = connect(Path(temp) / "bench.db")
            run = begin_run(conn, "a", "mock", ["JP"], comparison_key="p")
            first = measurement("alpha", "one")
            first.chatgpt = "available:JP"
            second = measurement("alpha", "two")
            second.chatgpt = "challenge:JP"
            third = measurement("beta", "three")
            third.chatgpt = "available:US"
            add_measurements(conn, run, [first, second, third])
            finish_run(conn, run, "ok")
            title, subtitle, message = notification_text(conn, run, "ok")
            self.assertEqual(title, "Clash Bench：ChatGPT 2/3 可用")
            self.assertEqual(subtitle, "评测完成")
            self.assertIn("alpha 1/2", message)
            self.assertIn("beta 1/1", message)
            conn.close()

        completed = MagicMock(returncode=0, stdout="", stderr="")
        with patch("clashbench.notifications.subprocess.run", return_value=completed) as run_command:
            send_macos_notification("title", "subtitle", "message")
        self.assertEqual(run_command.call_args.args[0][0], "/usr/bin/osascript")

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
timezone = "Asia/Shanghai"
""", encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(cli_main(["run", "--config", str(config), "--mock", "--quick"]), 0)
            self.assertIn("[run] 开始", output.getvalue())
            self.assertIn("[1/2 alpha] 生成 mock 测试数据", output.getvalue())
            self.assertIn("[report] 生成", output.getvalue())
            self.assertRegex(
                output.getvalue(),
                re.compile(r"\[20\d\d-\d\d-\d\d \d\d:\d\d:\d\d\+08:00\] \[run\] 开始"),
            )
            conn = connect(root / "data/bench.sqlite3")
            run = conn.execute("SELECT * FROM runs").fetchone()
            self.assertTrue(run["comparison_key"])
            self.assertEqual(json.loads(run["parameters_json"])["speed_mode"], "fast")
            self.assertEqual(len(conn.execute("SELECT * FROM provider_runs").fetchall()), 2)
            run_id = run["id"]
            conn.close()
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli_main(["report", "--config", str(config), "--days", "3", "--run-id", run_id]), 0)
            self.assertTrue((root / "reports/latest.md").exists())
            self.assertTrue((root / "reports/trend-3d.md").exists())
            self.assertEqual(len(list((root / "reports/runs").glob("*.md"))), 1)
            latest = (root / "reports/latest.md").read_text(encoding="utf-8")
            self.assertIn("+08:00（Asia/Shanghai）", latest)
            self.assertIn("评测时区：Asia/Shanghai", latest)

    def test_schedule_render_creates_launchd_log_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "bench.toml"
            config.write_text("""
[[providers]]
name = "demo"
source_env = "DEMO_URL"
""", encoding="utf-8")
            output = root / "agent.plist"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli_main([
                    "schedule", "render", "--config", str(config), "--output", str(output),
                ]), 0)
            self.assertTrue((root / "data").is_dir())
            self.assertTrue(output.exists())


if __name__ == "__main__":
    unittest.main()
