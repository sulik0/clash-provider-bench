from __future__ import annotations

import csv
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .engine import Measurement


SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, started_at TEXT NOT NULL, ended_at TEXT, status TEXT NOT NULL,
  config_digest TEXT NOT NULL, engine TEXT NOT NULL, regions_json TEXT NOT NULL, error TEXT
);
CREATE TABLE IF NOT EXISTS measurements (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES runs(id),
  tested_at TEXT NOT NULL, provider TEXT NOT NULL, node_name TEXT NOT NULL, node_key TEXT NOT NULL,
  proxy_type TEXT, region TEXT NOT NULL, available INTEGER NOT NULL,
  ttfb_ms REAL, jitter_ms REAL, packet_loss_pct REAL, download_mbps REAL, upload_mbps REAL,
  status TEXT NOT NULL, error TEXT, exit_ip TEXT, exit_country TEXT, exit_region TEXT,
  asn TEXT, as_org TEXT, chatgpt TEXT, youtube TEXT, netflix TEXT, raw_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_measurements_time ON measurements(tested_at);
CREATE INDEX IF NOT EXISTS idx_measurements_provider_region ON measurements(provider, region);
CREATE INDEX IF NOT EXISTS idx_measurements_node ON measurements(node_key, tested_at);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def begin_run(conn: sqlite3.Connection, digest: str, engine: str, regions: list[str]) -> str:
    run_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO runs VALUES (?, ?, NULL, 'running', ?, ?, ?, NULL)",
        (run_id, datetime.now(timezone.utc).isoformat(), digest, engine, json.dumps(regions)),
    )
    conn.commit()
    return run_id


def add_measurements(conn: sqlite3.Connection, run_id: str, values: Iterable[Measurement]) -> int:
    now = datetime.now(timezone.utc).isoformat()
    rows = []
    for item in values:
        raw = item.as_dict()
        rows.append((run_id, now, item.provider, item.node_name, item.node_key, item.proxy_type, item.region,
            int(item.available), item.ttfb_ms, item.jitter_ms, item.packet_loss_pct, item.download_mbps,
            item.upload_mbps, item.status, item.error, item.exit_ip, item.exit_country, item.exit_region,
            item.asn, item.as_org, item.chatgpt, item.youtube, item.netflix, json.dumps(raw, ensure_ascii=False)))
    conn.executemany(
        """INSERT INTO measurements (
        run_id,tested_at,provider,node_name,node_key,proxy_type,region,available,ttfb_ms,jitter_ms,
        packet_loss_pct,download_mbps,upload_mbps,status,error,exit_ip,exit_country,exit_region,asn,
        as_org,chatgpt,youtube,netflix,raw_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
    conn.commit()
    return len(rows)


def finish_run(conn: sqlite3.Connection, run_id: str, status: str, error: str | None = None) -> None:
    conn.execute("UPDATE runs SET ended_at=?, status=?, error=? WHERE id=?",
                 (datetime.now(timezone.utc).isoformat(), status, error, run_id))
    conn.commit()


def export_csv(conn: sqlite3.Connection, path: Path, since: str | None = None) -> int:
    query = "SELECT * FROM measurements"
    params: tuple[str, ...] = ()
    if since:
        query += " WHERE tested_at >= ?"
        params = (since,)
    query += " ORDER BY tested_at, provider, region, node_name"
    rows = conn.execute(query, params).fetchall()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(rows[0].keys() if rows else ["id", "run_id", "tested_at", "provider", "node_name"])
        writer.writerows(tuple(row) for row in rows)
    return len(rows)

