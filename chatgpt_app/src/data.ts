import Database from "better-sqlite3";
import { closeSync, openSync, readSync, readdirSync, statSync } from "node:fs";
import { basename, resolve, sep } from "node:path";

export const DEFAULT_DB = "/home/komichris/.local/share/owui-swarm/catalog.sqlite3";
export const DEFAULT_RUNS = "/home/komichris/.local/share/owui-swarm/runs";
export const MAX_PAGE = 100;
const MAX_FILE = 256 * 1024;
const MAX_TEXT = 48_000;
const MAX_META = 700_000;
const RUN_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
const SAFE_FILE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;

type Row = Record<string, unknown>;
type Page = { limit?: number; offset?: number };

export class PublicError extends Error {}

export function redact(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(redact);
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [key,
      /^(?:authorization|open_webui_api_key|api_?key|access_?token|auth_?token|bearer_?token|client_?secret|password|passwd|secret|cookie|set-cookie)$/i.test(key)
        ? "[REDACTED]" : redact(item),
    ]));
  }
  if (typeof value !== "string") return value;
  return value
    .replace(/-----BEGIN [^-]*PRIVATE KEY-----[\s\S]*?-----END [^-]*PRIVATE KEY-----/gi, "[REDACTED PRIVATE KEY]")
    .replace(/\b(authorization\s*[:=]\s*)(?:bearer|basic)\s+[^\s,;]+/gi, "$1[REDACTED]")
    .replace(/\b(cookie|set-cookie)\s*[:=]\s*[^\r\n]+/gi, "$1: [REDACTED]")
    .replace(/\b(AUTHORIZATION|OPEN_WEBUI_API_KEY|API_KEY|APIKEY|ACCESS_TOKEN|AUTH_TOKEN|BEARER_TOKEN|CLIENT_SECRET|PASSWORD|PASSWD|SECRET|COOKIE)\b(\s*[:=]\s*)(["']?)[^\s"';,}]+\3/gi, "$1$2$3[REDACTED]$3")
    .replace(/\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b/g, "[REDACTED JWT]")
    .replace(/\b(?:sk|pk|ghp|github_pat|xox[baprs])[-_][A-Za-z0-9_-]{16,}\b/gi, "[REDACTED TOKEN]")
    .replace(/\b(?=[A-Za-z0-9+/_=-]{40,}\b)(?=[A-Za-z0-9+/_=-]*[A-Za-z])(?=[A-Za-z0-9+/_=-]*\d)[A-Za-z0-9+/_=-]{40,}\b/g, "[REDACTED CREDENTIAL-LIKE VALUE]");
}

export function boundedText(value: unknown, limit = MAX_TEXT): { text: string; truncated: boolean; originalChars: number } {
  const raw = String(redact(value ?? ""));
  return { text: raw.slice(0, limit), truncated: raw.length > limit, originalChars: raw.length };
}

function json(value: string | undefined, fallback: unknown): unknown {
  try { return JSON.parse(value || ""); } catch { return fallback; }
}

function clampPage({ limit = 20, offset = 0 }: Page): Required<Page> {
  if (!Number.isInteger(limit) || !Number.isInteger(offset)) throw new PublicError("Pagination values must be integers.");
  return { limit: Math.max(1, Math.min(MAX_PAGE, limit)), offset: Math.max(0, Math.min(100_000, offset)) };
}

function timeOf(events: Row[], name: string): string {
  return String(events.find((event) => event.event === name)?.time || "");
}

function duration(events: Row[]): number | null {
  const first = Date.parse(String(events[0]?.time || ""));
  const last = Date.parse(String(events.at(-1)?.time || ""));
  return Number.isFinite(first) && Number.isFinite(last) ? Math.max(0, last - first) : null;
}

export class SwarmData {
  readonly dbPath: string;
  readonly runRoot: string;

  constructor(dbPath = process.env.SWARM_DB_PATH || DEFAULT_DB, runRoot = process.env.SWARM_RUN_DIR || DEFAULT_RUNS) {
    this.dbPath = resolve(dbPath);
    this.runRoot = resolve(runRoot);
  }

  private db(): Database.Database {
    try {
      const db = new Database(this.dbPath, { readonly: true, fileMustExist: true });
      db.pragma("query_only = ON");
      return db;
    } catch {
      throw new PublicError("Swarm catalog is unavailable.");
    }
  }

  private runDir(runId: string): string {
    if (!RUN_ID.test(runId) || runId === "." || runId === "..") throw new PublicError("Invalid run ID.");
    const path = resolve(this.runRoot, runId);
    if (!path.startsWith(this.runRoot + sep)) throw new PublicError("Invalid run ID.");
    try { if (!statSync(path).isDirectory()) throw new Error(); } catch { throw new PublicError("Run not found."); }
    return path;
  }

  private read(path: string, shouldRedact = true): { text: string; truncated: boolean; originalBytes: number } {
    const target = resolve(path);
    if (!target.startsWith(this.runRoot + sep)) throw new PublicError("Artifact path is outside the run directory.");
    try {
      const size = statSync(target).size;
      const handle = openSync(target, "r");
      const buffer = Buffer.alloc(Math.min(size, MAX_FILE));
      try { readSync(handle, buffer, 0, buffer.length, 0); } finally { closeSync(handle); }
      const data = buffer.toString("utf8");
      const safe = shouldRedact ? boundedText(data) : { text: data.slice(0, MAX_TEXT), truncated: data.length > MAX_TEXT, originalChars: data.length };
      return { text: safe.text, truncated: size > MAX_FILE || safe.truncated, originalBytes: size };
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "ENOENT") return { text: "", truncated: false, originalBytes: 0 };
      throw new PublicError("Run artifact is unavailable.");
    }
  }

  private readJson(path: string): Row {
    const item = this.read(path, false);
    const parsed = json(item.text, {});
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? redact(parsed) as Row : {};
  }

  private events(dir: string): Row[] {
    return this.read(`${dir}/events.jsonl`, false).text.split("\n").filter(Boolean).slice(0, 5000).flatMap((line) => {
      const parsed = json(line, null);
      return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? [redact(parsed) as Row] : [];
    });
  }

  private runIds(): string[] {
    try {
      return readdirSync(this.runRoot, { withFileTypes: true })
        .filter((item) => item.isDirectory() && RUN_ID.test(item.name))
        .map((item) => item.name).sort().reverse();
    } catch { throw new PublicError("Swarm run directory is unavailable."); }
  }

  private runBrief(runId: string): Row {
    const dir = this.runDir(runId);
    const task = this.readJson(`${dir}/task.json`);
    const final = this.readJson(`${dir}/final.json`);
    const events = this.events(dir);
    const failure = this.read(`${dir}/failure.txt`).text;
    const terminal = events.find((event) => event.event === "run_complete" || event.event === "run_failed");
    const failedEvent = events.find((event) => String(event.event).endsWith("_failed"));
    const status = terminal?.event === "run_complete" || Object.keys(final).length ? "complete" : failure || terminal?.event === "run_failed" ? "failed" : "running";
    const objective = boundedText(task.objective, 240);
    return {
      runId, status, mode: task.mode || "", objective: objective.text,
      objectiveTruncated: objective.truncated, createdAt: timeOf(events, "run_created"),
      completedAt: String(terminal?.time || ""), durationMs: duration(events),
      workerCount: Array.isArray(task.workers) ? task.workers.length : 0,
      judgeModel: (task.judge as Row | undefined)?.model || "",
      confidence: typeof final.confidence === "number" ? final.confidence : null,
      failureCategory: failedEvent?.failure_category || (failure ? "failure" : ""),
      timeout: failedEvent?.failure_category === "timeout",
    };
  }

  listRuns(filters: Page & { status?: string; mode?: string; model?: string; dateFrom?: string; dateTo?: string } = {}): Row {
    const page = clampPage(filters);
    let runs = this.runIds().map((id) => this.runBrief(id));
    if (filters.status) runs = runs.filter((run) => run.status === filters.status);
    if (filters.mode) runs = runs.filter((run) => run.mode === filters.mode);
    if (filters.model) {
      const needle = filters.model.toLowerCase();
      runs = runs.filter((run) => {
        const task = this.readJson(`${this.runDir(String(run.runId))}/task.json`);
        return JSON.stringify({ workers: task.workers, judge: task.judge }).toLowerCase().includes(needle);
      });
    }
    if (filters.dateFrom) runs = runs.filter((run) => String(run.createdAt) >= filters.dateFrom!);
    if (filters.dateTo) runs = runs.filter((run) => String(run.createdAt) <= filters.dateTo!);
    return { runs: runs.slice(page.offset, page.offset + page.limit), total: runs.length, limit: page.limit, offset: page.offset, nextOffset: page.offset + page.limit < runs.length ? page.offset + page.limit : null };
  }

  status(): Row {
    const db = this.db();
    try {
      const modelCount = Number((db.prepare("SELECT count(*) n FROM models").get() as Row).n);
      const attempts = db.prepare("SELECT status, elapsed_ms FROM task_attempts ORDER BY attempted_at DESC, id DESC LIMIT 100").all() as Row[];
      const qualityEvents = Number((db.prepare("SELECT count(*) n FROM quality_events").get() as Row).n);
      const benchmarks = Number((db.prepare("SELECT count(*) n FROM benchmark_results").get() as Row).n);
      const runs = this.runIds().slice(0, 20).map((id) => this.runBrief(id));
      const completed = runs.filter((run) => run.status === "complete");
      const failed = runs.filter((run) => run.status === "failed");
      const active = runs.filter((run) => run.status === "running");
      const successAttempts = attempts.filter((row) => row.status === "success");
      const timeoutAttempts = attempts.filter((row) => row.status === "timeout");
      const latencies = successAttempts.map((row) => Number(row.elapsed_ms)).sort((a, b) => a - b);
      const median = latencies.length ? latencies[Math.floor(latencies.length / 2)] : null;
      const updatedAt = [statSync(this.dbPath).mtime.toISOString(), ...runs.map((run) => String(run.completedAt || run.createdAt || ""))].sort().at(-1) || "";
      return {
        modelCount, recentRunCount: runs.length, activeRunCount: active.length,
        recentSuccessCount: completed.length, recentFailureCount: failed.length,
        successRate: runs.length ? completed.length / runs.length : null,
        failureRate: runs.length ? failed.length / runs.length : null,
        timeoutRate: attempts.length ? timeoutAttempts.length / attempts.length : null,
        recentMedianLatencyMs: median,
        latestCompletedRun: completed[0] || null,
        reliability: { evidenceCount: attempts.length, successCount: successAttempts.length, timeoutCount: timeoutAttempts.length },
        qualityEvidence: { eventCount: qualityEvents, benchmarkCount: benchmarks, available: qualityEvents + benchmarks > 0 },
        lastUpdatedAt: updatedAt,
      };
    } finally { db.close(); }
  }

  runSummary(runId: string): Row {
    const summary = this.runBrief(runId);
    const dir = this.runDir(runId);
    const task = this.readJson(`${dir}/task.json`);
    const final = this.readJson(`${dir}/final.json`);
    const events = this.events(dir);
    const failures = events.filter((event) => String(event.event).endsWith("_failed")).map((event) => ({ role: event.agent || "", model: event.model || "", category: event.failure_category || "failure" }));
    return {
      ...summary, objective: boundedText(task.objective, 800).text,
      acceptanceCriteria: boundedText(task.acceptance, 800).text,
      workers: (Array.isArray(task.workers) ? task.workers : []).map((item) => {
        const worker = item as Row; return { role: worker.name || "", model: worker.model || "", selectionReason: boundedText(worker.selection_reason, 400).text };
      }),
      judge: { model: (task.judge as Row | undefined)?.model || "" }, failures,
      finalSynthesis: boundedText(final.answer, 1800).text,
      confidence: final.confidence ?? null,
      confidenceReasons: Array.isArray(final.confidence_reasons) ? final.confidence_reasons.slice(0, 20) : [],
      verificationRequirements: Array.isArray(final.verification) ? final.verification.slice(0, 30) : [],
      timing: { createdAt: summary.createdAt, completedAt: summary.completedAt, durationMs: summary.durationMs },
    };
  }

  runDetails(runId: string): { concise: Row; detail: Row } {
    const concise = this.runSummary(runId);
    const dir = this.runDir(runId);
    const task = this.readJson(`${dir}/task.json`);
    const final = this.readJson(`${dir}/final.json`);
    const events = this.events(dir);
    const readGroup = (subdir: string, allowed: RegExp) => {
      const root = `${dir}/${subdir}`;
      try {
        return Object.fromEntries(readdirSync(root).filter((name) => SAFE_FILE.test(name) && allowed.test(name)).sort().map((name) => [name, this.read(`${root}/${name}`)]));
      } catch { return {}; }
    };
    const workers = readGroup("workers", /^(?:[A-Za-z0-9_-]+\.md|_failures\.json|_errors\.txt)$/);
    const prompts = readGroup("prompts", /^(?:worker-[A-Za-z0-9_-]+|judge)\.txt$/);
    const quality = this.evidenceForRun(runId);
    const workerCards = (Array.isArray(task.workers) ? task.workers : []).map((item) => {
      const worker = item as Row;
      const role = String(worker.name || "");
      const modelId = String(worker.model || "");
      const sent = events.find((event) => event.event === "worker_sent" && event.agent === role);
      const done = events.find((event) => ["worker_returned", "worker_failed"].includes(String(event.event)) && event.agent === role);
      const profile = modelId ? this.model(modelId) : {};
      return {
        role, model: modelId,
        selection: String(worker.selection_reason || "").toLowerCase().startsWith("explicit") ? "explicit" : "automatic",
        selectionReason: worker.selection_reason || "", sentAt: sent?.time || "", completedAt: done?.time || "",
        latencyMs: done?.duration_ms ?? null, timeoutSeconds: done?.timeout_seconds ?? sent?.timeout_seconds ?? null,
        status: done?.event === "worker_returned" ? "success" : done ? "failed" : "running",
        failureCategory: done?.failure_category || "",
        reliabilityPenalty: (profile.reliability as Row | undefined)?.reliabilityPenalty ?? null,
        reliability: profile.reliability || {},
        roleQualityScore: ((profile.quality as Row | undefined)?.roleScores as Row | undefined)?.[role] ?? null,
        response: workers[`${role}.md`] || { text: "", truncated: false, originalBytes: 0 },
      };
    });
    const detail: Row = {
      runId, task, events, context: { manifest: this.readJson(`${dir}/context/manifest.json`), sent: this.read(`${dir}/context/sent.txt`) },
      prompts, workers, workerCards, judge: { response: this.read(`${dir}/judge/response.md`), failure: this.read(`${dir}/judge/failure.json`), model: task.judge || {} },
      final, finalMarkdown: this.read(`${dir}/final.md`), failure: this.read(`${dir}/failure.txt`),
      reliabilityEvidence: quality.attempts, qualityEvidence: quality.quality, benchmarkEvidence: quality.benchmarks,
    };
    const size = Buffer.byteLength(JSON.stringify(detail));
    if (size > MAX_META) {
      delete (detail.context as Row).sent;
      delete detail.workers;
      for (const item of Object.values(detail.prompts as Row)) {
        if (item && typeof item === "object") (item as Row).text = boundedText((item as Row).text, 8_000).text;
      }
      for (const card of detail.workerCards as Row[]) {
        const response = card.response as Row;
        response.text = boundedText(response.text, 16_000).text;
        response.truncated = true;
      }
      const judge = detail.judge as Row;
      (judge.response as Row).text = boundedText((judge.response as Row).text, 16_000).text;
      (detail.finalMarkdown as Row).text = boundedText((detail.finalMarkdown as Row).text, 16_000).text;
      detail.payloadTruncated = true;
      detail.originalPayloadBytes = size;
      if (Buffer.byteLength(JSON.stringify(detail)) > MAX_META) {
        detail.prompts = {};
        for (const card of detail.workerCards as Row[]) (card.response as Row).text = "[TRUNCATED: payload limit]";
      }
    }
    return { concise, detail };
  }

  private evidenceForRun(runId: string): { attempts: Row[]; quality: Row[]; benchmarks: Row[] } {
    const db = this.db();
    try {
      return {
        attempts: db.prepare("SELECT model_id, role, mode, attempted_at, status, elapsed_ms, retry_count FROM task_attempts WHERE run_id=? ORDER BY id").all(runId) as Row[],
        quality: db.prepare("SELECT model_id, role, mode, category, severity, judge_caught, reached_final, codex_verified, note, created_at FROM quality_events WHERE run_id=? ORDER BY id").all(runId) as Row[],
        benchmarks: (db.prepare("SELECT benchmark_id, benchmark_version, model_id, role, mode, checks, dimensions, evaluator_source, note, evaluated_at FROM benchmark_results WHERE run_id=? ORDER BY id").all(runId) as Row[]).map((row) => ({ ...row, checks: json(String(row.checks), {}), dimensions: json(String(row.dimensions), {}) })),
      };
    } finally { db.close(); }
  }

  private modelMetrics(db: Database.Database, modelId: string): Row {
    const attempts = db.prepare("SELECT status, elapsed_ms, attempted_at FROM task_attempts WHERE model_id=? ORDER BY attempted_at DESC, id DESC LIMIT 20").all(modelId) as Row[];
    const successes = attempts.filter((row) => row.status === "success");
    const timeouts = attempts.filter((row) => row.status === "timeout");
    const latencies = successes.map((row) => Number(row.elapsed_ms)).sort((a, b) => a - b);
    const events = db.prepare("SELECT role, category, severity, run_id, note, created_at FROM quality_events WHERE model_id=? ORDER BY created_at DESC LIMIT 100").all(modelId) as Row[];
    const benchmarks = db.prepare("SELECT benchmark_id, run_id, role, dimensions, checks, note, evaluated_at FROM benchmark_results WHERE model_id=? ORDER BY evaluated_at DESC LIMIT 20").all(modelId) as Row[];
    const evidence = new Set([...events, ...benchmarks].map((row) => String(row.run_id || `${row.benchmark_id}:${row.evaluated_at}`))).size;
    const scores: Record<string, number[]> = {};
    for (const row of benchmarks) for (const value of Object.values(json(String(row.dimensions), {}) as Row)) if (typeof value === "number") (scores[String(row.role)] ||= []).push(value);
    const roleScores = Object.fromEntries(Object.entries(scores).map(([role, values]) => [role, Math.round(values.reduce((a, b) => a + b, 0) / values.length * 50) / 10]));
    const categories = events.reduce<Record<string, number>>((out, row) => { const key = String(row.category); out[key] = (out[key] || 0) + 1; return out; }, {});
    const strengths = Object.keys(categories).filter((key) => ["useful_dissent", "caught_peer_error", "clean_candidate"].includes(key));
    const failures = Object.keys(categories).filter((key) => !strengths.includes(key));
    const recommended = Object.entries(roleScores).filter(([, score]) => score >= 7 && evidence >= 3).map(([role]) => role);
    const discouraged = Object.entries(roleScores).filter(([, score]) => score < 4 && evidence >= 3).map(([role]) => role);
    let consecutiveFailures = 0;
    for (const row of attempts) { if (row.status === "success") break; consecutiveFailures += 1; }
    const otherFailures = attempts.length - successes.length - timeouts.length;
    const protocolFailures = attempts.filter((row) => row.status === "protocol").length;
    const reliabilityPenalty = attempts.length
      ? timeouts.length / attempts.length * 0.20 + otherFailures / attempts.length * 0.15 + consecutiveFailures * 0.10 + protocolFailures * 0.15
      : 0;
    const qualityValues = Object.values(roleScores);
    return redact({
      reliability: {
        attemptCount: attempts.length, successRate: attempts.length ? successes.length / attempts.length : null,
        timeoutRate: attempts.length ? timeouts.length / attempts.length : null,
        medianLatencyMs: latencies.length ? latencies[Math.floor(latencies.length / 2)] : null,
        reliabilityPenalty: Math.round(reliabilityPenalty * 1000) / 1000,
      },
      quality: { score: qualityValues.length ? qualityValues.reduce((a, b) => a + b, 0) / qualityValues.length : null, roleScores, evidenceCount: evidence, provisional: evidence < 3 },
      knownStrengths: strengths, knownFailureCategories: failures,
      recommendedRoles: recommended, discouragedRoles: discouraged,
      recentBenchmarks: benchmarks.slice(0, 10).map((row) => ({ ...row, dimensions: json(String(row.dimensions), {}), checks: json(String(row.checks), {}) })),
    }) as Row;
  }

  listModels(filters: Page & { search?: string; enabled?: boolean; chatCompatible?: boolean; family?: string; recommendedRole?: string } = {}): Row {
    const page = clampPage(filters);
    const db = this.db();
    try {
      const where: string[] = [];
      const params: unknown[] = [];
      if (filters.search) { where.push("(model_id LIKE ? ESCAPE '\\' OR notes LIKE ? ESCAPE '\\')"); const q = `%${filters.search.replace(/[\\%_]/g, "\\$&")}%`; params.push(q, q); }
      if (filters.enabled !== undefined) { where.push("enabled=?"); params.push(Number(filters.enabled)); }
      if (filters.chatCompatible !== undefined) { where.push(filters.chatCompatible ? "kind='chat'" : "kind<>'chat'"); }
      if (filters.family) { where.push("family=?"); params.push(filters.family); }
      const roleFilter = Boolean(filters.recommendedRole);
      const rows = db.prepare(`SELECT * FROM models ${where.length ? `WHERE ${where.join(" AND ")}` : ""} ORDER BY enabled DESC, model_id LIMIT ? OFFSET ?`).all(...params, roleFilter ? 100_000 : page.limit, roleFilter ? 0 : page.offset) as Row[];
      let models: Row[] = rows.map((row) => ({
        modelId: row.model_id, family: row.family, kind: row.kind, capabilities: json(String(row.capabilities), []),
        enabled: Boolean(row.enabled), available: Boolean(row.available), probeStatus: row.probe_status,
        representativeLatencyMs: row.probe_ms, manualQuality: row.quality, ...this.modelMetrics(db, String(row.model_id)),
      }));
      if (filters.recommendedRole) models = models.filter((model) => (model.recommendedRoles as string[]).includes(filters.recommendedRole!));
      const count = filters.recommendedRole ? models.length : Number((db.prepare(`SELECT count(*) n FROM models ${where.length ? `WHERE ${where.join(" AND ")}` : ""}`).get(...params) as Row).n);
      if (roleFilter) models = models.slice(page.offset, page.offset + page.limit);
      return { models, total: count, limit: page.limit, offset: page.offset, nextOffset: page.offset + page.limit < count ? page.offset + page.limit : null };
    } finally { db.close(); }
  }

  model(modelId: string): Row {
    if (!modelId || modelId.length > 300) throw new PublicError("Invalid model ID.");
    const db = this.db();
    try {
      const row = db.prepare("SELECT * FROM models WHERE model_id=?").get(modelId) as Row | undefined;
      if (!row) throw new PublicError("Model not found.");
      const metrics = this.modelMetrics(db, modelId);
      return redact({
        modelId: row.model_id, provider: row.provider, family: row.family, kind: row.kind,
        capabilities: json(String(row.capabilities), []), enabled: Boolean(row.enabled), available: Boolean(row.available),
        contextLength: row.context_length, probeStatus: row.probe_status, probeLatencyMs: row.probe_ms,
        lastSeen: row.last_seen, lastSuccessfulProbe: row.last_successful_probe, lastFailure: row.last_failure,
        manualQuality: row.quality, manualSpeed: row.speed, notes: boundedText(row.notes, 1000).text, ...metrics,
      }) as Row;
    } finally { db.close(); }
  }
}
