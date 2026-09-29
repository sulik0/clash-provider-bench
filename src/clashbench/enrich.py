from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import ssl
import struct
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from collections.abc import Callable, Iterable
from pathlib import Path
from threading import Lock
from dataclasses import dataclass

import yaml
import certifi
from curl_cffi import requests as browser_requests

from .engine import Measurement
from .util import ensure_private_file

TLS = ssl.create_default_context(cafile=certifi.where())
UNLOCK_SERVICES = frozenset({"chatgpt", "youtube", "netflix"})


@dataclass(frozen=True)
class ChatGPTProbeResult:
    overall: str
    auth: str
    static: str


@dataclass(frozen=True)
class WebSocketStabilityResult:
    handshake: str
    connected_seconds: float
    abnormal_disconnects: int
    reconnect: str
    stability: str


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


def classify_reachability_response(
    status: int, body: bytes, headers: dict[str, str] | None = None,
) -> str:
    """Classify domain/network reachability without claiming authentication success."""
    headers = {str(key).lower(): str(value).lower() for key, value in (headers or {}).items()}
    text = body.decode("utf-8", "replace").lower()
    if headers.get("cf-mitigated") == "challenge" or any(marker in text for marker in (
        "challenge-error-text", "attention required! | cloudflare", "cf-chl-", "just a moment...",
    )):
        return "challenge"
    if status == 429:
        return "rate-limited"
    if status < 500:
        return f"reachable:http-{status}"
    return f"http-{status}"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class MihomoEnricher:
    """Optional per-node egress/unlock enrichment through an isolated Mihomo process."""

    def __init__(
        self, binary: str, runtime: Path, source_config: Path, unlock: bool = False, timeout: int = 15,
        checks: Iterable[str] | None = None, chatgpt_unsupported_countries: Iterable[str] = (),
        openai_api_key: str | None = None, realtime_model: str = "gpt-realtime-2.1",
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
        self.openai_api_key = openai_api_key or None
        self.realtime_model = realtime_model
        self.mixed_port = _free_port()
        self.controller_port = _free_port()
        while self.controller_port == self.mixed_port:
            self.controller_port = _free_port()
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
            # Global mode never evaluates rules. Removing them from the isolated
            # temporary config avoids needless GeoIP/GeoSite database downloads
            # for every worker while preserving all proxies and proxy groups.
            "rules": [], "rule-providers": {},
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
        stderr = ""
        if self.process and self.process.stderr:
            try:
                os.set_blocking(self.process.stderr.fileno(), False)
                stderr = self.process.stderr.read(500) or ""
            except (OSError, ValueError):
                pass
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
            return exc.code, exc.read(limit), dict(exc.headers.items()) if exc.headers else {}

    def _browser_proxied(
        self, url: str, limit: int, session: browser_requests.Session,
        *, method: str = "GET", headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes, dict[str, str]]:
        """Request through Mihomo with a real Chrome TLS/HTTP2 fingerprint."""
        proxy = f"http://127.0.0.1:{self.mixed_port}"
        response = session.request(
            method, url, proxy=proxy, timeout=self.timeout, allow_redirects=True,
            headers=headers,
        )
        try:
            return response.status_code, response.content[:limit], dict(response.headers.items())
        finally:
            response.close()

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
                    result = self._chatgpt(item.exit_country)
                    if isinstance(result, ChatGPTProbeResult):
                        item.chatgpt = result.overall
                        item.chatgpt_auth = result.auth
                        item.chatgpt_static = result.static
                    else:  # Compatibility for custom/mock enrichers returning the legacy string.
                        item.chatgpt = result
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

    @staticmethod
    def _send_websocket_frame(sock: ssl.SSLSocket, opcode: int, payload: bytes = b"") -> None:
        mask = secrets.token_bytes(4)
        length = len(payload)
        header = bytearray([0x80 | opcode])
        if length < 126:
            header.append(0x80 | length)
        elif length < 65_536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        sock.sendall(bytes(header) + mask + masked)

    @staticmethod
    def _recv_websocket_frame(
        sock: ssl.SSLSocket, buffered: bytes,
    ) -> tuple[int, bytes, bytes]:
        def take(count: int, value: bytes) -> tuple[bytes, bytes]:
            while len(value) < count:
                chunk = sock.recv(max(4096, count - len(value)))
                if not chunk:
                    raise ConnectionError("websocket closed")
                value += chunk
            return value[:count], value[count:]

        header, buffered = take(2, buffered)
        opcode = header[0] & 0x0F
        masked = bool(header[1] & 0x80)
        length = header[1] & 0x7F
        if length == 126:
            raw_length, buffered = take(2, buffered)
            length = struct.unpack("!H", raw_length)[0]
        elif length == 127:
            raw_length, buffered = take(8, buffered)
            length = struct.unpack("!Q", raw_length)[0]
        if length > 2_000_000:
            raise ValueError("websocket frame too large")
        mask = b""
        if masked:
            mask, buffered = take(4, buffered)
        payload, buffered = take(length, buffered)
        if masked:
            payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        return opcode, payload, buffered

    def _open_realtime_websocket(self) -> tuple[ssl.SSLSocket | None, str, bytes]:
        """Open the documented Realtime API WebSocket through the selected node."""
        target = "api.openai.com"
        port = 443
        raw_sock: socket.socket | None = socket.create_connection(
            ("127.0.0.1", self.mixed_port), timeout=self.timeout,
        )
        tls: ssl.SSLSocket | None = None
        try:
            raw_sock.settimeout(self.timeout)
            raw_sock.sendall(
                f"CONNECT {target}:{port} HTTP/1.1\r\nHost: {target}:{port}\r\n"
                "Proxy-Connection: keep-alive\r\n\r\n".encode()
            )
            response = b""
            while b"\r\n\r\n" not in response and len(response) < 64_000:
                chunk = raw_sock.recv(4096)
                if not chunk:
                    break
                response += chunk
            first = response.split(b"\r\n", 1)[0].decode("ascii", "replace")
            if " 200 " not in first:
                return None, f"proxy-{first or 'no-response'}", b""
            tls = TLS.wrap_socket(raw_sock, server_hostname=target)
            raw_sock = None
            key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
            model = urllib.parse.quote(self.realtime_model, safe="")
            authorization = (
                f"Authorization: Bearer {self.openai_api_key}\r\n"
                if self.openai_api_key else ""
            )
            request = (
                f"GET /v1/realtime?model={model} HTTP/1.1\r\nHost: {target}\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
                f"{authorization}User-Agent: clash-provider-bench\r\n\r\n"
            )
            tls.sendall(request.encode("ascii"))
            response = b""
            while b"\r\n\r\n" not in response and len(response) < 64_000:
                chunk = tls.recv(4096)
                if not chunk:
                    break
                response += chunk
            header, _, buffered = response.partition(b"\r\n\r\n")
            match = re.match(rb"HTTP/\d(?:\.\d)?\s+(\d{3})", header)
            if not match:
                tls.close()
                return None, "invalid-response", b""
            status = int(match.group(1))
            if status != 101:
                tls.close()
                result = (
                    f"reachable:http-{status}" if status in (401, 403)
                    else f"http-{status}"
                )
                return None, result, b""
            expected = base64.b64encode(hashlib.sha1(
                (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")
            ).digest()).decode("ascii")
            header_text = header.decode("latin-1", "replace")
            response_headers = {
                name.strip().lower(): value.strip()
                for line in header_text.split("\r\n")[1:]
                if ":" in line
                for name, value in (line.split(":", 1),)
            }
            if response_headers.get("sec-websocket-accept") != expected:
                tls.close()
                return None, "invalid-accept", b""
            return tls, "upgrade-101", buffered
        finally:
            if raw_sock is not None:
                raw_sock.close()

    def _hold_chatgpt_websocket(
        self, sock: ssl.SSLSocket, buffered: bytes, hold_seconds: int,
    ) -> tuple[float, bool]:
        started = time.monotonic()
        next_ping = started + min(5, max(1, hold_seconds / 2))
        try:
            while True:
                elapsed = time.monotonic() - started
                if elapsed >= hold_seconds:
                    try:
                        self._send_websocket_frame(sock, 0x8, struct.pack("!H", 1000))
                    except OSError:
                        pass
                    return elapsed, True
                if time.monotonic() >= next_ping:
                    self._send_websocket_frame(sock, 0x9, b"clashbench")
                    next_ping = time.monotonic() + 5
                sock.settimeout(min(1.0, max(0.1, hold_seconds - elapsed)))
                try:
                    opcode, payload, buffered = self._recv_websocket_frame(sock, buffered)
                except socket.timeout:
                    continue
                if opcode == 0x8:
                    return time.monotonic() - started, False
                if opcode == 0x9:
                    self._send_websocket_frame(sock, 0xA, payload)
        except (ConnectionError, OSError, ssl.SSLError, ValueError):
            return time.monotonic() - started, False
        finally:
            sock.close()

    def _websocket_stability(
        self, hold_seconds: int, reconnect_attempts: int,
    ) -> WebSocketStabilityResult:
        longest_connected = 0.0
        disconnects = 0
        sock, handshake, buffered = self._open_realtime_websocket()
        if not self.openai_api_key:
            boundary = handshake == "upgrade-101" or handshake.startswith("reachable:http-")
            if sock:
                try:
                    self._send_websocket_frame(sock, 0x8, struct.pack("!H", 1000))
                except OSError:
                    pass
                finally:
                    sock.close()
            return WebSocketStabilityResult(
                handshake, 0.0, 0, "not-attempted",
                "api-auth-boundary" if boundary else "handshake-failed",
            )
        if not sock:
            return WebSocketStabilityResult(
                handshake, 0.0, 0, "not-attempted", "handshake-failed",
            )
        connected, survived = self._hold_chatgpt_websocket(sock, buffered, hold_seconds)
        longest_connected = max(longest_connected, connected)
        if survived:
            return WebSocketStabilityResult(
                handshake, round(longest_connected, 1), 0, "not-needed", "stable",
            )
        disconnects += 1
        if reconnect_attempts <= 0:
            return WebSocketStabilityResult(
                handshake, round(longest_connected, 1), disconnects, "disabled",
                "unstable-disconnected",
            )
        last_reconnect = "failed"
        for attempt in range(1, reconnect_attempts + 1):
            sock, reconnect_handshake, buffered = self._open_realtime_websocket()
            if not sock:
                last_reconnect = f"failed:{reconnect_handshake}"
                continue
            connected, survived = self._hold_chatgpt_websocket(sock, buffered, hold_seconds)
            longest_connected = max(longest_connected, connected)
            if survived:
                return WebSocketStabilityResult(
                    handshake, round(longest_connected, 1), disconnects,
                    f"succeeded:{attempt}", "stable-after-reconnect",
                )
            disconnects += 1
            last_reconnect = f"disconnected:{attempt}"
        return WebSocketStabilityResult(
            handshake, round(longest_connected, 1), disconnects, last_reconnect,
            "unstable-disconnected",
        )

    def _chatgpt(self, location: str | None = None) -> ChatGPTProbeResult:
        # A fresh session per node prevents cookies and pooled proxy connections
        # from leaking across a GLOBAL selector change. HTTP requests for one node
        # intentionally share a session, matching normal browser behavior.
        session = browser_requests.Session(impersonate="chrome")
        try:
            if not location:
                trace_status, trace_raw, _ = self._browser_proxied(
                    "https://chatgpt.com/cdn-cgi/trace", 64_000, session,
                )
                if trace_status == 200:
                    trace = trace_raw.decode("utf-8", "replace")
                    location = next((line.split("=", 1)[1] for line in trace.splitlines() if line.startswith("loc=")), "")
            status, raw, headers = self._browser_proxied("https://chatgpt.com/", 256_000, session)
            homepage = classify_chatgpt_response(
                status, raw, headers, location, self.chatgpt_unsupported_countries,
            )
            if not homepage.startswith("available"):
                return ChatGPTProbeResult(homepage, "not-run", "not-run")

            # A page shell can load even when the ChatGPT backend is challenged. An
            # unauthenticated backend response (normally 401/403) proves reachability
            # without using account cookies or treating authentication failure as a block.
            backend_status, backend_raw, backend_headers = self._browser_proxied(
                "https://chatgpt.com/backend-api/me", 64_000, session,
            )
            backend = classify_chatgpt_response(
                backend_status, backend_raw, backend_headers, location,
                self.chatgpt_unsupported_countries,
            )
            backend_text = backend_raw.decode("utf-8", "replace").lower()
            if backend.startswith(("challenge", "unsupported-country", "rate-limited")):
                return ChatGPTProbeResult(backend, "not-run", "not-run")
            if backend.startswith("blocked") and any(marker in backend_text for marker in (
                "sorry, you have been blocked", "access denied", "you are unable to access chatgpt.com",
            )):
                return ChatGPTProbeResult(backend, "not-run", "not-run")
            if backend_status >= 500:
                overall = f"backend-http-{backend_status}{':' + location.upper() if location else ''}"
                return ChatGPTProbeResult(overall, "not-run", "not-run")

            try:
                auth = classify_reachability_response(*self._browser_proxied(
                    "https://auth.openai.com/", 64_000, session,
                ))
            except Exception as exc:
                auth = f"error:{type(exc).__name__}"
            try:
                static = classify_reachability_response(*self._browser_proxied(
                    "https://cdn.oaistatic.com/", 64_000, session,
                ))
            except Exception as exc:
                static = f"error:{type(exc).__name__}"
            overall = homepage
            for name, result in (("auth", auth), ("static", static)):
                if not result.startswith("reachable:"):
                    suffix = f":{location.upper()}" if location else ""
                    overall = f"{name}-{result}{suffix}"
                    break
            return ChatGPTProbeResult(overall, auth, static)
        finally:
            session.close()

    def probe_websocket_stability(
        self, values: list[Measurement], hold_seconds: int, reconnect_attempts: int,
        progress: Callable[[int, int, Measurement, str], None] | None = None,
    ) -> None:
        total = len(values)
        for index, item in enumerate(values, 1):
            if progress:
                progress(index, total, item, "start")
            try:
                name = urllib.parse.quote("GLOBAL", safe="")
                self._api("PUT", f"/proxies/{name}", {"name": item.node_name})
                time.sleep(.15)
                result = self._websocket_stability(hold_seconds, reconnect_attempts)
                item.chatgpt_websocket = result.handshake
                item.chatgpt_websocket_seconds = result.connected_seconds
                item.chatgpt_websocket_disconnects = result.abnormal_disconnects
                item.chatgpt_websocket_reconnect = result.reconnect
                item.chatgpt_websocket_stability = result.stability
                if result.stability not in {
                    "stable", "stable-after-reconnect", "api-auth-boundary",
                }:
                    detail = f"websocket:{result.stability}"
                    item.enrichment_error = ";".join(filter(None, (item.enrichment_error, detail)))
                    item.enrichment_status = "partial" if item.enrichment_status == "ok" else item.enrichment_status
            except Exception as exc:
                item.chatgpt_websocket = f"error:{type(exc).__name__}"
                item.chatgpt_websocket_seconds = 0.0
                item.chatgpt_websocket_disconnects = 0
                item.chatgpt_websocket_reconnect = "not-attempted"
                item.chatgpt_websocket_stability = "handshake-failed"
                detail = f"websocket:{type(exc).__name__}"
                item.enrichment_error = ";".join(filter(None, (item.enrichment_error, detail)))
                item.enrichment_status = "partial" if item.enrichment_status == "ok" else item.enrichment_status
            if progress:
                progress(index, total, item, "done")

    def _simple_unlock(self, url: str) -> str:
        status, _, _ = self._proxied(url, 128_000)
        return "reachable" if 200 <= status < 400 else f"blocked:{status}"


class MihomoEnricherPool:
    """Run independent Mihomo instances so per-node HTTP checks can overlap safely.

    A single Mihomo GLOBAL selector is process-wide, so sharing one process across
    threads would route requests through the wrong node. Each worker therefore has
    its own controller, proxy port, data directory, and selector state.
    """

    def __init__(
        self, binary: str, runtime: Path, source_config: Path, unlock: bool = False,
        timeout: int = 15, checks: Iterable[str] | None = None,
        chatgpt_unsupported_countries: Iterable[str] = (), workers: int = 1,
        openai_api_key: str | None = None, realtime_model: str = "gpt-realtime-2.1",
    ):
        self.binary = binary
        self.runtime = runtime
        self.source_config = source_config
        self.unlock = unlock
        self.timeout = timeout
        self.requested_checks = checks
        self.chatgpt_unsupported_countries = tuple(chatgpt_unsupported_countries)
        self.requested_workers = max(1, int(workers))
        self.openai_api_key = openai_api_key or None
        self.realtime_model = realtime_model
        self._stack: ExitStack | None = None
        self._workers: list[MihomoEnricher] = []
        self.startup_errors: list[str] = []
        self.checks: set[str] = set()

    @property
    def active_workers(self) -> int:
        return len(self._workers)

    def __enter__(self):
        self._stack = ExitStack()
        for index in range(self.requested_workers):
            worker_runtime = self.runtime / f"enrichment-worker-{index + 1}"
            try:
                worker = MihomoEnricher(
                    self.binary, worker_runtime, self.source_config, self.unlock,
                    self.timeout, self.requested_checks,
                    self.chatgpt_unsupported_countries,
                    self.openai_api_key, self.realtime_model,
                )
                # Start workers one at a time. This lets each Mihomo bind its ports
                # before another worker asks the OS for ephemeral ports.
                self._workers.append(self._stack.enter_context(worker))
            except Exception as exc:
                self.startup_errors.append(type(exc).__name__)
        if not self._workers:
            self._stack.close()
            self._stack = None
            detail = ",".join(self.startup_errors) or "unknown"
            raise RuntimeError(f"no Mihomo enrichment worker started: {detail}")
        self.checks = set(self._workers[0].checks)
        return self

    def __exit__(self, *args):
        if self._stack:
            self._stack.__exit__(*args)
            self._stack = None
        self._workers = []

    def enrich(
        self, values: list[Measurement],
        progress: Callable[[int, int, Measurement, str], None] | None = None,
    ) -> None:
        if not self._workers:
            raise RuntimeError("Mihomo enrichment pool is not running")
        if len(self._workers) == 1 or len(values) <= 1:
            self._workers[0].enrich(values, progress)
            return

        chunks = [values[index::len(self._workers)] for index in range(len(self._workers))]
        counters = {"start": 0, "finish": 0}
        lock = Lock()

        def pooled_progress(_index: int, _count: int, item: Measurement, phase: str) -> None:
            if not progress:
                return
            with lock:
                key = "start" if phase == "start" else "finish"
                counters[key] += 1
                index = counters[key]
                progress(index, len(values), item, phase)

        with ThreadPoolExecutor(
            max_workers=len(self._workers), thread_name_prefix="clashbench-enrichment",
        ) as executor:
            futures = [
                executor.submit(worker.enrich, chunk, pooled_progress)
                for worker, chunk in zip(self._workers, chunks)
                if chunk
            ]
            for future in futures:
                future.result()

    def probe_websocket_stability(
        self, values: list[Measurement], hold_seconds: int, reconnect_attempts: int,
        progress: Callable[[int, int, Measurement, str], None] | None = None,
    ) -> None:
        if not self._workers:
            raise RuntimeError("Mihomo enrichment pool is not running")
        if len(self._workers) == 1 or len(values) <= 1:
            self._workers[0].probe_websocket_stability(
                values, hold_seconds, reconnect_attempts, progress,
            )
            return

        chunks = [values[index::len(self._workers)] for index in range(len(self._workers))]
        counters = {"start": 0, "finish": 0}
        lock = Lock()

        def pooled_progress(_index: int, _count: int, item: Measurement, phase: str) -> None:
            if not progress:
                return
            with lock:
                key = "start" if phase == "start" else "finish"
                counters[key] += 1
                progress(counters[key], len(values), item, phase)

        with ThreadPoolExecutor(
            max_workers=len(self._workers), thread_name_prefix="clashbench-websocket",
        ) as executor:
            futures = [
                executor.submit(
                    worker.probe_websocket_stability, chunk, hold_seconds,
                    reconnect_attempts, pooled_progress,
                )
                for worker, chunk in zip(self._workers, chunks)
                if chunk
            ]
            for future in futures:
                future.result()
