from __future__ import annotations

import hashlib
import json
import platform
import urllib.parse
from typing import Any

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
    endpoint_value = str(engine.get("server_url", "https://speed.cloudflare.com"))
    endpoint_query = urllib.parse.urlsplit(endpoint_value).query
    parameters = {
        "adapter": str(engine.get("adapter", "faceair")),
        "engine_version": engine_version,
        "speed_mode": str(engine.get("speed_mode", "full")),
        "server_url": safe_endpoint(endpoint_value),
        "server_query_hash": hashlib.sha256(endpoint_query.encode()).hexdigest()[:12] if endpoint_query else None,
        "download_size_mb": int(engine.get("download_size_mb", 20)),
        "upload_size_mb": int(engine.get("upload_size_mb", 10)),
        "timeout_seconds": int(engine.get("timeout_seconds", 8)),
        "process_timeout_seconds": int(engine.get("process_timeout_seconds", 3600)),
        "concurrent": int(engine.get("concurrent", 4)),
        "regions": sorted(set(regions)),
        "providers": sorted(provider.name for provider in settings.providers),
        "enrichment_enabled": bool(enrichment.get("enabled", False)),
        "unlock_enabled": bool(enrichment.get("unlock", False)),
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
