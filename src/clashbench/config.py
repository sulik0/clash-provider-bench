from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


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
    database = (root / raw.get("database", "data/bench.sqlite3")).resolve()
    return Settings(
        root=root,
        database=database,
        providers=providers,
        regions=[str(x).upper() for x in raw.get("regions", [])],
        engine=dict(raw.get("engine", {})),
        report=dict(raw.get("report", {})),
        enrichment=dict(raw.get("enrichment", {})),
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
