# Open WebUI Codex Swarm

A local, token-conscious orchestration layer for models exposed through Open
WebUI. Workers and the judge produce untrusted proposals; Codex keeps repository
access, testing, final verification, changes, and user-facing authority.

The read-only native ChatGPT MCP App is documented in [chatgpt_app/README.md](chatgpt_app/README.md).

## Authority

The enforced order is:

1. User requirements
2. Codex instructions
3. Worker role instructions
4. Worker assumptions

No model is universally required. Planner, implementer, critic, verifier, and
judge can use different model IDs on every task.

## Debian install

Extract the archive so the project is at `~/openwebui-codex-swarm`, then run:

```bash
cd ~/openwebui-codex-swarm
chmod +x scripts/install.sh
./scripts/install.sh
```

The installer creates:

- `~/openwebui-codex-swarm/.venv`
- `~/.local/bin/owui-swarm`
- `~/.config/owui-swarm/config.toml`
- `~/.config/owui-swarm/environment` with mode `0600`
- `~/.agents/skills/openwebui-swarm`

Keep the API key out of TOML. Put it only in the protected environment file:

```text
OPEN_WEBUI_API_KEY=<restricted-key>
```

Ensure `~/.local/bin` is in `PATH`, then verify:

```bash
owui-swarm --help
owui-swarm doctor
```

## Endpoints and limits

`~/.config/owui-swarm/config.toml` configures the discovered Open WebUI base
URL, health/model/chat endpoints, separate finite worker/judge/probe timeouts,
worker count, parallel call limits, input characters, worker and judge output
tokens, returned synthesis characters, and recent reliability policy.

Normal model timeouts are not retried. This avoids duplicate hosted work when a
request may still be running remotely. Each dispatch records its timeout,
failure category, and zero retry count.

Only selected text files are sent with `--context`; whole repositories are not
loaded automatically. Every run records original, sent, and omitted character
counts for every supplied file, including files omitted after the limit.

## Model discovery and probes

```bash
owui-swarm models
owui-swarm probe MODEL_ID
owui-swarm probe --enabled
```

The catalog is `~/.local/share/owui-swarm/catalog.sqlite3`. It records provider,
family, provisional category and capabilities, enabled/currently-exposed state,
context length when supplied, exact chat-probe result and latency, success and
failure timestamps, manual quality/speed scores, notes, and probe history.

Name classification is conservative and provisional. Embedding, reranking,
guardrail, image, speech, OCR, retrieval, and specialist endpoints are disabled
as ordinary workers until manually reviewed. A probe is healthy only when its
trimmed response is exactly `HEALTHY`; one failure never aborts other probes.

Automatic routing uses only enabled, currently exposed, successfully probed chat
models. It considers role/task capability, manual quality, recent probe and task
success, timeout/protocol failure rates, consecutive failures, representative
successful latency, and model-family diversity. Three consecutive recent
failures cause a 60-minute automatic-routing cooldown; a later successful task
clears the streak. One timeout adds a temporary penalty but does not disable a
model. Explicit overrides remain available and record that they bypassed the
recommendation. Each concise operational reason is stored in `task.json`.

Role-specific answer-quality evidence is additive and deliberately bounded. The
catalog records concise quality events and compact benchmark results, derives a
provisional role score, and applies only a small quality contribution alongside
the existing reliability gate. One weak answer cannot disable a model; repeated
reviewed defects increase the role penalty and later clean results reduce it.
Routing explanations include the role score, evidence count, provisional status,
known strengths/failures, reliability, latency, and family diversity.

When some workers fail, the judge receives only compact missing-role metadata,
not exception traces. It may synthesize the remaining candidates with a reduced
confidence cap. If every worker fails, the judge is skipped and the run ends
with a bounded failure artifact. Judge failure leaves worker artifacts intact.

Judge confidence is also capped by observable evidence quality: missing context,
unexecuted code, a single candidate, missing workers, material dissent, shared
unsupported assumptions, invented filenames, unsupported safety/concurrency
claims, and unsupplied numeric thresholds. Reasons are stored in `final.json`.

## Compact quality benchmarks

The versioned five-task fixture is `swarm_router/benchmarks/quality-v1.json`. List or run one
model/task pair at a time:

```bash
owui-swarm benchmark --list
owui-swarm benchmark retry-helper \
  --model minimaxai/minimax-m3 --role implementer --max-tokens 600
```

