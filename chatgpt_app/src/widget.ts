import { App } from "@modelcontextprotocol/ext-apps";
import "./widget.css";

type Json = Record<string, any>;
type ToolResult = { structuredContent?: Json; _meta?: Json; content?: Array<{ type: string; text?: string }>; isError?: boolean };
const root = document.querySelector<HTMLElement>("#app")!;
const refresh = document.querySelector<HTMLButtonElement>("#refresh")!;
const nav = [...document.querySelectorAll<HTMLButtonElement>("nav button")];
const bridge = new App({ name: "Swarm Control", version: "1.0.0" });
let view = "overview";
let state: Json = { overview: null, runs: null, models: null, detail: null };
let timer: number | undefined;

function node(tag: string, text = "", className = ""): HTMLElement {
  const item = document.createElement(tag);
  item.textContent = text;
  if (className) item.className = className;
  return item;
}

function button(text: string, action: () => void): HTMLButtonElement {
  const item = document.createElement("button");
  item.type = "button"; item.textContent = text; item.addEventListener("click", action); return item;
}

function value(item: unknown, fallback = "—"): string {
  return item === null || item === undefined || item === "" ? fallback : String(item);
}

function percent(item: unknown): string {
  return typeof item === "number" ? `${(item * 100).toFixed(1)}%` : "—";
}

function duration(item: unknown): string {
  if (typeof item !== "number") return "—";
  return item < 1000 ? `${item} ms` : `${(item / 1000).toFixed(1)} s`;
}

function statusClass(status: unknown): string { return `badge ${status === "complete" || status === "success" ? "good" : status === "running" ? "warn" : "bad"}`; }

function card(label: string, display: string): HTMLElement {
  const item = node("section", "", "metric"); item.append(node("span", label), node("strong", display)); return item;
}

function message(text: string, kind = "empty"): void { root.replaceChildren(node("p", text, kind)); }

function ingest(result: ToolResult): Json {
  if (result.isError) throw new Error(result.content?.find((item) => item.type === "text")?.text || "Tool call failed.");
  return result.structuredContent?.data || {};
}

async function call(name: string, args: Json = {}): Promise<ToolResult> {
  refresh.disabled = true;
  try { return await bridge.callServerTool({ name, arguments: args }) as ToolResult; }
  finally { refresh.disabled = false; }
}

function renderOverview(): void {
  const data = state.overview;
  if (!data) return message("No overview data is available.");
  const grid = node("div", "", "metrics");
  grid.append(
    card("Models", value(data.modelCount)), card("Active runs", value(data.activeRunCount)),
    card("Success rate", percent(data.successRate)), card("Failure rate", percent(data.failureRate)),
    card("Timeout rate", percent(data.timeoutRate)), card("Median latency", duration(data.recentMedianLatencyMs)),
    card("Quality events", value(data.qualityEvidence?.eventCount)), card("Benchmarks", value(data.qualityEvidence?.benchmarkCount)),
  );
  const latest = node("section", "", "panel"); latest.append(node("h2", "Latest completed run"));
  if (data.latestCompletedRun) latest.append(runRow(data.latestCompletedRun)); else latest.append(node("p", "No completed runs."));
  latest.append(node("p", `Last updated ${value(data.lastUpdatedAt)}`, "muted"));
  root.replaceChildren(grid, latest);
}

function runRow(run: Json): HTMLElement {
  const row = node("article", "", "run-row");
  const top = node("div", "", "row");
  const objective = button(value(run.objective, "Untitled run"), () => void loadRun(value(run.runId, "")));
  objective.className = "link";
  top.append(objective, node("span", value(run.status), statusClass(run.status)));
  row.append(top, node("p", `${value(run.createdAt)} · ${value(run.mode)} · ${value(run.workerCount)} workers · ${duration(run.durationMs)} · confidence ${value(run.confidence)}`, "muted"));
  return row;
}

function renderRuns(): void {
  const data = state.runs;
  if (!data) return message("No run data is available.");
  const controls = node("form", "", "controls");
  const status = document.createElement("select");
  for (const [label, val] of [["Any status", ""], ["Complete", "complete"], ["Running", "running"], ["Failed", "failed"]]) { const option = document.createElement("option"); option.textContent = label; option.value = val; status.append(option); }
  const mode = document.createElement("select");
  for (const val of ["", "auto", "code", "spec", "research", "general"]) { const option = document.createElement("option"); option.value = val; option.textContent = val || "Any mode"; mode.append(option); }
  controls.append(status, mode, button("Apply", () => void loadRuns({ status: status.value || undefined, taskMode: mode.value || undefined })));
  controls.addEventListener("submit", (event) => event.preventDefault());
  const list = node("section", "", "panel");
  if (!data.runs?.length) list.append(node("p", "No runs match these filters."));
  else for (const run of data.runs) list.append(runRow(run));
  const pages = node("div", "", "pager");
  if (data.offset > 0) pages.append(button("Previous", () => void loadRuns({ offset: Math.max(0, data.offset - data.limit) })));
  if (data.nextOffset !== null) pages.append(button("Next", () => void loadRuns({ offset: data.nextOffset })));
  root.replaceChildren(controls, list, pages);
}

