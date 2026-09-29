from __future__ import annotations

import hashlib
import json
import os
import platform
import urllib.parse
from typing import Any

import curl_cffi

from .config import Settings


def safe_endpoint(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    return urllib.parse.urlunsplit((parsed.scheme, host + port, parsed.path, "", ""))


def benchmark_profile(
    settings: Settings, regions: list[str], engine_version: str,
    environment: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    engine = settings.engine
    enrichment = settings.enrichment
    unlock_enabled = bool(enrichment.get("unlock", False))
    enrichment_checks = enrichment.get("checks")
    if enrichment_checks is None:
        enrichment_checks = ["chatgpt", "youtube", "netflix"] if unlock_enabled else []
    if not unlock_enabled:
        enrichment_checks = []
    endpoint_value = str(engine.get("server_url", "https://speed.cloudflare.com"))
    endpoint_query = urllib.parse.urlsplit(endpoint_value).query
    openai_api_key_env = str(enrichment.get(
        "openai_api_key_env", "OPENAI_API_KEY",
    ))
    parameters = {
        "adapter": str(engine.get("adapter", "faceair")),
        "engine_version": engine_version,
        "test_strategy": str(engine.get("test_strategy", "single-stage")),
        "two_stage_merge_strategy": (
            "preserve-stage1-network-v1"
            if str(engine.get("test_strategy", "single-stage")) == "two-stage"
            else None
        ),
        "speed_mode": str(engine.get("speed_mode", "full")),
        "server_url": safe_endpoint(endpoint_value),
        "server_query_hash": hashlib.sha256(endpoint_query.encode()).hexdigest()[:12] if endpoint_query else None,
        "download_size_mb": int(engine.get("download_size_mb", 20)),
        "upload_size_mb": int(engine.get("upload_size_mb", 10)),
        "two_stage_download_size_mb": int(engine.get("two_stage_download_size_mb", 50)),
        "two_stage_upload_size_mb": int(engine.get("two_stage_upload_size_mb", 20)),
        "timeout_seconds": int(engine.get("timeout_seconds", 8)),
        "process_timeout_seconds": int(engine.get("process_timeout_seconds", 3600)),
        "concurrent": int(engine.get("concurrent", 4)),
        "regions": sorted(set(regions)),
        "providers": sorted(provider.name for provider in settings.providers),
        "enrichment_enabled": bool(enrichment.get("enabled", False)),
        "enrichment_workers": int(enrichment.get("workers", 1)),
        "unlock_enabled": unlock_enabled,
        "enrichment_checks": sorted(str(value).lower() for value in enrichment_checks),
        "chatgpt_unsupported_countries": sorted(
            str(value).upper() for value in enrichment.get("chatgpt_unsupported_countries", [])
        ) if "chatgpt" in {str(value).lower() for value in enrichment_checks} else [],
        "chatgpt_probe": {
            "client": "curl_cffi", "version": curl_cffi.__version__, "impersonate": "chrome",
            "endpoints": [
                "chatgpt.com homepage", "chatgpt.com/backend-api/me", "auth.openai.com",
                "cdn.oaistatic.com", "api.openai.com/v1/realtime",
            ],
            "websocket_kind": "openai-realtime-api-fallback",
            "websocket_validation": "session-created+ping-pong-v1",
            "realtime_model": str(enrichment.get("realtime_model", "gpt-realtime-2.1")),
            "websocket_hold_seconds": int(enrichment.get("websocket_hold_seconds", 15)),
            "websocket_reconnect_attempts": int(
                enrichment.get("websocket_reconnect_attempts", 1)
            ),
            "websocket_auth_mode": (
                "api-key" if os.environ.get(openai_api_key_env, "").strip()
                else "auth-boundary-only"
            ),
        } if "chatgpt" in {str(value).lower() for value in enrichment_checks} else None,
        "evaluation_timezone": str(settings.report.get("timezone", "Asia/Shanghai")),
        "platform": platform.system(),
        "machine": platform.machine(),
        "os_version": platform.mac_ver()[0] or platform.release(),
    }
    environment = environment or {}
    proxy = environment.get("system_proxy", {})
    parameters["network_context"] = {
        "default_interface": environment.get("default_interface"),
        "proxy_enabled": sorted(
            key for key, value in proxy.items() if key.endswith("_enabled") and value
        ),
        "proxy_server_hashes": sorted(
            value for key, value in proxy.items() if key.endswith("_server_hash")
        ),
        "active_tunnels": sorted(environment.get("tunnel_interfaces_active", [])),
    }
    canonical = json.dumps(parameters, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:20], parameters
