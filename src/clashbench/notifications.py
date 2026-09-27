from __future__ import annotations

import sqlite3
import subprocess


def notification_text(
    conn: sqlite3.Connection, run_id: str, status: str,
) -> tuple[str, str, str]:
    rows = conn.execute(
        """SELECT provider, COUNT(*) AS total, SUM(available) AS available,
                  SUM(CASE WHEN chatgpt IS NOT NULL THEN 1 ELSE 0 END) AS checked,
                  SUM(CASE WHEN chatgpt LIKE 'available%' THEN 1 ELSE 0 END) AS chatgpt_ok
           FROM measurements WHERE run_id=? GROUP BY provider ORDER BY provider""",
        (run_id,),
    ).fetchall()
    total = sum(int(row["total"] or 0) for row in rows)
    available = sum(int(row["available"] or 0) for row in rows)
    checked = sum(int(row["checked"] or 0) for row in rows)
    chatgpt_ok = sum(int(row["chatgpt_ok"] or 0) for row in rows)
    if checked:
        title = f"Clash Bench：ChatGPT {chatgpt_ok}/{checked} 可用"
        parts = [
            f"{row['provider']} {int(row['chatgpt_ok'] or 0)}/{int(row['checked'] or 0)}"
            for row in rows
        ]
    else:
        title = f"Clash Bench：节点 {available}/{total} 可用"
        parts = [
            f"{row['provider']} {int(row['available'] or 0)}/{int(row['total'] or 0)}"
            for row in rows
        ]
    subtitle = {"ok": "评测完成", "partial": "评测部分完成", "failed": "评测失败"}.get(status, status)
    message = " · ".join(parts) if parts else "本次运行没有保存节点结果"
    return title, subtitle, message


def send_macos_notification(title: str, subtitle: str, message: str) -> None:
    script = """
on run argv
    display notification (item 3 of argv) with title (item 1 of argv) subtitle (item 2 of argv)
end run
""".strip()
    result = subprocess.run(
        ["/usr/bin/osascript", "-e", script, title, subtitle, message],
        capture_output=True, text=True, timeout=10, check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "osascript failed").strip()
        raise RuntimeError(detail[:240])


def notify_run(
    conn: sqlite3.Connection, run_id: str, status: str, mode: str = "macos",
) -> tuple[str, str, str]:
    title, subtitle, message = notification_text(conn, run_id, status)
    if mode == "macos":
        send_macos_notification(title, subtitle, message)
    else:
        raise ValueError(f"Unsupported notification mode: {mode}")
    return title, subtitle, message
