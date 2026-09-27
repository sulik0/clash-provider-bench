#!/usr/bin/env python3
"""Download verified official macOS releases of clash-speedtest and optional Mihomo."""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import platform
import shutil
import ssl
import tarfile
import tempfile
import urllib.request
from pathlib import Path

import certifi


ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / "tools" / "bin"
UA = {"User-Agent": "clash-provider-bench-installer/0.1"}
TLS = ssl.create_default_context(cafile=certifi.where())


def json_url(url: str):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30, context=TLS) as response:
        return json.load(response)


def fetch(url: str, path: Path) -> None:
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=120, context=TLS) as source, path.open("wb") as out:
        shutil.copyfileobj(source, out)


def verify(path: Path, digest: str | None) -> None:
    if not digest or not digest.startswith("sha256:"):
        raise RuntimeError(f"GitHub did not provide a SHA-256 digest for {path.name}; refusing unverified install")
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    expected = digest.split(":", 1)[1]
    if actual != expected:
        raise RuntimeError(f"SHA-256 mismatch for {path.name}")


def install_speedtest(arch: str) -> None:
    release = json_url("https://api.github.com/repos/faceair/clash-speedtest/releases/latest")
    wanted = f"clash-speedtest_Darwin_{'arm64' if arch == 'arm64' else 'x86_64'}.tar.gz"
    asset = next(a for a in release["assets"] if a["name"] == wanted)
    with tempfile.TemporaryDirectory() as temp:
        archive = Path(temp) / wanted
        fetch(asset["browser_download_url"], archive); verify(archive, asset.get("digest"))
        with tarfile.open(archive, "r:gz") as bundle:
            member = next(m for m in bundle.getmembers() if Path(m.name).name == "clash-speedtest")
            source = bundle.extractfile(member)
            assert source
            target = DEST / "clash-speedtest"
            with target.open("wb") as out: shutil.copyfileobj(source, out)
            target.chmod(0o755)
    print(f"installed clash-speedtest {release['tag_name']} -> {target}")


def install_mihomo(arch: str) -> None:
    release = json_url("https://api.github.com/repos/MetaCubeX/mihomo/releases/latest")
    suffix = f"darwin-{'arm64' if arch == 'arm64' else 'amd64'}-{release['tag_name']}.gz"
    asset = next(a for a in release["assets"] if a["name"].endswith(suffix) and "go1" not in a["name"] and "-v1-" not in a["name"] and "-v2-" not in a["name"] and "-v3-" not in a["name"])
    with tempfile.TemporaryDirectory() as temp:
        archive = Path(temp) / asset["name"]
        fetch(asset["browser_download_url"], archive); verify(archive, asset.get("digest"))
        target = DEST / "mihomo"
        with gzip.open(archive, "rb") as source, target.open("wb") as out: shutil.copyfileobj(source, out)
        target.chmod(0o755)
    print(f"installed mihomo {release['tag_name']} -> {target}")


def main() -> None:
    if platform.system() != "Darwin": raise SystemExit("This installer currently targets macOS only")
    arch = platform.machine().lower()
    if arch not in ("arm64", "x86_64"): raise SystemExit(f"Unsupported architecture: {arch}")
    DEST.mkdir(parents=True, exist_ok=True)
    install_speedtest(arch); install_mihomo(arch)


if __name__ == "__main__": main()
