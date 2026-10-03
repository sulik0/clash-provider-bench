from __future__ import annotations

import html
import json
import sqlite3
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from .db import latest_run_id
from .util import percentile


def since_iso(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _f(value: float | None, suffix: str = "", digits: int = 1) -> str:
    return "—" if value is None else f"{value:.{digits}f}{suffix}"


def _json(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value) if value else fallback
    except json.JSONDecodeError:
        return fallback


def _cell(value: Any) -> str:
    return str(value if value is not None else "—").replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")


def _row_get(row: sqlite3.Row | dict[str, Any], key: str, default: Any = None) -> Any:
    return row[key] if key in row.keys() else default


def chatgpt_state(value: str | None) -> str:
    if not value:
        return "not-tested"
    return value.split(":", 1)[0]


def chatgpt_summary(rows: Iterable[sqlite3.Row | dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[Any]] = defaultdict(list)
    for row in rows:
        groups[(row["provider"], row["region"])].append(row)
    output = []
    for (provider, region), items in groups.items():
        states = Counter(chatgpt_state(row["chatgpt"]) for row in items)
        checked = len(items) - states["not-tested"]
        available = states["available"]
        unavailable = checked - available
        output.append({
            "provider": provider, "region": region, "samples": len(items), "checked": checked,
            "available": available, "unsupported": states["unsupported-country"],
            "challenge": states["challenge"], "blocked": states["blocked"],
            "other": unavailable - states["unsupported-country"] - states["challenge"] - states["blocked"],
            "availability": available / checked * 100 if checked else None,
        })
    return sorted(output, key=lambda row: (row["region"], row["provider"]))


def _chatgpt_summary_table(items: list[dict[str, Any]]) -> list[str]:
    lines = [
        "| 地区 | 服务商 | 总记录数 | 已检查 | 通过检查 | 地区不支持 | Cloudflare 要求验证 | 请求被拒绝 | 其他失败 | ChatGPT 基础检查通过率 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in items:
        lines.append(
            f"| {_cell(row['region'])} | {_cell(row['provider'])} | {row['samples']} | {row['checked']} | "
            f"{row['available']} | {row['unsupported']} | {row['challenge']} | {row['blocked']} | "
            f"{row['other']} | {_f(row['availability'], '%')} |"
        )
    if not items:
        lines.append("| — | — | 0 | 0 | 0 | 0 | 0 | 0 | 0 | — |")
    return lines


def websocket_summary(rows: Iterable[sqlite3.Row | dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[Any]] = defaultdict(list)
    for row in rows:
        if _row_get(row, "chatgpt_websocket_stability"):
            groups[(row["provider"], row["region"])].append(row)
    output = []
    for (provider, region), items in groups.items():
        states = Counter(_row_get(row, "chatgpt_websocket_stability") for row in items)
        durations = [
            float(_row_get(row, "chatgpt_websocket_seconds")) for row in items
            if _row_get(row, "chatgpt_websocket_seconds") is not None
            and _row_get(row, "chatgpt_websocket_stability") not in {
                "auth-required", "api-auth-boundary",
            }
        ]
        disconnects = sum(int(_row_get(row, "chatgpt_websocket_disconnects", 0) or 0) for row in items)
        stable = states["stable"] + states["stable-after-reconnect"]
        boundary_only = states["auth-required"] + states["api-auth-boundary"]
        evaluated = len(items) - boundary_only
        output.append({
            "provider": provider, "region": region, "candidates": len(items),
            "evaluated": evaluated, "boundary_only": boundary_only,
            "handshake_ok": sum(_row_get(row, "chatgpt_websocket") == "upgrade-101" for row in items),
            "stable": states["stable"], "recovered": states["stable-after-reconnect"],
            "unstable": states["unstable-disconnected"],
            "unresponsive": states["unstable-unresponsive"],
            "session_failed": states["session-failed"],
            "handshake_failed": states["handshake-failed"],
            "disconnects": disconnects, "duration_p50": percentile(durations, .5),
            "stable_rate": stable / evaluated * 100 if evaluated else None,
        })
    return sorted(output, key=lambda row: (row["region"], row["provider"]))


def _websocket_summary_table(items: list[dict[str, Any]]) -> list[str]:
    lines = [
        "| 地区 | 服务商 | 已检查的候选节点 | 参与稳定率统计 | 仅检查服务器响应 | 握手成功 | 首次连接稳定 | 重连后稳定 | 提前断开 | Ping 未收到回复 | 会话失败 | 握手失败 | 提前断开次数 | 最长持续连接 P50 | 最终稳定率 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in items:
        lines.append(
            f"| {_cell(row['region'])} | {_cell(row['provider'])} | {row['candidates']} | "
            f"{row['evaluated']} | {row['boundary_only']} | {row['handshake_ok']} | "
            f"{row['stable']} | {row['recovered']} | {row['unstable']} | {row['unresponsive']} | "
            f"{row['session_failed']} | {row['handshake_failed']} | {row['disconnects']} | "
            f"{_f(row['duration_p50'], ' s')} | "
            f"{_f(row['stable_rate'], '%')} |"
        )
    if not items:
        lines.append("| — | — | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | — | — |")
    return lines


def _chatgpt_node_table(rows: Iterable[sqlite3.Row | dict[str, Any]]) -> list[str]:
    lines = [
        "| 服务商 | 地区 | 节点 | 协议 | ChatGPT 基础检查 | 登录服务 | 静态资源服务器 | WebSocket 握手 | 最长持续连接 | 提前断开次数 | 重连结果 | 连接是否稳定 | 下载/上传测试 | 出口国家 | ASN |",
        "|---|---|---|---|---|---|---|---|---:|---:|---|---|---|---|---|",
    ]
    found = False
    for row in rows:
        if not row["chatgpt"]:
            continue
        found = True
        lines.append(
            f"| {_cell(row['provider'])} | {_cell(row['region'])} | {_cell(row['node_name'])} | "
            f"{_cell(_dimension_value(row, 'proxy_type'))} | `{_cell(row['chatgpt'])}` | "
            f"`{_cell(_row_get(row, 'chatgpt_auth'))}` | `{_cell(_row_get(row, 'chatgpt_static'))}` | "
            f"`{_cell(_row_get(row, 'chatgpt_websocket'))}` | "
            f"{_f(_row_get(row, 'chatgpt_websocket_seconds'), ' s')} | "
            f"{_cell(_row_get(row, 'chatgpt_websocket_disconnects'))} | "
            f"`{_cell(_row_get(row, 'chatgpt_websocket_reconnect'))}` | "
            f"`{_cell(_row_get(row, 'chatgpt_websocket_stability'))}` | "
            f"{'旧数据' if _row_get(row, 'throughput_attempted') is None else '已执行' if bool(_row_get(row, 'throughput_attempted')) else '未执行'} | "
            f"{_cell(row['exit_country'])} | {_cell(row['asn'])} |"
        )
    if not found:
        lines.append("| — | — | — | — | 未开启检查，或没有完成检查的记录 | — | — | — | — | — | — | — | — | — | — |")
    return lines


def _dimension_value(row: sqlite3.Row | dict[str, Any], key: str) -> Any:
    value = row[key] or "unknown"
    if key != "proxy_type":
        return value
    normalized = {
        "vless": "VLESS", "vmess": "VMess", "hysteria2": "Hysteria2", "hysteria": "Hysteria",
        "shadowsocks": "Shadowsocks", "ss": "Shadowsocks", "anytls": "AnyTLS",
        "trojan": "Trojan", "tuic": "TUIC",
    }
    return normalized.get(str(value).lower(), value)


def summarize_rows(
    rows: Iterable[sqlite3.Row | dict[str, Any]], dimensions: tuple[str, ...],
    timezone_name: str = "Asia/Shanghai",
) -> list[dict[str, Any]]:
    timezone = ZoneInfo(timezone_name)
    groups: dict[tuple[Any, ...], list[Any]] = defaultdict(list)
    for row in rows:
        groups[tuple(_dimension_value(row, key) for key in dimensions)].append(row)
    output: list[dict[str, Any]] = []
    for key, items in groups.items():
        available = [row for row in items if bool(row["available"])]
        attempted = [
            row for row in items
            if _row_get(row, "throughput_attempted", None) is None
            or bool(_row_get(row, "throughput_attempted"))
        ]
        successful = [
            row for row in attempted
            if (row["status"] if "status" in row.keys() else ("ok" if row["available"] else "failed")) == "ok"
            and row["download_mbps"] is not None
        ]
        failed = len(attempted) - len(successful)
        ttfb = [row["ttfb_ms"] for row in available if row["ttfb_ms"] is not None]
        jitter = [row["jitter_ms"] for row in available if row["jitter_ms"] is not None]
        loss = [row["packet_loss_pct"] for row in items if row["packet_loss_pct"] is not None]
        download = [row["download_mbps"] for row in available if row["download_mbps"] is not None]
        upload = [row["upload_mbps"] for row in available if row["upload_mbps"] is not None]
        evening = [row["download_mbps"] for row in available if row["download_mbps"] is not None
                   and datetime.fromisoformat(row["tested_at"]).astimezone(timezone).hour in (20, 21, 22, 23)]
        daytime = [row["download_mbps"] for row in available if row["download_mbps"] is not None
                   and datetime.fromisoformat(row["tested_at"]).astimezone(timezone).hour in (9, 14)]
        day_med, eve_med = percentile(daytime, .5), percentile(evening, .5)
        decline = None if day_med in (None, 0) or eve_med is None else (day_med - eve_med) / day_med * 100
        cv = None
        if len(download) >= 2 and statistics.mean(download):
            cv = statistics.pstdev(download) / statistics.mean(download) * 100
        summary = {name: value for name, value in zip(dimensions, key)}
        summary.update({
            "samples": len(items), "available_n": len(available), "throughput_n": len(attempted),
            "success_n": len(successful), "failure_n": failed,
            "availability": len(available) / len(items) * 100,
            "success_rate": len(successful) / len(attempted) * 100 if attempted else None,
            "failure_rate": failed / len(attempted) * 100 if attempted else None,
            "ttfb_n": len(ttfb), "ttfb_p50": percentile(ttfb, .5), "ttfb_p95": percentile(ttfb, .95),
            "jitter_n": len(jitter), "jitter_p50": percentile(jitter, .5),
            "loss_n": len(loss), "loss_p50": percentile(loss, .5),
            "download_n": len(download), "down_p50": percentile(download, .5), "down_p95": percentile(download, .95),
            "upload_n": len(upload), "up_p50": percentile(upload, .5),
            "evening_n": len(evening), "daytime_n": len(daytime), "evening_decline": decline,
            "speed_cv": cv, "speed_cv_n": len(download),
        })
        output.append(summary)
    output.sort(key=lambda row: tuple(str(row[name]) for name in dimensions) + (-row["availability"],))
    return output


def find_anomalies(summary: list[dict[str, Any]]) -> list[dict[str, Any]]:
    region_medians = {
        region: percentile([row["ttfb_p50"] for row in summary if row.get("region") == region], .5)
        for region in {row.get("region") for row in summary}
    }
    anomalies = []
    for item in summary:
        reasons = []
        baseline = region_medians.get(item.get("region"))
        if item["failure_rate"] is not None and item["failure_rate"] >= 30:
            reasons.append(f"失败率 {item['failure_rate']:.0f}% ({item['failure_n']}/{item['throughput_n']})")
        if item["loss_p50"] is not None and item["loss_p50"] >= 10:
            reasons.append(f"丢包 P50 {item['loss_p50']:.1f}% (n={item['loss_n']})")
        if baseline and item["ttfb_p50"] and item["ttfb_p50"] > baseline * 2:
            reasons.append("TTFB P50 高于同地区中位数 2×")
        if item["speed_cv"] is not None and item["speed_cv"] >= 50:
            reasons.append(f"下载速度 CV {item['speed_cv']:.0f}% (n={item['speed_cv_n']})")
        if reasons:
            anomalies.append({**item, "reason": "；".join(reasons)})
    return anomalies


def infrastructure_summary(rows: Iterable[sqlite3.Row | dict[str, Any]]) -> list[dict[str, Any]]:
    # Use the latest observation for each node so repeated schedules do not inflate concentration.
    latest: dict[tuple[str, str, str], Any] = {}
    for row in rows:
        key = (row["provider"], row["region"], row["node_key"])
        if key not in latest or row["tested_at"] > latest[key]["tested_at"]:
            latest[key] = row
    groups: dict[tuple[str, str], list[Any]] = defaultdict(list)
    for row in latest.values():
        groups[(row["provider"], row["region"])].append(row)
    result = []
    for (provider, region), items in groups.items():
        available = [row for row in items if row["available"]]
        with_ip = [row for row in available if row["exit_ip"]]
        ips = Counter(row["exit_ip"] for row in with_ip)
        asns = Counter(row["asn"] for row in available if row["asn"])
        orgs = Counter(row["as_org"] for row in available if row["as_org"])
        enrichment_failed = sum(1 for row in available if row["enrichment_status"] in ("failed", "partial"))
        result.append({
            "provider": provider, "region": region, "nodes": len(items), "available_nodes": len(available),
            "ip_coverage_n": len(with_ip), "ip_coverage": len(with_ip) / len(available) * 100 if available else 0,
            "unique_ips": len(ips), "unique_asns": len(asns), "unique_orgs": len(orgs),
            "top_ip_share": max(ips.values()) / len(with_ip) * 100 if ips else None,
            "top_asn_share": max(asns.values()) / sum(asns.values()) * 100 if asns else None,
            "enrichment_failed_n": enrichment_failed,
        })
    return sorted(result, key=lambda row: (row["region"], row["provider"]))


def _run_and_rows(conn: sqlite3.Connection, run_id: str) -> tuple[sqlite3.Row, list[sqlite3.Row]]:
    run = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if not run:
        raise ValueError(f"Unknown run: {run_id}")
    rows = conn.execute("SELECT * FROM measurements WHERE run_id=? ORDER BY provider,region,node_name", (run_id,)).fetchall()
    return run, rows


def _trend_rows(conn: sqlite3.Connection, anchor: sqlite3.Row, days: int) -> tuple[list[sqlite3.Row], int, int]:
    since = since_iso(days)
    if anchor["comparison_key"]:
        runs = conn.execute(
            """SELECT id FROM runs WHERE started_at>=? AND status='ok' AND comparison_key=? ORDER BY started_at""",
            (since, anchor["comparison_key"]),
        ).fetchall()
    else:
        runs = conn.execute(
            """SELECT id FROM runs WHERE started_at>=? AND status='ok' AND comparison_key IS NULL
               AND config_digest=? AND engine=? ORDER BY started_at""",
            (since, anchor["config_digest"], anchor["engine"]),
        ).fetchall()
    ids = [row["id"] for row in runs]
    total = conn.execute("SELECT COUNT(*) FROM runs WHERE started_at>=? AND status!='running'", (since,)).fetchone()[0]
    if not ids:
        return [], 0, total
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT * FROM measurements WHERE run_id IN ({placeholders}) ORDER BY tested_at", ids
    ).fetchall()
    return rows, len(ids), total - len(ids)


def build_summary(
    conn: sqlite3.Connection, days: int = 7, run_id: str | None = None,
    timezone_name: str = "Asia/Shanghai",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    selected = run_id or latest_run_id(conn)
    if not selected:
        return [], []
    run, current_rows = _run_and_rows(conn, selected)
    rows = current_rows if run_id else _trend_rows(conn, run, days)[0]
    summary = summarize_rows(rows, ("provider", "region"), timezone_name)
    return summary, find_anomalies(summary)


def _summary_table(summary: list[dict[str, Any]], dimensions: tuple[str, ...]) -> list[str]:
    labels = {"provider": "供应商", "region": "地区", "proxy_type": "协议"}
    headers = [labels[name] for name in dimensions] + [
        "总记录数", "节点可用记录数", "下载/上传尝试数", "下载/上传成功数/失败数", "节点可用率", "下载/上传成功率", "下载/上传失败率", "TTFB P50/P95 (n)", "抖动 P50 (n)", "丢包 P50 (n)",
        "下载 P50/P95 (n)", "上传 P50 (n)", "下载速度 CV (n)", "晚高峰速度下降 (晚间/白天 n)",
    ]
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(dimensions) + ["---:"] * 14) + "|"]
    for row in summary:
        prefix = [str(row[name]) for name in dimensions]
        values = [
            str(row["samples"]), str(row["available_n"]), str(row["throughput_n"]), f"{row['success_n']}/{row['failure_n']}",
            _f(row["availability"], "%"), _f(row["success_rate"], "%"), _f(row["failure_rate"], "%"),
            f"{_f(row['ttfb_p50'],' ms')}/{_f(row['ttfb_p95'],' ms')} (n={row['ttfb_n']})",
            f"{_f(row['jitter_p50'],' ms')} (n={row['jitter_n']})",
            f"{_f(row['loss_p50'],'%')} (n={row['loss_n']})",
            f"{_f(row['down_p50'],' Mbps')}/{_f(row['down_p95'],' Mbps')} (n={row['download_n']})",
            f"{_f(row['up_p50'],' Mbps')} (n={row['upload_n']})",
            f"{_f(row['speed_cv'],'%')} (n={row['speed_cv_n']})",
            f"{_f(row['evening_decline'],'%')} ({row['evening_n']}/{row['daytime_n']})",
        ]
        lines.append("| " + " | ".join(prefix + values) + " |")
    if not summary:
        lines.append("| " + " | ".join(["—"] * len(headers)) + " |")
    return lines


def _infra_table(items: list[dict[str, Any]]) -> list[str]:
    lines = [
        "| 地区 | 供应商 | 节点数 | 查到出口 IP 的比例 | 不同出口 IP 数 | 不同 ASN 数 | 不同组织数 | 最常用出口 IP 占比 | 最常用 ASN 占比 | 额外检查出错记录数 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in items:
        lines.append(
            f"| {row['region']} | {row['provider']} | {row['nodes']} | {_f(row['ip_coverage'],'%')} "
            f"({row['ip_coverage_n']}/{row['available_nodes']}) | {row['unique_ips']} | {row['unique_asns']} | "
            f"{row['unique_orgs']} | {_f(row['top_ip_share'],'%')} | {_f(row['top_asn_share'],'%')} | "
            f"{row['enrichment_failed_n']} |"
        )
    if not items:
        lines.append("| — | — | 0 | — | 0 | 0 | 0 | — | — | 0 |")
    return lines


def _conditions(run: sqlite3.Row) -> list[str]:
    params = _json(run["parameters_json"], {})
    env = _json(run["environment_json"], {})
    proxy = env.get("system_proxy", {})
    enabled = [name.removesuffix("_enabled") for name, value in proxy.items() if name.endswith("_enabled") and value]
    checks = params.get("enrichment_checks", [])
    chatgpt_probe = params.get("chatgpt_probe") or {}
    return [
        f"- 对比条件 ID：`{run['comparison_key'] or 'legacy:' + run['config_digest']}`",
        f"- 测速工具：{run['engine_version'] or run['engine']}；运行方式：{params.get('test_strategy', 'legacy-single-stage')}；测速模式：{params.get('speed_mode', 'legacy-unknown')}；测速地址：{params.get('server_url', 'legacy-unknown')}",
        f"- 每个节点的测试数据量：普通模式下载 {params.get('download_size_mb', '—')} MB、上传 {params.get('upload_size_mb', '—')} MB；两阶段模式中，通过 ChatGPT 检查后下载 {params.get('two_stage_download_size_mb', '—')} MB、上传 {params.get('two_stage_upload_size_mb', '—')} MB。实际测下载还是上传，由测速模式决定。下载并发数为 {params.get('concurrent', '—')}，超时时间为 {params.get('timeout_seconds', '—')} 秒。",
        f"- 两阶段记录的合并方式（版本）：{params.get('two_stage_merge_strategy') or '本次不适用，或旧记录未保存'}。当前实现保留第一阶段的基础网络结果，再补上第二阶段的下载和上传结果。",
        f"- 检查的服务：{','.join(checks) if checks else '未开启服务检查'}；同时检查节点的 worker 数：{params.get('enrichment_workers', 1)}；配置中标记为不支持 ChatGPT 的地区：{','.join(params.get('chatgpt_unsupported_countries', [])) or '无'}。",
        f"- ChatGPT 请求使用：{chatgpt_probe.get('client', 'legacy-unknown')} {chatgpt_probe.get('version', '')}；模拟浏览器的请求特征：{chatgpt_probe.get('impersonate', 'legacy-unknown')}。",
        f"- WebSocket 检查使用 OpenAI Realtime API `/v1/realtime`。模型为 {chatgpt_probe.get('realtime_model', 'legacy-unknown')}，验证方法为 {chatgpt_probe.get('websocket_validation', 'legacy-hold-only')}，认证方式为 {chatgpt_probe.get('websocket_auth_mode', 'legacy-unknown')}。要求连接保持 {chatgpt_probe.get('websocket_hold_seconds', '—')} 秒，检查失败后最多重连 {chatgpt_probe.get('websocket_reconnect_attempts', '—')} 次。",
        f"- 评测时区：{params.get('evaluation_timezone', 'Asia/Shanghai')}（数据库原始时间仍保存为 UTC）",
        f"- 默认网络接口：{env.get('default_interface') or '未知'}；启用的系统代理：{','.join(enabled) if enabled else '未检测到启用'}；正在使用的 TUN/VPN 接口：{','.join(env.get('tunnel_interfaces_active', [])) or '未检测到'}。",
        f"- 服务商测试顺序：{' → '.join(_json(run['provider_order_json'], [])) or '旧记录未保存'}。",
    ]


def current_report(conn: sqlite3.Connection, run_id: str, timezone_name: str = "Asia/Shanghai") -> str:
    run, rows = _run_and_rows(conn, run_id)
    timezone = ZoneInfo(timezone_name)
    started_at = datetime.fromisoformat(run["started_at"]).astimezone(timezone).isoformat(timespec="seconds")
    provider_runs = conn.execute("SELECT * FROM provider_runs WHERE run_id=? ORDER BY ordinal", (run_id,)).fetchall()
    regional = summarize_rows(rows, ("provider", "region"), timezone_name)
    protocol = summarize_rows(rows, ("provider", "region", "proxy_type"), timezone_name)
    anomalies = find_anomalies(regional)
    out = ["# Clash/Mihomo 单次评测报告", "", f"运行：`{run_id}`；开始：{started_at}（{timezone_name}）；状态：**{run['status']}**。", "",
           "## 本次测试条件", "", *_conditions(run), "", "## 各家服务商是否完成测试", "",
           "| 顺序 | 服务商 | 状态 | 测量记录数 | 错误 |", "|---:|---|---|---:|---|"]
    if provider_runs:
        for item in provider_runs:
            out.append(f"| {item['ordinal'] + 1} | {item['provider']} | {item['status']} | {item['measurement_count']} | {item['error'] or '—'} |")
    else:
        out.append("| — | 旧数据未记录 | — | — | — |")
    out += ["", "## 按服务商和地区比较本次结果", "", *_summary_table(regional, ("provider", "region")),
            "", "## 按服务商、地区和协议比较本次结果", "", *_summary_table(protocol, ("provider", "region", "proxy_type")),
            "", "## 哪些节点通过了 ChatGPT 基础检查", "", *_chatgpt_summary_table(chatgpt_summary(rows)),
            "", "> `available` 表示 ChatGPT 首页、后端、登录服务和静态资源服务器通过基础检查。第二阶段连接的是 Realtime API。看到 `upgrade-101` 时，还要查看会话是否建立、连接是否保持；实际聊天和账号权限需要在 ChatGPT 中验证。", "",
            "## 本次 Realtime API WebSocket 连接结果", "", *_websocket_summary_table(websocket_summary(rows)), "",
            *_chatgpt_node_table(rows),
            "", "## 本次查到了多少不同的出口和网络", "", *_infra_table(infrastructure_summary(rows)), "", "## 需要留意的测速结果", ""]
    out += [f"- {item['region']} / {item['provider']}：{item['reason']}" for item in anomalies] or ["未发现达到默认阈值的测速异常。"]
    enrichment_failures = sum(1 for row in rows if row["enrichment_status"] in ("failed", "partial"))
    out += ["", f"> 有 {enrichment_failures} 条记录在查询出口或检查服务、WebSocket 时出错。已取得的测速结果仍然保留，这些额外检查错误不计入下载/上传失败率。", "", *_methodology()]
    return "\n".join(out) + "\n"


def trend_report(
    conn: sqlite3.Connection, run_id: str, days: int,
    timezone_name: str = "Asia/Shanghai",
) -> str:
    anchor, _ = _run_and_rows(conn, run_id)
    timezone = ZoneInfo(timezone_name)
    rows, run_count, excluded = _trend_rows(conn, anchor, days)
    regional = summarize_rows(rows, ("provider", "region"), timezone_name)
    protocol = summarize_rows(rows, ("provider", "region", "proxy_type"), timezone_name)
    anomalies = find_anomalies(regional)
    profiles = conn.execute(
        """SELECT COALESCE(comparison_key,'legacy:'||config_digest||':'||engine) AS profile,
                  COUNT(*) AS runs, SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END) AS ok_runs
           FROM runs WHERE started_at>=? AND status!='running' GROUP BY profile ORDER BY runs DESC""",
        (since_iso(days),),
    ).fetchall()
    out = [f"# Clash/Mihomo 最近 {days} 天趋势报告", "", f"生成时间：{datetime.now(timezone).isoformat(timespec='seconds')}（{timezone_name}）。", "",
           f"> 本报告以运行 `{run_id}` 的测试条件选择历史数据，共统计 {run_count} 次条件相同、状态为 `ok` 的运行。另有 {excluded} 次运行没有参加统计：它们的条件不同、属于其他旧数据分组，或者状态为 `partial`（部分完成）或 `failed`（失败）。", "",
           "## 这些运行使用的测试条件", "", *_conditions(anchor), "", "## 按服务商和地区比较趋势", "",
           *_summary_table(regional, ("provider", "region")), "", "## 按服务商、地区和协议比较趋势", "",
           *_summary_table(protocol, ("provider", "region", "proxy_type")), "", "## ChatGPT 基础检查结果随时间的变化", "",
           *_chatgpt_summary_table(chatgpt_summary(rows)), "", "## Realtime API WebSocket 连接结果随时间的变化", "",
           *_websocket_summary_table(websocket_summary(rows)), "", "## 查到了多少不同的出口和网络（每个节点取最近一条记录）", "",
           *_infra_table(infrastructure_summary(rows)), "", "## 需要留意的测速结果", ""]
    out += [f"- {item['region']} / {item['provider']}：{item['reason']}" for item in anomalies] or ["未发现达到默认阈值的测速异常。"]
    out += ["", "## 历史记录使用过哪些测试条件（各组分别统计）", "", "| 条件 ID | 运行总次数 | 所有服务商完成测试的次数 |", "|---|---:|---:|"]
    for profile in profiles:
        out.append(f"| `{profile['profile']}` | {profile['runs']} | {profile['ok_runs']} |")
    out += ["", *_methodology()]
    return "\n".join(out) + "\n"


def _methodology() -> list[str]:
    return [
        "## 报告里的数字怎么算", "",
        "- 每测一个节点，记为一条记录。同一节点测多次，就有多条记录。延迟测试成功且丢包低于 100% 时，记为节点可用。两阶段模式使用第一阶段的基础网络结果。",
        "- 下载/上传成功率和失败率只统计实际尝试测速的记录。其中 `status=ok` 且有下载速度的记录计为成功，其余已尝试记录计为失败。没有通过 ChatGPT 基础检查的节点会跳过下载和上传，不计入这个分母；旧记录没有保存是否尝试的标记，按已经尝试处理。",
        "- P50 是中位数，P95 表示 95% 的样本不超过这个值。每个指标后的 `n` 是计算时用了多少条记录。TTFB（等待首个响应字节的时间）、抖动、下载和上传只使用节点可用且对应数值存在的记录；丢包使用所有带有丢包数值的记录。",
        "- 下载速度 CV（变异系数）= 总体标准差 / 平均值 × 100%。至少有 2 条下载记录才计算。数值越低说明这组速度越接近；分组可能包含不同节点，因此不能单凭它判断某一个节点是否稳定。",
        "- 晚高峰为报告时区中的 20:00–23:59，白天的比较数据取 09:00–09:59 和 14:00–14:59，默认使用北京时间 Asia/Shanghai。程序比较两个时段的下载中位数，括号显示晚间和白天各用了多少条记录。下降比例为负时，表示晚间反而更快。",
        "- 趋势只统计对比条件 ID 相同、运行状态为 `ok` 的记录。这里的 `ok` 表示各家服务商都完成测试，其中仍可能有失败节点。测速地址、模式、文件大小、并发、超时、地区、工具版本、验证方法或机器架构改变后，会生成新的条件 ID。",
        "- 查询出口或检查服务、WebSocket 时出错，不会改变已经取得的测速结果。共用出口和 ASN 的比例按每个节点最近一条记录计算，避免重复测试同一节点时重复计数。",
        "- ChatGPT 基础检查通过率 = 结果为 `available` 的记录数 / 有 ChatGPT 检查结果的记录数。程序检查首页、后端、登录服务和静态资源服务器，不读取你的 ChatGPT 账号或浏览器 Cookie。WebSocket 结果另行统计。",
        "- 两阶段模式会连接 OpenAI Realtime API `/v1/realtime`，检查 WebSocket。没有配置 `OPENAI_API_KEY` 时，101 或 401/403 只记为 `api-auth-boundary`，说明服务器已经响应；程序没有测试持续连接，因此不计入稳定率，也不记录持续时长和提前断开次数。",
        "- 配置 API key 后，程序要求握手成功、收到 `session.created`、发送 Ping 后收到对应的 Pong，而且连接保持到设定时间。第一次就通过检查记为 `stable`，重连后通过记为 `stable-after-reconnect`；最终稳定率包含这两种结果，报告也会分别显示数量。",
        "- 连接提前关闭记为 `unstable-disconnected`，Ping 迟迟没有收到回复记为 `unstable-unresponsive`。服务器返回错误、会话没有建立或协议数据无法解析时，记为 `session-failed`。重连会再次连接同一地址，持续时长取各次尝试中最长的一次，不把短连接相加。",
        "- Realtime API 的结果说明节点与该 API 之间的连接情况。ChatGPT 网页使用的接口、账号权限和实际聊天请求仍需要在 ChatGPT 中验证。",
    ]


def html_report(markdown_text: str, title: str = "Clash/Mihomo 供应商评测报告") -> str:
    body: list[str] = []
    in_table = False
    for line in markdown_text.splitlines():
        is_table = line.startswith("|")
        if in_table and not is_table:
            body.append("</tbody></table></div>")
            in_table = False
        if line.startswith("# "):
            body.append(f"<h1>{html.escape(line[2:])}</h1>")
        elif line.startswith("## "):
            body.append(f"<h2>{html.escape(line[3:])}</h2>")
        elif line.startswith("|---"):
            continue
        elif is_table:
            cells = [html.escape(value.strip()) for value in line.strip("|").split("|")]
            if not in_table:
                body.append("<div class='scroll'><table><thead><tr>" + "".join(f"<th>{value}</th>" for value in cells) + "</tr></thead><tbody>")
                in_table = True
            else:
                body.append("<tr>" + "".join(f"<td>{value}</td>" for value in cells) + "</tr>")
        elif line.startswith("- "):
            body.append(f"<p class='bullet'>• {html.escape(line[2:])}</p>")
        elif line.startswith("> "):
            body.append(f"<aside>{html.escape(line[2:])}</aside>")
        elif line:
            body.append(f"<p>{html.escape(line)}</p>")
    if in_table:
        body.append("</tbody></table></div>")
    css = """body{font:15px -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;margin:36px;color:#18212b;background:#f7f9fb}main{max-width:1600px;margin:auto;background:white;padding:32px;border-radius:16px;box-shadow:0 6px 24px #0001}h1{margin-top:0}h2{margin-top:32px}.scroll{overflow:auto}table{border-collapse:collapse;width:100%;white-space:nowrap}th,td{padding:9px 10px;border-bottom:1px solid #dde3ea;text-align:right}th:first-child,td:first-child,th:nth-child(2),td:nth-child(2){text-align:left}th{background:#eef3f8;position:sticky;top:0}aside{padding:12px;background:#eef6ff;border-left:4px solid #3182ce}.bullet{margin:6px 0}@media(prefers-color-scheme:dark){body{background:#111820;color:#e8edf2}main{background:#18212b}th{background:#243242}th,td{border-color:#344454}}"""
    return f"<!doctype html><html lang='zh-CN'><meta charset='utf-8'><meta name='viewport' content='width=device-width'><title>{html.escape(title)}</title><style>{css}</style><main>{''.join(body)}</main></html>"


def write_reports(
    conn: sqlite3.Connection, out_dir: Path, days: int = 7, run_id: str | None = None,
    timezone_name: str = "Asia/Shanghai",
) -> dict[str, Path]:
    selected = run_id or latest_run_id(conn)
    if not selected:
        raise ValueError("No completed runs are available for reporting")
    run = conn.execute("SELECT started_at FROM runs WHERE id=?", (selected,)).fetchone()
    timezone = ZoneInfo(timezone_name)
    stamp = datetime.fromisoformat(run["started_at"]).astimezone(timezone).strftime("%Y%m%d-%H%M%S")
    current_md = current_report(conn, selected, timezone_name)
    trend_md = trend_report(conn, selected, days, timezone_name)
    run_dir = out_dir / "runs"
    run_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "run_md": run_dir / f"{stamp}_{selected[:8]}.md",
        "run_html": run_dir / f"{stamp}_{selected[:8]}.html",
        "latest_md": out_dir / "latest.md", "latest_html": out_dir / "latest.html",
        "trend_md": out_dir / f"trend-{days}d.md", "trend_html": out_dir / f"trend-{days}d.html",
    }
    paths["run_md"].write_text(current_md, encoding="utf-8")
    paths["run_html"].write_text(html_report(current_md, "Clash/Mihomo 单次评测报告"), encoding="utf-8")
    paths["latest_md"].write_text(current_md, encoding="utf-8")
    paths["latest_html"].write_text(html_report(current_md, "Clash/Mihomo 最新单次报告"), encoding="utf-8")
    paths["trend_md"].write_text(trend_md, encoding="utf-8")
    paths["trend_html"].write_text(html_report(trend_md, f"Clash/Mihomo {days} 天趋势"), encoding="utf-8")
    return paths
