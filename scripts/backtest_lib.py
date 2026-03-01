#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any

SKIP_COMPACTION_FOR = {"read", "edit", "write", "apply_patch", "multiedit", "lsp"}

SIGNAL_PATTERNS = [
    re.compile(r"(error|failed|exception|traceback|panic|fatal|denied|cannot|timed out|timeout)", re.I),
    re.compile(r"(warn|warning|deprecated)", re.I),
    re.compile(r"(test|passed|failed|skipped|coverage|assert|snapshot)", re.I),
    re.compile(r"(exit code|aborted|killed|terminated)", re.I),
]


@dataclass
class ToolCall:
    session_id: str
    tool: str
    output: str
    input: dict[str, Any]


def to_bytes(value: str) -> int:
    return len(value.encode("utf-8"))


def truncate_to_bytes(text: str, max_bytes: int) -> str:
    if to_bytes(text) <= max_bytes:
        return text

    suffix = "\n...[context-shield truncated summary]"
    low = 0
    high = len(text)

    while low < high:
        mid = (low + high + 1) // 2
        if to_bytes(text[:mid]) <= max_bytes:
            low = mid
        else:
            high = mid - 1

    end = low
    while end > 0 and to_bytes(text[:end] + suffix) > max_bytes:
        end -= 1

    return text[: max(0, end)] + suffix


def collect_signals(lines: list[str], limit: int = 12) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()

    for line in lines:
        trimmed = line.strip()
        if not trimmed:
            continue
        if not any(pattern.search(trimmed) for pattern in SIGNAL_PATTERNS):
            continue

        key = trimmed.lower()
        if key in seen:
            continue
        seen.add(key)

        if len(trimmed) > 220:
            result.append(trimmed[:220] + "...")
        else:
            result.append(trimmed)

        if len(result) >= limit:
            break

    return result


def compact_output(tool: str, output: str, threshold_bytes: int, target_bytes: int) -> tuple[str, bool, int, int]:
    original_bytes = to_bytes(output)
    if original_bytes <= threshold_bytes:
        return output, False, original_bytes, original_bytes

    lines = output.split("\n")
    head = lines[:24]
    tail = lines[max(len(lines) - 24, 0) :]
    omitted = max(len(lines) - len(head) - len(tail), 0)
    signals = collect_signals(lines)

    out: list[str] = [
        "[context-shield] Compacted large tool output to reduce context usage.",
        f"tool: {tool}",
        f"original_size: {original_bytes / 1024:.1f}KB ({len(lines)} lines)",
    ]

    if signals:
        out.append("signals:")
        out.extend(f"- {item}" for item in signals)

    if head:
        out.append("head:")
        out.append("\n".join(head))

    if omitted > 0:
        out.append(f"... {omitted} lines omitted ...")

    if tail:
        out.append("tail:")
        out.append("\n".join(tail))

    out.append("tip: narrow the next tool call (path, query, range, or filters) to avoid noisy output.")

    compacted = truncate_to_bytes("\n".join(out), target_bytes)
    compacted_bytes = to_bytes(compacted)

    if compacted_bytes >= original_bytes:
        return output, False, original_bytes, original_bytes

    return compacted, True, original_bytes, compacted_bytes


def parse_thresholds(raw: str) -> list[int]:
    values: list[int] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        values.append(int(float(chunk) * 1024))
    return values


def load_tool_calls_for_sessions(conn: sqlite3.Connection, session_ids: list[str]) -> dict[str, list[ToolCall]]:
    if not session_ids:
        return {}

    qmarks = ",".join("?" for _ in session_ids)
    rows = conn.execute(
        f"""
        select session_id, data
        from part
        where session_id in ({qmarks})
        """,
        session_ids,
    ).fetchall()

    by_session: dict[str, list[ToolCall]] = {session_id: [] for session_id in session_ids}

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

        input_data = state.get("input") if isinstance(state.get("input"), dict) else {}
        by_session[row["session_id"]].append(
            ToolCall(
                session_id=row["session_id"],
                tool=payload.get("tool") or "unknown",
                output=output,
                input=input_data,
            ),
        )

    return by_session


def estimate_read_limit_savings(calls: list[ToolCall], read_limit: int) -> tuple[int, int, int]:
    orig = 0
    est_new = 0
    affected = 0

    for call in calls:
        if call.tool != "read":
            continue

        output_bytes = to_bytes(call.output)
        orig += output_bytes

        numbered_lines = 0
        for line in call.output.split("\n"):
            if re.match(r"^\d+:\s", line):
                numbered_lines += 1

        limit_arg = call.input.get("limit")
        limit_arg = limit_arg if isinstance(limit_arg, int) else None

        if numbered_lines > read_limit and (limit_arg is None or limit_arg > read_limit):
            ratio = read_limit / numbered_lines
            estimated = max(1, int(output_bytes * ratio))
            est_new += estimated
            affected += 1
        else:
            est_new += output_bytes

    return orig, est_new, affected
