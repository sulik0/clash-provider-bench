from __future__ import annotations

import csv
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .engine import Measurement


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, started_at TEXT NOT NULL, ended_at TEXT, status TEXT NOT NULL,
  config_digest TEXT NOT NULL, engine TEXT NOT NULL, regions_json TEXT NOT NULL, error TEXT,
  comparison_key TEXT, engine_version TEXT, parameters_json TEXT, environment_json TEXT,
  provider_order_json TEXT
);
CREATE TABLE IF NOT EXISTS provider_runs (
  run_id TEXT NOT NULL REFERENCES runs(id), provider TEXT NOT NULL, ordinal INTEGER NOT NULL,
  started_at TEXT NOT NULL, ended_at TEXT, status TEXT NOT NULL, measurement_count INTEGER NOT NULL DEFAULT 0,
  error TEXT, PRIMARY KEY (run_id, provider)
);
CREATE TABLE IF NOT EXISTS measurements (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES runs(id),
  tested_at TEXT NOT NULL, provider TEXT NOT NULL, node_name TEXT NOT NULL, node_key TEXT NOT NULL,
  proxy_type TEXT, region TEXT NOT NULL, available INTEGER NOT NULL,
  ttfb_ms REAL, jitter_ms REAL, packet_loss_pct REAL, download_mbps REAL, upload_mbps REAL,
  status TEXT NOT NULL, error TEXT, exit_ip TEXT, exit_country TEXT, exit_region TEXT,
  asn TEXT, as_org TEXT, chatgpt TEXT, youtube TEXT, netflix TEXT, raw_json TEXT NOT NULL,
  enrichment_status TEXT NOT NULL DEFAULT 'legacy-unknown', enrichment_error TEXT,
  chatgpt_auth TEXT, chatgpt_static TEXT, chatgpt_websocket TEXT,
  throughput_attempted INTEGER
);
CREATE INDEX IF NOT EXISTS idx_measurements_time ON measurements(tested_at);
CREATE INDEX IF NOT EXISTS idx_measurements_provider_region ON measurements(provider, region);
CREATE INDEX IF NOT EXISTS idx_measurements_node ON measurements(node_key, tested_at);
"""


RUN_MIGRATIONS = {
    "comparison_key": "TEXT", "engine_version": "TEXT", "parameters_json": "TEXT",
    "environment_json": "TEXT", "provider_order_json": "TEXT",
}
MEASUREMENT_MIGRATIONS = {
    "enrichment_status": "TEXT NOT NULL DEFAULT 'legacy-unknown'",
    "enrichment_error": "TEXT",
    "chatgpt_auth": "TEXT",
    "chatgpt_static": "TEXT",
    "chatgpt_websocket": "TEXT",
    "throughput_attempted": "INTEGER",
}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _migrate(conn: sqlite3.Connection) -> None:
    for table, migrations in (("runs", RUN_MIGRATIONS), ("measurements", MEASUREMENT_MIGRATIONS)):
        existing = _columns(conn, table)
        for name, declaration in migrations.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_comparison ON runs(comparison_key, started_at)")
    conn.commit()


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def rotated_providers(conn: sqlite3.Connection, names: list[str], comparison_key: str) -> list[str]:
    if not names:
        return []
    completed = conn.execute(
        "SELECT COUNT(*) FROM runs WHERE comparison_key=? AND status IN ('ok','partial','failed')",
        (comparison_key,),
    ).fetchone()[0]
    offset = int(completed) % len(names)
    return names[offset:] + names[:offset]


def begin_run(
    conn: sqlite3.Connection, digest: str, engine: str, regions: list[str], *,
    comparison_key: str | None = None, engine_version: str | None = None,
    parameters: dict[str, Any] | None = None, environment: dict[str, Any] | None = None,
    provider_order: list[str] | None = None,
) -> str:
    run_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO runs
        (id,started_at,ended_at,status,config_digest,engine,regions_json,error,comparison_key,
         engine_version,parameters_json,environment_json,provider_order_json)
        VALUES (?, ?, NULL, 'running', ?, ?, ?, NULL, ?, ?, ?, ?, ?)""",
        (run_id, datetime.now(timezone.utc).isoformat(), digest, engine, json.dumps(regions), comparison_key,
         engine_version, json.dumps(parameters, ensure_ascii=False) if parameters is not None else None,
         json.dumps(environment, ensure_ascii=False) if environment is not None else None,
         json.dumps(provider_order, ensure_ascii=False) if provider_order is not None else None),
    )
    conn.commit()
    return run_id


