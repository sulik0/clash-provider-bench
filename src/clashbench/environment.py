from __future__ import annotations

import hashlib
import platform
import re
import subprocess
from typing import Any, Callable


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:12]


def _run(command: list[str]) -> str:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=5, check=False)
        return result.stdout if result.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def capture_network_environment(runner: Callable[[list[str]], str] = _run) -> dict[str, Any]:
    """Capture comparison context without recording IPs, SSIDs, or proxy credentials."""
    proxy_text = runner(["scutil", "--proxy"])
    route_text = runner(["route", "-n", "get", "default"])
    interfaces_text = runner(["ifconfig", "-l"])
    nwi_text = runner(["scutil", "--nwi"])

    proxy: dict[str, Any] = {}
    for kind in ("HTTP", "HTTPS", "SOCKS", "ProxyAutoConfig"):
        enabled = re.search(rf"(?m)^\s*{kind}Enable\s*:\s*(\d+)", proxy_text)
        proxy[f"{kind.lower()}_enabled"] = bool(enabled and enabled.group(1) == "1")
    for kind in ("HTTP", "HTTPS", "SOCKS"):
        server = re.search(rf"(?m)^\s*{kind}Proxy\s*:\s*(\S+)", proxy_text)
        if server:
            proxy[f"{kind.lower()}_server_hash"] = _hash(server.group(1))
    pac = re.search(r"(?m)^\s*ProxyAutoConfigURLString\s*:\s*(\S+)", proxy_text)
    if pac:
        proxy["proxyautoconfig_server_hash"] = _hash(pac.group(1))

    default_match = re.search(r"(?m)^\s*interface:\s*(\S+)", route_text)
    interfaces = interfaces_text.split()
    tunnel_interfaces = sorted(
        name for name in interfaces if re.match(r"^(utun|tun|tap|ppp|ipsec|wg)", name, re.IGNORECASE)
    )
    active_tunnels = sorted(name for name in tunnel_interfaces if re.search(rf"\b{re.escape(name)}\b", nwi_text))
    return {
        "os": platform.system(),
        "os_version": platform.mac_ver()[0] or platform.release(),
        "machine": platform.machine(),
        "default_interface": default_match.group(1) if default_match else None,
        "system_proxy": proxy,
        "tunnel_interfaces_present": tunnel_interfaces,
        "tunnel_interfaces_active": active_tunnels,
        "vpn_or_tun_possible": bool(active_tunnels),
    }
