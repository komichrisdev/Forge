import assert from "node:assert/strict";
import { after, before, describe, test } from "node:test";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import Database from "better-sqlite3";
import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { redact, SwarmData } from "../src/data.js";
import { startHttpServer } from "../src/main.js";
import { createServer, WIDGET_URI } from "../src/server.js";

type Json = Record<string, any>;
const contractFixture = JSON.parse(readFileSync(new URL("./fixtures/mcp-live-contract.json", import.meta.url), "utf8")) as Json;
const temp = mkdtempSync(join(tmpdir(), "swarm-control-test-"));
const dbPath = join(temp, "catalog.sqlite3");
const runRoot = join(temp, "runs");
let data: SwarmData;

function json(path: string, value: unknown): void { writeFileSync(path, JSON.stringify(value, null, 2)); }
function normalized(value: any): any {
  if (Array.isArray(value)) return value.map(normalized);
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.keys(value).sort().map((key) => [key, normalized(value[key])]));
  }
  return value;
}

function createFixture(): void {
  mkdirSync(runRoot, { recursive: true });
  const db = new Database(dbPath);
  db.exec(`
    CREATE TABLE models(model_id TEXT PRIMARY KEY,provider TEXT NOT NULL DEFAULT '',family TEXT NOT NULL DEFAULT '',kind TEXT NOT NULL,capabilities TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 1,available INTEGER NOT NULL DEFAULT 1,context_length INTEGER,quality INTEGER NOT NULL DEFAULT 0,speed INTEGER NOT NULL DEFAULT 0,notes TEXT NOT NULL DEFAULT '',last_seen TEXT NOT NULL DEFAULT '',probe_status TEXT NOT NULL DEFAULT 'untested',probe_ms INTEGER,probe_error TEXT NOT NULL DEFAULT '',last_probe TEXT NOT NULL DEFAULT '',last_successful_probe TEXT NOT NULL DEFAULT '',last_failure TEXT NOT NULL DEFAULT '');
    CREATE TABLE task_attempts(id INTEGER PRIMARY KEY,run_id TEXT NOT NULL,model_id TEXT NOT NULL,role TEXT NOT NULL,mode TEXT NOT NULL,attempted_at TEXT NOT NULL,status TEXT NOT NULL,elapsed_ms INTEGER NOT NULL,retry_count INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE probe_history(id INTEGER PRIMARY KEY,model_id TEXT,probed_at TEXT,status TEXT,elapsed_ms INTEGER,error TEXT);
    CREATE TABLE quality_events(id INTEGER PRIMARY KEY,run_id TEXT NOT NULL,model_id TEXT NOT NULL,role TEXT NOT NULL,mode TEXT NOT NULL,category TEXT NOT NULL,severity INTEGER NOT NULL,judge_caught INTEGER NOT NULL DEFAULT 0,reached_final INTEGER NOT NULL DEFAULT 0,codex_verified INTEGER NOT NULL DEFAULT 0,note TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL);
    CREATE TABLE benchmark_results(id INTEGER PRIMARY KEY,benchmark_id TEXT NOT NULL,benchmark_version INTEGER NOT NULL DEFAULT 1,run_id TEXT NOT NULL,model_id TEXT NOT NULL,role TEXT NOT NULL,mode TEXT NOT NULL,response_path TEXT NOT NULL DEFAULT '',checks TEXT NOT NULL DEFAULT '{}',dimensions TEXT NOT NULL DEFAULT '{}',evaluator_source TEXT NOT NULL,note TEXT NOT NULL DEFAULT '',evaluated_at TEXT NOT NULL);
  `);
  const model = db.prepare("INSERT INTO models(model_id,provider,family,kind,capabilities,enabled,available,quality,speed,notes,last_seen,probe_status,probe_ms,last_successful_probe) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)");
  model.run("vendor/code-model", "vendor", "vendor/code", "chat", '["chat","code"]', 1, 1, 8, 7, "Strong implementation model", "2026-07-18T22:00:00+00:00", "healthy", 120, "2026-07-18T22:00:00+00:00");
  model.run("vendor/embed-model", "vendor", "vendor/embed", "embedding", '["embedding"]', 0, 1, 0, 9, "", "2026-07-18T22:00:00+00:00", "untested", null, "");
  const attempt = db.prepare("INSERT INTO task_attempts VALUES(NULL,?,?,?,?,?,?,?,?)");
  attempt.run("run-complete", "vendor/code-model", "implementer", "code", "2026-07-18T22:01:00+00:00", "success", 1000, 0);
  attempt.run("run-failed", "vendor/code-model", "implementer", "code", "2026-07-18T22:02:00+00:00", "timeout", 2000, 0);
  db.prepare("INSERT INTO quality_events VALUES(NULL,?,?,?,?,?,?,?,?,?,?,?)").run("run-complete", "vendor/code-model", "implementer", "code", "clean_candidate", 0, 0, 0, 1, "No secret", "2026-07-18T22:03:00+00:00");
  db.prepare("INSERT INTO benchmark_results VALUES(NULL,?,?,?,?,?,?,?,?,?,?,?,?)").run("retry-helper", 1, "bench-1", "vendor/code-model", "implementer", "code", "", '{"passed":true}', '{"correctness":2,"role_usefulness":2}', "codex_review", "Good", "2026-07-18T22:04:00+00:00");
  db.close();

  const makeRun = (id: string, status: "complete" | "failed" | "running") => {
    const dir = join(runRoot, id); for (const sub of ["context", "prompts", "workers", "judge"]) mkdirSync(join(dir, sub), { recursive: true });
    const task = {
      run_id: id, objective: `Objective ${id} API_KEY=topsecret`, mode: id === "run-running" ? "general" : "code", acceptance: "Stay read only",
      context_manifest: [{ label: "input", original_chars: 10, sent_chars: 5, omitted_chars: 5 }],
      workers: [{ name: "implementer", model: "vendor/code-model", selection_reason: "Automatic: healthy; role quality 10." }],
      judge: { model: "vendor/code-model", selection_reason: "Explicit override." },
    };
    json(join(dir, "task.json"), task); json(join(dir, "context/manifest.json"), task.context_manifest); writeFileSync(join(dir, "context/sent.txt"), "Authorization: Bearer abcdefghijklmnop");
    const events = [
      { time: "2026-07-18T22:00:00+00:00", event: "run_created", run_id: id },
      { time: "2026-07-18T22:00:01+00:00", event: "worker_sent", agent: "implementer", model: "vendor/code-model", timeout_seconds: 240 },
      status === "failed" ? { time: "2026-07-18T22:00:03+00:00", event: "worker_failed", agent: "implementer", model: "vendor/code-model", failure_category: "timeout", duration_ms: 2000 } : { time: "2026-07-18T22:00:02+00:00", event: "worker_returned", agent: "implementer", model: "vendor/code-model", duration_ms: 1000 },
      ...(status === "complete" ? [{ time: "2026-07-18T22:00:04+00:00", event: "run_complete", run_id: id }] : status === "failed" ? [{ time: "2026-07-18T22:00:04+00:00", event: "run_failed", run_id: id }] : []),
    ];
    writeFileSync(join(dir, "events.jsonl"), events.map((event) => JSON.stringify(event)).join("\n"));
    writeFileSync(join(dir, "prompts/worker-implementer.txt"), "PASSWORD=hunter2\nPrompt");
    writeFileSync(join(dir, "workers/implementer.md"), `${"x".repeat(300_000)}\nCookie: session=secret`);
    writeFileSync(join(dir, "judge/response.md"), "Judge response");
    if (status === "complete") { json(join(dir, "final.json"), { answer: "Final synthesis", confidence: 0.8, verification: ["Inspect output"], confidence_reasons: ["Evidence"] }); writeFileSync(join(dir, "final.md"), "Final synthesis"); }
    if (status === "failed") writeFileSync(join(dir, "failure.txt"), "Failure without secret");
  };
  makeRun("run-complete", "complete"); makeRun("run-failed", "failed"); makeRun("run-running", "running");
  data = new SwarmData(dbPath, runRoot);
}

