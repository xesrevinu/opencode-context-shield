import { defineConfig } from "tsdown";

export default defineConfig({
  entry: ["src/index.ts"],
  outDir: "dist",
  platform: "node",
  format: "esm",
  dts: true,
  sourcemap: true,
  clean: true,
  fixedExtension: false,
  ignoreWatch: [
    ".git",
    ".repo",
    ".direnv",
    ".lalph",
    ".codemogger",
    ".specs",
    ".jj",
    "dist",
    "node_modules",
    "bun.lock",
    "flake.lock",
  ],
  exports: { all: true },
});
