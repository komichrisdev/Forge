from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from time import monotonic
from typing import Any
from urllib.parse import unquote, urlparse
import json
import os
import traceback
import base64


MAX_IMAGE_BYTES = 10 * 1024 * 1024

from .catalog import ModelCatalog
from .client import OpenWebUIClient
from .config import AppConfig
from .orchestrator import SwarmOrchestrator


HTML = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Open WebUI Swarm Control</title>
<style>
:root{color-scheme:dark;--bg:#0c0f14;--panel:#151a22;--panel2:#1b2230;--line:#2d3748;--text:#e9edf5;--muted:#9aa7b7;--accent:#8ab4ff;--good:#67d391;--bad:#ff7b72;--warn:#f2cc60}*{box-sizing:border-box}body{margin:0;font:14px/1.45 system-ui,sans-serif;background:var(--bg);color:var(--text)}header{padding:14px 20px;border-bottom:1px solid var(--line);display:flex;gap:16px;align-items:center;position:sticky;top:0;background:rgba(12,15,20,.96);z-index:5}header h1{font-size:18px;margin:0}button,input,select,textarea{background:#10151d;color:var(--text);border:1px solid var(--line);border-radius:7px;padding:8px}button{cursor:pointer}button:hover{border-color:var(--accent)}main{display:grid;grid-template-columns:minmax(390px,520px) 1fr;gap:14px;padding:14px;height:calc(100vh - 61px)}.column{min-height:0;display:flex;flex-direction:column;gap:14px}.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px;min-height:0}.grow{flex:1;overflow:auto}h2{font-size:15px;margin:0 0 10px}label{display:block;color:var(--muted);margin:8px 0 4px}textarea{width:100%;min-height:90px;resize:vertical}.row{display:flex;gap:8px;align-items:center}.row>*{min-width:0}.roles{display:grid;grid-template-columns:110px 1fr;gap:6px;align-items:center}.muted{color:var(--muted)}.badge{display:inline-block;padding:2px 7px;border:1px solid var(--line);border-radius:999px;margin:2px;font-size:12px}.good{color:var(--good)}.bad{color:var(--bad)}.warn{color:var(--warn)}table{width:100%;border-collapse:collapse}th,td{text-align:left;border-bottom:1px solid var(--line);padding:7px;vertical-align:top}th{position:sticky;top:0;background:var(--panel)}.run{padding:9px;border-bottom:1px solid var(--line);cursor:pointer}.run:hover{background:var(--panel2)}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#0f131a;padding:10px;border-radius:7px;border:1px solid var(--line)}details{border:1px solid var(--line);border-radius:7px;margin:8px 0;padding:8px}summary{cursor:pointer}.event{font-family:ui-monospace,monospace;font-size:12px;border-left:3px solid var(--line);padding:4px 8px;margin:3px 0}.event.worker_returned,.event.judge_returned,.event.run_complete{border-color:var(--good)}.event.worker_failed,.event.run_failed{border-color:var(--bad)}@media(max-width:1000px){main{grid-template-columns:1fr;height:auto}.grow{max-height:600px}}
</style></head><body>
<header><h1>Open WebUI Swarm Control</h1><span class="muted">Workers propose. Codex supervises.</span><div style="margin-left:auto" class="row"><input id="token" type="password" placeholder="Dashboard token"><button onclick="saveToken()">Save token</button><button onclick="refreshAll()">Refresh</button></div></header>
<main>
<div class="column">
<section class="panel"><h2>Dispatch task</h2>
<label>Objective</label><textarea id="objective" placeholder="Task for the subordinate model swarm"></textarea>
<div class="row"><div style="flex:1"><label>Mode</label><select id="mode"><option>auto</option><option>code</option><option>spec</option><option>research</option><option>general</option></select></div><div style="flex:2"><label>Acceptance criteria</label><input id="acceptance" style="width:100%"></div></div>
<label>Optional supplied context</label><textarea id="context" placeholder="Paste only the narrow context workers need"></textarea><div id="attachments" class="muted" aria-live="polite">Drop text files or images here.</div>
<label>Per-run model assignment</label><div id="roles" class="roles"></div>
<div class="row" style="margin-top:10px"><button onclick="dispatch()">Enlist swarm</button><span id="dispatchStatus" class="muted"></span></div>
</section>
<section class="panel grow"><div class="row"><h2 style="flex:1">Model catalog</h2><button onclick="syncModels()">Sync</button><button onclick="probeEnabled()">Probe enabled</button></div><div id="models"></div></section>
</div>
<div class="column">
<section class="panel" style="max-height:230px;overflow:auto"><h2>Runs</h2><div id="runs"></div></section>
<section class="panel grow"><h2>Run inspector</h2><div id="detail" class="muted">Select a run to view chunking, enlisted agents, outbound prompts, returns, timing, and final synthesis.</div></section>
</div></main>
<script>
let roleNames=[];let models=[];let imageParts=[];
function token(){return localStorage.getItem('swarmToken')||''}function saveToken(){localStorage.setItem('swarmToken',document.getElementById('token').value);refreshAll()}
async function api(path,opts={}){opts.headers={...(opts.headers||{}),'Content-Type':'application/json','X-Swarm-Token':token()};const r=await fetch(path,opts);if(!r.ok)throw new Error(await r.text());return await r.json()}
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function modelSelect(role){const configured=window.state?.defaults?.[role]||'';return `<select data-role="${role}"><option value="">Configured default (${esc(configured)})</option>${models.filter(m=>m.kind==='chat'&&m.enabled&&m.available&&m.probe_status==='healthy').map(m=>`<option value="${esc(m.model_id)}">${esc(m.model_id)} ✓</option>`).join('')}</select>`}
function renderRoles(){document.getElementById('roles').innerHTML=roleNames.map(r=>`<span>${r==='__judge__'?'judge':esc(r)}</span>${modelSelect(r)}`).join('')}
function jsarg(s){return esc(JSON.stringify(s))}
async function refreshModels(){const d=await api('/api/models');models=d.models;window.state=d;roleNames=Object.keys(d.defaults||{}).filter(x=>x!=='__judge__').concat('__judge__');renderRoles();const history=(d.probe_history||[]).slice(0,100);const benchmarks=(d.benchmark_results||[]).slice(0,100);document.getElementById('models').innerHTML=`<table><thead><tr><th>Model</th><th>Type/family</th><th>Health</th><th>Reliability</th><th>Quality</th><th></th></tr></thead><tbody>${models.map(m=>`<tr><td>${esc(m.model_id)}<div>${m.capabilities.map(c=>`<span class="badge">${esc(c)}</span>`).join('')}</div>${m.notes?`<div class="muted">${esc(m.notes)}</div>`:''}</td><td>${esc(m.kind)}<div class="muted">${esc(m.provider)} · ${esc(m.family)}${m.context_length?` · ${m.context_length} ctx`:''}</div></td><td class="${m.probe_status==='healthy'?'good':m.probe_status==='failed'?'bad':'muted'}">${esc(m.probe_status)}${m.probe_ms?` ${m.probe_ms}ms`:''}<div class="muted">recent ${m.probe_success_rate===null?'n/a':Math.round(m.probe_success_rate*100)+'%'} · ${esc(m.last_probe||'')}${m.probe_error?`<br>${esc(m.probe_error)}`:''}</div></td><td>success ${m.recent_success_rate===null?'n/a':Math.round(m.recent_success_rate*100)+'%'}<br>timeout ${m.recent_timeout_rate===null?'n/a':Math.round(m.recent_timeout_rate*100)+'%'}<div class="muted">streak ${m.consecutive_failures} · median ${m.recent_median_latency_ms??'n/a'}ms<br>last success ${esc(m.last_successful_completion||'n/a')}<br>penalty ${m.reliability_penalty}${m.cooldown?` · <span class="warn">cooldown</span>`:''}</div></td><td>Manual Q${m.quality} / S${m.speed}<br>evidence ${m.quality_evidence_count} · ${m.quality_provisional?'provisional':'established'}<div class="muted">clean ${Math.round(m.clean_candidate_rate*100)}% · unsupported ${Math.round(m.hallucination_event_rate*100)}%<br>judge catch ${m.judge_catch_rate===null?'n/a':Math.round(m.judge_catch_rate*100)+'%'} · final defect ${Math.round(m.final_synthesis_defect_rate*100)}%<br>quality contribution ${m.quality_contribution}<br>recommended ${esc((m.recommended_roles||[]).join(', ')||'none')}<br>discouraged ${esc((m.discouraged_roles||[]).join(', ')||'none')}<br>strengths ${esc((m.known_strengths||[]).join(', ')||'none')}<br>failures ${esc((m.known_failure_categories||[]).join(', ')||'none')}</div><details><summary>Role scores</summary><pre>${esc(JSON.stringify(m.quality_by_role,null,2))}</pre></details></td><td><button onclick="probeOne(${jsarg(m.model_id)})">Probe</button> <button onclick="rateModel(${jsarg(m.model_id)})">Rate</button> <button onclick="toggleModel(${jsarg(m.model_id)},${!m.enabled})">${m.enabled?'Disable':'Enable'}</button></td></tr>`).join('')}</tbody></table><details><summary>Probe history (${history.length}) · timeout ${d.probe_timeout_seconds}s</summary><table><thead><tr><th>Time</th><th>Model</th><th>Status</th><th>Latency/error</th></tr></thead><tbody>${history.map(h=>`<tr><td>${esc(h.probed_at)}</td><td>${esc(h.model_id)}</td><td>${esc(h.status)}</td><td>${h.elapsed_ms} ms${h.error?`<br>${esc(h.error)}`:''}</td></tr>`).join('')}</tbody></table></details><details><summary>Recent quality benchmarks (${benchmarks.length})</summary><table><thead><tr><th>Time</th><th>Task/model/role</th><th>Checks</th><th>Review</th></tr></thead><tbody>${benchmarks.map(b=>`<tr><td>${esc(b.evaluated_at)}</td><td>${esc(b.benchmark_id)}<br>${esc(b.model_id)} · ${esc(b.role)}</td><td>${esc(JSON.stringify(b.checks))}</td><td>${esc(b.evaluator_source)}<br>${esc(JSON.stringify(b.dimensions))}<br>${esc(b.note)}</td></tr>`).join('')}</tbody></table></details>`}
async function syncModels(){await api('/api/models/sync',{method:'POST',body:'{}'});await refreshModels()}
async function probeOne(id){await api('/api/models/probe',{method:'POST',body:JSON.stringify({model_ids:[id]})});await refreshModels()}
async function probeEnabled(){await api('/api/models/probe',{method:'POST',body:JSON.stringify({model_ids:models.filter(m=>m.enabled&&m.kind==='chat').map(m=>m.model_id)})});await refreshModels()}
async function toggleModel(id,enabled){await api('/api/models/update',{method:'POST',body:JSON.stringify({model_id:id,enabled})});await refreshModels()}
async function rateModel(id){const m=models.find(x=>x.model_id===id);if(!m)return;const quality=Number(prompt('Quality rating 0-10',m.quality));if(!Number.isFinite(quality))return;const speed=Number(prompt('Speed rating 0-10',m.speed));if(!Number.isFinite(speed))return;const newCaps=prompt('Capabilities, comma separated',m.capabilities.join(','));if(newCaps===null)return;const newNotes=prompt('Notes',m.notes);if(newNotes===null)return;await api('/api/models/update',{method:'POST',body:JSON.stringify({model_id:id,quality:Math.max(0,Math.min(10,quality)),speed:Math.max(0,Math.min(10,speed)),capabilities:newCaps.split(',').map(x=>x.trim()).filter(Boolean),notes:newNotes})});await refreshModels()}
async function dispatch(){const assignments={};document.querySelectorAll('[data-role]').forEach(e=>{if(e.value)assignments[e.dataset.role]=e.value});const payload={objective:objective.value,mode:mode.value,acceptance:acceptance.value,context:context.value,images:imageParts,assignments};dispatchStatus.textContent='Dispatching…';try{const d=await api('/api/runs',{method:'POST',body:JSON.stringify(payload)});dispatchStatus.textContent=`Run ${d.run_id} started`;await refreshRuns();setTimeout(()=>showRun(d.run_id),400)}catch(e){dispatchStatus.textContent=e.message}}
context.addEventListener('dragover',e=>e.preventDefault());context.addEventListener('drop',async e=>{e.preventDefault();for(const file of e.dataTransfer.files){if(file.type.startsWith('text/')||/\.(txt|md|csv|json|ya?ml|xml|js|ts|py|css|html)$/i.test(file.name)){context.value+=(context.value?'\n\n':'')+`===== FILE: ${file.name} =====\n`+await file.text()}else if(file.type.startsWith('image/')){const data=await new Promise(r=>{const reader=new FileReader();reader.onload=()=>r(reader.result);reader.readAsDataURL(file)});imageParts.push({type:'image',data,name:file.name})}else{dispatchStatus.textContent=`Unsupported file: ${file.name}`}}document.getElementById('attachments').textContent=imageParts.length?`Attached images: ${imageParts.map(x=>x.name).join(', ')}`:'Drop text files or images here.'});
async function refreshRuns(){const d=await api('/api/runs');document.getElementById('runs').innerHTML=d.runs.map(r=>`<div class="run" onclick='showRun(${JSON.stringify(r.run_id)})'><b>${esc(r.run_id)}</b> <span class="badge">${esc(r.status)}</span><div class="muted">${esc(r.mode)} · ${esc((r.objective||'').slice(0,130))}</div></div>`).join('')||'<span class="muted">No runs yet.</span>'}
function fileBlock(title,value){if(!value)return'';return `<details><summary>${esc(title)}</summary><pre>${esc(value)}</pre></details>`}
async function showRun(id){const d=await api('/api/runs/'+encodeURIComponent(id));const task=d.task||{};const events=(d.events||[]).map(e=>`<div class="event ${esc(e.event)}"><b>${esc(e.event)}</b> · ${esc(e.agent||e.model||'')} ${e.duration_ms?`· ${e.duration_ms}ms`:''}${e.timeout_seconds?` · timeout ${e.timeout_seconds}s`:''}${e.retry_count!==undefined?` · retries ${e.retry_count}`:''}${e.failure_category?` · ${esc(e.failure_category)}`:''}<br><span class="muted">${esc(e.time||'')}</span>${e.error?`<br>${esc(e.error)}`:''}</div>`).join('');const workers=Object.entries(d.workers||{}).map(([n,v])=>fileBlock(`Return: ${n}`,v)).join('');const prompts=Object.entries(d.prompts||{}).map(([n,v])=>fileBlock(`Sent prompt: ${n}`,v)).join('');detail.innerHTML=`<h3>${esc(id)}</h3><p><b>${esc(task.mode||'')}</b> · ${esc(task.objective||'')}</p>${fileBlock('Acceptance criteria',task.acceptance)}${fileBlock('Task, context accounting, routing, and timeouts',JSON.stringify(task,null,2))}${fileBlock('Partial-success and confidence record',JSON.stringify(d.final_json||{},null,2))}<details open><summary>Timeline</summary>${events||'<span class="muted">No events yet.</span>'}</details>${fileBlock('Final synthesis',d.final)}${fileBlock('Raw judge response',d.judge)}${prompts}${workers}${fileBlock('Failure',d.failure)}`;if(!['complete','failed'].includes(d.status))setTimeout(()=>showRun(id),1500)}
async function refreshAll(){document.getElementById('token').value=token();try{await refreshModels();await refreshRuns()}catch(e){detail.textContent=e.message}}
refreshAll();setInterval(refreshRuns,5000);
</script></body></html>'''


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    result = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
            if isinstance(item, dict):
                result.append(item)
        except json.JSONDecodeError:
            continue
    return result


class DashboardApp:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.catalog = ModelCatalog(config.swarm.catalog_path)
        self.catalog.import_run_history(config.swarm.run_directory)
        self.client = OpenWebUIClient(
            config.openwebui.base_url,
            config.openwebui.endpoint,
            config.openwebui.api_key_env,
            config.openwebui.timeout_seconds,
            config.openwebui.health_endpoint,
            config.openwebui.models_endpoint,
        )
        self.run_root = Path(config.swarm.run_directory).expanduser().resolve()
        self.run_root.mkdir(parents=True, exist_ok=True)
        self.auth_token = os.environ.get(config.dashboard.auth_token_env, "")
        self.metadata_root = Path(config.dashboard.metadata_directory).expanduser().resolve()
        self.metadata_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.metadata_root.chmod(0o700)

    def defaults(self) -> dict[str, str]:
        return {**{w.name: w.model for w in self.config.workers}, "__judge__": self.config.judge.model}

    def list_models(self) -> list[dict[str, Any]]:
        return [self.catalog.as_dict(record, self.config.reliability) for record in self.catalog.list()]

    def sync_models(self) -> list[dict[str, Any]]:
        self.catalog.sync(self.client.list_model_entries())
        return self.list_models()

    def probe_models(self, model_ids: list[str]) -> list[dict[str, Any]]:
        unique_ids = sorted(set(model_ids))

        def probe_one(model_id: str) -> None:
            started = monotonic()
            try:
                result = self.client.chat(
                    model=model_id,
                    system="Return only the requested token.",
                    user="Return exactly: HEALTHY",
                    max_tokens=20,
                    temperature=0.0,
                    timeout_seconds=self.config.probe.timeout_seconds,
                )
                elapsed = int((monotonic() - started) * 1000)
                status = "healthy" if result.content.strip() == "HEALTHY" else "failed"
                self.catalog.record_probe(
                    model_id, status, elapsed,
                    "" if status == "healthy" else f"Unexpected response: {result.content[:200]}",
                )
            except Exception as exc:
                elapsed = int((monotonic() - started) * 1000)
                self.catalog.record_probe(model_id, "failed", elapsed, str(exc))

        with ThreadPoolExecutor(max_workers=min(self.config.probe.max_parallel, max(1, len(unique_ids)))) as executor:
            futures = [executor.submit(probe_one, model_id) for model_id in unique_ids]
            for future in as_completed(futures):
                future.result()
        return self.list_models()

    def list_runs(self) -> list[dict[str, Any]]:
        runs = []
        for run_dir in sorted(self.run_root.iterdir(), reverse=True) if self.run_root.exists() else []:
            if not run_dir.is_dir():
                continue
            task = _read_json(run_dir / "task.json") or {}
            events = _events(run_dir / "events.jsonl")
            status = "running"
            if (run_dir / "final.md").exists():
                status = "complete"
            elif (run_dir / "failure.txt").exists():
                status = "failed"
            runs.append(
                {
                    "run_id": run_dir.name,
                    "status": status,
                    "mode": task.get("mode", ""),
                    "objective": task.get("objective", ""),
                    "events": len(events),
                }
            )
        return runs[:200]

    def run_detail(self, run_id: str) -> dict[str, Any]:
        run_dir = (self.run_root / run_id).resolve()
        if run_dir.parent != self.run_root or not run_dir.exists():
            raise FileNotFoundError(run_id)
        prompts = {p.name: _read_text(p) for p in sorted((run_dir / "prompts").glob("*.txt"))} if (run_dir / "prompts").exists() else {}
        workers = {p.stem: _read_text(p) for p in sorted((run_dir / "workers").glob("*.md"))} if (run_dir / "workers").exists() else {}
        status = "complete" if (run_dir / "final.md").exists() else ("failed" if (run_dir / "failure.txt").exists() else "running")
        return {
            "run_id": run_id,
            "status": status,
            "task": _read_json(run_dir / "task.json") or {},
            "events": _events(run_dir / "events.jsonl"),
            "final": _read_text(run_dir / "final.md"),
            "final_json": _read_json(run_dir / "final.json") or {},
            "judge": _read_text(run_dir / "judge" / "response.md"),
            "failure": _read_text(run_dir / "failure.txt"),
            "prompts": prompts,
            "workers": workers,
        }

    def start_run(self, body: dict[str, Any]) -> str:
        objective = str(body.get("objective", "")).strip()
        if not objective:
            raise ValueError("Objective is required.")
        mode = str(body.get("mode", "auto"))
        if mode not in {"auto", "code", "spec", "research", "general"}:
            raise ValueError("Invalid task mode.")
        acceptance = str(body.get("acceptance", ""))
        context = str(body.get("context", ""))
        image_parts = []
        images = body.get("images", []) if isinstance(body.get("images", []), list) else []
        if len(images) > 8:
            raise ValueError("At most 8 image attachments are allowed.")
        for item in images:
            if not isinstance(item, dict):
                raise ValueError("Invalid image attachment.")
            data_url = str(item.get("data", ""))
            if not data_url.startswith("data:image/") or ";base64," not in data_url:
                raise ValueError("Invalid image attachment.")
            try:
                raw_size = len(base64.b64decode(data_url.split(",", 1)[1], validate=True))
            except ValueError as exc:
                raise ValueError("Invalid image attachment.") from exc
            if raw_size > MAX_IMAGE_BYTES:
                raise ValueError("Image attachment exceeds 10 MiB.")
            image_parts.append({"type": "image_url", "image_url": {"url": data_url}})
        assignments = body.get("assignments", {}) if isinstance(body.get("assignments", {}), dict) else {}
        known_roles = {worker.name for worker in self.config.workers} | {"__judge__"}
        unknown_roles = set(map(str, assignments)) - known_roles
        if unknown_roles:
            raise ValueError("Unknown worker role(s): " + ", ".join(sorted(unknown_roles)))
        for model_id in (str(value) for value in assignments.values() if value):
            record = self.catalog.get(model_id)
            if not record or not (record.enabled and record.available and record.kind == "chat" and record.probe_status == "healthy"):
                raise ValueError(f"Dashboard model is not an enabled, available, healthy chat model: {model_id}")
        role_overrides = {str(k): str(v) for k, v in assignments.items() if k != "__judge__" and v}
        judge_override = str(assignments.get("__judge__", "")) or None
        selection_reasons = {
            role: self.catalog.explicit_override_reason(
                self.catalog.get(model), self.config.reliability
            )
            for role, model in {**role_overrides, **({"__judge__": judge_override} if judge_override else {})}.items()
        }
        stamp = datetime.now(timezone.utc).strftime("web-%Y%m%dT%H%M%SZ-%f")

        def target() -> None:
            try:
                SwarmOrchestrator(self.config).run(
                    objective=objective,
                    mode=mode,
                    acceptance=acceptance,
                    context_parts=[("dashboard-context", context)] if context else [],
                    image_parts=image_parts,
                    role_model_overrides=role_overrides,
                    judge_model_override=judge_override,
                    selection_reasons=selection_reasons,
                    run_id=stamp,
                )
            except Exception:
                traceback.print_exc()

        Thread(target=target, daemon=True).start()
        return stamp


class Handler(BaseHTTPRequestHandler):
    server_version = "OWUISwarmDashboard/0.2"

    @property
    def app(self) -> DashboardApp:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _authorized(self) -> bool:
        return not self.app.auth_token or self.headers.get("X-Swarm-Token", "") == self.app.auth_token

    def _json(self, status: int, data: Any) -> None:
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        data = self.rfile.read(length) if length else b"{}"
        parsed = json.loads(data.decode("utf-8"))
        return parsed if isinstance(parsed, dict) else {}

    def do_GET(self) -> None:
        path = unquote(urlparse(self.path).path)
        if path == "/":
            payload = HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if not self._authorized():
            self._json(401, {"error": "Dashboard token required."})
            return
        try:
            if path == "/api/models":
                self._json(200, {"models": self.app.list_models(), "defaults": self.app.defaults(), "probe_history": self.app.catalog.probe_history(), "probe_timeout_seconds": self.app.config.probe.timeout_seconds, "benchmark_results": self.app.catalog.benchmark_results()})
            elif path == "/api/runs":
                self._json(200, {"runs": self.app.list_runs()})
            elif path.startswith("/api/runs/"):
                self._json(200, self.app.run_detail(path.removeprefix("/api/runs/")))
            else:
                self._json(404, {"error": "Not found."})
        except FileNotFoundError:
            self._json(404, {"error": "Run not found."})
        except Exception as exc:
            self._json(500, {"error": str(exc)})

    def do_POST(self) -> None:
        path = unquote(urlparse(self.path).path)
        if not self._authorized():
            self._json(401, {"error": "Dashboard token required."})
            return
        try:
            body = self._body()
            if path == "/api/models/sync":
                self._json(200, {"models": self.app.sync_models()})
            elif path == "/api/models/probe":
                ids = [str(value) for value in body.get("model_ids", [])]
                self._json(200, {"models": self.app.probe_models(ids)})
            elif path == "/api/models/update":
                model_id = str(body.pop("model_id"))
                record = self.app.catalog.update(model_id, **body)
                self._json(200, self.app.catalog.as_dict(record, self.app.config.reliability))
            elif path == "/api/runs":
                self._json(202, {"run_id": self.app.start_run(body)})
            else:
                self._json(404, {"error": "Not found."})
        except Exception as exc:
            self._json(400, {"error": str(exc)})


def serve(config: AppConfig, host: str | None = None, port: int | None = None) -> None:
    app = DashboardApp(config)
    bind_host = host or config.dashboard.host
    bind_port = port or config.dashboard.port
    if bind_host != "127.0.0.1":
        raise RuntimeError("Dashboard must bind exactly to 127.0.0.1.")
    metadata = app.metadata_root / "server.json"
    metadata.write_text(
        json.dumps({"host": bind_host, "port": bind_port, "started_at": datetime.now(timezone.utc).isoformat()}, indent=2) + "\n",
        encoding="utf-8",
    )
    metadata.chmod(0o600)
    server = ThreadingHTTPServer((bind_host, bind_port), Handler)
    server.app = app  # type: ignore[attr-defined]
    print(f"Swarm dashboard: http://{bind_host}:{bind_port}")
    if app.auth_token:
        print(f"API authentication enabled via {config.dashboard.auth_token_env}.")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
