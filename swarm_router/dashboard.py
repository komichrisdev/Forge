from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
from hmac import compare_digest
from ipaddress import ip_address
from pathlib import Path
from threading import Thread
from time import monotonic
from typing import Any
from urllib.parse import unquote, urlparse
from urllib import error, request
import json
import os
import secrets
import socket
import subprocess
import traceback
import base64


MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_DASHBOARD_BODY_BYTES = 64 * 1024
SESSION_SECONDS = 12 * 60 * 60
FORGE_VERSION = "0.11-dev"
ARCHITECTURE_REVISION = "R11"

from .agents import default_registry
from .catalog import ModelCatalog
from .client import OpenWebUIClient
from .config import AppConfig
from .discord_notifications import NotificationStore, load_config as load_discord_config
from .journal import TaskJournal
from .night_owl import default_state_dir, forge_script_root, validate_night_owl_payload
from .orchestrator import SwarmOrchestrator
from .scheduler import ScheduleError, ScheduleStore, Scheduler


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


FORGE_HTML = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Forge LAN Operations</title>
<style>
:root{color-scheme:dark;--bg:#0b0e13;--panel:#141922;--panel2:#1b2330;--line:#2d3848;--text:#edf2f7;--muted:#9aa8ba;--good:#61d394;--warn:#f2c85b;--bad:#ff7b72;--accent:#8ab4ff}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}header{position:sticky;top:0;z-index:2;display:flex;gap:12px;align-items:center;justify-content:space-between;padding:12px 16px;border-bottom:1px solid var(--line);background:rgba(11,14,19,.96)}h1{font-size:18px;margin:0}a{color:var(--accent)}button,input,select,textarea{background:#10151d;color:var(--text);border:1px solid var(--line);border-radius:8px;padding:8px}button{cursor:pointer}button:hover{border-color:var(--accent)}nav{display:flex;gap:6px;overflow:auto;padding:10px 16px;border-bottom:1px solid var(--line)}nav button[aria-current=true]{border-color:var(--accent);background:var(--panel2)}main{padding:14px;max-width:1280px;margin:auto}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:10px}.panel{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:12px;margin-bottom:12px}.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.muted{color:var(--muted)}.badge{display:inline-block;border:1px solid var(--line);border-radius:999px;padding:2px 8px;font-size:12px}.healthy,.completed,.confirmed,.enabled{color:var(--good)}.warning,.overdue,.unknown,.running,.blocked{color:var(--warn)}.failed,.disabled,.error{color:var(--bad)}table{width:100%;border-collapse:collapse}th,td{text-align:left;vertical-align:top;border-bottom:1px solid var(--line);padding:7px}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#0f131a;border:1px solid var(--line);border-radius:8px;padding:10px;max-height:360px;overflow:auto}.hide{display:none}.actions{display:flex;gap:6px;flex-wrap:wrap}.login{max-width:420px;margin:10vh auto}@media(max-width:720px){header{align-items:flex-start;flex-direction:column}main{padding:10px}th,td{font-size:12px;padding:5px}.actions button{width:100%}}
</style></head><body>
<header><div><h1>Forge LAN Operations</h1><div class="muted">Owner-operated dashboard for local Forge automation.</div></div><div class="row"><a id="openwebui" href="#" target="_blank" rel="noreferrer">Open WebUI</a><button id="refresh">Refresh</button><button id="logout">Logout</button></div></header>
<section id="login" class="panel login"><h2>Owner login</h2><p class="muted">Use the dashboard secret from the local Forge configuration.</p><input id="secret" type="password" autocomplete="current-password" style="width:100%" placeholder="Dashboard secret"><div class="row" style="margin-top:10px"><button id="loginButton">Login</button><span id="loginStatus" class="muted"></span></div></section>
<nav id="nav" class="hide" aria-label="Dashboard views"><button data-view="overview" aria-current="true">Overview</button><button data-view="tasks">Tasks</button><button data-view="schedules">Schedules</button><button data-view="nightowl">Night Owl</button><button data-view="notifications">Notifications</button><button data-view="agents">Agents</button><button data-view="providers">Providers</button><button data-view="dispatch">Dispatch</button></nav>
<main id="app" class="hide"><div id="content" class="panel">Loading…</div></main>
<script>
let csrf='',view='overview',cache={};
const $=id=>document.getElementById(id);
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function cls(s){s=String(s??'').toLowerCase();return s.includes('fail')||s==='disabled'||s==='error'?'failed':s.includes('warn')||s.includes('unknown')||s==='blocked'||s==='overdue'||s==='running'?'warning':'healthy'}
async function api(path,opts={}){opts.headers={...(opts.headers||{}),'Content-Type':'application/json'};if(csrf)opts.headers['X-CSRF-Token']=csrf;const r=await fetch(path,opts);let text=await r.text();let data={};try{data=text?JSON.parse(text):{}}catch{data={error:text}}if(!r.ok)throw new Error(data.error||data.message||text||r.status);return data}
async function login(){try{const d=await api('/api/login',{method:'POST',body:JSON.stringify({secret:$('secret').value})});csrf=d.csrf_token;$('login').classList.add('hide');$('nav').classList.remove('hide');$('app').classList.remove('hide');await load()}catch(e){$('loginStatus').textContent=e.message}}
async function session(){try{const d=await api('/api/session');csrf=d.csrf_token||'';if(d.authenticated){$('login').classList.add('hide');$('nav').classList.remove('hide');$('app').classList.remove('hide');await load();return}}catch{}$('app').classList.add('hide');$('nav').classList.add('hide');$('login').classList.remove('hide')}
function card(k,v,s=''){return `<section class="panel"><div class="muted">${esc(k)}</div><strong class="${cls(s||v)}">${esc(v)}</strong></section>`}
function table(rows,cols){return `<table><thead><tr>${cols.map(c=>`<th>${esc(c[0])}</th>`).join('')}</tr></thead><tbody>${rows.map(r=>`<tr>${cols.map(c=>`<td>${c[1](r)}</td>`).join('')}</tr>`).join('')||`<tr><td colspan="${cols.length}" class="muted">No rows.</td></tr>`}</tbody></table>`}
async function load(){try{cache[view]=await api('/api/'+view.replace('nightowl','night-owl'));render()}catch(e){$('content').innerHTML=`<p class="failed">${esc(e.message)}</p>`}}
function render(){const d=cache[view]||{};if(view==='overview')return renderOverview(d);if(view==='tasks')return renderTasks(d);if(view==='schedules')return renderSchedules(d);if(view==='nightowl')return renderNightOwl(d);if(view==='notifications')return renderNotifications(d);if(view==='agents')return renderAgents(d);if(view==='providers')return renderProviders(d);renderDispatch(d)}
function renderOverview(d){$('openwebui').href=d.openwebui?.url||'#';$('content').innerHTML=`<h2>Overview</h2><div class="grid">${card('Forge',`${d.forge_version} / ${d.architecture_revision}`)+card('Personal backend',d.personal_backend?.status||'unknown')+card('Scheduler service',d.scheduler_service?.status||'unknown')+card('Dashboard service',d.dashboard_service?.status||'unknown')+card('Open WebUI',d.openwebui?.status||'unknown')+card('Night Owl',`${d.night_owl?.enabled?'enabled':'disabled'} · ${d.night_owl?.next_run_at||'no next run'}`,d.night_owl?.enabled?'enabled':'disabled')+card('Discord',d.discord?.valid?'configured':'not configured',d.discord?.valid?'healthy':'failed')+card('Notifications',d.discord?.latest_delivery?.status||'none')+card('Providers',`${d.providers?.provider_count||0} providers / ${d.providers?.model_count||0} models`)+card('Quarantined models',d.providers?.quarantined_model_count||0,d.providers?.quarantined_model_count?'warning':'healthy')+card('Active tasks',d.tasks?.active_count||0,d.tasks?.active_count?'warning':'healthy')+card('Failed tasks',d.tasks?.failed_count||0,d.tasks?.failed_count?'failed':'healthy')+card('Suspected orphans',d.tasks?.suspected_orphan_count||0,d.tasks?.suspected_orphan_count?'warning':'healthy')}</div><pre>${esc(JSON.stringify(d,null,2))}</pre>`}
function renderTasks(d){$('content').innerHTML=`<h2>Tasks</h2>${table(d.tasks||[],[['Forge task',r=>`<button onclick="taskDetail('${esc(r.task_id)}')">${esc(r.task_id)}</button>`],['Personal',r=>esc(r.personal_task_id||'')],['Type',r=>esc(r.task_type||'')],['Agent',r=>esc((r.agents||[]).join(', ')||r.agent_id||'')],['Status',r=>`<span class="${cls(r.status)}">${esc(r.status)}</span>`],['Created',r=>esc(r.created_at||'')],['Schedule',r=>esc(r.schedule_id||'')],['Recovery',r=>esc(r.recovery_status?.replay_safety||'')]])}<div id="detail"></div>`}
async function taskDetail(id){const d=await api('/api/tasks/'+encodeURIComponent(id));$('detail').innerHTML=`<h3>${esc(id)}</h3><h4>Events</h4>${table(d.events||[],[['Time',r=>esc(r.timestamp)],['Event',r=>esc(r.event_type)],['Agent',r=>esc(r.agent_id)],['Stage',r=>esc(r.stage)],['Side effect',r=>esc(r.side_effect_state)],['Message',r=>esc(r.message)]])}<h4>Checkpoints</h4><pre>${esc(JSON.stringify(d.checkpoints||[],null,2))}</pre><h4>Task</h4><pre>${esc(JSON.stringify(d.personal_task||{},null,2))}</pre>`}
async function scheduleAction(id,action,confirmText){const body={confirm:confirmText};const d=await api('/api/schedules/'+encodeURIComponent(id)+'/'+action,{method:'POST',body:JSON.stringify(body)});alert(JSON.stringify(d,null,2));await load()}
function renderSchedules(d){$('content').innerHTML=`<h2>Schedules</h2>${table(d.schedules||[],[['ID',r=>esc(r.schedule_id)],['Name',r=>esc(r.name)],['Task',r=>esc(r.task_type)],['Agent',r=>esc(r.agent_id)],['State',r=>`<span class="${cls(r.state)}">${esc(r.enabled?'enabled':'disabled')} · ${esc(r.state)}</span>`],['Trigger',r=>esc(r.trigger_type+' '+JSON.stringify(r.trigger_configuration))],['Next',r=>esc(r.next_run_at)],['Policies',r=>esc(`${r.overlap_policy}/${r.misfire_policy}`)],['Actions',r=>`<div class="actions"><button onclick="scheduleAction('${esc(r.schedule_id)}','enable','enable ${esc(r.schedule_id)}')">Enable</button><button onclick="scheduleAction('${esc(r.schedule_id)}','disable','disable ${esc(r.schedule_id)}')">Disable</button><button onclick="scheduleAction('${esc(r.schedule_id)}','run-now','run now ${esc(r.schedule_id)}')">Run now</button></div>`]])}`}
async function night(action,confirmText){const d=await api('/api/night-owl/'+action,{method:'POST',body:JSON.stringify({confirm:confirmText})});alert(JSON.stringify(d,null,2));await load()}
function renderNightOwl(d){$('content').innerHTML=`<h2>Night Owl</h2><div class="grid">${card('Schedule',d.schedule?.enabled?'enabled':'disabled',d.schedule?.enabled?'enabled':'disabled')+card('Cadence',d.schedule?.trigger_configuration?.expression||'')+card('Next run',d.schedule?.next_run_at||'')+card('Last run',d.schedule?.last_run_at||'')+card('Legacy cron',d.legacy_cron?.status||'unknown')+card('Last Discord',d.last_discord_delivery?.status||'none')}</div><div class="panel actions"><button onclick="night('dry-run','run night owl dry-run')">Run dry-run</button><button onclick="night('live','RUN NIGHT OWL LIVE')">Run live</button></div><pre>${esc(JSON.stringify(d,null,2))}</pre>`}
function renderNotifications(d){$('content').innerHTML=`<h2>Notifications</h2>${(d.unknown||[]).length?'<p class="warning">Unknown deliveries require manual review.</p>':''}${table(d.notifications||[],[['ID',r=>esc(r.notification_id)],['Event',r=>esc(r.event_type)],['Severity',r=>esc(r.severity)],['State',r=>`<span class="${cls(r.status)}">${esc(r.status)} / ${esc(r.side_effect_state)}</span>`],['Task',r=>esc(r.forge_task_id||r.task_id||'')],['Time',r=>esc(r.created_at)],['External',r=>esc(r.external_message_id||'')],['Error',r=>esc(r.error_summary||'')]])}`}
function renderAgents(d){$('content').innerHTML=`<h2>Agents</h2>${table(d.agents||[],[['ID',r=>esc(r.agent_id)],['Name',r=>esc(r.display_name)],['Enabled',r=>esc(r.enabled)],['Task types',r=>esc((r.supported_task_types||[]).join(', '))],['Version',r=>esc(r.version)],['Description',r=>esc(r.description)]])}`}
function renderProviders(d){$('content').innerHTML=`<h2>Providers</h2>${table(d.providers||[],[['Provider',r=>esc(r.provider_id)],['Health',r=>`<span class="${cls(r.health)}">${esc(r.health)}</span>`],['Revision',r=>esc(r.inventory_revision)],['Last refresh',r=>esc(r.last_refresh_attempt||'')],['Cooldown',r=>esc(r.cooldown_until||'')]])}<h3>Models</h3>${table(d.models||[],[['Provider',r=>esc(r.provider_id)],['Model',r=>esc(r.model_id)],['Health',r=>esc(r.health)],['Capabilities',r=>esc((r.capabilities||[]).join(', '))],['Flags',r=>esc(`${r.quarantined?'quarantined ':''}${r.available?'available':'disabled'}`)]])}`}
async function dispatch(action){const body={task_type:$('taskType').value,mode:$('dispatchMode').value,confirm:$('dispatchConfirm').value};const d=await api('/api/dispatch',{method:'POST',body:JSON.stringify(body)});$('dispatchResult').textContent=JSON.stringify(d,null,2)}
function renderDispatch(){ $('content').innerHTML=`<h2>Dispatch approved Forge task</h2><p class="muted">Only approved task types are available. No shell commands or arbitrary paths are accepted.</p><label>Task type</label><select id="taskType"><option value="night_owl">Night Owl</option></select><label>Mode</label><select id="dispatchMode"><option value="dry_run">dry-run</option><option value="live">live</option></select><label>Confirmation</label><input id="dispatchConfirm" style="width:100%" placeholder="dry-run: run night owl dry-run; live: RUN NIGHT OWL LIVE"><div class="actions" style="margin-top:10px"><button onclick="dispatch()">Submit task</button></div><pre id="dispatchResult"></pre>`}
$('loginButton').onclick=login;$('secret').onkeydown=e=>{if(e.key==='Enter')login()};$('refresh').onclick=load;$('logout').onclick=async()=>{await api('/api/logout',{method:'POST',body:'{}'}).catch(()=>{});csrf='';session()};document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>{view=b.dataset.view;document.querySelectorAll('nav button').forEach(x=>x.setAttribute('aria-current',x===b?'true':'false'));load()});session();
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


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _public_url(base_url: str, host: str) -> str:
    parsed = urlparse(base_url)
    if parsed.hostname in {"127.0.0.1", "localhost"} and host not in {"127.0.0.1", "localhost"}:
        port = f":{parsed.port}" if parsed.port else ""
        return f"{parsed.scheme}://{host}{port}"
    return base_url


