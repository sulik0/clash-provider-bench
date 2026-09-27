from __future__ import annotations

import json
import os
import secrets
import shutil
import socket
import ssl
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import yaml
import certifi

from .engine import Measurement
from .util import ensure_private_file

TLS = ssl.create_default_context(cafile=certifi.where())


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class MihomoEnricher:
    """Optional per-node egress/unlock enrichment through an isolated Mihomo process."""

    def __init__(self, binary: str, runtime: Path, source_config: Path, unlock: bool = False, timeout: int = 15):
        self.binary = binary
        self.runtime = runtime / "mihomo"
        self.source_config = source_config
        self.unlock = unlock
        self.timeout = timeout
        self.mixed_port, self.controller_port = _free_port(), _free_port()
        self.secret = secrets.token_urlsafe(24)
        self.process: subprocess.Popen | None = None

    def __enter__(self):
        resolved = self.binary if Path(self.binary).is_file() else shutil.which(self.binary)
        if not resolved:
            raise FileNotFoundError(f"mihomo not found: {self.binary}")
        config = yaml.safe_load(self.source_config.read_text(encoding="utf-8")) or {}
        config.update({
            "mixed-port": self.mixed_port, "external-controller": f"127.0.0.1:{self.controller_port}",
            "secret": self.secret, "allow-lan": False, "mode": "global", "log-level": "silent",
        })
        self.runtime.mkdir(parents=True, exist_ok=True)
        generated = self.runtime / "config.yaml"
        ensure_private_file(generated, yaml.safe_dump(config, allow_unicode=True, sort_keys=False).encode())
        self.process = subprocess.Popen([resolved, "-d", str(self.runtime), "-f", str(generated)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                self._api("GET", "/version")
                return self
            except Exception:
                if self.process.poll() is not None:
                    break
                time.sleep(.25)
        stderr = self.process.stderr.read(500) if self.process and self.process.stderr else ""
        self.__exit__(None, None, None)
        raise RuntimeError(f"mihomo did not become ready: {stderr}")

    def __exit__(self, *_):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try: self.process.wait(timeout=5)
            except subprocess.TimeoutExpired: self.process.kill()

    def _api(self, method: str, path: str, body: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(f"http://127.0.0.1:{self.controller_port}{path}", data=data, method=method,
                                         headers={"Authorization": f"Bearer {self.secret}", "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=5) as response:
            raw = response.read()
            return json.loads(raw) if raw else None

    def _proxied(self, url: str, limit: int = 512_000) -> tuple[int, bytes]:
        proxy = f"http://127.0.0.1:{self.mixed_port}"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}), urllib.request.HTTPSHandler(context=TLS))
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 clash-provider-bench/0.1", "Accept-Language": "en-US,en;q=0.8"})
        try:
            with opener.open(request, timeout=self.timeout) as response:
                return response.status, response.read(limit)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(limit)

    def enrich(self, values: list[Measurement]) -> None:
        for item in values:
            if not item.available:
                continue
            try:
                name = urllib.parse.quote("GLOBAL", safe="")
                self._api("PUT", f"/proxies/{name}", {"name": item.node_name})
                time.sleep(.15)
                status, raw = self._proxied("https://ipwho.is/")
                payload = json.loads(raw)
                if status == 200 and payload.get("success", True):
                    item.exit_ip = payload.get("ip")
                    item.exit_country = payload.get("country_code")
                    item.exit_region = payload.get("region")
                    conn = payload.get("connection") or {}
                    item.asn = str(conn.get("asn") or "") or None
                    item.as_org = conn.get("org") or conn.get("isp")
                if self.unlock:
                    item.chatgpt = self._chatgpt()
                    item.youtube = self._simple_unlock("https://www.youtube.com/premium")
                    item.netflix = self._simple_unlock("https://www.netflix.com/title/81215567")
            except Exception as exc:
                # Enrichment is best-effort and must not invalidate throughput results.
                item.error = (item.error + "; " if item.error else "") + f"enrichment: {type(exc).__name__}"

    def _chatgpt(self) -> str:
        status, raw = self._proxied("https://chatgpt.com/cdn-cgi/trace", 64_000)
        text = raw.decode("utf-8", "replace")
        loc = next((line.split("=", 1)[1] for line in text.splitlines() if line.startswith("loc=")), "")
        return f"reachable:{loc}" if status == 200 else f"blocked:{status}"

    def _simple_unlock(self, url: str) -> str:
        status, _ = self._proxied(url, 128_000)
        return "reachable" if 200 <= status < 400 else f"blocked:{status}"
