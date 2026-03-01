#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from pathlib import Path

from backtest_lib import SKIP_COMPACTION_FOR, compact_output, parse_thresholds, to_bytes


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backtest context-shield on a single OpenCode session")
    parser.add_argument(
        "--db",
        default=str(Path.home() / ".local/share/opencode/opencode.db"),
        help="Path to opencode.db",
    )
    parser.add_argument("--session", required=True, help="Session ID, eg ses_xxx")
    parser.add_argument(
        "--thresholds",
        default="12,10,8,6,4,3,2",
        help="Compaction thresholds in KB (comma-separated)",
    )
    parser.add_argument("--target-kb", type=float, default=4, help="Target compacted output size in KB")
    parser.add_argument("--json-out", help="Optional path to write JSON report")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    thresholds = parse_thresholds(args.thresholds)
    target_bytes = int(args.target_kb * 1024)

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    session = conn.execute(
        """
        select id, directory, title, time_created
        from session
        where id = ?
        """,
        (args.session,),
    ).fetchone()

    if not session:
        raise SystemExit(f"Session not found: {args.session}")

    rows = conn.execute(
        """
        select data
        from part
        where session_id = ?
        """,
        (args.session,),
    ).fetchall()
    conn.close()

    calls: list[tuple[str, str]] = []
    for row in rows:
        payload = json.loads(row["data"])
        if payload.get("type") != "tool":
            continue
        state = payload.get("state") or {}
        if state.get("status") != "completed":
            continue
        output = state.get("output")
        if not isinstance(output, str):
            continue
        calls.append((payload.get("tool") or "unknown", output))

    if not calls:
        raise SystemExit("No completed tool outputs found in this session")

    original_total = sum(to_bytes(output) for _, output in calls)

    threshold_report: list[dict[str, int | float]] = []
    for threshold in thresholds:
        compacted_calls = 0
        new_total = 0
        for tool, output in calls:
            if tool in SKIP_COMPACTION_FOR:
                new_total += to_bytes(output)
                continue
            _, compacted, _, new_bytes = compact_output(tool, output, threshold, target_bytes)
            new_total += new_bytes
            if compacted:
                compacted_calls += 1

        saved = original_total - new_total
        threshold_report.append(
            {
                "threshold_kb": threshold / 1024,
                "original_bytes": original_total,
                "new_bytes": new_total,
                "saved_bytes": saved,
                "saved_pct": (saved / original_total * 100) if original_total else 0,
                "compacted_calls": compacted_calls,
            },
        )

    # Per-tool report at threshold=8KB or nearest available
    default_threshold = 8 * 1024
    if default_threshold not in thresholds:
        default_threshold = thresholds[0]

    per_tool = defaultdict(lambda: {"calls": 0, "orig": 0, "new": 0, "compacted": 0})
    for tool, output in calls:
        ob = to_bytes(output)
        per_tool[tool]["calls"] += 1
        per_tool[tool]["orig"] += ob
        if tool in SKIP_COMPACTION_FOR:
            per_tool[tool]["new"] += ob
            continue
        _, compacted, _, nb = compact_output(tool, output, default_threshold, target_bytes)
        per_tool[tool]["new"] += nb
        if compacted:
            per_tool[tool]["compacted"] += 1

    print(f"session={session['id']}")
    print(f"directory={session['directory']}")
    print(f"title={session['title']}")
    print(f"tool_calls={len(calls)}")
    print("")
    print("threshold_kb,original_bytes,new_bytes,saved_bytes,saved_pct,compacted_calls")
    for item in threshold_report:
        print(
            f"{item['threshold_kb']:.0f},{item['original_bytes']},{item['new_bytes']},{item['saved_bytes']},{item['saved_pct']:.2f},{item['compacted_calls']}",
        )

    print("")
    print(f"per_tool_at_threshold={default_threshold / 1024:.0f}KB,target={args.target_kb:.0f}KB")
    print("tool,calls,orig_bytes,new_bytes,saved_bytes,saved_pct,compacted_calls")
    for tool, stats in sorted(per_tool.items(), key=lambda item: item[1]["orig"], reverse=True):
        saved = stats["orig"] - stats["new"]
        pct = (saved / stats["orig"] * 100) if stats["orig"] else 0
        print(
            f"{tool},{stats['calls']},{stats['orig']},{stats['new']},{saved},{pct:.2f},{stats['compacted']}",
        )

    if args.json_out:
        report = {
            "session": {
                "id": session["id"],
                "directory": session["directory"],
                "title": session["title"],
            },
            "tool_calls": len(calls),
            "target_kb": args.target_kb,
            "threshold_report": threshold_report,
            "per_tool_default_threshold_kb": default_threshold / 1024,
            "per_tool": {
                tool: {
                    "calls": int(stats["calls"]),
                    "original_bytes": int(stats["orig"]),
                    "new_bytes": int(stats["new"]),
                    "saved_bytes": int(stats["orig"] - stats["new"]),
                    "saved_pct": ((stats["orig"] - stats["new"]) / stats["orig"] * 100)
                    if stats["orig"]
                    else 0,
                    "compacted_calls": int(stats["compacted"]),
                }
                for tool, stats in per_tool.items()
            },
        }
        Path(args.json_out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