function detailSection(title: string, content: unknown, open = false): HTMLElement {
  const details = document.createElement("details"); details.open = open;
  const summary = document.createElement("summary"); summary.textContent = title;
  const pre = node("pre", typeof content === "string" ? content : JSON.stringify(content ?? {}, null, 2));
  details.append(summary, pre); return details;
}

function renderDetail(): void {
  const data = state.detail;
  if (!data) return message("Run details are unavailable.");
  const summary = state.detailSummary || {};
  const head = node("section", "", "panel");
  head.append(button("← Recent runs", () => setView("runs")), node("h2", value(summary.objective, "Run details")), node("span", value(summary.status), statusClass(summary.status)));
  head.append(node("p", `${value(summary.runId)} · ${value(summary.mode)} · ${duration(summary.durationMs)} · confidence ${value(summary.confidence)}`, "muted"));
  const tabs = node("div", "", "detail-grid");
  const overview = node("section", "", "panel"); overview.append(node("h3", "Summary"), node("p", value(data.final?.answer || summary.finalSynthesis)), detailSection("Acceptance criteria", data.task?.acceptance || ""), detailSection("Verification requirements", data.final?.verification || []));
  const timeline = node("section", "", "panel"); timeline.append(node("h3", "Timeline")); for (const event of data.events || []) timeline.append(node("p", `${value(event.time)} · ${value(event.event)} · ${value(event.agent || event.run_id, "")}`, "timeline"));
  tabs.append(overview, timeline);
  const workers = node("section", "", "panel"); workers.append(node("h3", "Workers"));
  for (const worker of data.workerCards || []) {
    const item = node("article", "", "worker");
    item.append(node("h4", `${value(worker.role)} · ${value(worker.model)}`), node("span", value(worker.status), statusClass(worker.status)));
    item.append(node("p", `${value(worker.selection)} selection · ${value(worker.selectionReason)} · latency ${duration(worker.latencyMs)} · timeout ${value(worker.timeoutSeconds)}s · failure ${value(worker.failureCategory)} · reliability penalty ${value(worker.reliabilityPenalty)} · role quality ${value(worker.roleQualityScore)}`, "muted"));
    item.append(detailSection(`Response${worker.response?.truncated ? " (truncated)" : ""}`, worker.response?.text || "No response.")); workers.append(item);
  }
  const evidence = node("section", "", "panel"); evidence.append(node("h3", "Judge, reliability, quality, and context"), detailSection("Judge response", data.judge?.response?.text || ""), detailSection("Final result", data.final || {}), detailSection("Reliability evidence", data.reliabilityEvidence || []), detailSection("Quality evidence", data.qualityEvidence || []), detailSection("Benchmark evidence", data.benchmarkEvidence || []), detailSection("Context manifest", data.context?.manifest || {}), detailSection("Worker and judge prompts", data.prompts || {}));
  root.replaceChildren(head, tabs, workers, evidence);
  window.clearInterval(timer);
  if (summary.status === "running") timer = window.setInterval(() => void loadRun(summary.runId, true), 15_000);
}

function modelRow(model: Json): HTMLElement {
  const item = node("article", "", "model");
  const top = node("div", "", "row"); const link = button(value(model.modelId), () => void loadModel(model.modelId)); link.className = "link";
  top.append(link, node("span", model.enabled ? "enabled" : "disabled", `badge ${model.enabled ? "good" : "bad"}`)); item.append(top);
  item.append(node("p", `${value(model.family)} · reliability ${percent(model.reliability?.successRate)} · quality ${value(model.quality?.score)} · evidence ${value(model.quality?.evidenceCount)} · median ${duration(model.reliability?.medianLatencyMs)} · timeout ${percent(model.reliability?.timeoutRate)}`, "muted"));
  item.append(node("p", `Recommended: ${value(model.recommendedRoles?.join(", "))} · Discouraged: ${value(model.discouragedRoles?.join(", "))}`)); return item;
}

function renderModels(): void {
  const data = state.models;
  if (!data) return message("No model data is available.");
  const controls = node("form", "", "controls");
  const search = document.createElement("input"); search.type = "search"; search.placeholder = "Search exact IDs or notes"; search.maxLength = 200;
  const chat = document.createElement("select"); for (const [label, val] of [["All kinds", ""], ["Chat compatible", "true"], ["Non-chat", "false"]]) { const option = document.createElement("option"); option.textContent = label; option.value = val; chat.append(option); }
  controls.append(search, chat, button("Search", () => void loadModels({ search: search.value || undefined, chatCompatible: chat.value ? chat.value === "true" : undefined })));
  controls.addEventListener("submit", (event) => event.preventDefault());
  const list = node("section", "", "panel"); if (!data.models?.length) list.append(node("p", "No models match these filters.")); else for (const model of data.models) list.append(modelRow(model));
  const pages = node("div", "", "pager"); if (data.offset > 0) pages.append(button("Previous", () => void loadModels({ offset: Math.max(0, data.offset - data.limit) }))); if (data.nextOffset !== null) pages.append(button("Next", () => void loadModels({ offset: data.nextOffset })));
  root.replaceChildren(controls, list, pages);
}

