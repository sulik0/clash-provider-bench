from __future__ import annotations

import base64
import csv
import io
import os
import re
import shutil
import ssl
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

import yaml
import certifi

from .config import Provider, Settings, provider_source
from .regions import classify_region, region_filter_regex
from .util import ensure_private_file, redact, stable_node_key


@dataclass
class Measurement:
    provider: str
    node_name: str
    node_key: str
    proxy_type: str
    region: str
    available: bool
    ttfb_ms: float | None = None
    jitter_ms: float | None = None
    packet_loss_pct: float | None = None
    download_mbps: float | None = None
    upload_mbps: float | None = None
    status: str = "ok"
    error: str | None = None
    exit_ip: str | None = None
    exit_country: str | None = None
    exit_region: str | None = None
    asn: str | None = None
    as_org: str | None = None
    chatgpt: str | None = None
    youtube: str | None = None
    netflix: str | None = None
    enrichment_status: str = "not_requested"
    enrichment_error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _parse_duration(value: str) -> float | None:
    value = value.strip()
    if value.upper() == "N/A" or not value:
        return None
    match = re.fullmatch(r"([0-9.]+)ms", value)
    return float(match.group(1)) if match else None


def _parse_percent(value: str) -> float | None:
    match = re.fullmatch(r"([0-9.]+)%", value.strip())
    return float(match.group(1)) if match else None


def _parse_speed_mbps(value: str) -> tuple[float | None, str | None]:
    value = value.strip()
    if not value or value.upper() == "N/A":
        return None, None
    match = re.fullmatch(r"([0-9.]+)(B/s|KB/s|MB/s|GB/s|TB/s)", value)
    if not match:
        return None, redact(value)[:240]
    amount = float(match.group(1))
    multipliers = {"B/s": 1, "KB/s": 1024, "MB/s": 1024**2, "GB/s": 1024**3, "TB/s": 1024**4}
    return amount * multipliers[match.group(2)] * 8 / 1_000_000, None


def parse_faceair_tsv(text: str, provider: str, nodes: dict[str, dict[str, Any]], patterns=None) -> list[Measurement]:
    reader = csv.DictReader(io.StringIO(text), delimiter="\t")
    results: list[Measurement] = []
    for row in reader:
        name = (row.get("节点名称") or row.get("Name") or "").strip()
        if not name:
            continue
        latency = _parse_duration(row.get("延迟", row.get("Latency", "")))
        jitter = _parse_duration(row.get("抖动", row.get("Jitter", "")))
        loss = _parse_percent(row.get("丢包率", row.get("Packet Loss", "")))
        download, download_error = _parse_speed_mbps(row.get("下载速度", row.get("Download Speed", "")))
        upload, upload_error = _parse_speed_mbps(row.get("上传速度", row.get("Upload Speed", "")))
        error = download_error or upload_error
        available = latency is not None and (loss is None or loss < 100)
        if not available and not error:
            error = "latency probe failed"
        node = nodes.get(name, {})
        results.append(
            Measurement(
                provider=provider,
                node_name=name,
                node_key=stable_node_key(provider, name, node),
                proxy_type=(row.get("类型") or row.get("Type") or node.get("type") or "unknown"),
                region=classify_region(name, patterns),
                available=available,
                ttfb_ms=latency,
                jitter_ms=jitter,
                packet_loss_pct=loss,
                download_mbps=download,
                upload_mbps=upload,
                status="ok" if available and not error else "failed",
                error=error,
            )
        )
    return results


def load_nodes(path: Path) -> dict[str, dict[str, Any]]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    return {str(node.get("name")): node for node in data.get("proxies", []) if isinstance(node, dict) and node.get("name")}


def subscription_format(content: bytes) -> str:
    """Classify a subscription without returning or logging any credential-bearing content."""
    if len(content) < 10:
        return "empty"
    text = content.decode("utf-8", "replace").lstrip("\ufeff\r\n \t")
    try:
        data = yaml.safe_load(text)
        if isinstance(data, dict) and (isinstance(data.get("proxies"), list) or isinstance(data.get("proxy-providers"), dict)):
            return "clash-yaml"
    except yaml.YAMLError:
        pass
    if re.match(r"(?i)^(vless|vmess|trojan|ss|ssr|hysteria2?|tuic|anytls)://", text):
        return "uri-list"
    try:
        decoded = base64.b64decode("".join(text.split()), validate=True).decode("utf-8", "replace").lstrip()
        if re.match(r"(?i)^(vless|vmess|trojan|ss|ssr|hysteria2?|tuic|anytls)://", decoded):
            return "base64-uri-list"
    except (ValueError, UnicodeError):
        pass
    if text[:64].lower().startswith(("<!doctype html", "<html")):
        return "html"
    return "unknown"