def begin_provider(conn: sqlite3.Connection, run_id: str, provider: str, ordinal: int) -> None:
    conn.execute(
        "INSERT INTO provider_runs VALUES (?, ?, ?, ?, NULL, 'running', 0, NULL)",
        (run_id, provider, ordinal, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def finish_provider(
    conn: sqlite3.Connection, run_id: str, provider: str, status: str,
    measurement_count: int = 0, error: str | None = None,
) -> None:
    conn.execute(
        """UPDATE provider_runs SET ended_at=?,status=?,measurement_count=?,error=?
           WHERE run_id=? AND provider=?""",
        (datetime.now(timezone.utc).isoformat(), status, measurement_count, error, run_id, provider),
    )
    conn.commit()


def add_measurements(conn: sqlite3.Connection, run_id: str, values: Iterable[Measurement]) -> int:
    now = datetime.now(timezone.utc).isoformat()
    rows = []
    for item in values:
        raw = item.as_dict()
        rows.append((run_id, now, item.provider, item.node_name, item.node_key, item.proxy_type, item.region,
            int(item.available), item.ttfb_ms, item.jitter_ms, item.packet_loss_pct, item.download_mbps,
            item.upload_mbps, item.status, item.error, item.exit_ip, item.exit_country, item.exit_region,
            item.asn, item.as_org, item.chatgpt, item.youtube, item.netflix, json.dumps(raw, ensure_ascii=False),
            item.enrichment_status, item.enrichment_error, item.chatgpt_auth, item.chatgpt_static,
            item.chatgpt_websocket,
            None if item.throughput_attempted is None else int(item.throughput_attempted)))
    conn.executemany(
        """INSERT INTO measurements (
        run_id,tested_at,provider,node_name,node_key,proxy_type,region,available,ttfb_ms,jitter_ms,
        packet_loss_pct,download_mbps,upload_mbps,status,error,exit_ip,exit_country,exit_region,asn,
        as_org,chatgpt,youtube,netflix,raw_json,enrichment_status,enrichment_error,chatgpt_auth,
        chatgpt_static,chatgpt_websocket,throughput_attempted)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
    conn.commit()
    return len(rows)


def finish_run(conn: sqlite3.Connection, run_id: str, status: str, error: str | None = None) -> None:
    conn.execute("UPDATE runs SET ended_at=?, status=?, error=? WHERE id=?",
                 (datetime.now(timezone.utc).isoformat(), status, error, run_id))
    conn.commit()


def latest_run_id(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT id FROM runs WHERE status != 'running' ORDER BY started_at DESC LIMIT 1").fetchone()
    return row["id"] if row else None


def export_csv(conn: sqlite3.Connection, path: Path, since: str | None = None) -> int:
    query = """SELECT m.*,r.status AS run_status,r.comparison_key,r.engine_version,r.parameters_json,
               r.environment_json,r.provider_order_json FROM measurements m JOIN runs r ON r.id=m.run_id"""
    params: tuple[str, ...] = ()
    if since:
        query += " WHERE m.tested_at >= ?"
        params = (since,)
    query += " ORDER BY m.tested_at, m.provider, m.region, m.node_name"
    rows = conn.execute(query, params).fetchall()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(rows[0].keys() if rows else ["id", "run_id", "tested_at", "provider", "node_name"])
        writer.writerows(tuple(row) for row in rows)
    return len(rows)
