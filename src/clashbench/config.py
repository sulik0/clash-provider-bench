from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@dataclass
class Provider:
    name: str
    path: Path | None = None
    source_env: str | None = None
    user_agent: str | None = None


@dataclass
class Settings:
    root: Path
    database: Path
    providers: list[Provider]
    regions: list[str]
    engine: dict[str, Any]
    report: dict[str, Any] = field(default_factory=dict)
    enrichment: dict[str, Any] = field(default_factory=dict)
    notification: dict[str, Any] = field(default_factory=dict)
    region_patterns: dict[str, list[str]] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


def load_config(path: str | Path) -> Settings:
    config_path = Path(path).expanduser().resolve()
    with config_path.open("rb") as handle:
        raw = tomllib.load(handle)
    root = config_path.parent
    providers: list[Provider] = []
    for item in raw.get("providers", []):
        name = str(item.get("name", "")).strip()
        if not name:
            raise ValueError("Every provider requires a non-empty name")
        has_path = bool(item.get("path"))
        has_env = bool(item.get("source_env"))
        if has_path == has_env:
            raise ValueError(f"Provider {name!r} requires exactly one of path or source_env")
        provider_path = (root / item["path"]).resolve() if has_path else None
        providers.append(Provider(name=name, path=provider_path, source_env=item.get("source_env"),
                                  user_agent=item.get("user_agent")))
    if not providers:
        raise ValueError("At least one [[providers]] entry is required")
    engine = dict(raw.get("engine", {}))
    speed_mode = str(engine.get("speed_mode", "full"))
    if speed_mode not in {"fast", "download", "full"}:
        raise ValueError("engine.speed_mode must be fast, download, or full")
    for name, default in (
        ("download_size_mb", 20), ("upload_size_mb", 10), ("timeout_seconds", 8),
        ("process_timeout_seconds", 3600), ("concurrent", 4), ("progress_interval_seconds", 5),
        ("two_stage_download_size_mb", 50), ("two_stage_upload_size_mb", 20),
    ):
        if int(engine.get(name, default)) <= 0:
            raise ValueError(f"engine.{name} must be greater than zero")
    enrichment = dict(raw.get("enrichment", {}))
    checks = enrichment.get("checks")
    if checks is not None:
        if not isinstance(checks, list) or not all(isinstance(value, str) for value in checks):
            raise ValueError("enrichment.checks must be a list of service names")
        unknown = {value.lower() for value in checks} - {"chatgpt", "youtube", "netflix"}
        if unknown:
            raise ValueError(f"Unknown enrichment checks: {', '.join(sorted(unknown))}")
    unsupported = enrichment.get("chatgpt_unsupported_countries", [])
    if not isinstance(unsupported, list) or not all(isinstance(value, str) and len(value) == 2 for value in unsupported):
        raise ValueError("enrichment.chatgpt_unsupported_countries must contain two-letter country codes")
    workers = int(enrichment.get("workers", 1))
    if not 1 <= workers <= 16:
        raise ValueError("enrichment.workers must be between 1 and 16")
    enrichment["workers"] = workers
    notification = dict(raw.get("notification", {}))
    notification_mode = str(notification.get("mode", "macos")).lower()
    if notification_mode not in {"macos"}:
        raise ValueError(f"Unknown notification.mode: {notification_mode}")
    notification["mode"] = notification_mode
    report = dict(raw.get("report", {}))
    timezone_name = str(report.get("timezone", "Asia/Shanghai"))
    try:
        ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown report.timezone: {timezone_name}") from exc
    report["timezone"] = timezone_name
    database = (root / raw.get("database", "data/bench.sqlite3")).resolve()
    return Settings(
        root=root,
        database=database,
        providers=providers,
        regions=[str(x).upper() for x in raw.get("regions", [])],
        engine=engine,
        report=report,
        enrichment=enrichment,
        notification=notification,
        region_patterns={k.upper(): list(v) for k, v in raw.get("region_patterns", {}).items()},
        raw=raw,
    )


def provider_source(provider: Provider) -> str | Path:
    if provider.path:
        if not provider.path.exists():
            raise FileNotFoundError(f"Provider {provider.name!r} YAML does not exist: {provider.path}")
        return provider.path
    assert provider.source_env
    value = os.environ.get(provider.source_env)
    if not value:
        raise ValueError(f"Missing environment variable {provider.source_env} for provider {provider.name!r}")
    return value