def materialize_provider(
    provider: Provider, runtime: Path, user_agent: str,
    progress: Callable[[str], None] | None = None,
) -> Path:
    source = provider_source(provider)
    target = runtime / "configs" / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', provider.name)}.yaml"
    if isinstance(source, Path):
        if progress:
            progress("读取本地订阅配置")
        content = source.read_bytes()
        detected = subscription_format(content)
    else:
        # Several providers return a generic Base64 URI list for Mihomo's own UA,
        # but native Clash/Mihomo YAML for Clash.Meta. Prefer a native response over
        # reimplementing protocol conversion locally.
        candidates = [provider.user_agent, user_agent, "clash.meta", "Clash.Meta", "ClashforWindows/0.20.39"]
        candidates = list(dict.fromkeys(value for value in candidates if value))
        content, detected = b"", "empty"
        context = ssl.create_default_context(cafile=certifi.where())
        for index, candidate in enumerate(candidates, 1):
            if progress:
                progress(f"请求订阅：尝试 {index}/{len(candidates)}")
            request = urllib.request.Request(source, headers={"User-Agent": candidate, "Accept": "*/*"})
            try:
                with urllib.request.urlopen(request, timeout=30, context=context) as response:
                    content = response.read()
            except urllib.error.HTTPError as exc:
                detected = f"http-{exc.code}"
                continue
            except urllib.error.URLError:
                detected = "network-error"
                continue
            detected = subscription_format(content)
            if detected == "clash-yaml":
                break
    if detected != "clash-yaml":
        raise ValueError(
            f"Provider {provider.name!r} returned {detected}, not Clash/Mihomo YAML; "
            "set provider.user_agent to a UA supported by the subscription service"
        )
    ensure_private_file(target, content)
    if progress:
        progress(f"订阅配置就绪：{len(load_nodes(target))} 个内嵌节点")
    return target


class FaceairAdapter:
    def __init__(self, settings: Settings):
        self.settings = settings
        configured = str(settings.engine.get("binary", "clash-speedtest"))
        candidate = (settings.root / configured).resolve() if "/" in configured else Path(configured)
        self.binary = str(candidate)

    def doctor(self) -> tuple[bool, str]:
        found = self.binary if Path(self.binary).is_file() else shutil.which(self.binary)
        if not found:
            return False, f"clash-speedtest not found: {self.binary}"
        proc = subprocess.run([found, "-v"], capture_output=True, text=True, timeout=10)
        return proc.returncode == 0, (proc.stdout or proc.stderr).strip()

    def run(
        self, provider: Provider, config_path: Path, regions: list[str],
        progress: Callable[[str], None] | None = None,
    ) -> list[Measurement]:
        engine = self.settings.engine
        speed_mode = str(engine.get("speed_mode", "full"))
        nodes = load_nodes(config_path)
        selected_nodes = sum(
            1 for name in nodes
            if not regions or classify_region(name, self.settings.region_patterns) in regions
        )
        args = [
            self.binary,
            "-c", str(config_path),
            "-f", region_filter_regex(regions, self.settings.region_patterns),
            "-speed-mode", speed_mode,
            "-server-url", str(engine.get("server_url", "https://speed.cloudflare.com")),
            "-download-size", str(int(engine.get("download_size_mb", 20)) * 1024 * 1024),
            "-upload-size", str(int(engine.get("upload_size_mb", 10)) * 1024 * 1024),
            "-timeout", f"{int(engine.get('timeout_seconds', 8))}s",
            "-concurrent", str(int(engine.get("concurrent", 4))),
        ]
        process_timeout = int(engine.get("process_timeout_seconds", 3600))
        progress_interval = max(1, int(engine.get("progress_interval_seconds", 5)))
        started = time.monotonic()
        if progress:
            count = str(selected_nodes) if nodes else "未知"
            progress(f"测速内核运行中：已匹配 {count} 个节点")
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        while True:
            elapsed = time.monotonic() - started
            remaining = process_timeout - elapsed
            if remaining <= 0:
                proc.kill()
                stdout, stderr = proc.communicate()
                raise subprocess.TimeoutExpired(args, process_timeout, output=stdout, stderr=stderr)
            try:
                stdout, stderr = proc.communicate(timeout=min(progress_interval, remaining))
                break
            except subprocess.TimeoutExpired:
                if progress:
                    progress(f"测速内核运行中：已用时 {int(time.monotonic() - started)} 秒")
        if proc.returncode != 0:
            raise RuntimeError(f"clash-speedtest failed for provider {provider.name!r}: {redact(stderr)[-600:]}")
        values = parse_faceair_tsv(stdout, provider.name, nodes, self.settings.region_patterns)
        if progress:
            progress(f"测速内核完成：返回 {len(values)} 条节点结果，用时 {int(time.monotonic() - started)} 秒")
        return values


def write_mock_tsv(path: Path, provider: str) -> str:
    # Deterministic fixture used only by `run --mock`; it never represents a live test.
    offset = sum(ord(c) for c in provider) % 17
    rows = [
        ["1.", "🇯🇵 Tokyo 01", "Shadowsocks", f"{42 + offset}ms", "5ms", "0.0%", "18.50MB/s", "8.20MB/s"],
        ["2.", "🇭🇰 Hong Kong 01", "Trojan", f"{28 + offset}ms", "3ms", "0.0%", "22.10MB/s", "10.00MB/s"],
        ["3.", "🇸🇬 Singapore 01", "VLESS", "N/A", "N/A", "100.0%", "N/A", "N/A"],
        ["4.", "🇺🇸 Los Angeles 01", "Trojan", f"{145 + offset}ms", "11ms", "16.7%", "9.80MB/s", "4.40MB/s"],
    ]
    output = io.StringIO()
    writer = csv.writer(output, delimiter="\t", lineterminator="\n")
    writer.writerow(["序号", "节点名称", "类型", "延迟", "抖动", "丢包率", "下载速度", "上传速度"])
    writer.writerows(rows)
    return output.getvalue()
