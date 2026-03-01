#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from pathlib import Path

from backtest_lib import (
    SKIP_COMPACTION_FOR,
    compact_output,
    estimate_read_limit_savings,
    load_tool_calls_for_sessions,
    parse_thresholds,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Batch backtest context-shield across real OpenCode sessions")
    parser.add_argument(
        "--db",
        default=str(Path.home() / ".local/share/opencode/opencode.db"),
        help="Path to opencode.db",
    )
    parser.add_argument(
        "--min-tool-bytes",
        type=int,
        default=30000,
        help="Only include sessions with tool output bytes above this threshold",
    )
    parser.add_argument("--limit", type=int, default=80, help="Maximum sessions to analyze")
    parser.add_argument(
        "--thresholds",
        default="12,10,8,6,4,3,2",
        help="Compaction thresholds in KB (comma-separated)",
    )
    parser.add_argument("--target-kb", type=float, default=4, help="Compaction target size in KB")
    parser.add_argument(
        "--target-sweep",
        default="6,4,3,2",
        help="Target sweep in KB while fixing threshold to 8KB",
    )
    parser.add_argument(
        "--read-limits",
        default="800,600,400,200",
        help="Read limit values for rough read-savings estimate",
    )
    parser.add_argument("--json-out", help="Optional path to write JSON report")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    thresholds = parse_thresholds(args.thresholds)
    target_bytes = int(args.target_kb * 1024)
    target_sweep = parse_thresholds(args.target_sweep)
    read_limits = [int(float(item.strip())) for item in args.read_limits.split(",") if item.strip()]

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    sessions = conn.execute(
        """
        with tool_bytes as (
          select session_id, sum(length(json_extract(data,'$.state.output'))) as tool_output_bytes
          from part
          where json_extract(data,'$.type')='tool'
            and json_extract(data,'$.state.status')='completed'
          group by session_id
        )
        select s.id, s.directory, s.time_created, tb.tool_output_bytes
        from session s
        join tool_bytes tb on tb.session_id=s.id
        where tb.tool_output_bytes > ?
        order by s.time_created desc
        limit ?
        """,
        (args.min_tool_bytes, args.limit),
    ).fetchall()

    session_ids = [row["id"] for row in sessions]
    by_session = load_tool_calls_for_sessions(conn, session_ids)
    conn.close()

    if not sessions:
        raise SystemExit("No sessions matched filters")

    aggregate = {
        threshold: {
            "original_bytes": 0,
            "new_bytes": 0,
            "compacted_calls": 0,
            "sessions_impacted": 0,
            "total_calls": 0,
        }
        for threshold in thresholds
    }

    per_session: dict[int, dict[str, tuple[int, int, int]]] = {threshold: {} for threshold in thresholds}

    for row in sessions:
        sid = row["id"]
        calls = by_session.get(sid, [])
        for threshold in thresholds:
            original_total = 0
            new_total = 0
            compacted_calls = 0

            for call in calls:
                _, _, original_bytes, new_bytes = compact_output(
                    call.tool,
                    call.output,
                    threshold,
                    target_bytes,
                )
                original_total += original_bytes

                if call.tool in SKIP_COMPACTION_FOR:
                    new_total += original_bytes
                else:
                    new_total += new_bytes
                    if new_bytes < original_bytes:
                        compacted_calls += 1

            aggregate[threshold]["original_bytes"] += original_total
            aggregate[threshold]["new_bytes"] += new_total
            aggregate[threshold]["compacted_calls"] += compacted_calls
            aggregate[threshold]["total_calls"] += len(calls)
            if new_total < original_total:
                aggregate[threshold]["sessions_impacted"] += 1

            per_session[threshold][sid] = (original_total, new_total, compacted_calls)

    # target sweep with fixed threshold of 8KB (or closest threshold)
    fixed_threshold = 8 * 1024 if 8 * 1024 in thresholds else thresholds[0]
    aggregate_target = {
        target: {"original_bytes": 0, "new_bytes": 0, "compacted_calls": 0}
        for target in target_sweep
    }
    for row in sessions:
        sid = row["id"]
        calls = by_session.get(sid, [])
        for target in target_sweep:
            original_total = 0
            new_total = 0
            compacted_calls = 0
            for call in calls:
                _, _, original_bytes, new_bytes = compact_output(
                    call.tool,
                    call.output,
                    fixed_threshold,
                    target,
                )
                original_total += original_bytes
                if call.tool in SKIP_COMPACTION_FOR:
                    new_total += original_bytes
                else:
                    new_total += new_bytes
                    if new_bytes < original_bytes:
                        compacted_calls += 1

            aggregate_target[target]["original_bytes"] += original_total
            aggregate_target[target]["new_bytes"] += new_total
            aggregate_target[target]["compacted_calls"] += compacted_calls

    per_tool = defaultdict(
        lambda: {"calls": 0, "original_bytes": 0, "new_bytes": 0, "compacted_calls": 0},
    )
    for calls in by_session.values():
        for call in calls:
            _, _, original_bytes, new_bytes = compact_output(
                call.tool,
                call.output,
                fixed_threshold,
                target_bytes,
            )
            per_tool[call.tool]["calls"] += 1
            per_tool[call.tool]["original_bytes"] += original_bytes

            if call.tool in SKIP_COMPACTION_FOR:
                per_tool[call.tool]["new_bytes"] += original_bytes
            else:
                per_tool[call.tool]["new_bytes"] += new_bytes
                if new_bytes < original_bytes:
                    per_tool[call.tool]["compacted_calls"] += 1

    all_calls = [call for calls in by_session.values() for call in calls]
    read_estimates = {}
    for limit in read_limits:
        original, estimated_new, affected = estimate_read_limit_savings(all_calls, limit)
        saved = original - estimated_new
        read_estimates[limit] = {
            "original_bytes": original,
            "estimated_new_bytes": estimated_new,
            "estimated_saved_bytes": saved,
            "estimated_saved_pct": (saved / original * 100) if original else 0,
            "affected_calls": affected,
        }

    print(f"sessions_analyzed={len(sessions)}")
    print(f"tool_parts_total={sum(len(calls) for calls in by_session.values())}")
    print("")

    print(f"=== Threshold Sweep (target={args.target_kb:.0f}KB) ===")
    print("threshold_kb,original_bytes,new_bytes,saved_bytes,saved_pct,compacted_calls,sessions_impacted")
    for threshold in thresholds:
        item = aggregate[threshold]
        saved = item["original_bytes"] - item["new_bytes"]
        saved_pct = (saved / item["original_bytes"] * 100) if item["original_bytes"] else 0
        print(
            f"{threshold / 1024:.0f},{item['original_bytes']},{item['new_bytes']},{saved},{saved_pct:.2f},{item['compacted_calls']},{item['sessions_impacted']}",
        )

    print("")
    print(f"=== Target Sweep (threshold={fixed_threshold / 1024:.0f}KB) ===")
    print("target_kb,original_bytes,new_bytes,saved_bytes,saved_pct,compacted_calls")
    for target in target_sweep:
        item = aggregate_target[target]
        saved = item["original_bytes"] - item["new_bytes"]
        saved_pct = (saved / item["original_bytes"] * 100) if item["original_bytes"] else 0
        print(
            f"{target / 1024:.0f},{item['original_bytes']},{item['new_bytes']},{saved},{saved_pct:.2f},{item['compacted_calls']}",
        )

    print("")
    print("=== Read Limit Estimate (rough, proportional) ===")
    print("read_limit,original_bytes,estimated_new_bytes,estimated_saved_bytes,estimated_saved_pct,affected_calls")
    for limit in read_limits:
        item = read_estimates[limit]
        print(
            f"{limit},{item['original_bytes']},{item['estimated_new_bytes']},{item['estimated_saved_bytes']},{item['estimated_saved_pct']:.2f},{item['affected_calls']}",
        )

    print("")
    print(f"=== Top Session Savings @{fixed_threshold / 1024:.0f}KB/{args.target_kb:.0f}KB ===")
    print("saved_bytes,saved_pct,compacted_calls,session_id,directory")
    rows = []
    for row in sessions:
        sid = row["id"]
        original, new, compacted = per_session[fixed_threshold][sid]
        saved = original - new
        saved_pct = (saved / original * 100) if original else 0
        rows.append((saved, saved_pct, compacted, sid, row["directory"]))

    rows.sort(reverse=True)
    for saved, saved_pct, compacted, sid, directory in rows[:20]:
        print(f"{saved},{saved_pct:.2f},{compacted},{sid},{directory}")

    print("")
    print(f"=== Per Tool @{fixed_threshold / 1024:.0f}KB/{args.target_kb:.0f}KB ===")
    print("tool,calls,original_bytes,new_bytes,saved_bytes,saved_pct,compacted_calls")
    for tool, item in sorted(per_tool.items(), key=lambda x: x[1]["original_bytes"], reverse=True):
        saved = item["original_bytes"] - item["new_bytes"]
        saved_pct = (saved / item["original_bytes"] * 100) if item["original_bytes"] else 0
        print(
            f"{tool},{item['calls']},{item['original_bytes']},{item['new_bytes']},{saved},{saved_pct:.2f},{item['compacted_calls']}",
        )

    if args.json_out:
        report = {
            "sessions_analyzed": len(sessions),
            "tool_parts_total": sum(len(calls) for calls in by_session.values()),
            "threshold_sweep": {
                threshold: {
                    **aggregate[threshold],
                    "saved_bytes": aggregate[threshold]["original_bytes"] - aggregate[threshold]["new_bytes"],
                    "saved_pct": (
                        (
                            aggregate[threshold]["original_bytes"]
                            - aggregate[threshold]["new_bytes"]
                        )
                        / aggregate[threshold]["original_bytes"]
                        * 100
                    )
                    if aggregate[threshold]["original_bytes"]
                    else 0,
                }
                for threshold in thresholds
            },
            "target_sweep": {
                target: {
                    **aggregate_target[target],
                    "saved_bytes": aggregate_target[target]["original_bytes"]
                    - aggregate_target[target]["new_bytes"],
                    "saved_pct": (
                        (
                            aggregate_target[target]["original_bytes"]
                            - aggregate_target[target]["new_bytes"]
                        )
                        / aggregate_target[target]["original_bytes"]
                        * 100
                    )
                    if aggregate_target[target]["original_bytes"]
                    else 0,
                }
                for target in target_sweep
            },
            "read_limit_estimates": read_estimates,
            "per_tool": per_tool,
        }
        Path(args.json_out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
