from __future__ import annotations

import html
import sqlite3
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .util import percentile


def since_iso(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _f(value: float | None, suffix: str = "", digits: int = 1) -> str:
    return "—" if value is None else f"{value:.{digits}f}{suffix}"


def build_summary(conn: sqlite3.Connection, days: int = 7) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows = conn.execute("SELECT * FROM measurements WHERE tested_at >= ? ORDER BY tested_at", (since_iso(days),)).fetchall()
    groups: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        groups[(row["provider"], row["region"])].append(row)
    summary: list[dict[str, Any]] = []
    for (provider, region), items in groups.items():
        available = [x for x in items if x["available"]]
        ttfb = [x["ttfb_ms"] for x in available]
        downloads = [x["download_mbps"] for x in available]
        uploads = [x["upload_mbps"] for x in available]
        evening = [x["download_mbps"] for x in available if datetime.fromisoformat(x["tested_at"]).astimezone().hour in (20, 21, 22, 23)]
        daytime = [x["download_mbps"] for x in available if datetime.fromisoformat(x["tested_at"]).astimezone().hour in (9, 14)]
        day_med, eve_med = percentile(daytime, .5), percentile(evening, .5)
        decline = None if day_med in (None, 0) or eve_med is None else max(0.0, (day_med - eve_med) / day_med * 100)
        speed_values = [x for x in downloads if x is not None]
        cv = None
        if len(speed_values) >= 2 and statistics.mean(speed_values):
            cv = statistics.pstdev(speed_values) / statistics.mean(speed_values) * 100
        summary.append({
            "provider": provider, "region": region, "samples": len(items),
            "availability": len(available) / len(items) * 100,
            "failure_rate": (len(items) - len(available)) / len(items) * 100,
            "ttfb_p50": percentile(ttfb, .5), "ttfb_p95": percentile(ttfb, .95),
            "jitter_p50": percentile([x["jitter_ms"] for x in available], .5),
            "loss_p50": percentile([x["packet_loss_pct"] for x in items], .5),
            "down_p50": percentile(downloads, .5), "down_p95": percentile(downloads, .95),
            "up_p50": percentile(uploads, .5), "evening_decline": decline, "speed_cv": cv,
        })
    summary.sort(key=lambda x: (x["region"], -x["availability"], -(x["down_p50"] or -1), x["ttfb_p50"] or 1e9))

    region_medians = {region: percentile([x["ttfb_p50"] for x in summary if x["region"] == region], .5)
                      for region in {x["region"] for x in summary}}
    anomalies = []
    for item in summary:
        baseline = region_medians.get(item["region"])
        reasons = []
        if item["failure_rate"] >= 30: reasons.append(f"失败率 {item['failure_rate']:.0f}%")
        if item["loss_p50"] is not None and item["loss_p50"] >= 10: reasons.append(f"丢包 P50 {item['loss_p50']:.1f}%")
        if baseline and item["ttfb_p50"] and item["ttfb_p50"] > baseline * 2: reasons.append("延迟高于同地区中位数 2×")
        if item["speed_cv"] is not None and item["speed_cv"] >= 50: reasons.append(f"速度波动 CV {item['speed_cv']:.0f}%")
        if reasons:
            anomalies.append({**item, "reason": "；".join(reasons)})
    return summary, anomalies


def markdown_report(summary: list[dict[str, Any]], anomalies: list[dict[str, Any]], days: int) -> str:
    generated = datetime.now().astimezone().isoformat(timespec="seconds")
    out = [f"# Clash/Mihomo 供应商评测报告", "", f"生成时间：{generated}；统计窗口：最近 {days} 天。", "",
           "> P50/P95 是该供应商同地区所有节点与所有测试时刻的分布；样本较少时只作参考。", "",
           "## 同地区汇总排名", "",
           "| 地区 | 供应商 | 样本 | 可用率 | 失败率 | TTFB P50/P95 | 抖动 P50 | 丢包 P50 | 下载 P50/P95 | 上传 P50 | 晚高峰衰减 | 稳定性 CV |",
           "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for x in summary:
        out.append(f"| {x['region']} | {x['provider']} | {x['samples']} | {_f(x['availability'],'%')} | {_f(x['failure_rate'],'%')} | {_f(x['ttfb_p50'],' ms')}/{_f(x['ttfb_p95'],' ms')} | {_f(x['jitter_p50'],' ms')} | {_f(x['loss_p50'],'%')} | {_f(x['down_p50'],' Mbps')}/{_f(x['down_p95'],' Mbps')} | {_f(x['up_p50'],' Mbps')} | {_f(x['evening_decline'],'%')} | {_f(x['speed_cv'],'%')} |")
    out += ["", "## 异常", ""]
    if anomalies:
        out += [f"- {x['region']} / {x['provider']}：{x['reason']}" for x in anomalies]
    else:
        out.append("未发现达到默认阈值的异常。")
    out += ["", "## 口径", "", "- 可用：延迟探测成功且丢包小于 100%。",
            "- 晚高峰：本机时区 20:00–23:59；基准时段：09:00 和 14:00。衰减为两个时段下载速度中位数之差。",
            "- 稳定性 CV：下载速度总体标准差 / 均值；越低越稳定。", "- 排名先按可用率，再按下载 P50，最后按 TTFB P50。", ""]
    return "\n".join(out)


def html_report(markdown_text: str, title: str = "Clash/Mihomo 供应商评测报告") -> str:
    # Small, dependency-free renderer tailored to the generated Markdown.
    lines, body, in_table = markdown_text.splitlines(), [], False
    for line in lines:
        if line.startswith("# "):
            body.append(f"<h1>{html.escape(line[2:])}</h1>")
        elif line.startswith("## "):
            if in_table: body.append("</tbody></table>"); in_table = False
            body.append(f"<h2>{html.escape(line[3:])}</h2>")
        elif line.startswith("|---"):
            continue
        elif line.startswith("|"):
            cells = [html.escape(x.strip()) for x in line.strip("|").split("|")]
            if not in_table:
                body.append("<div class='scroll'><table><thead><tr>" + "".join(f"<th>{x}</th>" for x in cells) + "</tr></thead><tbody>")
                in_table = True
            else:
                body.append("<tr>" + "".join(f"<td>{x}</td>" for x in cells) + "</tr>")
        elif line.startswith("- "):
            body.append(f"<p class='bullet'>• {html.escape(line[2:])}</p>")
        elif line.startswith("> "):
            body.append(f"<aside>{html.escape(line[2:])}</aside>")
        elif line and not in_table:
            body.append(f"<p>{html.escape(line)}</p>")
    if in_table: body.append("</tbody></table></div>")
    css = """body{font:15px -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;margin:36px;color:#18212b;background:#f7f9fb}main{max-width:1400px;margin:auto;background:white;padding:32px;border-radius:16px;box-shadow:0 6px 24px #0001}h1{margin-top:0}h2{margin-top:32px}.scroll{overflow:auto}table{border-collapse:collapse;width:100%;white-space:nowrap}th,td{padding:9px 10px;border-bottom:1px solid #dde3ea;text-align:right}th:first-child,td:first-child,th:nth-child(2),td:nth-child(2){text-align:left}th{background:#eef3f8;position:sticky;top:0}aside{padding:12px;background:#eef6ff;border-left:4px solid #3182ce}.bullet{margin:6px 0}@media(prefers-color-scheme:dark){body{background:#111820;color:#e8edf2}main{background:#18212b}th{background:#243242}th,td{border-color:#344454}}"""
    return f"<!doctype html><html lang='zh-CN'><meta charset='utf-8'><meta name='viewport' content='width=device-width'><title>{html.escape(title)}</title><style>{css}</style><main>{''.join(body)}</main></html>"


def write_reports(conn: sqlite3.Connection, out_dir: Path, days: int = 7) -> tuple[Path, Path]:
    summary, anomalies = build_summary(conn, days)
    md = markdown_report(summary, anomalies, days)
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path, html_path = out_dir / "latest.md", out_dir / "latest.html"
    md_path.write_text(md, encoding="utf-8")
    html_path.write_text(html_report(md), encoding="utf-8")
    return md_path, html_path