function renderModel(): void {
  const model = state.model;
  if (!model) return message("Model details are unavailable.");
  const head = node("section", "", "panel"); head.append(button("← Model catalog", () => setView("models")), node("h2", value(model.modelId)), node("p", `${value(model.family)} · ${value(model.kind)} · ${value(model.capabilities?.join(", "))}`, "muted"));
  const metrics = node("div", "", "metrics"); metrics.append(card("Reliability", percent(model.reliability?.successRate)), card("Quality", value(model.quality?.score)), card("Evidence", value(model.quality?.evidenceCount)), card("Median latency", duration(model.reliability?.medianLatencyMs)), card("Timeout rate", percent(model.reliability?.timeoutRate)), card("Provisional", value(model.quality?.provisional)));
  const details = node("section", "", "panel"); details.append(node("p", `Recommended roles: ${value(model.recommendedRoles?.join(", "))}`), node("p", `Discouraged roles: ${value(model.discouragedRoles?.join(", "))}`), node("p", `Known strengths: ${value(model.knownStrengths?.join(", "))}`), node("p", `Known failures: ${value(model.knownFailureCategories?.join(", "))}`), detailSection("Recent benchmarks", model.recentBenchmarks || []));
  root.replaceChildren(head, metrics, details);
}

function render(): void {
  window.clearInterval(timer);
  if (view === "overview") renderOverview(); else if (view === "runs") renderRuns(); else if (view === "detail") renderDetail(); else if (view === "model") renderModel(); else renderModels();
}

function setView(next: string): void {
  view = next; nav.forEach((item) => item.setAttribute("aria-current", item.dataset.view === next ? "page" : "false")); render();
}

async function loadRuns(args: Json = {}): Promise<void> { try { message("Loading runs…", "loading"); state.runs = ingest(await call("list_swarm_runs", { limit: 20, ...args })); setView("runs"); } catch (error) { message(error instanceof Error ? error.message : "Runs could not be loaded.", "error"); } }
async function loadModels(args: Json = {}): Promise<void> { try { message("Loading models…", "loading"); state.models = ingest(await call("list_swarm_models", { limit: 25, ...args })); setView("models"); } catch (error) { message(error instanceof Error ? error.message : "Models could not be loaded.", "error"); } }
async function loadRun(runId: string, quiet = false): Promise<void> { try { if (!quiet) message("Loading run details…", "loading"); const response = await call("get_swarm_run_details", { runId }); state.detailSummary = ingest(response); state.detail = response._meta?.swarmControl?.detail || null; setView("detail"); } catch (error) { message(error instanceof Error ? error.message : "Run could not be loaded.", "error"); } }
async function loadModel(modelId: string): Promise<void> { try { message("Loading model details…", "loading"); state.model = ingest(await call("get_swarm_model", { modelId })); setView("model"); } catch (error) { message(error instanceof Error ? error.message : "Model could not be loaded.", "error"); } }
async function refreshAll(): Promise<void> { try { message("Refreshing…", "loading"); const response = await call("render_swarm_control"); const meta = response._meta?.swarmControl || {}; state = { ...state, overview: meta.overview || ingest(response), runs: meta.runs || state.runs, models: meta.models || state.models }; render(); } catch (error) { message(error instanceof Error ? error.message : "Swarm data could not be refreshed.", "error"); } }

bridge.ontoolresult = (result) => {
  try {
    const response = result as ToolResult; const meta = response._meta?.swarmControl || {}; const data = ingest(response);
    if (meta.overview) state = { ...state, overview: meta.overview, runs: meta.runs, models: meta.models };
    else if (meta.detail) { state.detail = meta.detail; state.detailSummary = data; view = "detail"; }
    else if ("modelCount" in data) state.overview = data;
    render();
  } catch (error) { message(error instanceof Error ? error.message : "Initial data could not be displayed.", "error"); }
};
nav.forEach((item) => item.addEventListener("click", () => { const next = item.dataset.view!; if (next === "runs" && !state.runs) void loadRuns(); else if (next === "models" && !state.models) void loadModels(); else setView(next); }));
refresh.addEventListener("click", () => { if (view === "detail" && state.detailSummary?.runId) void loadRun(state.detailSummary.runId); else if (view === "models") void loadModels(); else if (view === "runs") void loadRuns(); else void refreshAll(); });
bridge.connect();
window.setTimeout(() => { if (!state.overview && !state.detail) void refreshAll(); }, 1200);
