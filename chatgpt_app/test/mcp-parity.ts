import assert from "node:assert/strict";
import { test } from "node:test";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";

const project = resolve(import.meta.dirname, "..");
const candidate = process.env.MCP_CANDIDATE_ROOT;
assert.ok(candidate, "MCP_CANDIDATE_ROOT is required");

async function modules(root: string) {
  return {
    server: await import(pathToFileURL(join(root, "build/server.js")).href),
    data: await import(pathToFileURL(join(root, "build/data.js")).href),
  };
}

function syntheticData(error?: Error) {
  const value = (data: Record<string, unknown>) => {
    if (error) throw error;
    return data;
  };
  return {
    status: () => value({ state: "synthetic" }),
    listRuns: (input: unknown) => value({ runs: [], total: 0, input }),
    runSummary: (runId: string) => value({ runId, status: "complete" }),
    runDetails: (runId: string) => value({ concise: { runId }, detail: { evidence: "synthetic" } }),
    listModels: (input: unknown) => value({ models: [], total: 0, input }),
    model: (modelId: string) => value({ modelId, enabled: true }),
  };
}

async function connect(createServer: (data: any) => any, data: any) {
  const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
  const client = new Client({ name: "mcp-parity", version: "1.0.0" }, { capabilities: {} });
  const server = createServer(data);
  await Promise.all([server.connect(serverTransport), client.connect(clientTransport)]);
  return { client, server };
}

async function comparedCall(left: Client, right: Client, name: string, args: Record<string, unknown>) {
  const settle = async (client: Client) => {
    try { return await client.callTool({ name, arguments: args }); }
    catch (error) { return { thrown: String(error) }; }
  };
  assert.deepEqual(await settle(left), await settle(right), `${name} parity`);
}

test("committed and candidate MCP builds have contract, result, resource, and error parity", async () => {
  const deployed = await modules(project);
  const rebuilt = await modules(candidate);
  const left = await connect(deployed.server.createServer, syntheticData());
  const right = await connect(rebuilt.server.createServer, syntheticData());

  try {
    assert.deepEqual(await left.client.listTools(), await right.client.listTools());
    assert.deepEqual(await left.client.listResources(), await right.client.listResources());
    assert.deepEqual(await left.client.listResourceTemplates(), await right.client.listResourceTemplates());
    assert.deepEqual(await left.client.readResource({ uri: deployed.server.WIDGET_URI }),
      await right.client.readResource({ uri: rebuilt.server.WIDGET_URI }));

    for (const [name, args] of [
      ["get_swarm_status", {}],
      ["list_swarm_runs", { limit: 2, taskMode: "code" }],
      ["get_swarm_run_summary", { runId: "synthetic-run" }],
      ["get_swarm_run_details", { runId: "synthetic-run" }],
      ["list_swarm_models", { enabled: true }],
      ["get_swarm_model", { modelId: "synthetic/model" }],
      ["render_swarm_control", {}],
      ["get_swarm_run_summary", {}],
      ["list_swarm_runs", { limit: "invalid" }],
      ["__phase_0_3_unknown_tool__", {}],
    ] as const) {
      await comparedCall(left.client, right.client, name, args);
    }
  } finally {
    await Promise.all([left.client.close(), right.client.close(), left.server.close(), right.server.close()]);
  }

  for (const failure of ["unavailable", "internal"] as const) {
    const leftError = failure === "unavailable"
      ? new deployed.data.PublicError("Swarm runtime data is unavailable.")
      : new Error("synthetic internal failure");
    const rightError = failure === "unavailable"
      ? new rebuilt.data.PublicError("Swarm runtime data is unavailable.")
      : new Error("synthetic internal failure");
    const failedLeft = await connect(deployed.server.createServer, syntheticData(leftError));
    const failedRight = await connect(rebuilt.server.createServer, syntheticData(rightError));
    try {
      await comparedCall(failedLeft.client, failedRight.client, "get_swarm_status", {});
    } finally {
      await Promise.all([
        failedLeft.client.close(), failedRight.client.close(),
        failedLeft.server.close(), failedRight.server.close(),
      ]);
    }
  }
});