before(createFixture);
after(() => rmSync(temp, { recursive: true, force: true }));

describe("read-only data boundary", () => {
  test("status summarizes reliability and quality", () => {
    const value = data.status();
    assert.equal(value.modelCount, 2); assert.equal(value.activeRunCount, 1);
    assert.deepEqual(value.reliability, { evidenceCount: 2, successCount: 1, timeoutCount: 1 });
    assert.deepEqual(value.qualityEvidence, { eventCount: 1, benchmarkCount: 1, available: true });
  });

  test("run filtering and bounded pagination work", () => {
    const complete = data.listRuns({ status: "complete", mode: "code", limit: 1, offset: 0 });
    assert.equal(complete.total, 1); assert.equal((complete.runs as Json[])[0].runId, "run-complete");
    assert.equal(data.listRuns({ model: "vendor/code-model" }).total, 3);
    assert.equal(data.listRuns({ model: "' OR 1=1 --" }).total, 0);
    assert.equal(data.listRuns({ dateFrom: "2026-07-18T21:00:00+00:00", dateTo: "2026-07-18T23:00:00+00:00" }).total, 3);
  });

  test("summary is concise and details are metadata-ready, redacted, and truncated", () => {
    const summary = data.runSummary("run-complete");
    assert.equal(summary.status, "complete"); assert.equal(summary.confidence, 0.8);
    assert.ok(!JSON.stringify(summary).includes("topsecret"));
    const details = data.runDetails("run-complete"); const serialized = JSON.stringify(details.detail);
    assert.ok(!serialized.includes("hunter2")); assert.ok(!serialized.includes("session=secret"));
    assert.equal((details.detail.workerCards as Json[])[0].response.truncated, true);
    assert.ok(Buffer.byteLength(serialized) < 700_000);
  });

  test("invalid, missing, and traversal run IDs are rejected", () => {
    for (const id of ["../environment", "..", "/etc/passwd", "missing"]) assert.throws(() => data.runSummary(id), /Invalid run ID|Run not found/);
  });

  test("model listing filters and exact detail work", () => {
    assert.equal(data.listModels({ enabled: true, chatCompatible: true }).total, 1);
    assert.equal(data.listModels({ search: "%' OR 1=1 --" }).total, 0);
    const model = data.model("vendor/code-model"); assert.equal(model.modelId, "vendor/code-model");
    assert.equal((model.quality as Json).evidenceCount, 2); assert.equal((model.reliability as Json).timeoutRate, 0.5);
    assert.throws(() => data.model("missing"), /Model not found/);
  });

  test("credential patterns are redacted without changing stored files", () => {
    const source = "Authorization: Bearer secret\nOPEN_WEBUI_API_KEY=abc123\neyJabcdefgh.abcdefgh.abcdefgh\nCookie: a=b\nsk-live_12345678901234567890";
    const safe = String(redact(source)); assert.ok(!safe.includes("secret")); assert.ok(!safe.includes("abc123")); assert.ok(!safe.includes("session=secret"));
    assert.ok(readFileSync(join(runRoot, "run-complete", "prompts/worker-implementer.txt"), "utf8").includes("hunter2"));
  });

  test("unavailable database and run directory fail safely", () => {
    assert.throws(() => new SwarmData(join(temp, "missing.db"), runRoot).status(), /catalog is unavailable/);
    assert.throws(() => new SwarmData(dbPath, join(temp, "missing-runs")).listRuns(), /run directory is unavailable/);
  });
});

