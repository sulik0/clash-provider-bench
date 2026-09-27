from __future__ import annotations

import html
import json
import sqlite3
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

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
        "| 地区 | Provider | 总样本 | 已检测 | 可用 | 地区不支持 | Cloudflare 挑战 | 阻断 | 其他失败 | ChatGPT 可用率 |",
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


def _chatgpt_node_table(rows: Iterable[sqlite3.Row | dict[str, Any]]) -> list[str]:
    lines = [
        "| Provider | 地区 | 节点 | 协议 | ChatGPT 结果 | 出口国家 | ASN |",
        "|---|---|---|---|---|---|---|",
    ]
    found = False
    for row in rows:
        if not row["chatgpt"]:
            continue
        found = True
        lines.append(
            f"| {_cell(row['provider'])} | {_cell(row['region'])} | {_cell(row['node_name'])} | "
            f"{_cell(_dimension_value(row, 'proxy_type'))} | `{_cell(row['chatgpt'])}` | "
            f"{_cell(row['exit_country'])} | {_cell(row['asn'])} |"
        )
    if not found:
        lines.append("| — | — | — | — | 未启用或没有完成检测 | — | — |")
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


def summarize_rows(rows: Iterable[sqlite3.Row | dict[str, Any]], dimensions: tuple[str, ...]) -> list[dict[str, Any]]:
    groups: dict[tuple[Any, ...], list[Any]] = defaultdict(list)
    for row in rows:
        groups[tuple(_dimension_value(row, key) for key in dimensions)].append(row)
    output: list[dict[str, Any]] = []
    for key, items in groups.items():
        available = [row for row in items if bool(row["available"])]
        successful = [row for row in items if (row["status"] if "status" in row.keys() else ("ok" if row["available"] else "failed")) == "ok"]
        failed = len(items) - len(successful)
        ttfb = [row["ttfb_ms"] for row in available if row["ttfb_ms"] is not None]
        jitter = [row["jitter_ms"] for row in available if row["jitter_ms"] is not None]
        loss = [row["packet_loss_pct"] for row in items if row["packet_loss_pct"] is not None]
        download = [row["download_mbps"] for row in available if row["download_mbps"] is not None]
        upload = [row["upload_mbps"] for row in available if row["upload_mbps"] is not None]
        evening = [row["download_mbps"] for row in available if row["download_mbps"] is not None
                   and datetime.fromisoformat(row["tested_at"]).astimezone().hour in (20, 21, 22, 23)]
        daytime = [row["download_mbps"] for row in available if row["download_mbps"] is not None
                   and datetime.fromisoformat(row["tested_at"]).astimezone().hour in (9, 14)]
        day_med, eve_med = percentile(daytime, .5), percentile(evening, .5)
        decline = None if day_med in (None, 0) or eve_med is None else (day_med - eve_med) / day_med * 100
        cv = None
        if len(download) >= 2 and statistics.mean(download):
            cv = statistics.pstdev(download) / statistics.mean(download) * 100
        summary = {name: value for name, value in zip(dimensions, key)}
        summary.update({
            "samples": len(items), "available_n": len(available), "success_n": len(successful), "failure_n": failed,
            "availability": len(available) / len(items) * 100,
            "success_rate": len(successful) / len(items) * 100, "failure_rate": failed / len(items) * 100,
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
        if item["failure_rate"] >= 30:
            reasons.append(f"失败率 {item['failure_rate']:.0f}% ({item['failure_n']}/{item['samples']})")
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
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    selected = run_id or latest_run_id(conn)
    if not selected:
        return [], []
    run, current_rows = _run_and_rows(conn, selected)
    rows = current_rows if run_id else _trend_rows(conn, run, days)[0]
    summary = summarize_rows(rows, ("provider", "region"))
    return summary, find_anomalies(summary)


def _summary_table(summary: list[dict[str, Any]], dimensions: tuple[str, ...]) -> list[str]:
    labels = {"provider": "供应商", "region": "地区", "proxy_type": "协议"}
    headers = [labels[name] for name in dimensions] + [
        "总样本", "可用节点", "测速成功/失败", "可用率", "测速成功率", "测速失败率", "TTFB P50/P95 (n)", "抖动 P50 (n)", "丢包 P50 (n)",
        "下载 P50/P95 (n)", "上传 P50 (n)", "下载 CV (n)", "晚高峰衰减 (晚/日 n)",
    ]
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(dimensions) + ["---:"] * 13) + "|"]
    for row in summary:
        prefix = [str(row[name]) for name in dimensions]
        values = [
            str(row["samples"]), str(row["available_n"]), f"{row['success_n']}/{row['failure_n']}",
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
        "| 地区 | 供应商 | 节点 | 出口覆盖 | 独立出口 | 独立 ASN | 独立组织 | 最大出口集中度 | 最大 ASN 集中度 | enrichment 异常 |",
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
    return [
        f"- 对比条件 ID：`{run['comparison_key'] or 'legacy:' + run['config_digest']}`",
        f"- 引擎：{run['engine_version'] or run['engine']}；模式：{params.get('speed_mode', 'legacy-unknown')}；端点：{params.get('server_url', 'legacy-unknown')}",
        f"- 文件大小：下载 {params.get('download_size_mb', '—')} MB / 上传 {params.get('upload_size_mb', '—')} MB；并发 {params.get('concurrent', '—')}；超时 {params.get('timeout_seconds', '—')} 秒",
        f"- 附加检测：{','.join(checks) if checks else '未启用专项可用性检测'}；ChatGPT 配置排除地区：{','.join(params.get('chatgpt_unsupported_countries', [])) or '无'}",
        f"- 默认接口：{env.get('default_interface') or '未知'}；系统代理：{','.join(enabled) if enabled else '未检测到启用'}；活动 TUN/VPN 接口：{','.join(env.get('tunnel_interfaces_active', [])) or '未检测到'}",
        f"- Provider 顺序：{' → '.join(_json(run['provider_order_json'], [])) or '旧数据未记录'}",
    ]


def current_report(conn: sqlite3.Connection, run_id: str) -> str:
    run, rows = _run_and_rows(conn, run_id)
    provider_runs = conn.execute("SELECT * FROM provider_runs WHERE run_id=? ORDER BY ordinal", (run_id,)).fetchall()
    regional = summarize_rows(rows, ("provider", "region"))
    protocol = summarize_rows(rows, ("provider", "region", "proxy_type"))
    anomalies = find_anomalies(regional)
    out = ["# Clash/Mihomo 单次评测报告", "", f"运行：`{run_id}`；开始：{run['started_at']}；状态：**{run['status']}**。", "",
           "## 本次测试条件", "", *_conditions(run), "", "## Provider 执行状态", "",
           "| 顺序 | Provider | 状态 | 测量数 | 错误 |", "|---:|---|---|---:|---|"]
    if provider_runs:
        for item in provider_runs:
            out.append(f"| {item['ordinal'] + 1} | {item['provider']} | {item['status']} | {item['measurement_count']} | {item['error'] or '—'} |")
    else:
        out.append("| — | 旧数据未记录 | — | — | — |")
    out += ["", "## 本次：Provider × 地区", "", *_summary_table(regional, ("provider", "region")),
            "", "## 本次：Provider × 地区 × 协议", "", *_summary_table(protocol, ("provider", "region", "proxy_type")),
            "", "## 本次 ChatGPT 可用性", "", *_chatgpt_summary_table(chatgpt_summary(rows)),
            "", "> `available` 表示该节点可正常取得 ChatGPT 未登录首页；`challenge`、`blocked` 和 `unsupported-country` 均不计为可用。该检测不使用账号，因此不能证明登录后对话一定成功。", "",
            *_chatgpt_node_table(rows),
            "", "## 本次基础设施多样性", "", *_infra_table(infrastructure_summary(rows)), "", "## 本次异常", ""]
    out += [f"- {item['region']} / {item['provider']}：{item['reason']}" for item in anomalies] or ["未发现达到默认阈值的测速异常。"]
    enrichment_failures = sum(1 for row in rows if row["enrichment_status"] in ("failed", "partial"))
    out += ["", f"> 附加信息异常 {enrichment_failures} 条；它们不会计入节点测速失败，也不会改变成功率。", "", *_methodology()]
    return "\n".join(out) + "\n"


def trend_report(conn: sqlite3.Connection, run_id: str, days: int) -> str:
    anchor, _ = _run_and_rows(conn, run_id)
    rows, run_count, excluded = _trend_rows(conn, anchor, days)
    regional = summarize_rows(rows, ("provider", "region"))
    protocol = summarize_rows(rows, ("provider", "region", "proxy_type"))
    anomalies = find_anomalies(regional)
    profiles = conn.execute(
        """SELECT COALESCE(comparison_key,'legacy:'||config_digest||':'||engine) AS profile,
                  COUNT(*) AS runs, SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END) AS ok_runs
           FROM runs WHERE started_at>=? AND status!='running' GROUP BY profile ORDER BY runs DESC""",
        (since_iso(days),),
    ).fetchall()
    out = [f"# Clash/Mihomo 最近 {days} 天趋势报告", "", f"生成时间：{datetime.now().astimezone().isoformat(timespec='seconds')}。", "",
           f"> 本报告只统计与锚点运行 `{run_id}` 对比条件完全一致且状态为 ok 的运行：{run_count} 次；另有 {excluded} 次因条件不同、旧格式、partial 或 failed 未混入统计。", "",
           "## 对比条件", "", *_conditions(anchor), "", "## Provider × 地区趋势", "",
           *_summary_table(regional, ("provider", "region")), "", "## Provider × 地区 × 协议趋势", "",
           *_summary_table(protocol, ("provider", "region", "proxy_type")), "", "## ChatGPT 可用性趋势", "",
           *_chatgpt_summary_table(chatgpt_summary(rows)), "", "## 基础设施多样性（每节点取窗口内最新观测）", "",
           *_infra_table(infrastructure_summary(rows)), "", "## 异常", ""]
    out += [f"- {item['region']} / {item['provider']}：{item['reason']}" for item in anomalies] or ["未发现达到默认阈值的测速异常。"]
    out += ["", "## 历史条件清单（各组不互相混合）", "", "| 条件 ID | 全部运行 | 完整运行 |", "|---|---:|---:|"]
    for profile in profiles:
        out.append(f"| `{profile['profile']}` | {profile['runs']} | {profile['ok_runs']} |")
    out += ["", *_methodology()]
    return "\n".join(out) + "\n"


def _methodology() -> list[str]:
    return [
        "## 统计口径", "",
        "- 总样本是节点测量记录数。可用表示延迟探测成功且丢包低于 100%；测速成功还要求当前模式所需的吞吐阶段没有错误。测速成功率与失败率的分母始终是总样本，因此下载失败但延迟可用的节点会计入“可用”，同时计入“测速失败”。",
        "- 每个 P50/P95、CV 后的 `n` 是该指标实际使用的非空有效样本数。TTFB、抖动、下载、上传只使用测速成功且对应数值存在的记录；丢包使用所有具有丢包数值的记录。",
        "- 下载 CV = 下载速度总体标准差 / 均值，仅在至少 2 个下载有效样本时计算。CV 越低表示窗口内波动越小。",
        "- 晚高峰为本机时区 20:00–23:59，日间基准为 09:00 与 14:00；括号显示晚高峰/日间有效下载样本数。负衰减表示晚高峰反而更快。",
        "- 趋势只纳入状态为 `ok` 且对比条件 ID 相同的运行。测速端点、模式、文件大小、并发、超时、地区、引擎版本或架构变化都会生成新的条件 ID。",
        "- enrichment 失败只影响出口 IP/ASN 覆盖率，不改变节点测速状态、成功率或吞吐统计。基础设施集中度以不同节点的最新出口观测计算，避免定时重复测试放大某个出口。",
        "- ChatGPT 可用率的分母是实际完成 ChatGPT 检测的样本，只把 `available` 计为可用；地区不支持、Cloudflare challenge、明确阻断、限流和网络错误均不计为可用。它是未登录网络可达性检查，不使用或验证你的 ChatGPT 账号。",
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


def write_reports(conn: sqlite3.Connection, out_dir: Path, days: int = 7, run_id: str | None = None) -> dict[str, Path]:
    selected = run_id or latest_run_id(conn)
    if not selected:
        raise ValueError("No completed runs are available for reporting")
    run = conn.execute("SELECT started_at FROM runs WHERE id=?", (selected,)).fetchone()
    stamp = datetime.fromisoformat(run["started_at"]).astimezone().strftime("%Y%m%d-%H%M%S")
    current_md = current_report(conn, selected)
    trend_md = trend_report(conn, selected, days)
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
