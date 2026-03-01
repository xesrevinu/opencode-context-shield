# opencode-context-shield

Context reduction plugin for [OpenCode](https://opencode.ai), designed for real-world coding sessions with heavy tool output.

It compacts noisy tool results into deterministic summaries, keeps key error/warning signals, and provides built-in stats so you can measure savings.

## What it does

- Compacts large tool outputs in `tool.execute.after` using a stable template:
  - `signals` (error/warn/test hints)
  - `head` / `tail`
  - omitted-line marker
- Skips compaction for edit/read-sensitive tools by default:
  - `read`, `edit`, `write`, `apply_patch`, `multiedit`, `lsp`
- Adds guardrail: if compacted output is not smaller, keeps original output
- Clamps `read` tool `limit` in `tool.execute.before` (default `800`)
- Injects routing hints into `task` prompts to reduce context bloat from subagents
- Adds two helper tools:
  - `cshield_toggle`
  - `cshield_stats`

## Why this is cache-friendly

Prompt cache pricing for Anthropic/OpenAI is often favorable, so the plugin favors deterministic output:

- No random `full_output_path` in compacted text
- Stable line ordering and section structure
- No timestamps in compacted payload body

This helps avoid unnecessary cache-prefix churn while still shrinking high-noise output.

## Install

### 1) Copy plugin file into your project config

OpenCode auto-loads `*.ts` in `.opencode/plugin/`.

```bash
mkdir -p .opencode/plugin
cp plugin/context-shield.ts .opencode/plugin/context-shield.ts
```

### 2) Ensure plugin package dependency exists

```bash
cd .opencode
bun add @opencode-ai/plugin
```

## Runtime config

Config file is created automatically at:

`<project>/.opencode/state/context-shield.json`

Default values:

```json
{
  "enabled": true,
  "readLimit": 800,
  "compactThresholdBytes": 8192,
  "compactTargetBytes": 4096
}
```

## Validate quickly

Use OpenCode run mode:

```bash
opencode run --format json "Call cshield_stats exactly once, then respond with OK."
```

## Local development

```bash
bun install
bun run typecheck
bun test
```

## Backtest scripts

These scripts evaluate expected context savings against your real `opencode.db` history.

### Single session sweep

```bash
python3 scripts/backtest-session.py \
  --session ses_371939900ffeZgZksolV6nC1A1 \
  --thresholds 12,10,8,6,4,3,2 \
  --target-kb 4
```

### Batch sweep across real sessions

```bash
python3 scripts/backtest-batch.py \
  --min-tool-bytes 30000 \
  --limit 80 \
  --thresholds 12,10,8,6,4,3,2 \
  --target-kb 4 \
  --target-sweep 6,4,3,2 \
  --read-limits 800,600,400,200
```

Both scripts support `--json-out <file>` for machine-readable reports.

## Notes

- Backtest is a replay-style estimate from stored tool outputs, not provider-side token accounting.
- For final decisions, compare A/B runs in live sessions (`tokens.input`, cache read/write, cost).