Each invocation makes one bounded request with no retry, stores the raw response
under `~/.local/share/owui-swarm/benchmarks`, applies deterministic checks, and
records that the subjective review is still pending. Codex—not the evaluated
model—must add subjective 0–2 dimension scores and concise quality events after
review. The dashboard shows recent checks, reviews, role scores, provisional
status, clean/unsupported rates, judge catch rate, and final-defect rate.

## Run a task

Explicitly assign every role:

```bash
owui-swarm run \
  --mode code \
  --prompt-file task.txt \
  --role-model planner=MODEL_A \
  --role-model implementer=MODEL_B \
  --role-model critic=MODEL_C \
  --role-model verifier=MODEL_D \
  --judge-model=MODEL_E
```

Or use healthy catalog recommendations for up to four workers plus a judge:

```bash
owui-swarm run --mode code --auto-models 4 --prompt-file task.txt
```

The CLI prints only the bounded final synthesis and artifact paths by default.
Codex should read `final.md`, inspect raw transcripts only when verification
fails or confidence is low, then inspect files and run checks itself.

## Local dashboard

Start interactively:

```bash
owui-swarm serve
```

Open `http://127.0.0.1:8787`. The server rejects every bind address other than
`127.0.0.1`; do not expose it through a public reverse proxy.

For the installed user service:

```bash
systemctl --user daemon-reload
systemctl --user enable --now owui-swarm-dashboard.service
systemctl --user status owui-swarm-dashboard.service
```

The dashboard shows run history, objectives, criteria, mode, context accounting,
role/model selection and reasons, exact prompts, timestamps, latency, errors,
raw worker and judge output, final synthesis, confidence, agreements,
disagreements, verification needs, catalog metadata, ratings, notes, and probe
history. It never displays API keys or authorization headers.

## Persistent artifacts

```text
~/.local/share/owui-swarm/
  catalog.sqlite3                model catalog and probe history
  dashboard/server.json          local dashboard metadata
  runs/<run-id>/
    task.json                    objective, criteria, context totals, routing reasons
    events.jsonl                 dispatch/completion/failure timeline
    context/manifest.json        per-file original/sent/omitted counts
    context/sent.txt             exact context sent
    prompts/worker-*.txt         exact outbound worker prompts
    prompts/judge.txt            exact outbound judge prompt
    workers/*.md                 raw worker responses
    judge/response.md            raw judge response
    final.json                   structured synthesis
    final.md                     compact synthesis Codex reads by default
```

Storage directories are private (`0700`) and run files are private (`0600`).

## Versioned local wiki

The canonical wiki is a separate Git repository at `/srv/swarm-wiki`, not
application runtime data and not part of this source repository. Override the
root for tests or another approved deployment with `OWUI_SWARM_WIKI_ROOT`; the
live user configuration is not changed automatically.

```bash
owui-swarm wiki init --with-samples
owui-swarm wiki validate
owui-swarm wiki status
owui-swarm wiki list
owui-swarm wiki get acme-orbit-overview
owui-swarm wiki backup
owui-swarm wiki restore-verify ~/backups/swarm-wiki/<timestamp>
```

Wiki commands do not load the Open WebUI API key, model catalog, or run history.
Pages and immutable source manifests are validated before atomic, locked writes.
Backups contain a Git bundle and tracked working files; restore verification
always uses a temporary directory and never replaces the canonical wiki.
Governance and recovery details are in
[`docs/wiki-storage-foundation.md`](docs/wiki-storage-foundation.md).

## Validation

```bash
cd ~/openwebui-codex-swarm
.venv/bin/python -m compileall -q -f swarm_router tests
.venv/bin/python -m unittest discover -s tests -v
owui-swarm --help
owui-swarm doctor
owui-swarm models
owui-swarm probe --enabled
```

## Windows dashboard access

Keep execution on Debian. From Windows PowerShell:

```powershell
ssh -L 8787:127.0.0.1:8787 <debian-user>@<debian-address>
```

Then browse to `http://127.0.0.1:8787`. Do not install another swarm service on
Windows.

## Security boundaries

- Workers receive prompts only: no shell, filesystem, Docker, SSH, browser, or
  write access.
- Never send credentials, `.env` files, cookies, private keys, unrelated private
  data, or production dumps as context.
- Worker and judge consensus is not evidence and generated patches are never
  applied automatically.
- Codex verifies current facts, repository state, tests, security, and final
  claims.
- The dashboard contains sensitive prompts and responses; keep it on loopback
  and use the SSH tunnel for remote viewing.
