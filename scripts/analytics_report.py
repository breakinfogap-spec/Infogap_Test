from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from typing import Any

GREATER_VANCOUVER_CITIES = [
    "Vancouver",
    "Burnaby",
    "Richmond",
    "Surrey",
    "Coquitlam",
    "Port Coquitlam",
    "Port Moody",
    "New Westminster",
    "North Vancouver",
    "West Vancouver",
    "Delta",
    "Langley",
    "Maple Ridge",
    "Pitt Meadows",
    "White Rock",
    "Bowen Island",
    "Lions Bay",
    "Anmore",
    "Belcarra",
]

QUERIES = {
    "城市分布（近 30 天，排除 bot）": """
        SELECT city, region, COUNT(*) AS views
        FROM page_views
        WHERE is_bot = 0 AND date_local >= date('now','-30 days')
        GROUP BY city, region
        ORDER BY views DESC
        LIMIT 30;
    """,
    "Section 排名（近 30 天，排除 bot）": """
        SELECT section, COUNT(*) AS views
        FROM page_views
        WHERE is_bot = 0 AND date_local >= date('now','-30 days')
        GROUP BY section
        ORDER BY views DESC;
    """,
    "每日趋势（近 30 天，排除 bot）": """
        SELECT date_local, COUNT(*) AS views
        FROM page_views
        WHERE is_bot = 0 AND date_local >= date('now','-30 days')
        GROUP BY date_local
        ORDER BY date_local;
    """,
}

GREATER_VANCOUVER_SQL = f"""
    SELECT
      COUNT(*) AS total_views,
      SUM(CASE WHEN city IN ({','.join('?' for _ in GREATER_VANCOUVER_CITIES)}) THEN 1 ELSE 0 END) AS metro_vancouver_views
    FROM page_views
    WHERE is_bot = 0 AND date_local >= date('now','-30 days');
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Print InfoGap anonymous analytics from Cloudflare D1.")
    parser.add_argument(
        "--database",
        default=os.getenv("ANALYTICS_D1_DATABASE", "infogap-analytics"),
        help="Cloudflare D1 database name. Default: infogap-analytics",
    )
    parser.add_argument("--local", action="store_true", help="Query the local Wrangler D1 database instead of --remote.")
    args = parser.parse_args()

    if not shutil.which("npx"):
        print("npx was not found. Install Node.js dependencies first with npm install.", file=sys.stderr)
        return 1

    remote_flag = [] if args.local else ["--remote"]
    for title, sql in QUERIES.items():
        rows = run_d1_query(args.database, sql, remote_flag)
        print_table(title, rows)

    gv_rows = run_d1_query(
        args.database,
        GREATER_VANCOUVER_SQL,
        remote_flag,
        params=GREATER_VANCOUVER_CITIES,
    )
    print_greater_vancouver_summary(gv_rows)
    return 0


def run_d1_query(database: str, sql: str, remote_flag: list[str], params: list[str] | None = None) -> list[dict[str, Any]]:
    command_sql = sql.strip()
    if params:
        # Wrangler's command mode does not support bound parameters, so we safely inline this fixed city whitelist.
        quoted = ",".join("'" + city.replace("'", "''") + "'" for city in params)
        command_sql = command_sql.replace(",".join("?" for _ in params), quoted)

    command = ["npx", "wrangler", "d1", "execute", database, *remote_flag, "--command", command_sql, "--json"]
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    if completed.returncode != 0:
        print(completed.stderr or completed.stdout, file=sys.stderr)
        raise SystemExit(completed.returncode)

    payload = json.loads(completed.stdout)
    return extract_rows(payload)


def extract_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        for item in payload:
            rows = item.get("results") if isinstance(item, dict) else None
            if isinstance(rows, list):
                return rows
    if isinstance(payload, dict):
        rows = payload.get("results")
        if isinstance(rows, list):
            return rows
        nested = payload.get("result")
        if isinstance(nested, list):
            return extract_rows(nested)
    return []


def print_table(title: str, rows: list[dict[str, Any]]) -> None:
    print(f"\n{title}")
    if not rows:
        print("无数据")
        return

    columns = list(rows[0].keys())
    widths = {column: max(len(str(column)), *(len(str(row.get(column, ""))) for row in rows)) for column in columns}
    print(" | ".join(column.ljust(widths[column]) for column in columns))
    print(" | ".join("-" * widths[column] for column in columns))
    for row in rows:
        print(" | ".join(str(row.get(column, "")).ljust(widths[column]) for column in columns))


def print_greater_vancouver_summary(rows: list[dict[str, Any]]) -> None:
    row = rows[0] if rows else {}
    total = int(row.get("total_views") or 0)
    metro = int(row.get("metro_vancouver_views") or 0)
    percent = (metro / total * 100) if total else 0
    print("\n大温地区占比（近 30 天，排除 bot）")
    print(f"近 30 天共 {total} 次浏览，其中 {metro} 次（{percent:.1f}%）来自大温地区。")


if __name__ == "__main__":
    raise SystemExit(main())
