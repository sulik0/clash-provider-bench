from __future__ import annotations

import json
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from pathlib import Path

import yaml
import certifi

from .engine import Measurement
from .util import ensure_private_file

TLS = ssl.create_default_context(cafile=certifi.where())
UNLOCK_SERVICES = frozenset({"chatgpt", "youtube", "netflix"})


def classify_chatgpt_response(
    status: int, body: bytes, headers: dict[str, str] | None = None,
    location: str | None = None, unsupported_countries: Iterable[str] = (),
) -> str:
    """Classify an unauthenticated ChatGPT homepage request without claiming login success."""
    headers = {str(key).lower(): str(value).lower() for key, value in (headers or {}).items()}
    country = (location or "").strip().upper()
    suffix = f":{country}" if country else ""
    text = body.decode("utf-8", "replace").lower()
    compact = re.sub(r"\s+", "", text)
    unsupported = {str(value).upper() for value in unsupported_countries}
    if country and country in unsupported:
        return f"unsupported-country{suffix}"
    if (
        re.search(r"unsupported[_-]?country.{0,40}(true|region|territor)", compact)
        or "not available in your country" in text
        or "unsupported country" in text
        or "unsupported_country_region_territory" in text
    ):
        return f"unsupported-country{suffix}"
    if headers.get("cf-mitigated") == "challenge" or any(marker in text for marker in (
        "challenge-error-text", "attention required! | cloudflare", "cf-chl-", "just a moment...",
    )):
        return f"challenge{suffix}"
    if 200 <= status < 400:
        return f"available{suffix}"
    if status == 429:
        return f"rate-limited{suffix}"
    if status in (401, 403) or any(marker in text for marker in (
        "sorry, you have been blocked", "access denied", "you are unable to access chatgpt.com",
    )):
        return f"blocked:{status}{suffix}"
    return f"http-{status}{suffix}"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class MihomoEnricher:
    """Optional per-node egress/unlock enrichment through an isolated Mihomo process."""

    def __init__(
        self, binary: str, runtime: Path, source_config: Path, unlock: bool = False, timeout: int = 15,
        checks: Iterable[str] | None = None, chatgpt_unsupported_countries: Iterable[str] = (),
    ):
        self.binary = binary
        self.runtime = runtime / "mihomo"
        self.source_config = source_config
        self.unlock = unlock
        self.timeout = timeout
        requested = {str(value).lower() for value in (UNLOCK_SERVICES if checks is None else checks)}
        unknown = requested - UNLOCK_SERVICES
        if unknown:
            raise ValueError(f"Unknown enrichment checks: {', '.join(sorted(unknown))}")
        self.checks = requested if unlock else set()
        self.chatgpt_unsupported_countries = {
            str(value).upper() for value in chatgpt_unsupported_countries
        }
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

    def _proxied(self, url: str, limit: int = 512_000) -> tuple[int, bytes, dict[str, str]]:
        proxy = f"http://127.0.0.1:{self.mixed_port}"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}), urllib.request.HTTPSHandler(context=TLS))
        request = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.8",
        })
        try:
            with opener.open(request, timeout=self.timeout) as response:
                return response.status, response.read(limit), dict(response.headers.items())
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(limit), dict(exc.headers.items())

    def enrich(
        self, values: list[Measurement],
        progress: Callable[[int, int, Measurement, str], None] | None = None,
    ) -> None:
        total = len(values)
        for index, item in enumerate(values, 1):
            if progress:
                progress(index, total, item, "start")
            if not item.available:
                item.enrichment_status = "skipped"
                if progress:
                    progress(index, total, item, "skipped")
                continue
            item.enrichment_status = "pending"
            try:
                name = urllib.parse.quote("GLOBAL", safe="")
                self._api("PUT", f"/proxies/{name}", {"name": item.node_name})
                time.sleep(.15)
            except Exception as exc:
                item.enrichment_status = "failed"
                item.enrichment_error = f"select:{type(exc).__name__}"
                if progress:
                    progress(index, total, item, "done")
                continue

            errors = []
            try:
                status, raw, _ = self._proxied("https://ipwho.is/")
                payload = json.loads(raw)
                if status == 200 and payload.get("success", True):
                    item.exit_ip = payload.get("ip")
                    item.exit_country = payload.get("country_code")
                    item.exit_region = payload.get("region")
                    conn = payload.get("connection") or {}
                    item.asn = str(conn.get("asn") or "") or None
                    item.as_org = conn.get("org") or conn.get("isp")
                else:
                    errors.append(f"ip:HTTP-{status}")
            except Exception as exc:
                errors.append(f"ip:{type(exc).__name__}")

            if "chatgpt" in self.checks:
                try:
                    item.chatgpt = self._chatgpt(item.exit_country)
                except Exception as exc:
                    item.chatgpt = f"error:{type(exc).__name__}"
                    errors.append(f"chatgpt:{type(exc).__name__}")
            if "youtube" in self.checks:
                try:
                    item.youtube = self._simple_unlock("https://www.youtube.com/premium")
                except Exception as exc:
                    item.youtube = f"error:{type(exc).__name__}"
                    errors.append(f"youtube:{type(exc).__name__}")
            if "netflix" in self.checks:
                try:
                    item.netflix = self._simple_unlock("https://www.netflix.com/title/81215567")
                except Exception as exc:
                    item.netflix = f"error:{type(exc).__name__}"
                    errors.append(f"netflix:{type(exc).__name__}")

            completed_check = any(
                value and not value.startswith("error:")
                for value in (item.chatgpt, item.youtube, item.netflix)
            )
            if errors:
                item.enrichment_status = "partial" if item.exit_ip or completed_check else "failed"
                item.enrichment_error = ";".join(errors)
            else:
                item.enrichment_status = "ok"
            if progress:
                progress(index, total, item, "done")

    def _chatgpt(self, location: str | None = None) -> str:
        if not location:
            trace_status, trace_raw, _ = self._proxied("https://chatgpt.com/cdn-cgi/trace", 64_000)
            if trace_status == 200:
                trace = trace_raw.decode("utf-8", "replace")
                location = next((line.split("=", 1)[1] for line in trace.splitlines() if line.startswith("loc=")), "")
        status, raw, headers = self._proxied("https://chatgpt.com/", 256_000)
        homepage = classify_chatgpt_response(
            status, raw, headers, location, self.chatgpt_unsupported_countries,
        )
        if not homepage.startswith("available"):
            return homepage

        # A page shell can load even when the ChatGPT backend is challenged. An
        # unauthenticated backend response (normally 401/403) proves reachability
        # without using account cookies or treating authentication failure as a block.
        backend_status, backend_raw, backend_headers = self._proxied(
            "https://chatgpt.com/backend-api/me", 64_000,
        )
        backend = classify_chatgpt_response(
            backend_status, backend_raw, backend_headers, location,
            self.chatgpt_unsupported_countries,
        )
        backend_text = backend_raw.decode("utf-8", "replace").lower()
        if backend.startswith(("challenge", "unsupported-country", "rate-limited")):
            return backend
        if backend.startswith("blocked") and any(marker in backend_text for marker in (
            "sorry, you have been blocked", "access denied", "you are unable to access chatgpt.com",
        )):
            return backend
        if backend_status >= 500:
            return f"backend-http-{backend_status}{':' + location.upper() if location else ''}"
        return homepage

    def _simple_unlock(self, url: str) -> str:
        status, _, _ = self._proxied(url, 128_000)
        return "reachable" if 200 <= status < 400 else f"blocked:{status}"
