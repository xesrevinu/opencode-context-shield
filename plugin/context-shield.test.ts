import { describe, expect, test } from "bun:test";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { ContextShieldPlugin } from "./context-shield";

function largeOutput(): string {
  const lines: string[] = [];
  for (let i = 0; i < 1400; i++) {
    if (i % 200 === 0) lines.push(`ERROR test failure at case ${i}`);
    else if (i % 125 === 0) lines.push(`WARNING deprecated API at block ${i}`);
    else lines.push(`line ${i} some regular log data`);
  }
  return lines.join("\n");
}

async function withPlugin(
  run: (hooks: Awaited<ReturnType<typeof ContextShieldPlugin>>) => Promise<void>,
) {
  const directory = mkdtempSync(path.join(tmpdir(), "context-shield-test-"));
  try {
    const hooks = await ContextShieldPlugin({ directory } as any);
    await run(hooks);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
}

describe("ContextShieldPlugin", () => {
  test("compacts large tool output and emits stats", async () => {
    await withPlugin(async (hooks) => {
      const after = hooks["tool.execute.after"];
      expect(after).toBeDefined();

      const source = largeOutput();
      const payload: { title: string; output: string; metadata: Record<string, unknown> } = {
        title: "",
        output: source,
        metadata: {},
      };

      await after?.(
        {
          tool: "bash",
          sessionID: "session_1",
          callID: "call_1",
          args: {},
        },
        payload,
      );

      expect(payload.output).toContain("[context-shield]");
      expect(payload.output.length).toBeLessThan(source.length);

      const statsTool = hooks.tool?.["cshield_stats"];
      expect(statsTool).toBeDefined();

      const stats = await statsTool!.execute({}, {} as any);
      expect(stats).toContain("Context shield stats");
      expect(stats).toContain("bash");
    });
  });

  test("clamps read limit and keeps read output uncompressed", async () => {
    await withPlugin(async (hooks) => {
      const before = hooks["tool.execute.before"];
      const after = hooks["tool.execute.after"];

      const readArgs: Record<string, unknown> = {
        filePath: "/tmp/demo.ts",
        limit: 5000,
      };

      await before?.(
        {
          tool: "read",
          sessionID: "session_2",
          callID: "call_2",
        },
        { args: readArgs },
      );

      expect(readArgs["limit"]).toBe(800);

      const source = largeOutput();
      const payload: { title: string; output: string; metadata: Record<string, unknown> } = {
        title: "",
        output: source,
        metadata: {},
      };

      await after?.(
        {
          tool: "read",
          sessionID: "session_2",
          callID: "call_3",
          args: {},
        },
        payload,
      );

      expect(payload.output).toBe(source);
    });
  });

  test("injects task routing hint", async () => {
    await withPlugin(async (hooks) => {
      const before = hooks["tool.execute.before"];
      const args: Record<string, unknown> = {
        description: "research request",
        subagent_type: "general",
        prompt: "Investigate current architecture and report findings.",
      };

      await before?.(
        {
          tool: "task",
          sessionID: "session_3",
          callID: "call_4",
        },
        { args },
      );

      expect(args["prompt"]).toBeString();
      expect(String(args["prompt"])).toContain("CONTEXT-SHIELD SUBAGENT ROUTING");
    });
  });

  test("toggle disables compaction", async () => {
    await withPlugin(async (hooks) => {
      const toggleTool = hooks.tool?.["cshield_toggle"];
      expect(toggleTool).toBeDefined();

      await toggleTool!.execute({ enabled: false }, {} as any);

      const after = hooks["tool.execute.after"];
      const source = largeOutput();
      const payload: { title: string; output: string; metadata: Record<string, unknown> } = {
        title: "",
        output: source,
        metadata: {},
      };

      await after?.(
        {
          tool: "bash",
          sessionID: "session_4",
          callID: "call_5",
          args: {},
        },
        payload,
      );

      expect(payload.output).toBe(source);
    });
  });
});
