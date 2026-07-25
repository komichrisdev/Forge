import { spawnSync } from "node:child_process";
import { copyFileSync, mkdtempSync, readdirSync, readFileSync, rmSync, statSync, symlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { basename, dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const temp = mkdtempSync(join(tmpdir(), "owui-swarm-mcp-build-"));
copyFileSync(join(root, "package.json"), join(temp, "package.json"));
symlinkSync(join(root, "node_modules"), join(temp, "node_modules"), "dir");

function run(command, args, env = process.env) {
  const result = spawnSync(command, args, { cwd: root, env, stdio: "inherit" });
  if (result.status !== 0) process.exitCode = result.status ?? 1;
  return result.status === 0;
}

function files(directory, base = directory) {
  const found = [];
  for (const entry of readdirSync(directory, { withFileTypes: true })) {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) found.push(...files(path, base));
    else if (entry.isFile()) found.push(relative(base, path));
  }
  return found;
}

function compare(expected, actual) {
  const names = new Set([...files(expected), ...files(actual)]);
  return [...names].sort().filter((name) => {
    const left = join(expected, name);
    const right = join(actual, name);
    try {
      if (name.endsWith(".js.map")) {
        const sourceMap = (path) => {
          const value = JSON.parse(readFileSync(path, "utf8"));
          value.sources = value.sources.map((source) => `../src/${basename(source)}`);
          return JSON.stringify(value);
        };
        return sourceMap(left) !== sourceMap(right);
      }
      return statSync(left).size !== statSync(right).size || !readFileSync(left).equals(readFileSync(right));
    } catch {
      return true;
    }
  });
}

try {
  const build = join(temp, "build");
  const dist = join(temp, "dist");
  if (!run(join(root, "node_modules/.bin/tsc"), ["-p", "tsconfig.server.json", "--outDir", build]) ||
      !run(join(root, "node_modules/.bin/vite"), ["build", "--outDir", dist, "--emptyOutDir"])) {
    process.exitCode = 1;
  } else if (process.argv[2] === "parity") {
    run(process.execPath, ["--test", "--import", "tsx", "test/mcp-parity.ts"], {
      ...process.env,
      MCP_CANDIDATE_ROOT: temp,
    });
  } else {
    const changed = [
      ...compare(join(root, "build"), build).map((name) => `build/${name}`),
      ...compare(join(root, "dist"), dist).map((name) => `dist/${name}`),
    ];
    if (changed.length) {
      console.error(`MCP generated output differs:\n${changed.join("\n")}`);
      process.exitCode = 1;
    } else {
      console.log("MCP generated output matches committed build/ and dist/.");
    }
  }
} finally {
  rmSync(temp, { recursive: true, force: true });
}
