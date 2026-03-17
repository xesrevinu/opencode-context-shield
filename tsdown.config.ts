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
  exports: { all: true },
});
