from __future__ import annotations

import hashlib
import json
import math
import os
import re
from pathlib import Path
from typing import Any, Iterable


SECRET_KEYS = re.compile(r"(?i)(token|secret|password|authorization|subscription|url)")
URL_RE = re.compile(r"https?://[^\s\]\[()<>\"']+")


def redact(value: Any) -> str:
    text = str(value)
    text = URL_RE.sub("<redacted-url>", text)
    text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1<redacted>", text)
    text = re.sub(r"(?i)(token|secret|password)=([^&\s]+)", r"\1=<redacted>", text)
    return text


def public_config_digest(config: dict[str, Any]) -> str:
    def clean(obj: Any) -> Any:
        if isinstance(obj, dict):
            return {k: ("<redacted>" if SECRET_KEYS.search(k) else clean(v)) for k, v in obj.items()}
        if isinstance(obj, list):
            return [clean(v) for v in obj]
        if isinstance(obj, str) and URL_RE.search(obj):
            return "<redacted-url>"
        return obj

    payload = json.dumps(clean(config), sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(payload).hexdigest()[:16]


def percentile(values: Iterable[float | int | None], p: float) -> float | None:
    items = sorted(float(v) for v in values if v is not None and math.isfinite(float(v)))
    if not items:
        return None
    if len(items) == 1:
        return items[0]
    rank = (len(items) - 1) * p
    lo, hi = math.floor(rank), math.ceil(rank)
    if lo == hi:
        return items[lo]
    return items[lo] + (items[hi] - items[lo]) * (rank - lo)


def stable_node_key(provider: str, name: str, node: dict[str, Any] | None = None) -> str:
    node = node or {}
    identity = "|".join(
        [provider, str(node.get("server", "")), str(node.get("port", "")), str(node.get("type", "")), name]
    )
    return hashlib.sha256(identity.encode("utf-8", "replace")).hexdigest()[:24]


def ensure_private_file(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, content)
    finally:
        os.close(fd)
    os.chmod(path, 0o600)


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"").strip("'")
        if key and key not in os.environ:
            os.environ[key] = value

