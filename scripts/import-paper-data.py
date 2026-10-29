#!/usr/bin/env python3
"""Fetch a bounded real BaoStock snapshot and optionally publish to ClickHouse.

Writer credentials are read from an explicit dotenv file, never CLI arguments.
Run from the project venv; --output stores the original validated evidence.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))

from dotenv import dotenv_values
from src.data_ingest.paper_snapshot import SnapshotWriter, _validate_envelope, fetch_snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", nargs="+")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--snapshot", type=Path, help="Import previously validated evidence instead of fetching")
    parser.add_argument("--writer-env", type=Path, help="Explicit local writer config; omit to fetch/validate only")
    parser.add_argument("--timeout", type=float, default=15)
    args = parser.parse_args()
    if args.snapshot:
        if args.symbols or args.start or args.end or args.output:
            parser.error("--snapshot cannot be combined with fetch/output arguments")
        if not args.writer_env:
            parser.error("--snapshot requires --writer-env")
        if args.snapshot.stat().st_size > 10_000_000:
            raise ValueError("Snapshot exceeds size limit")
        snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
    else:
        if not (args.symbols and args.start and args.end and args.output):
            parser.error("Fetch requires --symbols, --start, --end and --output")
        if args.output.exists():
            raise ValueError("Output already exists; choose a new evidence file")
        snapshot = fetch_snapshot(args.symbols, args.start, args.end, timeout=args.timeout)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(snapshot, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    _validate_envelope(snapshot)
    result = {
        "snapshot_id": snapshot["snapshot_id"],
        "source": snapshot["source"],
        "calendar_rows": len(snapshot["calendar"]),
        "daily_rows": len(snapshot["daily"]),
        "published": False,
    }
    if args.writer_env:
        config = dotenv_values(args.writer_env)
        writer = SnapshotWriter(
            config["CLICKHOUSE_INGEST_URL"],
            config["CLICKHOUSE_INGEST_USER"],
            config["CLICKHOUSE_INGEST_PASSWORD"],
            config["CLICKHOUSE_INGEST_DATABASE"],
        )
        try:
            writer.initialize()
            result.update(writer.publish(snapshot))
            result["published"] = True
        finally:
            writer.close()
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
