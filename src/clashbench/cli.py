from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import load_config
from .db import (
    add_measurements, begin_provider, begin_run, connect, export_csv, finish_provider,
    finish_run, latest_run_id, rotated_providers,
)
from .engine import FaceairAdapter, materialize_provider, parse_faceair_tsv, write_mock_tsv
from .enrich import MihomoEnricherPool
from .environment import capture_network_environment
from .notifications import notify_run, send_macos_notification
from .profile import benchmark_profile
from .report import since_iso, write_reports
from .schedule import LABEL, launch_agent
from .util import load_dotenv, public_config_digest, redact


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="clashbench", description="Clash/Mihomo provider benchmark orchestrator")
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("run", "doctor"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--config", default="bench.toml")
        if name == "run":
            cmd.add_argument("--regions", help="Comma-separated override, e.g. JP,HK")
            cmd.add_argument("--mock", action="store_true", help="Store deterministic fixtures; no network or real test")
            cmd.add_argument(
                "--quick", action="store_true",
                help="Skip download/upload throughput and prioritize availability/ChatGPT checks",
            )
    report = sub.add_parser("report")
    report.add_argument("--config", default="bench.toml"); report.add_argument("--days", type=int, default=7); report.add_argument("--output", default="reports")
    report.add_argument("--run-id", help="Regenerate the single-run report for this run and anchor its trend")
    export = sub.add_parser("export")
    export.add_argument("--config", default="bench.toml"); export.add_argument("--days", type=int, default=7); export.add_argument("--output", default="reports/results.csv")
    notify = sub.add_parser("notify")
    notify.add_argument("--config", default="bench.toml")
    notify.add_argument("--test", action="store_true", help="Send a test notification instead of the latest run summary")
    schedule = sub.add_parser("schedule")
    schedule.add_argument("action", choices=("render", "install", "uninstall")); schedule.add_argument("--config", default="bench.toml")
    schedule.add_argument("--times", default="09:00,14:00,20:00,22:00,00:00"); schedule.add_argument("--output")
    schedule.add_argument("--full", action="store_true", help="Schedule full throughput tests instead of the default quick checks")
    schedule.add_argument("--regions", help="Comma-separated region override for scheduled runs")
    return p


def _progress(
    prefix: str, message: str, timezone_name: str = "Asia/Shanghai", *, file=None,
) -> None:
    timestamp = datetime.now(ZoneInfo(timezone_name)).isoformat(sep=" ", timespec="seconds")
    print(f"[{timestamp}] [{prefix}] {message}", file=file if file is not None else sys.stdout, flush=True)


def cmd_run(args) -> int:
    config_path = Path(args.config).expanduser().resolve()
    load_dotenv(config_path.parent / ".env")
    settings = load_config(config_path)
    timezone_name = str(settings.report.get("timezone", "Asia/Shanghai"))
    if args.quick:
        settings.engine["speed_mode"] = "fast"

    def progress(prefix: str, message: str, *, file=None) -> None:
        _progress(prefix, message, timezone_name, file=file)

    regions = [x.strip().upper() for x in args.regions.split(",")] if args.regions else settings.regions
    conn = connect(settings.database)
    adapter = None if args.mock else FaceairAdapter(settings)
    if args.mock:
        engine_version = "mock-v1"
    else:
        _, engine_version = adapter.doctor()
        engine_version = engine_version or "unknown"
    environment = capture_network_environment()
    comparison_key, parameters = benchmark_profile(settings, regions, engine_version, environment)
    provider_names = [provider.name for provider in settings.providers]
    provider_order = rotated_providers(conn, provider_names, comparison_key)
    providers_by_name = {provider.name: provider for provider in settings.providers}
    run_id = begin_run(
        conn, public_config_digest(settings.raw), "mock" if args.mock else "faceair/clash-speedtest", regions,
        comparison_key=comparison_key, engine_version=engine_version, parameters=parameters,
        environment=environment, provider_order=provider_order,
    )
    runtime = settings.root / ".runtime" / run_id
    errors, total = [], 0
    mode = "快速可用性检查" if args.quick else "完整配置评测"
    progress("run", f"开始 {run_id}；模式：{mode}；Provider 顺序：{' → '.join(provider_order)}")
    active_provider: str | None = None
    try:
        for ordinal, provider_name in enumerate(provider_order):
            provider = providers_by_name[provider_name]
            active_provider = provider.name
            prefix = f"{ordinal + 1}/{len(provider_order)} {provider.name}"
            begin_provider(conn, run_id, provider.name, ordinal)
            try:
                if args.mock:
                    progress(prefix, "生成 mock 测试数据")
                    values = parse_faceair_tsv(write_mock_tsv(runtime, provider.name), provider.name, {}, settings.region_patterns)
                    values = [x for x in values if not regions or x.region in regions]
                else:
                    config = materialize_provider(
                        provider, runtime, str(settings.engine.get("user_agent", "mihomo/1.19")),
                        lambda message: progress(prefix, message),
                    )
                    values = adapter.run(provider, config, regions, lambda message: progress(prefix, message))
                    if settings.enrichment.get("enabled", False):
                        try:
                            mihomo_binary = str(settings.enrichment.get("mihomo_binary", "mihomo"))
                            if "/" in mihomo_binary:
                                mihomo_binary = str((settings.root / mihomo_binary).resolve())
                            configured_workers = int(settings.enrichment.get("workers", 1))
                            worker_count = min(configured_workers, max(1, sum(item.available for item in values)))
                            with MihomoEnricherPool(
                                mihomo_binary, runtime, config,
                                bool(settings.enrichment.get("unlock", False)),
                                int(settings.enrichment.get("timeout_seconds", 15)),
                                settings.enrichment.get("checks"),
                                settings.enrichment.get("chatgpt_unsupported_countries", ()),
                                worker_count,
                            ) as enricher:
                                checks = ",".join(sorted(enricher.checks)) or "仅出口信息"
                                progress(
                                    prefix,
                                    f"开始逐节点附加检测：{len(values)} 个节点；项目={checks}；"
                                    f"并行 worker={enricher.active_workers}",
                                )
                                if enricher.startup_errors:
                                    progress(
                                        prefix,
                                        f"部分 worker 启动失败，已降级为 {enricher.active_workers} 个",
                                        file=sys.stderr,
                                    )

                                def enrichment_progress(index, count, item, phase):
                                    name = " ".join(item.node_name.split())[:60]
                                    if phase == "start":
                                        progress(prefix, f"附加检测 {index}/{count}：{name}")
                                    else:
                                        result = item.chatgpt or item.enrichment_status
                                        progress(prefix, f"附加检测 {index}/{count} 完成：{result}")

                                enricher.enrich(values, enrichment_progress)
                        except Exception as exc:
                            for item in values:
                                if item.available:
                                    item.enrichment_status = "failed"
                                    item.enrichment_error = type(exc).__name__
                                else:
                                    item.enrichment_status = "skipped"
                            progress(prefix, f"enrichment failed: {type(exc).__name__}", file=sys.stderr)
                count = add_measurements(conn, run_id, values)
                total += count
                finish_provider(conn, run_id, provider.name, "ok", count)
                progress(prefix, f"已保存 {len(values)} 条测量")
            except Exception as exc:
                message = redact(exc)
                errors.append(f"{provider.name}: {message}")
                finish_provider(conn, run_id, provider.name, "failed", 0, message)
                progress(prefix, f"failed: {message}", file=sys.stderr)
            active_provider = None
        status = "partial" if errors and total else "failed" if errors else "ok"
        finish_run(conn, run_id, status, " | ".join(errors) or None)
    except KeyboardInterrupt:
        if active_provider:
            finish_provider(conn, run_id, active_provider, "failed", 0, "interrupted")
        finish_run(conn, run_id, "failed", "interrupted")
        raise
    finally:
        shutil.rmtree(runtime, ignore_errors=True)
    out_dir = settings.root / settings.report.get("output", "reports")
    progress("report", "生成单次报告、趋势报告和 CSV")
    reports = write_reports(
        conn, out_dir, int(settings.report.get("days", 7)), run_id, timezone_name,
    )
    export_csv(conn, out_dir / "results.csv")
    if settings.notification.get("enabled", False) and not args.mock:
        try:
            notify_run(
                conn, run_id, status,
                str(settings.notification.get("mode", "macos")),
            )
            progress("notification", "已发送本次 ChatGPT/节点可用性摘要")
        except Exception as exc:
            progress(
                "notification", f"发送失败：{type(exc).__name__}", file=sys.stderr,
            )
    print(f"run={run_id} status={'partial' if errors and total else 'failed' if errors else 'ok'} rows={total}")
    print(f"report={reports['run_md']}\ntrend={reports['trend_html']}")
    conn.close()
    return 1 if errors and not total else 0


def cmd_doctor(args) -> int:
    config_path = Path(args.config).expanduser().resolve(); load_dotenv(config_path.parent / ".env")
    settings = load_config(config_path)
    ok, detail = FaceairAdapter(settings).doctor()
    print(("OK" if ok else "MISSING") + f" clash-speedtest: {detail}")
    print(f"OK SQLite: {sqlite3.sqlite_version}")
    print(f"OK providers: {len(settings.providers)}; regions: {','.join(settings.regions) or 'ALL'}")
    for provider in settings.providers:
        state = "configured" if provider.path or (provider.source_env and os.environ.get(provider.source_env)) else "missing env"
        print(f"{'OK' if state == 'configured' else 'MISSING'} provider {provider.name}: {state}")
        if state != "configured": ok = False
    if settings.enrichment.get("enabled", False):
        configured = str(settings.enrichment.get("mihomo_binary", "mihomo"))
        candidate = (settings.root / configured).resolve() if "/" in configured else Path(configured)
        found = candidate.is_file() or bool(shutil.which(configured))
        checks = settings.enrichment.get("checks")
        if checks is None:
            checks = ["chatgpt", "youtube", "netflix"] if settings.enrichment.get("unlock") else []
        if not settings.enrichment.get("unlock"):
            checks = []
        print(f"{'OK' if found else 'MISSING'} Mihomo: {candidate if candidate.is_file() else configured}")
        print(
            f"OK enrichment: checks={','.join(checks) or 'egress-only'}; "
            f"timeout={int(settings.enrichment.get('timeout_seconds', 15))}s; "
            f"workers={int(settings.enrichment.get('workers', 1))}"
        )
        if not found:
            ok = False
    print(f"OK report timezone: {settings.report.get('timezone', 'Asia/Shanghai')}")
    state = "enabled" if settings.notification.get("enabled", False) else "disabled"
    print(f"OK notification: {state}; mode={settings.notification.get('mode', 'macos')}")
    return 0 if ok else 1


def cmd_report(args) -> int:
    settings = load_config(args.config); conn = connect(settings.database)
    reports = write_reports(
        conn, (settings.root / args.output).resolve(), args.days, args.run_id,
        str(settings.report.get("timezone", "Asia/Shanghai")),
    )
    print(f"{reports['latest_md']}\n{reports['trend_html']}"); conn.close(); return 0


def cmd_export(args) -> int:
    settings = load_config(args.config); conn = connect(settings.database)
    count = export_csv(conn, (settings.root / args.output).resolve(), since_iso(args.days))
    print(f"exported {count} rows"); conn.close(); return 0


def cmd_notify(args) -> int:
    settings = load_config(args.config)
    if args.test:
        send_macos_notification(
            "Clash Bench 通知测试", "macOS 定时通知已就绪",
            "锁屏时通知会进入通知中心；机器睡眠时将在唤醒后显示。",
        )
        print("test notification sent")
        return 0
    conn = connect(settings.database)
    run_id = latest_run_id(conn)
    if not run_id:
        conn.close()
        raise ValueError("No completed runs are available for notification")
    run = conn.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
    title, subtitle, message = notify_run(
        conn, run_id, run["status"], str(settings.notification.get("mode", "macos")),
    )
    conn.close()
    print(f"{title} | {subtitle} | {message}")
    return 0


def cmd_schedule(args) -> int:
    config = Path(args.config).expanduser().resolve(); project = config.parent
    target = Path(args.output).expanduser().resolve() if args.output else Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
    if args.action == "uninstall":
        if target.exists(): target.unlink()
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], check=False, capture_output=True)
        print(f"removed {target}"); return 0
    content = launch_agent(
        config, project, [x.strip() for x in args.times.split(",")],
        quick=not args.full, regions=args.regions,
    )
    (project / "data").mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.action == "render":
        target = Path(args.output or project / "data" / f"{LABEL}.plist").resolve()
    target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(content)
    if args.action == "install":
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], check=False, capture_output=True)
        subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(target)], check=True)
    print(f"{args.action}ed {target}"); return 0


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    return {
        "run": cmd_run, "doctor": cmd_doctor, "report": cmd_report,
        "export": cmd_export, "notify": cmd_notify, "schedule": cmd_schedule,
    }[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
