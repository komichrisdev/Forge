import { registerAppResource, registerAppTool, RESOURCE_MIME_TYPE } from "@modelcontextprotocol/ext-apps/server";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { z } from "zod";
import { PublicError, SwarmData, WikiData } from "./data.js";
export const WIDGET_URI = "ui://swarm-control/v1/widget.html";
const annotations = { readOnlyHint: true, destructiveHint: false, openWorldHint: false };
const toolMeta = { ui: { resourceUri: WIDGET_URI, visibility: ["model", "app"] } };
const outputSchema = { data: z.record(z.string(), z.unknown()) };
const page = {
    limit: z.number().int().min(1).max(100).optional(),
    offset: z.number().int().min(0).max(100_000).optional(),
};
const wikiId = z.string().regex(/^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$/).max(200);
const wikiSearch = {
    query: z.string().min(1).max(300),
    limit: z.number().int().min(1).max(100).optional(),
    verification: z.enum(["unverified", "verified", "conflicted", "superseded"]).optional(),
    minConfidence: z.number().int().min(0).max(100).optional(),
    jiraKey: z.string().max(32).optional(),
};
function result(data, detail) {
    return {
        content: [{ type: "text", text: JSON.stringify(data) }],
        structuredContent: { data },
        ...(detail === undefined ? {} : { _meta: { swarmControl: detail } }),
    };
}
function safe(callback) {
    try {
        return callback();
    }
    catch (error) {
        const message = error instanceof PublicError ? error.message : "Swarm data could not be read.";
        return { content: [{ type: "text", text: message }], isError: true };
    }
}
async function safeAsync(callback, message = "Swarm data could not be read.") {
    try {
        return await callback();
    }
    catch (error) {
        const detail = error instanceof PublicError ? error.message : message;
        return { content: [{ type: "text", text: detail }], isError: true };
    }
}
export function createServer(data = new SwarmData(), wiki = new WikiData()) {
    const server = new McpServer({ name: "Swarm Control", version: "1.0.0" });
    registerAppTool(server, "get_swarm_status", {
        title: "Get swarm status", description: "Read a concise snapshot of swarm runs, reliability, and quality evidence.",
        inputSchema: {}, outputSchema, annotations, _meta: toolMeta,
    }, async () => safe(() => result(data.status())));
    registerAppTool(server, "list_swarm_runs", {
        title: "List swarm runs", description: "Read concise run summaries with bounded filtering and pagination.",
        inputSchema: {
            ...page,
            status: z.enum(["running", "complete", "failed"]).optional(),
            taskMode: z.enum(["auto", "code", "spec", "research", "general"]).optional(),
            model: z.string().max(300).optional(),
            dateFrom: z.string().datetime({ offset: true }).optional(),
            dateTo: z.string().datetime({ offset: true }).optional(),
        }, outputSchema, annotations, _meta: toolMeta,
    }, async (input) => safe(() => result(data.listRuns({ ...input, mode: input.taskMode }))));
    registerAppTool(server, "get_swarm_run_summary", {
        title: "Get swarm run summary", description: "Read a concise summary for one validated run ID.",
        inputSchema: { runId: z.string().min(1).max(128) }, outputSchema, annotations, _meta: toolMeta,
    }, async ({ runId }) => safe(() => result(data.runSummary(runId))));
    registerAppTool(server, "get_swarm_run_details", {
        title: "Get swarm run details", description: "Read detailed, bounded run evidence; large widget-only data is returned in result metadata.",
        inputSchema: { runId: z.string().min(1).max(128) }, outputSchema, annotations, _meta: toolMeta,
    }, async ({ runId }) => safe(() => {
        const value = data.runDetails(runId);
        return result(value.concise, { detail: value.detail });
    }));
    registerAppTool(server, "list_swarm_models", {
        title: "List swarm models", description: "Read the model catalog with bounded search, filtering, and pagination.",
        inputSchema: {
            ...page,
            search: z.string().max(200).optional(), enabled: z.boolean().optional(),
            chatCompatible: z.boolean().optional(), family: z.string().max(200).optional(),
            recommendedRole: z.enum(["planner", "implementer", "critic", "verifier", "__judge__"]).optional(),
        }, outputSchema, annotations, _meta: toolMeta,
    }, async (input) => safe(() => result(data.listModels(input))));
    registerAppTool(server, "get_swarm_model", {
        title: "Get swarm model", description: "Read reliability, quality, role, and benchmark evidence for one exact model ID.",
        inputSchema: { modelId: z.string().min(1).max(300) }, outputSchema, annotations, _meta: toolMeta,
    }, async ({ modelId }) => safe(() => result(data.model(modelId))));
    registerAppTool(server, "wiki.search", {
        title: "Search wiki", description: "Read the validated wiki search index with bounded filters and deterministic ranking.",
        inputSchema: wikiSearch, outputSchema, annotations, _meta: toolMeta,
    }, async (input) => safeAsync(async () => result(await wiki.search(input)), "Wiki data could not be read."));
    registerAppTool(server, "wiki.page", {
        title: "Get wiki page", description: "Read one exact canonical wiki page by page ID or slug, including sources and relationships.",
        inputSchema: {
            pageId: wikiId.optional(),
            slug: wikiId.optional(),
        }, outputSchema, annotations, _meta: toolMeta,
    }, async ({ pageId, slug }) => safeAsync(async () => {
        if (Boolean(pageId) === Boolean(slug))
            throw new PublicError("Provide exactly one of pageId or slug.");
        return result(await wiki.page({ pageId, slug }));
    }, "Wiki data could not be read."));
    registerAppTool(server, "wiki.related", {
        title: "Get related wiki pages", description: "Read related canonical wiki pages ranked by the existing derived relationship logic.",
        inputSchema: { pageId: wikiId, limit: z.number().int().min(1).max(100).optional() }, outputSchema, annotations, _meta: toolMeta,
    }, async ({ pageId, limit }) => safeAsync(async () => result(await wiki.related({ pageId, limit })), "Wiki data could not be read."));
    registerAppTool(server, "wiki.status", {
        title: "Get wiki status", description: "Read canonical wiki, index, backup, and Git status without rebuilding or mutating anything.",
        inputSchema: {}, outputSchema, annotations, _meta: toolMeta,
    }, async () => safeAsync(async () => result(await wiki.status()), "Wiki data could not be read."));
    registerAppTool(server, "render_swarm_control", {
        title: "Render Swarm Control", description: "Render the read-only native Swarm Control widget.",
        inputSchema: {}, outputSchema, annotations, _meta: toolMeta,
    }, async () => safe(() => {
        const status = data.status();
        const runs = data.listRuns({ limit: 20 });
        const models = data.listModels({ limit: 25 });
        return result(status, { overview: status, runs, models });
    }));
    registerAppResource(server, "Swarm Control widget", WIDGET_URI, {
        description: "Version 1 of the self-contained, read-only Swarm Control interface.",
        mimeType: RESOURCE_MIME_TYPE,
        _meta: { ui: { csp: { connectDomains: [], resourceDomains: [], frameDomains: [], baseUriDomains: [] }, prefersBorder: true } },
    }, async () => ({
        contents: [{
                uri: WIDGET_URI, mimeType: RESOURCE_MIME_TYPE,
                text: await readFile(resolve(import.meta.dirname, "../dist/widget.html"), "utf8"),
                _meta: { ui: { csp: { connectDomains: [], resourceDomains: [], frameDomains: [], baseUriDomains: [] }, prefersBorder: true } },
            }],
    }));
    return server;
}
//# sourceMappingURL=server.js.map