def _service_status(name: str) -> dict[str, str]:
    try:
        result = subprocess.run(
            ["systemctl", "--user", "is-active", name],
            text=True,
            capture_output=True,
            timeout=2,
            check=False,
        )
        status = (result.stdout or result.stderr).strip() or "unknown"
    except Exception:
        status = "unknown"
    return {"name": name, "status": status}


def _http_health(url: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
    try:
        req = request.Request(url, headers=headers or {})
        with request.urlopen(req, timeout=2) as response:
            return {"status": "healthy" if 200 <= response.status < 300 else "warning", "http_status": response.status}
    except Exception as exc:
        return {"status": "failed", "error": str(exc)[:200]}


def _secretish(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in ("token", "secret", "password", "webhook", "cookie", "authorization", "api_key"))


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): ("<redacted>" if _secretish(str(k)) else _sanitize(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    if isinstance(value, str):
        if "discord.com/api/webhooks/" in value or "discordapp.com/api/webhooks/" in value:
            return "<redacted>"
        return value[:2000]
    return value


def _private_host(value: str) -> bool:
    try:
        address = ip_address(value)
    except ValueError:
        return value in {"localhost", "forge.local"}
    return address.is_loopback or address.is_private


def _host_without_port(header: str) -> str:
    host = header.split(",", 1)[0].strip()
    if host.startswith("[") and "]" in host:
        return host[1:host.index("]")]
    return host.rsplit(":", 1)[0] if ":" in host else host


def _cron_status() -> dict[str, Any]:
    try:
        result = subprocess.run(["crontab", "-l"], text=True, capture_output=True, timeout=2, check=False)
        text = result.stdout or ""
    except Exception as exc:
        return {"status": "unknown", "error": str(exc)[:200]}
    active = []
    disabled = []
    for line in text.splitlines():
        if "night-owl" not in line:
            continue
        (disabled if line.lstrip().startswith("#") else active).append(line.strip())
    return {
        "status": "active" if active else ("disabled" if disabled else "absent"),
        "active_entries": len(active),
        "disabled_entries": len(disabled),
    }


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
        self.journal = TaskJournal(config.swarm.catalog_path)
        self.schedules = ScheduleStore(config.swarm.catalog_path)
        self.notifications = NotificationStore(config.swarm.catalog_path)
        self.registry = default_registry()
        self.sessions: dict[str, dict[str, Any]] = {}
        self.failed_logins: dict[str, int] = {}
        self.allowed_hosts = {"127.0.0.1", "localhost", "forge.local", config.dashboard.host}

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

    def valid_host(self, host_header: str) -> bool:
        if not host_header:
            return False
        host = _host_without_port(host_header)
        return host in self.allowed_hosts and _private_host(host)

    def login(self, secret: str, remote: str) -> dict[str, str]:
        if not self.auth_token:
            raise ValueError(f"{self.config.dashboard.auth_token_env} is not configured")
        if self.failed_logins.get(remote, 0) >= 8:
            raise ValueError("Too many failed login attempts.")
        if not compare_digest(secret, self.auth_token):
            self.failed_logins[remote] = self.failed_logins.get(remote, 0) + 1
            self._audit("login", "owner", False, "dashboard", "invalid secret")
            raise ValueError("Invalid dashboard secret.")
        session_id = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        self.sessions[session_id] = {
            "csrf": csrf,
            "expires_at": _utc_now() + timedelta(seconds=SESSION_SECONDS),
        }
        self.failed_logins.pop(remote, None)
        self._audit("login", "owner", True, "dashboard", "")
        return {"session_id": session_id, "csrf_token": csrf}

    def session(self, session_id: str) -> dict[str, Any] | None:
        item = self.sessions.get(session_id)
        if not item:
            return None
        if item["expires_at"] <= _utc_now():
            self.sessions.pop(session_id, None)
            return None
        return item

    def logout(self, session_id: str) -> None:
        self.sessions.pop(session_id, None)
        self._audit("logout", "owner", True, "dashboard", "")

    def overview(self) -> dict[str, Any]:
        schedule_status = self.schedules.status(self._personal_status)
        night_owl = next((item for item in schedule_status["schedules"] if item["task_type"] == "night_owl"), None)
        tasks = self.task_rows()
        provider_state = self.provider_summary()
        discord = load_discord_config().public()
        delivery_status = self.notifications.status()
        return {
            "forge_version": FORGE_VERSION,
            "architecture_revision": ARCHITECTURE_REVISION,
            "personal_backend": _http_health(f"http://{self.config.personal.loopback_host}:{self.config.personal.port}/health"),
            "scheduler_service": _service_status("forge-scheduler.service"),
            "dashboard_service": _service_status("owui-swarm-dashboard.service"),
            "openwebui": {
                **_http_health(self.config.openwebui.base_url + self.config.openwebui.health_endpoint),
                "url": _public_url(self.config.openwebui.base_url, self.config.dashboard.host),
            },
            "night_owl": night_owl,
            "discord": {**discord, "latest_delivery": delivery_status.get("latest_success") or delivery_status.get("latest_failure")},
            "providers": provider_state,
            "tasks": {
                "active_count": sum(1 for item in tasks if item["status"] in {"created", "assigned", "running"}),
                "failed_count": sum(1 for item in tasks if item["status"] == "failed"),
                "suspected_orphan_count": len(self.journal.orphan_candidates()),
            },
        }

    def task_rows(self) -> list[dict[str, Any]]:
        personal = self._personal_index()
        rows = []
        for task in reversed(self.journal.list_tasks()):
            events = self.journal.events(task["task_id"])
            metadata: dict[str, Any] = {}
            for event in events:
                metadata.update(event.metadata)
            personal_task_id = str(metadata.get("personal_task_id") or "")
            item = {
                **task,
                "personal_task_id": personal_task_id,
                "task_type": metadata.get("task_type") or personal.get(personal_task_id, {}).get("task_type", ""),
                "agent_id": personal.get(personal_task_id, {}).get("agent_id", ""),
                "schedule_id": metadata.get("schedule_id", ""),
                "occurrence_id": metadata.get("occurrence_id", ""),
                "completion_time": personal.get(personal_task_id, {}).get("completion_time", ""),
                "recovery_status": self.journal.recovery_status(task["task_id"]),
            }
            rows.append(_sanitize(item))
        return rows[:100]

    def task_detail(self, task_id: str) -> dict[str, Any]:
        events = [event.to_dict() for event in self.journal.events(task_id)]
        metadata: dict[str, Any] = {}
        for event in events:
            metadata.update(event.get("metadata") if isinstance(event.get("metadata"), dict) else {})
        personal_task_id = str(metadata.get("personal_task_id") or "")
        return _sanitize({
            "task": self.journal.reconstruct(task_id),
            "events": events,
            "checkpoints": [checkpoint.to_dict() for checkpoint in self.journal.checkpoints(task_id)],
            "recovery_status": self.journal.recovery_status(task_id),
            "personal_task": self._personal_index().get(personal_task_id, {}),
            "notifications": [
                row for row in self.notifications.list(200)
                if row.get("forge_task_id") == task_id or row.get("task_id") == personal_task_id
            ],
        })

    def schedule_rows(self) -> dict[str, Any]:
        return _sanitize(self.schedules.status(self._personal_status))

    def set_schedule_enabled(self, schedule_id: str, enabled: bool, confirm: str) -> dict[str, Any]:
        expected = f"{'enable' if enabled else 'disable'} {schedule_id}"
        if confirm != expected:
            raise ValueError(f"Confirmation must be exactly: {expected}")
        schedule = self.schedules.set_enabled(schedule_id, enabled)
        self._audit("schedule_enable" if enabled else "schedule_disable", "owner", True, schedule_id, "")
        return _sanitize(schedule.to_dict())

    def schedule_run_now(self, schedule_id: str, confirm: str) -> dict[str, Any]:
        expected = f"run now {schedule_id}"
        if confirm != expected:
            raise ValueError(f"Confirmation must be exactly: {expected}")
        result = self._scheduler().run_once(schedule_id)
        self._audit("schedule_run_now", "owner", True, schedule_id, str(result.get("task_id") or ""))
        return _sanitize(result)

    def night_owl_status(self) -> dict[str, Any]:
        schedules = [item for item in self.schedule_rows()["schedules"] if item["task_type"] == "night_owl"]
        rows = [row for row in self.task_rows() if row.get("task_type") == "night_owl"]
        deliveries = [
            row for row in self.notifications.list(50)
            if str(row.get("event_type", "")).startswith("night_owl")
        ]
        return {
            "schedule": schedules[0] if schedules else None,
            "recent_tasks": rows[:10],
            "last_result": rows[0] if rows else None,
            "last_checkpoint": self._last_night_owl_checkpoint(rows[0]["task_id"]) if rows else None,
            "last_discord_delivery": deliveries[0] if deliveries else None,
            "legacy_cron": _cron_status(),
            "state_dir": str(default_state_dir()),
            "rollback": "Disable the Forge schedule, then uncomment the preserved Night Owl cron entry if rollback is required.",
        }

    def dispatch_night_owl(self, *, live: bool, confirm: str) -> dict[str, Any]:
        expected = "RUN NIGHT OWL LIVE" if live else "run night owl dry-run"
        if confirm != expected:
            raise ValueError(f"Confirmation must be exactly: {expected}")
        payload = self._night_owl_payload(live=live)
        body = {
            "model": self.config.personal.model_id,
            "messages": [{"role": "user", "content": "Forge dashboard Night Owl dispatch."}],
            "task_type": "night_owl",
            "agent_id": "night_owl",
            "task_payload": payload,
            "metadata": {"dashboard_action": "night_owl_live" if live else "night_owl_dry_run", "manual": True},
        }
        task = self._submit_personal_task(body)
        self._audit("night_owl_live" if live else "night_owl_dry_run", "owner", True, str(task.get("task_id") or ""), str(task.get("forge_task_id") or ""))
        return _sanitize(task)

    def dispatch(self, body: dict[str, Any]) -> dict[str, Any]:
        if set(body) - {"task_type", "mode", "confirm"}:
            raise ValueError("Unknown dispatch fields are not allowed.")
        if str(body.get("task_type")) != "night_owl":
            raise ValueError("Only night_owl dispatch is enabled.")
        mode = str(body.get("mode", "dry_run"))
        if mode not in {"dry_run", "live"}:
            raise ValueError("Unsupported dispatch mode.")
        return self.dispatch_night_owl(live=mode == "live", confirm=str(body.get("confirm", "")))

    def provider_summary(self) -> dict[str, Any]:
        state = self.catalog.provider_status()
        models = state["models"]
        providers = state["providers"]
        return _sanitize({
            "provider_count": len(providers),
            "model_count": len(models),
            "available_model_count": sum(1 for item in models if item["available"] and not item["quarantined"]),
            "quarantined_model_count": sum(1 for item in models if item["quarantined"]),
            "disabled_model_count": sum(1 for item in models if not item["available"]),
            "providers": providers,
            "models": models,
        })

    def notification_rows(self) -> dict[str, Any]:
        rows = self.notifications.list(100)
        return _sanitize({"notifications": rows, "unknown": [row for row in rows if row["status"] == "unknown"]})

    def agents_status(self) -> dict[str, Any]:
        return self.registry.status()

    def _scheduler(self) -> Scheduler:
        return Scheduler(self.config, submit_task=self._submit_schedule_task)

    def _submit_schedule_task(self, schedule: Any, occurrence: dict[str, Any]) -> dict[str, Any]:
        scheduler = Scheduler(self.config, submit_task=lambda _s, _o: {})
        return self._submit_personal_task(scheduler._task_body(schedule, occurrence))

    def _submit_personal_task(self, body: dict[str, Any]) -> dict[str, Any]:
        token = os.environ.get(self.config.personal.auth_token_env, "")
        if not token:
            raise ValueError(f"{self.config.personal.auth_token_env} is required")
        req = request.Request(
            f"http://{self.config.personal.loopback_host}:{self.config.personal.port}/api/personal-tasks",
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        try:
            with request.urlopen(req, timeout=10) as response:
                data = json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            raise ValueError(exc.read().decode("utf-8")[:500]) from exc
        return data if isinstance(data, dict) else {}

    def _night_owl_payload(self, *, live: bool) -> dict[str, Any]:
        schedules = [item for item in self.schedules.list() if item.task_type == "night_owl"]
        payload = dict(schedules[0].payload if schedules else {})
        payload.update({"mode": "live" if live else "dry_run", "dry_run": not live})
        payload.setdefault("operation", "run_nightly")
        payload.setdefault("script_path", str(forge_script_root() / "run_nightly.sh"))
        payload.setdefault("timeout_seconds", 14_400 if live else 300)
        payload.setdefault("run_hours", 4)
        issues = validate_night_owl_payload(payload)
        if issues:
            raise ValueError("; ".join(issues))
        return payload

    def _personal_index(self) -> dict[str, dict[str, Any]]:
        root = Path(self.config.personal.task_directory).expanduser().resolve()
        result: dict[str, dict[str, Any]] = {}
        if not root.exists():
            return result
        for path in root.iterdir():
            if not path.is_dir():
                continue
            data = _read_json(path / "task.json")
            if isinstance(data, dict):
                result[path.name] = _sanitize(data)
        return result

    def _personal_status(self, task_id: str) -> str:
        return str(self._personal_index().get(task_id, {}).get("status") or "")

    def _last_night_owl_checkpoint(self, task_id: str) -> dict[str, Any] | None:
        checkpoints = [checkpoint.to_dict() for checkpoint in self.journal.checkpoints(task_id)]
        return checkpoints[-1] if checkpoints else None

    def _audit(self, action: str, actor: str, success: bool, target: str, message: str) -> None:
        record = {
            "timestamp": _utc_now().isoformat(),
            "actor": actor,
            "action": action,
            "target": target,
            "success": success,
            "message": message[:300],
        }
        path = self.metadata_root / "audit.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(_sanitize(record), sort_keys=True) + "\n")
        path.chmod(0o600)


class Handler(BaseHTTPRequestHandler):
    server_version = "ForgeDashboard/0.11"

    @property
    def app(self) -> DashboardApp:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _valid_host(self) -> bool:
        return self.app.valid_host(self.headers.get("Host", ""))

    def _session_id(self) -> str:
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        morsel = cookie.get("forge_session")
        return morsel.value if morsel else ""

    def _session(self) -> dict[str, Any] | None:
        return self.app.session(self._session_id())

    def _authorized(self) -> bool:
        token = self.headers.get("X-Swarm-Token", "")
        if self.app.auth_token and compare_digest(token, self.app.auth_token):
            return True
        return self._session() is not None

    def _csrf_ok(self) -> bool:
        session = self._session()
        if not session:
            return bool(self.app.auth_token and compare_digest(self.headers.get("X-Swarm-Token", ""), self.app.auth_token))
        return compare_digest(self.headers.get("X-CSRF-Token", ""), str(session.get("csrf", "")))

    def _headers(self) -> None:
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        self.send_header("Cache-Control", "no-store")

    def _json(self, status: int, data: Any) -> None:
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self._headers()
        self.end_headers()
        self.wfile.write(payload)

    def _html(self, status: int, text: str) -> None:
        payload = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self._headers()
        self.end_headers()
        self.wfile.write(payload)

    def _set_cookie_json(self, status: int, data: Any, session_id: str) -> None:
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Set-Cookie", f"forge_session={session_id}; Path=/; Max-Age={SESSION_SECONDS}; HttpOnly; SameSite=Strict")
        self._headers()
        self.end_headers()
        self.wfile.write(payload)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > MAX_DASHBOARD_BODY_BYTES:
            raise ValueError("Request body is too large.")
        data = self.rfile.read(length) if length else b"{}"
        parsed = json.loads(data.decode("utf-8"))
        return parsed if isinstance(parsed, dict) else {}

    def _route_get(self, path: str) -> Any:
        if path == "/api/session":
            session = self._session()
            return {"authenticated": bool(session), "csrf_token": str(session.get("csrf", "")) if session else ""}
        if path == "/api/overview":
            return self.app.overview()
        if path == "/api/tasks":
            return {"tasks": self.app.task_rows()}
        if path.startswith("/api/tasks/"):
            return self.app.task_detail(path.removeprefix("/api/tasks/"))
        if path == "/api/schedules":
            return self.app.schedule_rows()
        if path == "/api/night-owl":
            return self.app.night_owl_status()
        if path == "/api/notifications":
            return self.app.notification_rows()
        if path.startswith("/api/notifications/"):
            return self.app.notifications.get(path.removeprefix("/api/notifications/"))
        if path == "/api/agents":
            return self.app.agents_status()
        if path == "/api/providers":
            return self.app.provider_summary()
        if path == "/api/models":
            return {"models": self.app.list_models(), "defaults": self.app.defaults(), "probe_history": self.app.catalog.probe_history(), "probe_timeout_seconds": self.app.config.probe.timeout_seconds, "benchmark_results": self.app.catalog.benchmark_results()}
        if path == "/api/runs":
            return {"runs": self.app.list_runs()}
        if path.startswith("/api/runs/"):
            return self.app.run_detail(path.removeprefix("/api/runs/"))
        raise FileNotFoundError(path)

    def _route_post(self, path: str, body: dict[str, Any]) -> Any:
        if path.startswith("/api/schedules/"):
            parts = path.split("/")
            if len(parts) == 5 and parts[4] in {"enable", "disable"}:
                return self.app.set_schedule_enabled(parts[3], parts[4] == "enable", str(body.get("confirm", "")))
            if len(parts) == 5 and parts[4] == "run-now":
                return self.app.schedule_run_now(parts[3], str(body.get("confirm", "")))
        if path == "/api/night-owl/dry-run":
            return self.app.dispatch_night_owl(live=False, confirm=str(body.get("confirm", "")))
        if path == "/api/night-owl/live":
            return self.app.dispatch_night_owl(live=True, confirm=str(body.get("confirm", "")))
        if path == "/api/dispatch":
            return self.app.dispatch(body)
        if path == "/api/models/sync":
            return {"models": self.app.sync_models()}
        if path == "/api/models/probe":
            ids = [str(value) for value in body.get("model_ids", [])]
            return {"models": self.app.probe_models(ids)}
        if path == "/api/models/update":
            model_id = str(body.pop("model_id"))
            record = self.app.catalog.update(model_id, **body)
            return self.app.catalog.as_dict(record, self.app.config.reliability)
        if path == "/api/runs":
            return {"run_id": self.app.start_run(body)}
        raise FileNotFoundError(path)

    def do_GET(self) -> None:
        path = unquote(urlparse(self.path).path)
        if not self._valid_host():
            self._json(421, {"error": "Invalid Host header."})
            return
        if path == "/health":
            self._json(200, {"status": "ok", "service": "forge-dashboard", "version": FORGE_VERSION})
            return
        if path == "/":
            self._html(200, FORGE_HTML)
            return
        if not self._authorized():
            self._json(401, {"error": "Dashboard token required."})
            return
        try:
            self._json(200, self._route_get(path))
        except FileNotFoundError:
            self._json(404, {"error": "Not found."})
        except Exception as exc:
            self._json(500, {"error": str(exc)[:500]})

    def do_POST(self) -> None:
        path = unquote(urlparse(self.path).path)
        if not self._valid_host():
            self._json(421, {"error": "Invalid Host header."})
            return
        if not self._authorized():
            if path == "/api/login":
                try:
                    session = self.app.login(str(self._body().get("secret", "")), self.client_address[0])
                    self._set_cookie_json(200, {"authenticated": True, "csrf_token": session["csrf_token"]}, session["session_id"])
                except Exception as exc:
                    self._json(401, {"error": str(exc)[:300]})
                return
            self._json(401, {"error": "Dashboard login required."})
            return
        if path == "/api/logout":
            if self._csrf_ok():
                self.app.logout(self._session_id())
            self.send_response(204)
            self.send_header("Set-Cookie", "forge_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict")
            self._headers()
            self.end_headers()
            return
        if not self._csrf_ok():
            self._json(403, {"error": "CSRF token required."})
            return
        try:
            status = 202 if path in {"/api/runs", "/api/night-owl/dry-run", "/api/night-owl/live", "/api/dispatch"} or path.endswith("/run-now") else 200
            self._json(status, self._route_post(path, self._body()))
        except FileNotFoundError:
            self._json(404, {"error": "Not found."})
        except Exception as exc:
            self._json(400, {"error": str(exc)[:500]})


def serve(config: AppConfig, host: str | None = None, port: int | None = None) -> None:
    app = DashboardApp(config)
    bind_host = host or config.dashboard.host
    bind_port = port or config.dashboard.port
    if not _private_host(bind_host):
        raise RuntimeError("Dashboard host must be localhost or a private LAN address.")
    bind_hosts = [bind_host] if bind_host in {"127.0.0.1", "localhost"} else ["127.0.0.1", bind_host]
    app.allowed_hosts.update(bind_hosts)
    metadata = app.metadata_root / "server.json"
    metadata.write_text(
        json.dumps({"hosts": bind_hosts, "port": bind_port, "started_at": datetime.now(timezone.utc).isoformat()}, indent=2) + "\n",
        encoding="utf-8",
    )
    metadata.chmod(0o600)
    servers = []
    for item in dict.fromkeys(bind_hosts):
        server = ThreadingHTTPServer((item, bind_port), Handler)
        server.app = app  # type: ignore[attr-defined]
        servers.append(server)
    print("Forge dashboard:")
    for server in servers:
        address = server.server_address
        print(f"- http://{address[0]}:{address[1]}")
    if app.auth_token:
        print(f"API authentication enabled via {config.dashboard.auth_token_env}.")
    print("Press Ctrl+C to stop.")
    threads = [Thread(target=server.serve_forever, daemon=True) for server in servers]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    except KeyboardInterrupt:
        pass
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