describe("MCP protocol and widget", () => {
  let client: Client;
  before(async () => {
    const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
    client = new Client({ name: "swarm-control-tests", version: "1.0.0" }, { capabilities: {} });
    await Promise.all([createServer(data).connect(serverTransport), client.connect(clientTransport)]);
  });

  test("initialization lists only seven read-only tools with schemas", async () => {
    const listed = await client.listTools();
    assert.deepEqual(listed.tools.map((tool) => tool.name).sort(), ["get_swarm_model", "get_swarm_run_details", "get_swarm_run_summary", "get_swarm_status", "list_swarm_models", "list_swarm_runs", "render_swarm_control"]);
    for (const tool of listed.tools) {
      assert.equal(tool.annotations?.readOnlyHint, true); assert.equal(tool.annotations?.destructiveHint, false); assert.equal(tool.annotations?.openWorldHint, false);
      assert.equal(tool.inputSchema.type, "object"); assert.equal(tool.outputSchema?.type, "object");
      assert.ok(!/(start|cancel|probe|update|delete|restart|execute|write)/i.test(tool.name));
    }
  });

  test("public MCP contract matches the live baseline fixture", async () => {
    const tools = (await client.listTools()).tools.sort((left, right) => left.name.localeCompare(right.name));
    const resources = (await client.listResources()).resources.sort((left, right) => left.uri.localeCompare(right.uri));
    const resourceTemplates = (await client.listResourceTemplates()).resourceTemplates;
    const { capturedTransport, errorShapes, ...expected } = contractFixture;
    assert.equal(capturedTransport, "streamable-http");
    assert.deepEqual(normalized({
      serverInfo: client.getServerVersion(),
      capabilities: client.getServerCapabilities(),
      tools,
      resources,
      resourceTemplates,
    }), normalized(expected));

    for (const [name, args, expectedError] of [
      ["__phase_0_3_unknown_tool__", {}, errorShapes.unknownTool],
      ["get_swarm_run_summary", {}, errorShapes.missingRequiredField],
    ] as const) {
      const response = await client.callTool({ name, arguments: args }) as Json;
      assert.equal(response.isError, expectedError.isError);
      assert.match(String((response.content[0] as Json).text), new RegExp(String(expectedError.code)));
    }
  });

  test("resource list and read use the MCP Apps MIME/profile and strict CSP", async () => {
    const listed = await client.listResources(); const resource = listed.resources.find((item) => item.uri === WIDGET_URI);
    assert.equal(resource?.mimeType, "text/html;profile=mcp-app");
    const read = await client.readResource({ uri: WIDGET_URI }); const html = String((read.contents[0] as Json).text);
    assert.match(html, /Swarm Control/); assert.match(html, /Content-Security-Policy/); assert.doesNotMatch(html, /127\.0\.0\.1:8787|<iframe/i);
    assert.match(html, /callServerTool/); assert.match(html, /render_swarm_control/); assert.match(html, /get_swarm_run_details/);
  });

  test("all tools return schema-valid concise data and details use metadata", async () => {
    for (const [name, args] of [
      ["get_swarm_status", {}], ["list_swarm_runs", { limit: 1 }], ["get_swarm_run_summary", { runId: "run-complete" }],
      ["list_swarm_models", { enabled: true }], ["get_swarm_model", { modelId: "vendor/code-model" }], ["render_swarm_control", {}],
    ] as const) {
      const response = await client.callTool({ name, arguments: args }); assert.equal(response.isError, undefined); assert.ok((response.structuredContent as Json).data);
    }
    const detail = await client.callTool({ name: "get_swarm_run_details", arguments: { runId: "run-complete" } });
    assert.ok((detail.structuredContent as Json).data); assert.ok((detail._meta as Json).swarmControl.detail); assert.ok(JSON.stringify(detail.structuredContent).length < JSON.stringify(detail._meta).length);
  });

  test("missing, malformed, injection, and traversal calls fail safely", async () => {
    for (const call of [
      { name: "get_swarm_run_summary", arguments: { runId: "missing" } },
      { name: "get_swarm_run_details", arguments: { runId: "../environment" } },
      { name: "get_swarm_model", arguments: { modelId: "missing" } },
      { name: "list_swarm_runs", arguments: { limit: 1000 } },
      { name: "list_swarm_models", arguments: { search: "' OR 1=1 --" } },
    ]) {
      try {
        const response = await client.callTool(call);
        if (call.name === "list_swarm_models") assert.equal(((response.structuredContent as Json).data as Json).total, 0); else assert.equal(response.isError, true);
      } catch (error) { assert.match(String(error), /validation|invalid|too_big|maximum/i); }
    }
  });
});

test("streamable HTTP initializes at /mcp", { skip: process.env.RUN_HTTP_INTEGRATION !== "1" }, async () => {
  const http = startHttpServer(0);
  await new Promise<void>((resolveReady) => http.listening ? resolveReady() : http.once("listening", resolveReady));
  const address = http.address(); assert.ok(address && typeof address === "object");
  const response = await fetch(`http://127.0.0.1:${address.port}/mcp`, {
    method: "POST",
    headers: { "content-type": "application/json", accept: "application/json, text/event-stream" },
    body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "initialize", params: { protocolVersion: "2025-11-25", capabilities: {}, clientInfo: { name: "http-test", version: "1.0.0" } } }),
  });
  assert.equal(response.status, 200);
  const initialized = await response.json() as Json;
  assert.equal(initialized.result.serverInfo.name, "Swarm Control");
  http.closeAllConnections(); await new Promise<void>((resolveClose) => http.close(() => resolveClose()));
});
